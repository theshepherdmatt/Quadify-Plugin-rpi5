'use strict';

const libQ  = require('kew');
const fs    = require('fs-extra');
const yaml  = require('js-yaml');
const path  = require('path');
const exec  = require('child_process').exec;
const Vconf = require('v-conf');

// Resolve system binaries once (works on both /bin and /usr/bin layouts)
const which = (cands) => cands.find(p => fs.existsSync(p)) || cands[cands.length - 1];
const SYSTEMCTL = which(['/bin/systemctl', '/usr/bin/systemctl', 'systemctl']);
const SUDO      = which(['/usr/bin/sudo', '/bin/sudo', 'sudo']);

// ---------- Paths ----------
const PLUGIN_ROOT = __dirname;
const YAML_PATH   = path.join(PLUGIN_ROOT, 'quadifyapp', 'config.yaml');

const CONFIG_DIR  = '/data/configuration/system_hardware/quadify';
const CONFIG_PATH = path.join(CONFIG_DIR, 'config.json');

const USERCONFIG_TXT  = '/boot/userconfig.txt';
const IR_OVERLAY_LINE = 'dtoverlay=gpio-ir,gpio_pin=27';

// Preference path candidates (src first; keep scr as legacy fallback)
const PREF_CANDIDATES = [
  path.join(PLUGIN_ROOT, 'quadifyapp', 'src', 'preference.json'),
  path.join(PLUGIN_ROOT, 'quadifyapp', 'scr', 'preference.json')
];

// Buttons & LEDs service candidates
const BUTTONSLEDS_UNIT_CANDIDATES = ['quadify-buttonsleds'];

// Safe-shutdown units we manage
const SAFE_SHUTDOWN_UNIT_CANDIDATES = ['clean-poweroff', 'volumio-clean-poweroff']; // support old/new names
const LEDSOFF_UNIT = 'quadify-leds-off';

// LIRC filesystem
const LIRC_PROFILES_DIR = path.join(PLUGIN_ROOT, 'quadifyapp', 'lirc', 'configurations');
const LIRC_DST_DIR      = '/etc/lirc';
const LIRCD_CONF_DST    = path.join(LIRC_DST_DIR, 'lircd.conf');
const LIRCRC_DST        = path.join(LIRC_DST_DIR, 'lircrc');

// ---------- FS helpers ----------
async function ensureDir(dir) {
  try { await fs.ensureDir(dir); } catch (_) {}
}

async function atomicWriteJSON(file, obj) {
  const tmp = `${file}.tmp`;
  await ensureDir(path.dirname(file));
  await fs.writeJson(tmp, obj, { spaces: 2 });
  await fs.move(tmp, file, { overwrite: true });
}

async function resolvePreferencePath() {
  for (const p of PREF_CANDIDATES) {
    if (await fs.pathExists(p)) return p;
  }
  return PREF_CANDIDATES[0];
}

// ----- High-signal diagnostics -----
async function systemdSnapshot(unitBare) {
  const unit = unitBare.endsWith('.service') ? unitBare : `${unitBare}.service`;
  try {
    const { stdout } = await pExec(`${SYSTEMCTL} show -p ActiveState -p UnitFileState -p FragmentPath ${unit}`, null);
    const map = Object.fromEntries(
      (stdout || '').trim().split('\n').filter(Boolean).map(l => {
        const i = l.indexOf('=');
        return [l.slice(0, i), l.slice(i + 1)];
      })
    );
    return {
      unit,
      active:  (map.ActiveState   || 'unknown').trim(),
      enabled: (map.UnitFileState || 'unknown').trim(),
      path:    (map.FragmentPath  || 'n/a').trim()
    };
  } catch (e) {
    return { unit, active: 'error', enabled: 'error', path: 'n/a', err: e?.message };
  }
}

async function logSnapshot(self, reason) {
  try {
    const hw   = self.loadConfigYaml();
    const raw  = await loadRawPreferenceJSON();
    const pref = await getCanonicalPreference(hw);
    const flat = getFlatConfig(self.config.get() || {});
    const btnU = self.buttonsLedsUnit || 'n/a';

    const [cava, btn, lircd, irl] = await Promise.all([
      systemdSnapshot('cava'),
      btnU && btnU !== 'n/a' ? systemdSnapshot(btnU) : Promise.resolve({unit:btnU,active:'n/a',enabled:'n/a',path:'n/a'}),
      systemdSnapshot('lircd'),
      systemdSnapshot('ir_listener')
    ]);

    self.logger.info(
      `[Quadify][SNAPSHOT] ${reason} :: ` +
      `pref{spec=${!!pref.display.spectrum},screen=${pref.display.screen},rot=${pref.display.rotate}} ` +
      `flat{spec=${flat.enableSpectrum},screen=${flat.display_screen},rot=${flat.display_rotate}} ` +
      `yaml{mcp=${hw.mcp23017_address||''},rot=${hw.display_rotate||''}} ` +
      `units{cava=${cava.active}/${cava.enabled}, ${btnU}=${btn.active}/${btn.enabled}, ` +
      `lircd=${lircd.active}/${lircd.enabled}, ir_listener=${irl.active}/${irl.enabled}}`
    );
  } catch (e) {
    self.logger.warn('[Quadify][SNAPSHOT] failed: ' + (e?.message || e));
  }
}


// ---------- Small utils ----------
function logicValue(v) {
  if (typeof v === 'boolean') return v;
  if (typeof v === 'string')  return v === 'true' || v === 'on' || v === '1';
  if (typeof v === 'number')  return !!v;
  return false;
}

function flatten(val) {
  while (val && typeof val === 'object' && 'value' in val) val = val.value;
  return val;
}

function getFlatConfig(config) {
  const out = {};
  Object.keys(config || {}).forEach(k => out[k] = flatten(config[k]));
  return out;
}

const pick = (v, fb) =>
  (v === undefined || v === null || v === '' || v === 'undefined') ? fb : v;

function shallowMerge(base, patch) {
  const out = { ...(base || {}) };
  Object.keys(patch || {}).forEach(k => {
    const v = patch[k];
    if (v && typeof v === 'object' && !Array.isArray(v)) {
      out[k] = { ...(base?.[k] || {}), ...v };
    } else {
      out[k] = v;
    }
  });
  return out;
}

function hexStrip0x(s) {
  const t = String(s ?? '').trim().toLowerCase();
  return t.startsWith('0x') ? t.slice(2) : t;
}

// "20" => 32; hex-only parsing
function hexNoPrefixToInt(s, fb = 32) {
  const v = parseInt(hexStrip0x(s), 16);
  return Number.isFinite(v) ? v : fb;
}

function intToHexNoPrefix(n) {
  return (Number.isFinite(+n) ? (+n).toString(16) : '20');
}


// Map canonical display.screen → legacy flat keys
function screenToLegacy(screen, prevRaw = {}) {
  const s = String(screen || '').toLowerCase();
  const res = {};

  if (s.startsWith('modern-')) {
    // modern-bars | modern-dots | modern-osci
    let mode = s.slice('modern-'.length);
    if (mode === 'osci') mode = 'scope';        // rename here
    res.display_mode = 'modern';
    res.modern_spectrum_mode = mode;            // bars | dots | scope
  } else if (s === 'modern') {
    // Keep prior mode, but normalize osci->scope on write
    let prev = (prevRaw.modern_spectrum_mode ?? '').toString().toLowerCase();
    if (prev === 'osci') prev = 'scope';
    res.display_mode = 'modern';
    res.modern_spectrum_mode = prev || undefined;
  } else {
    res.display_mode = s || 'modern';
    res.modern_spectrum_mode = undefined;       // remove for non-modern screens
  }
  return res;
}

function friendlyErr(e) {
  if (!e) return 'Unknown error';
  if (typeof e === 'string') return e;
  if (e.stderr && e.stderr.trim()) return e.stderr.trim();
  if (e.stdout && e.stdout.trim()) return e.stdout.trim();
  if (e.err && e.err.message) return e.err.message;
  if (e.message) return e.message;
  try { return JSON.stringify(e); } catch { return String(e); }
}

const LEGACY_FLAT_MAP_BASE = {
  cava_enabled: 'display.spectrum'
};

function getByPath(obj, pathStr, fb) {
  return pathStr.split('.').reduce((o, k) => (o && o[k] !== undefined ? o[k] : undefined), obj) ?? fb;
}

// Canonical + minimal mirrors (display_mode/modern_spectrum_mode + cava_enabled)
function mergedCanonicalWithMinimalMirrors(raw, canonical) {
  const out = { ...canonical };

  // screen → legacy flat
  const screenLegacy = screenToLegacy(canonical.display?.screen, raw);
  Object.keys(screenLegacy).forEach(k => {
    if (screenLegacy[k] === undefined) { if (k in out) delete out[k]; }
    else { out[k] = screenLegacy[k]; }
  });

  // remaining minimal mirrors
  Object.entries(LEGACY_FLAT_MAP_BASE).forEach(([flat, path]) => {
    out[flat] = getByPath(canonical, path);
  });

  // preserve unknown third-party keys only
  const KNOWN = new Set(['display','controls','ir','safety','display_mode','modern_spectrum_mode','cava_enabled']);
  Object.keys(raw || {}).forEach(k => { if (!KNOWN.has(k) && !(k in out)) out[k] = raw[k]; });

  return out;
}

// --------- Preference IO / Canonicalisation (minimal, single source of truth) ---------

// --- prefs IO (preserve top-level keys) ---
const PREF_PATH = path.join(__dirname, 'quadifyapp', 'src', 'preference.json');
const PREF_TMP  = PREF_PATH + '.tmp';

const PREF_NESTED_DEFAULTS = {
  display:  { spectrum: true, screen: 'modern', rotate: 180, oled_brightness: 255 },
  controls: { buttons_led_service: true, mcp23017_address: '20' },
  ir:       { enabled: true, profile: 'Xiaomi IR for TV box', gpio_bcm: 27 },
  safety:   { safe_shutdown: true }
};

function readPrefsRaw() {
  try { return JSON.parse(fs.readFileSync(PREF_PATH, 'utf8')) || {}; }
  catch { return {}; }
}

function normaliseNested(p) {
  p.display  = Object.assign({}, PREF_NESTED_DEFAULTS.display,  p.display  || {});
  p.controls = Object.assign({}, PREF_NESTED_DEFAULTS.controls, p.controls || {});
  p.ir       = Object.assign({}, PREF_NESTED_DEFAULTS.ir,       p.ir       || {});
  p.safety   = Object.assign({}, PREF_NESTED_DEFAULTS.safety,   p.safety   || {});
  return p;
}

// Atomic writer that DOES NOT drop unrelated top-level keys
function writePrefsRaw(obj) {
  fs.writeFileSync(PREF_TMP, JSON.stringify(obj, null, 2));
  fs.moveSync(PREF_TMP, PREF_PATH, { overwrite: true });
}


function coerceHexAddrSimple(v) {
  if (v == null || v === '') return '20';
  let s = String(v).trim().toLowerCase();
  if (!s.startsWith('0x')) s = '0x' + s;
  return s;
}

// Read the raw JSON as-is (may contain both top-level + nested keys)
async function loadRawPreferenceJSON() {
  try {
    const txt = fs.readFileSync(PREF_PATH, 'utf8');
    return JSON.parse(txt) || {};
  } catch {
    return {};
  }
}

// Save canonical (nested) back to file by merging with the raw JSON
async function saveCanonicalPreference(prefObj) {
  const raw = await loadRawPreferenceJSON();
  const toWrite = withFlatMirrors(raw, prefObj);   // merge + mirror + preserve unknowns
  await atomicWriteJSON(PREF_PATH, toWrite);       // atomic write
}

// Merge: keep unknown top-level keys, mirror nested display → flat keys ModeManager reads
function withFlatMirrors(raw, canonical) {
  // Start with canonical (nested sections) shallow-merged over raw
  // so we do not drop top-level keys like screensaver_* etc.
  const out = shallowMerge(raw || {}, canonical || {});

  // ---- Mirror nested → flat (ModeManager) ----
  // 1) spectrum flag
  out.cava_enabled = !!getByPath(out, 'display.spectrum', false);

  // 2) brightness duplicate
  const ob = parseInt(getByPath(out, 'display.oled_brightness', NaN), 10);
  if (!Number.isNaN(ob)) out.oled_brightness = ob;

  // 3) screen → display_mode / modern_spectrum_mode
  const screen = String(getByPath(out, 'display.screen', 'modern') || '').toLowerCase();
  if (screen.startsWith('modern-')) {
    let mode = screen.slice('modern-'.length);       // bars | dots | osci
    if (mode === 'osci') mode = 'scope';             // normalise if Python expects "scope"
    out.display_mode = 'modern';
    out.modern_spectrum_mode = mode;
  } else if (screen === 'modern') {
    out.display_mode = 'modern';
    if (!out.modern_spectrum_mode) out.modern_spectrum_mode = 'bars';
  } else {
    out.display_mode = screen || 'modern';              // digitalvuscreen | vuscreen | original | minimal | …
    if ('modern_spectrum_mode' in out) delete out.modern_spectrum_mode;
  }

  return out;
}

// Build canonical (nested) preference from RAW file + YAML overrides (no loss of top-level keys)
function buildCanonicalFromAny(rawOrIgnored, hwYaml = {}) {
  const raw = rawOrIgnored && Object.keys(rawOrIgnored).length ? rawOrIgnored : {};
  // Seed nested with defaults, then overlay any nested sections found in raw
  const out = shallowMerge(
    {
      display:  { spectrum: true,  screen: 'modern', rotate: 0, oled_brightness: 255 },
      controls: { buttons_led_service: true, mcp23017_address: '20' },
      ir:       { enabled: true,  profile: 'Xiaomi IR for TV box',      gpio_bcm: 27 },
      safety:   { safe_shutdown: true }
    },
    (raw.display || raw.controls || raw.ir || raw.safety) ? {
      display:  raw.display  || {},
      controls: raw.controls || {},
      ir:       raw.ir       || {},
      safety:   raw.safety   || {}
    } : {}
  );

  // Legacy flat → nested
  const legacyScreen = raw.display_screen || raw.display_mode;
  if (legacyScreen) out.display.screen = String(legacyScreen);
  if ('cava_enabled' in raw) out.display.spectrum = !!raw.cava_enabled;
  if ('oled_brightness' in raw) {
    const ob = parseInt(raw.oled_brightness, 10);
    if (!Number.isNaN(ob)) out.display.oled_brightness = ob;
  }

  // YAML overrides (hardware truths)
  if (hwYaml.display_rotate !== undefined) {
    out.display.rotate = parseInt(hwYaml.display_rotate, 10) || 0;
  }

  if (hwYaml.mcp23017_address !== undefined) {
    // Normalize YAML (number like 32, or string like "0x20" / "20") to no-prefix hex "20"
    const val = hwYaml.mcp23017_address;
    let noPrefix;

    if (typeof val === 'number' && Number.isFinite(val)) {
      noPrefix = val.toString(16);
    } else {
      const s = String(val).trim().toLowerCase();
      if (s.startsWith('0x')) {
        noPrefix = s.slice(2);
      } else if (/^[0-9a-f]{1,2}$/.test(s)) {
        noPrefix = s;
      } else {
        noPrefix = '20'; // safe fallback
      }
    }

    out.controls.mcp23017_address = noPrefix;
  }

if (hwYaml.ir_gpio_pin !== undefined) {
  out.ir.gpio_bcm = parseInt(hwYaml.ir_gpio_pin, 10) || 27;
}

return out;        // <-- add this
}    


async function getCanonicalPreference(hwYaml) {
  const raw = await loadRawPreferenceJSON();      // full file (top-level + nested)
  return buildCanonicalFromAny(raw, hwYaml);      // return nested canonical
}

// Mirror nested canonical into Volumio's v-conf (unchanged)
function applyPreferenceToVconfInstance(vconf, pref) {
  // display
  vconf.set('enableSpectrum', !!pref.display.spectrum);
  vconf.set('enableCava',     !!pref.display.spectrum); // legacy mirror
  vconf.set('display_screen', String(pref.display.screen || 'modern'));
  vconf.set('display_rotate', String(pref.display.rotate ?? '0'));

  // buttons/LEDs
  vconf.set('enableButtonsLED', !!pref.controls.buttons_led_service);
  vconf.set('mcp23017_address', String(pref.controls.mcp23017_address || '20'));

  // IR
  vconf.set('enableIR',          !!pref.ir.enabled);
  vconf.set('ir_remote_profile', String(pref.ir.profile || ''));
  vconf.set('ir_gpio_pin',        parseInt(pref.ir.gpio_bcm, 10) || 27);

  // safety
  vconf.set('safe_shutdown_enabled', !!pref.safety.safe_shutdown);
}

// ---------- Misc helpers ----------
function ensureIrOverlay(pinBcm) {
  const wanted = `dtoverlay=gpio-ir,gpio_pin=${parseInt(pinBcm,10)||27}`;
  let content = '';
  try { content = fs.readFileSync(USERCONFIG_TXT, 'utf8'); } catch { content = ''; }

  // remove any existing gpio-ir overlay lines, then append desired
  const lines = content.split('\n').filter(line => !/^dtoverlay=gpio-ir\b/.test(line));
  lines.push(wanted);
  fs.writeFileSync(USERCONFIG_TXT, lines.filter(Boolean).join('\n') + '\n', 'utf8');
}

ControllerQuadify.prototype.listIrProfiles = async function () {
  const opts = [];
  let entries = [];
  try {
    entries = await fs.readdir(LIRC_PROFILES_DIR, { withFileTypes: true });
  } catch {
    return opts;
  }
  for (const d of entries) {
    if (!d.isDirectory()) continue;
    const base = path.join(LIRC_PROFILES_DIR, d.name);
    const lircd = path.join(base, 'lircd.conf');
    const lircrc = path.join(base, 'lircrc');
    if (await fs.pathExists(lircd) && await fs.pathExists(lircrc)) {
      opts.push({ label: d.name, value: d.name });
    }
  }
  return opts.sort((a,b) => a.label.localeCompare(b.label, 'en'));
};

ControllerQuadify.prototype.installIrProfile = async function (profileName) {
  const safe = path.basename(String(profileName || ''));
  if (!safe) throw new Error('Empty IR profile');

  const base     = path.join(__dirname, 'quadifyapp', 'lirc', 'configurations', safe);
  const srcLircd = path.join(base, 'lircd.conf');

  if (!(await fs.pathExists(srcLircd))) {
    throw new Error(`IR profile missing lircd.conf: ${safe}`);
  }

  const dst = '/home/volumio/lircd.conf';

  try {
    await fs.copy(srcLircd, dst, { overwrite: true });
    this.logger.info(`[Quadify] Installed IR profile to ${dst}: ${safe}`);
  } catch (e) {
    this.logger.error(`[Quadify] installIrProfile failed: ${e.message}`);
    throw e;
  }
};

ControllerQuadify.prototype.enableOnly = function (service, enable) {
  const cmd = enable
    ? `${SUDO} -n ${SYSTEMCTL} enable ${service}.service`
    : `${SUDO} -n ${SYSTEMCTL} disable ${service}.service`;
  return pExec(cmd, this.logger).fail(() => libQ.resolve());
};

// --- helper: does a unit exist? (quiet) ---
function unitExists(name) {
  const unit = `${name}.service`;
  return pExec(`${SYSTEMCTL} show -p LoadState ${unit}`)
    .then(({ stdout }) => /LoadState=loaded/.test(stdout || ''))
    .fail(() => false);
}

// ---------- Controller ----------
function ControllerQuadify(context) {
  this.context       = context;
  this.commandRouter = context.coreCommand;
  this.logger        = context.logger;
  this.config        = new Vconf();
  this.buttonsLedsUnit = null; // resolved at runtime
}

// ----- Lifecycle -----
ControllerQuadify.prototype.onStart = function () {
  // Do NOT toggle services here; config/prefs may not be ready yet.
  const d = libQ.defer();
  try { this.config.loadFile(CONFIG_PATH); } catch (_) {}

  this.detectButtonsLedsUnit()
    .then(() => { this.logger.info('[Quadify] onStart ready'); d.resolve(); })
    .fail(err => { this.logger.error('[Quadify] onStart init failed: ' + (err?.message || err)); d.resolve(); });

  return d.promise;
};


ControllerQuadify.prototype.onVolumioStart = function () {
  const defer = libQ.defer();

  (async () => {
    this.logger.info('[Quadify] onVolumioStart – ensure config');

    // Ensure Volumio v-conf exists
    try {
      fs.ensureDirSync(CONFIG_DIR);
      if (!fs.existsSync(CONFIG_PATH)) fs.writeJsonSync(CONFIG_PATH, {}, { spaces: 2 });
    } catch (e) {
      this.logger.error('[Quadify] ensure config dir/file failed: ' + e.message);
    }

    // Load v-conf
    try { this.config.loadFile(CONFIG_PATH); }
    catch (e) { this.logger.warn('[Quadify] loadFile failed, starting fresh: ' + e.message); }

    // Seed defaults (safe)
    const defaults = {
      enableCava: true,
      enableButtonsLED: true,
      mcp23017_address: '20',
      display_mode: 'modern',
      clock_font_key: 'clock_sans',
      show_seconds: false,
      show_date: false,
      screensaver_enabled: true,
      screensaver_type: 'geo',
      screensaver_timeout: 3600,
      oled_brightness: 200,
      // New UI keys
      enableSpectrum: true,
      display_rotate: '0',
      enableIR: true,
      ir_remote_profile: '',
      ir_gpio_pin: 27,
      safe_shutdown_enabled: true
    };
    let changed = false;
    Object.keys(defaults).forEach(k => {
      if (this.config.get(k) === undefined) {
        this.config.set(k, defaults[k]);
        changed = true;
      }
    });
    if (changed) {
      this.logger.info('[Quadify] writing default config.json');
      this.config.save();
    }

    // Preference import/migration
    const hwCfg = this.loadConfigYaml();
    try {
      const raw       = await loadRawPreferenceJSON();
      const canonical = buildCanonicalFromAny(raw, hwCfg);
      const merged    = withFlatMirrors(raw, canonical);

      await saveCanonicalPreference(merged);                 // write back (atomic)
      applyPreferenceToVconfInstance(this.config, canonical); // mirror for UI
      this.config.save();

      // Resolve unit name once
      await this.detectButtonsLedsUnit();

      // >>> Only now, after canonical prefs are ready, apply service toggles <<<
      await this.applyAllServiceTogglesFromPreference();

    } catch (e) {
      this.logger.warn('[Quadify] pref migrate/import on boot: ' + e.message);
    }

    // Optional: hook
    try { await this.syncPreferenceToQuadify(); }
    catch (e) { this.logger.warn('[Quadify] pref sync on start: ' + e.message); }

    defer.resolve();
  })();

  return defer.promise;
};


ControllerQuadify.prototype.syncPreferenceToQuadify = function () {
  return libQ.resolve();
};

// ---------- UIConfig ----------
ControllerQuadify.prototype.getUIConfig = function () {
  const defer = libQ.defer();
  const lang_code  = this.commandRouter.sharedVars.get('language_code');
  const stringsLoc = path.join(__dirname, 'i18n', `strings_${lang_code}.json`);
  const stringsEn  = path.join(__dirname, 'i18n', 'strings_en.json');
  const uiConfig   = path.join(__dirname, 'UIConfig.json');

  const populate = async (uiconf) => {
    const hwCfg  = this.loadConfigYaml();
    const pref   = await getCanonicalPreference(hwCfg);
    const raw    = await loadRawPreferenceJSON(); // read flat mirrors

    // --- helpers for assigning values ---
    const setRaw = (sectionId, id, val) => {
      const sec = uiconf.sections.find(s => s.id === sectionId);
      if (!sec) return;
      const row = sec.content.find(c => c.id === id);
      if (row !== undefined) row.value = val;
    };

    // For selects: pick the actual option {label,value} so label renders correctly
    const setSelect = (sectionId, id, want) => {
      const sec = uiconf.sections.find(s => s.id === sectionId);
      if (!sec) return;
      const row = sec.content.find(c => c.id === id);
      if (!row) return;

      const opts = Array.isArray(row.options) ? row.options : [];
      const match = opts.find(o => String(o.value) === String(want));

      // If not found, still set a sane value object to avoid empty display
      row.value = match || { label: String(want), value: String(want) };
    };

    // ---- Derive display_screen for UI from canonical + flat mirrors ----
    let screenForUi = String(pref.display.screen || 'modern');
    if (screenForUi === 'modern') {
      // prefer raw flat mirror; fall back to canonical, then to 'bars'
      let m = String((raw.modern_spectrum_mode ?? pref.modern_spectrum_mode ?? 'bars')).toLowerCase();
      if (m === 'scope') m = 'osci'; // UI uses 'osci'
      if (['bars', 'dots', 'osci'].includes(m)) {
        screenForUi = `modern-${m}`;
      }
    } else if (screenForUi.endsWith('-scope')) {
      // normalize any legacy "modern-scope" to "modern-osci"
      screenForUi = 'modern-osci';
    }

    this.logger.info(
      `[Quadify] UI populate: screen=${pref.display.screen} modern=${pref.modern_spectrum_mode} -> ui=${screenForUi}, ` +
      `rotate=${String(hwCfg.display_rotate ?? pref.display.rotate ?? '0')}, spectrum=${!!pref.display.spectrum}`
    );

    // ---- YAML truths (hardware) ----
    const rotateWant = String(hwCfg.display_rotate ?? pref.display.rotate ?? '0');
    setSelect('display_settings', 'display_rotate', rotateWant); // dropdown

    setRaw   ('ir_controller',    'ir_gpio_pin',
      parseInt(hwCfg.ir_gpio_pin ?? pref.ir.gpio_bcm ?? 27, 10));

    // ---- Preference truths (toggles/choices) ----
    setRaw   ('display_settings', 'enableSpectrum', !!pref.display.spectrum);
    setSelect('display_settings', 'display_screen', screenForUi); // dropdown

    setRaw('buttons_leds', 'mcp23017_address',
      hexStrip0x(hwCfg.mcp23017_address ?? pref.controls.mcp23017_address ?? '20'));

    setRaw   ('ir_controller',    'enableIR',          !!pref.ir.enabled);

    // ---- IR profile options (from filesystem) ----
    const irSec = uiconf.sections.find(s => s.id === 'ir_controller');
    if (irSec) {
      const profileEl = irSec.content.find(c => c.id === 'ir_remote_profile');
      if (profileEl) {
        const opts = await this.listIrProfiles();
        profileEl.options = [{ label: '— none —', value: '' }, ...opts];

        const stored = String(pref.ir.profile || '');
        setSelect('ir_controller', 'ir_remote_profile', stored); // dropdown
      }
    }

    setRaw('safety_controls', 'safe_shutdown_enabled', !!pref.safety.safe_shutdown);

    // ---- Legacy back-compat mirrors (safe no-ops if sections don’t exist) ----
    const flatConfig = getFlatConfig(this.config.get() || {});
    if (flatConfig.mcp23017_address !== undefined) {
      flatConfig.mcp23017_address = coerceHexAddr(flatConfig.mcp23017_address);
    }
    const legacyApply = (sectionId, keys) => {
      const sec = uiconf.sections.find(s => s.id === sectionId);
      if (!sec) return;
      keys.forEach(k => {
        const row = sec.content.find(c => c.id === k);
        if (row && flatConfig[k] !== undefined) row.value = flatConfig[k];
      });
    };
    legacyApply('display_controls', ['display_mode', 'enableCava', 'enableButtonsLED']);
    legacyApply('mcp23017_config',  ['mcp23017_address']);

    return uiconf;
  };

  this.commandRouter.i18nJson(stringsLoc, stringsEn, uiConfig)
    .then(uiconf => populate(uiconf))
    .then(uiconf => defer.resolve(uiconf))
    .fail(async (err) => {
      this.logger.warn('[Quadify] i18nJson failed, falling back to raw UIConfig.json: ' + err);
      try {
        const raw = await fs.readJson(uiConfig);
        const uiconf = await populate(raw);
        defer.resolve(uiconf);
      } catch (e2) {
        this.logger.error('[Quadify] getUIConfig fallback failed: ' + e2);
        defer.reject(e2);
      }
    });

  return defer.promise;
};


// ---------- UI Save (whole page) ----------
ControllerQuadify.prototype.setUIConfig = function (data) {
  this.logger.info('[Quadify] setUIConfig: ' + JSON.stringify(data));

  try {
    fs.ensureDirSync(CONFIG_DIR);
    if (!fs.existsSync(CONFIG_PATH)) fs.writeJsonSync(CONFIG_PATH, {}, { spaces: 2 });
    this.config.loadFile(CONFIG_PATH);
  } catch (e) {
    this.logger.error('[Quadify] ensure/load in setUIConfig failed: ' + e.message);
  }

  const oldConfig = getFlatConfig(this.config.get() || {});
  const mergedConfig = {
    enableCava:       logicValue(data.enableCava       !== undefined ? flatten(data.enableCava)       : oldConfig.enableCava),
    enableButtonsLED: logicValue(data.enableButtonsLED !== undefined ? flatten(data.enableButtonsLED) : oldConfig.enableButtonsLED),

    mcp23017_address: pick(
      data.mcp23017_address !== undefined ? flatten(data.mcp23017_address) : undefined,
      oldConfig.mcp23017_address || '20'
    ),

    display_mode: pick(
      data.display_mode !== undefined ? flatten(data.display_mode) : undefined,
      oldConfig.display_mode || 'modern'
    ),

    clock_font_key: pick(
      data.clock_font_key !== undefined ? flatten(data.clock_font_key) : undefined,
      oldConfig.clock_font_key || 'clock_sans'
    ),

    show_seconds: logicValue(data.show_seconds !== undefined ? flatten(data.show_seconds) : oldConfig.show_seconds),
    show_date:    logicValue(data.show_date    !== undefined ? flatten(data.show_date)    : oldConfig.show_date),

    screensaver_enabled: logicValue(
      data.screensaver_enabled !== undefined ? flatten(data.screensaver_enabled) : oldConfig.screensaver_enabled
    ),
    screensaver_type: pick(
      data.screensaver_type !== undefined ? flatten(data.screensaver_type) : undefined,
      oldConfig.screensaver_type || 'geo'
    ),

    screensaver_timeout: (data.screensaver_timeout !== undefined && data.screensaver_timeout !== null)
      ? parseInt(flatten(data.screensaver_timeout), 10)
      : parseInt(oldConfig.screensaver_timeout || 3600, 10),

    oled_brightness: (data.oled_brightness !== undefined && data.oled_brightness !== null)
      ? parseInt(flatten(data.oled_brightness), 10)
      : parseInt(oldConfig.oled_brightness || 200, 10)
  };

  if (mergedConfig.mcp23017_address) {
    mergedConfig.mcp23017_address = hexStrip0x(mergedConfig.mcp23017_address);
  }

  Object.keys(mergedConfig).forEach(k => this.config.set(k, mergedConfig[k]));
  this.config.save();

  const hwCfg = this.loadConfigYaml();
  return loadRawPreferenceJSON()
    .then(raw => {
      const hwCfg      = this.loadConfigYaml();
      const canonical  = buildCanonicalFromAny(raw, hwCfg);
      const desired    = buildPreferenceFromVconf(this.config);
      const mergedPref = withFlatMirrors(raw, shallowMerge(canonical, desired));
      return saveCanonicalPreference(mergedPref).then(() => mergedPref);
    })
    .then(() => this.applyAllServiceTogglesFromPreference())
    .then(() => {
      this.commandRouter.pushToastMessage('success', 'Quadify', 'Configuration saved');
      return {};
    })
    .catch(err => {
      this.commandRouter.pushToastMessage('error', 'Quadify', 'Saved, but preference sync failed');
      this.logger.error('[Quadify] preference sync failed: ' + (err?.message || err));
      return {};
    });

};


// ---------- Service control ----------
function pExec(cmd, logger) {
  const d = libQ.defer();
  exec(cmd, (err, stdout, stderr) => {
    if (err) {
      logger && logger.error(`[Quadify] CMD FAIL: ${cmd}\nstdout: ${stdout || ''}\nstderr: ${stderr || ''}\nerr: ${err.message}`);
      d.reject({ err, stdout, stderr, cmd });
    } else {
      logger && logger.info(`[Quadify] CMD OK: ${cmd}\nstdout: ${stdout || ''}\nstderr: ${stderr || ''}`);
      d.resolve({ stdout, stderr, cmd });
    }
  });
  return d.promise;
}

ControllerQuadify.prototype.controlService = function (service, enable) {
  const logger     = this.logger;
  const sudo       = SUDO;
  const systemctl  = SYSTEMCTL;
  const unit       = `${service}.service`;

  // Quiet exec with logging — returns a real promise
  const pExecQuiet = (cmd, tag = '') => {
    const d = libQ.defer();
    logger.info(`[Quadify][SYSD] exec${tag ? `(${tag})` : ''}: ${cmd}`);
    exec(cmd, (err, stdout, stderr) => {
      if (err) {
        logger.warn(
          `[Quadify][SYSD] cmd FAIL${tag ? `(${tag})` : ''}: ${cmd}\n` +
          `stdout: ${stdout || ''}\n` +
          `stderr: ${stderr || ''}\n` +
          `err: ${err.message}`
        );
        d.reject({ err, stdout, stderr, cmd });
        return;
      }
      if (stdout && stdout.trim()) {
        logger.info(`[Quadify][SYSD] cmd OK${tag ? `(${tag})` : ''}: ${cmd}\n${stdout.trim()}`);
      } else {
        logger.info(`[Quadify][SYSD] cmd OK${tag ? `(${tag})` : ''}: ${cmd}`);
      }
      d.resolve({ stdout, stderr, cmd });
    });
    return d.promise;
  };

  const runSeq = (seq, tag) =>
    seq.reduce(
      (p, cmd) => p.then(() => pExecQuiet(cmd, tag).fail(() => libQ.resolve())), // keep going; we've logged the fail
      libQ.resolve()
    );

  // Use sudo -n to fail fast if not permitted
  const enableSeq       = [`${sudo} -n ${systemctl} daemon-reload`, `${sudo} -n ${systemctl} enable --now ${unit}`];
  const enableFallback  = [`${sudo} -n ${systemctl} start ${unit}`];
  const disableSeq      = [`${sudo} -n ${systemctl} disable --now ${unit}`];
  const disableFallback = [`${sudo} -n ${systemctl} stop ${unit}`, `${sudo} -n ${systemctl} disable ${unit}`];

  logger.info(`[Quadify][SYSD] plan ${enable ? 'ENABLE+START' : 'DISABLE+STOP'} ${unit}`);

  const verify = () =>
    pExecQuiet(
      `${systemctl} show -p ActiveState -p SubState -p UnitFileState -p FragmentPath -p Result -p ExecMainPID -p ExecMainStatus ${unit}`,
      'verify'
    )
      .then(({ stdout }) => {
        const map = Object.fromEntries(
          (stdout || '')
            .trim()
            .split('\n')
            .filter(Boolean)
            .map((l) => {
              const i = l.indexOf('=');
              return [l.slice(0, i), l.slice(i + 1)];
            })
        );
        const snap = {
          active:  (map.ActiveState    || 'unknown').trim(),
          sub:     (map.SubState       || 'unknown').trim(),
          enabled: (map.UnitFileState  || 'unknown').trim(),
          path:    (map.FragmentPath   || 'n/a').trim(),
          pid:     (map.ExecMainPID    || '').trim(),
          status:  (map.ExecMainStatus || '').trim(),
          result:  (map.Result         || '').trim()
        };
        logger.info(
          `[Quadify][SYSD] VERIFY ${service}: ` +
          `active=${snap.active} sub=${snap.sub} enabled=${snap.enabled} ` +
          `pid=${snap.pid} status=${snap.status} result=${snap.result} path=${snap.path}`
        );
        return snap;
      })
      .fail((e) => {
        logger.warn(`[Quadify][SYSD] VERIFY ${service} failed: ${e?.err?.message || e}`);
        return { active: 'unknown', enabled: 'unknown' };
      });

  const plan = enable ? [enableSeq, enableFallback] : [disableSeq, disableFallback];

  return runSeq(plan[0], 'plan')
    .then(verify)
    .then((state) => {
      const wantActive  = enable ? 'active'  : 'inactive';
      const wantEnabled = enable ? 'enabled' : 'disabled';

      if (state.active !== wantActive || state.enabled !== wantEnabled) {
        logger.warn(
          `[Quadify][SYSD] ${service} verify miss (have active=${state.active} enabled=${state.enabled}, ` +
          `want ${wantActive}/${wantEnabled}) → fallback`
        );

        return runSeq(plan[1], 'fallback')
          .then(verify)
          .then(async (finalState) => {
            if (finalState.active !== wantActive || finalState.enabled !== wantEnabled) {
              // Surface last unit logs so we can see the real reason
              await pExecQuiet(`${sudo} -n journalctl -u ${unit} -n 40 -o cat`, 'logs').fail(() => libQ.resolve());
            }
            return finalState;
          });
      }
      return state;
    })
    .then((finalState) => {
      logger.info(`[Quadify][SYSD] ${service} => active=${finalState.active} enabled=${finalState.enabled}`);
      return finalState;
    })
    .fail((err) => {
      logger.error(`[Quadify][SYSD] controlService(${service}, ${enable}) ERROR: ${err?.err?.message || err}`);
    });
};


ControllerQuadify.prototype.controlButtonsLeds = function (enable) {
  const self = this;
  self.logger.info(`[Quadify] controlButtonsLeds(${enable ? 'ON' : 'OFF'}) unit=${self.buttonsLedsUnit || 'n/a'}`);
  return libQ.resolve()
    .then(() => (!self.buttonsLedsUnit ? self.detectButtonsLedsUnit() : null))
    .then(() => {
      if (!self.buttonsLedsUnit) {
        self.logger.warn('[Quadify] Skipping Buttons/LEDs toggle: no unit detected');
        return { active: 'unknown', enabled: 'unknown' };
      }
      // return the final state object from controlService
      return self.controlService(self.buttonsLedsUnit, enable);
    });
};

ControllerQuadify.prototype.enforceButtonsFromPreference = async function () {
  try {
    const hwCfg = this.loadConfigYaml();
    const pref  = await getCanonicalPreference(hwCfg);
    const want  = !!pref.controls?.buttons_led_service;

    await this.detectButtonsLedsUnit();
    await this.controlButtonsLeds(want);

    this.logger.info(`[Quadify] Buttons/LEDs boot enforce → ${want ? 'ON' : 'OFF'}`);
  } catch (e) {
    this.logger.warn('[Quadify] enforceButtonsFromPreference failed: ' + (e?.message || e));
  }
};

ControllerQuadify.prototype.controlSafeShutdown = async function (enable) {
  const candidates = ['quadify-leds-off', 'clean-poweroff', 'volumio-clean-poweroff'];
  const existing = [];
  for (const name of candidates) {
    // check if the unit is installed before toggling
    if (await unitExists(name)) existing.push(name);
  }
  if (!existing.length) {
    this.logger.warn('[Quadify] No safe-shutdown units installed; skipping toggle');
    return libQ.resolve();
  }
  return libQ.allSettled(existing.map(n => this.enableOnly(n, enable)));
};


// ---------- MCP23017 / YAML ----------
ControllerQuadify.prototype.updateMcpConfig = function (data) {
  const self = this;

  // --- helpers: UI uses hex-without-0x (e.g. "20") ---
  const hexStrip0x = (s) => {
    const t = String(s ?? '').trim().toLowerCase();
    return t.startsWith('0x') ? t.slice(2) : t;
  };
  const hexNoPrefixToInt = (s, fb = 32) => {
    const v = parseInt(hexStrip0x(s), 16);
    return Number.isFinite(v) ? v : fb;
  };

  // 1) Read UI value (hex, no prefix). Default "20" (=> 20).
  const rawUi = data?.mcp23017_address;
  const hexUi = hexStrip0x(rawUi || '20');   // e.g. "20"
  const i2cInt = hexNoPrefixToInt(hexUi, 32); // e.g. 32

  // 2) YAML (daemon) wants a NUMBER
  const cfg = self.loadConfigYaml();
  cfg.mcp23017_address = i2cInt; // numeric for Python
  self.saveConfigYaml(cfg);
  self.logger.info(`[Quadify][YAML] mcp23017_address <= ${i2cInt} (from UI ${hexUi})`);

  // 3) v-conf/UI mirror stores *no-prefix hex* (e.g. "20")
  self.config.set('mcp23017_address', hexUi);
  self.config.save();

  self.commandRouter.pushToastMessage('success', 'Quadify', `MCP23017 address saved: ${hexUi}`);

  // 4) preference.json: keep controls.mcp23017_address as *no-prefix hex* "20"
  const hwCfg = self.loadConfigYaml();
  return loadRawPreferenceJSON()
    .then(raw => {
      const canonical = buildCanonicalFromAny(raw, hwCfg);
      canonical.controls.mcp23017_address = hexUi; // store "20", not "20"
      return saveCanonicalPreference(withFlatMirrors(raw, canonical));
    })
    .then(async () => {
      // 5) Restart the daemon so it re-reads YAML immediately
      await self.detectButtonsLedsUnit();
      if (self.buttonsLedsUnit) {
        self.logger.info(`[Quadify][SYSD] restart ${self.buttonsLedsUnit}.service to apply new I2C addr`);
        return pExec(`${SUDO} -n ${SYSTEMCTL} restart ${self.buttonsLedsUnit}.service`, self.logger)
          .fail(() => libQ.resolve());
      }
    })
    .then(() => ({}))
    .catch(e => {
      self.logger.warn('[Quadify] pref sync after updateMcpConfig: ' + (e?.message || e));
      return {};
    });
};

ControllerQuadify.prototype.autoDetectMCP = function () {
  const defer = libQ.defer();

  exec('i2cdetect -y 1', (err, stdout) => {
    if (err) {
      this.commandRouter.pushToastMessage('error', 'Quadify', 'i2cdetect failed');
      return defer.reject(err);
    }

    const lines = stdout.split('\n').slice(1);
    let foundAddr = null;

    for (const line of lines) {
      const parts = line.trim().split(/\s+/);
      if (!parts.length) continue;
      const row = parts[0]?.replace(':', '');
      for (let i = 1; i < parts.length; i++) {
        if (parts[i] !== '--') {
          foundAddr = '0x' + (parseInt(row, 16) + (i - 1)).toString(16);
          break;
        }
      }
      if (foundAddr) break;
    }

    const cfg = this.loadConfigYaml();

    // Keep YAML numeric (e.g. 32), UI/v-conf hex (e.g. "0x20")
    if (foundAddr) {
      const i2cInt = parseInt(foundAddr, 16); // "0x20" -> 32
      cfg.mcp23017_address = i2cInt;          // YAML numeric
      this.saveConfigYaml(cfg);

      this.config.set('mcp23017_address', foundAddr.toLowerCase()); // v-conf hex string for UI
      this.config.save();
    } else {
      // If nothing found, don’t change YAML; only notify
      this.saveConfigYaml(cfg);
    }

    const hwCfg = this.loadConfigYaml();
    loadRawPreferenceJSON()
      .then(raw => {
        const canonical = buildCanonicalFromAny(raw, hwCfg);
        if (foundAddr) canonical.controls.mcp23017_address = foundAddr;
        return saveCanonicalPreference(withFlatMirrors(raw, canonical));
      })
      .catch(e => this.logger.warn('[Quadify] pref sync after autoDetect: ' + (e?.message || e)));

    if (!foundAddr) {
      this.commandRouter.pushToastMessage('error', 'Quadify', 'No MCP23017 board detected');
      return defer.resolve();
    }

    this.commandRouter.pushToastMessage('success', 'Quadify', 'Detected MCP23017 at ' + foundAddr);
    defer.resolve({ mcp23017_address: foundAddr });
  });

  return defer.promise;
};


ControllerQuadify.prototype.loadConfigYaml = function () {
  try { return yaml.load(fs.readFileSync(YAML_PATH, 'utf8')) || {}; }
  catch { return {}; }
};

ControllerQuadify.prototype.saveConfigYaml = function (cfg) {
  fs.writeFileSync(YAML_PATH, yaml.dump(cfg), 'utf8');
};

ControllerQuadify.prototype.cavaStartStop = function (on) {
  // single, consistent path: enable+start when on; disable+stop when off
  this.logger.info(`[Quadify][TOGGLE] CAVA => ${on ? 'enable' : 'disable'}`);
  return this.controlService('cava', on);
};

// ---------- Misc / Stubs ----------
ControllerQuadify.prototype.restartQuadify = function () {
  this.logger.info('[Quadify] Restart requested via UI.');
  return libQ.nfcall(exec, 'sudo systemctl restart quadify.service')
    .then(() => { this.commandRouter.pushToastMessage('success', 'Quadify', 'Quadify service restarted.'); return {}; })
    .fail((err) => {
      this.logger.error('[Quadify] Failed to restart quadify.service: ' + err.message);
      this.commandRouter.pushToastMessage('error', 'Quadify', 'Failed to restart Quadify service.');
      throw err;
    });
};

ControllerQuadify.prototype.updateRotaryConfig = function () { return libQ.resolve(); };

ControllerQuadify.prototype.refreshIRRemotes = async function () {
  try {
    const opts = await this.listIrProfiles();
    this.commandRouter.pushToastMessage('success', 'Quadify', `Found ${opts.length} IR profiles. Reopen this page to see the list.`);
  } catch (e) {
    this.logger.warn('[Quadify] refreshIRRemotes failed: ' + e.message);
    this.commandRouter.pushToastMessage('error', 'Quadify', 'Failed to refresh IR profiles');
  }
  return {};
};


// ---------- Section SAVE handlers ----------
ControllerQuadify.prototype.saveDisplay_settings = function (data) {
  const self = this;
  self.logger.info('[Quadify][DISPLAY] saveDisplay_settings() called: raw=' + JSON.stringify(data));

  const flat = getFlatConfig(data || {});
  self.logger.info('[Quadify][DISPLAY] flat=' + JSON.stringify(flat));

  // --- YAML: display_rotate is a hardware truth
  const cfg = self.loadConfigYaml();
  if (flat.display_rotate !== undefined) {
    const prev = cfg.display_rotate;
    cfg.display_rotate = String(flat.display_rotate);
    self.saveConfigYaml(cfg);
    self.logger.info(`[Quadify][YAML] display_rotate <= ${cfg.display_rotate} (from ${prev})`);
  }

  const hwCfg = self.loadConfigYaml();

  return loadRawPreferenceJSON()
    .then(async (raw) => {
      // snapshot before we change anything
      await logSnapshot(self, 'display-save pre');

      const pref = buildCanonicalFromAny(raw, hwCfg);

      // --- Spectrum toggle (prefer new key; fallback to legacy)
      let spectrumSet = false;
      let spectrumVal;
      if (flat.enableSpectrum !== undefined) {
        spectrumVal = logicValue(flat.enableSpectrum);
        spectrumSet = true;
      } else if (flat.enableCava !== undefined) {
        spectrumVal = logicValue(flat.enableCava);
        spectrumSet = true;
      }
      if (spectrumSet) {
        self.logger.info(`[Quadify][DISPLAY] spectrum: ${pref.display.spectrum} -> ${spectrumVal}`);
        pref.display.spectrum = spectrumVal;
      }

      // --- Screen selection (validate + normalise)
      if (flat.display_screen !== undefined) {
        const scr = String(flat.display_screen);
        const allowed = [
          'vu','minimal','digitalvuscreen','vuscreen',
          'modern','modern-bars','modern-dots','modern-osci',
          'fm4','original'
        ];
        const next = allowed.includes(scr) ? scr : 'modern';
        if (pref.display.screen !== next) {
          self.logger.info(`[Quadify][DISPLAY] screen: ${pref.display.screen} -> ${next}`);
        }
        pref.display.screen = next;
      }

      // --- Persist preference.json with flat mirrors that Python/ModeManager read
      const toWrite = mergedCanonicalWithMinimalMirrors(raw, pref);
      const mirrors = {
        display_mode: toWrite.display_mode,
        modern_spectrum_mode: toWrite.modern_spectrum_mode,
        cava_enabled: toWrite.cava_enabled
      };
      return saveCanonicalPreference(toWrite)
        .then(() => {
          self.logger.info(`[Quadify][DISPLAY] preference.json saved (mirrors=${JSON.stringify(mirrors)})`);
          return pref;
        });
    })
    .then((pref) => {
      // Mirror to Volumio v-conf (UI reflection)
      applyPreferenceToVconfInstance(self.config, pref);
      self.config.save();
      self.logger.info(
        `[Quadify][DISPLAY] vconf mirrored: enableSpectrum=${pref.display.spectrum} ` +
        `screen=${pref.display.screen} rotate=${pref.display.rotate}`
      );

      // Request CAVA state to match spectrum
      self.logger.info(`[Quadify][DISPLAY] request CAVA => ${pref.display.spectrum ? 'enable' : 'disable'}`);
      return self.cavaStartStop(!!pref.display.spectrum).then(() => pref);
    })
    .then(async () => {
      await logSnapshot(self, 'display-save post');
      self.commandRouter.pushToastMessage('success', 'Quadify', 'Display settings saved');
      return {};
    })
    .catch((err) => {
      const msg = friendlyErr(err);
      self.logger.error('[Quadify][DISPLAY] saveDisplay_settings ERROR: ' + msg);
      self.commandRouter.pushToastMessage('error', 'Quadify', 'Failed to save display settings: ' + msg);
      return {};
    });
};


ControllerQuadify.prototype.saveIr_controller = function (data) {
  const self = this;
  const flat = getFlatConfig(data || {});
  self.logger.info('[Quadify] saveIr_controller flat: ' + JSON.stringify(flat));
  const cfg  = self.loadConfigYaml();

  // 1) Persist GPIO pin to YAML and ensure overlay
  try {
    if (flat.ir_gpio_pin !== undefined) {
      cfg.ir_gpio_pin = parseInt(flat.ir_gpio_pin, 10) || 27;
      self.saveConfigYaml(cfg);
      try { ensureIrOverlay(cfg.ir_gpio_pin); }
      catch (e) { self.logger.warn('[Quadify] ensureIrOverlay failed: ' + friendlyErr(e)); }
    }
  } catch (e) {
    const msg = friendlyErr(e);
    self.logger.error('[Quadify] Failed to write IR GPIO to YAML: ' + msg);
    self.commandRouter.pushToastMessage('error', 'Quadify', 'Failed to save IR GPIO pin: ' + msg);
    return libQ.resolve({});
  }

  const hwCfg = self.loadConfigYaml();

  // 2) Build & persist preference; install profile if enabling
  return loadRawPreferenceJSON()
    .then(async (raw) => {
      const pref = buildCanonicalFromAny(raw, hwCfg);

      if (flat.enableIR !== undefined)          pref.ir.enabled = logicValue(flat.enableIR);
      if (flat.ir_remote_profile !== undefined) pref.ir.profile = String(flat.ir_remote_profile || '');

      // Install selected profile when enabling IR
      if (pref.ir.enabled) {
        if (!pref.ir.profile) throw new Error('IR enabled but no profile selected');
        try {
          await self.installIrProfile(pref.ir.profile); // should handle sudo internally
        } catch (e) {
          const msg = friendlyErr(e);
          // Make common sudo error clearer
          const friendly = /sudo|password|permission denied|EACCES/i.test(msg)
            ? 'Permission denied writing /etc/lirc (sudo required).'
            : msg;
          throw new Error(friendly);
        }
      }

      const merged = withFlatMirrors(raw, pref);
      await saveCanonicalPreference(merged);
      return pref;
    })
    .then(async (pref) => {
      // 3) Mirror to v-conf
      applyPreferenceToVconfInstance(self.config, pref);
      self.config.save();

      // 4) Toggle services to match desired state, but don’t fail the save if they error
      const want = !!pref.ir.enabled;
      const results = await libQ.allSettled([
        self.controlService('lircd',       want),
        self.controlService('ir_listener', want)
      ]);

      const fails = results.filter(r => r.state === 'rejected');
      if (fails.length) {
        const msgs = fails.map(f => friendlyErr(f.reason)).join(' | ');
        self.logger.warn('[Quadify] IR service toggle issues: ' + msgs);
        self.commandRouter.pushToastMessage('warning', 'Quadify', 'IR saved, but service toggle had issues: ' + msgs);
      } else {
        self.commandRouter.pushToastMessage('success', 'Quadify', 'IR settings saved');
      }

      return {};
    })
    .catch((err) => {
      const msg = friendlyErr(err);
      self.logger.error('[Quadify] saveIr_controller: ' + msg);
      self.commandRouter.pushToastMessage('error', 'Quadify', `Failed to save IR settings: ${msg}`);
      return {};
    });
};

ControllerQuadify.prototype.applyAllServiceTogglesFromPreference = async function () {
  const hwCfg = this.loadConfigYaml();
  const pref  = await getCanonicalPreference(hwCfg);

  // Resolve the unit before toggles
  await this.detectButtonsLedsUnit();

  const want = {
    cava:    !!pref.display.spectrum,
    buttons: !!pref.controls.buttons_led_service,
    ir:      !!pref.ir.enabled,
    safety:  !!pref.safety.safe_shutdown
  };

  // High-signal intent log
  this.logger.info(
    `[Quadify][TOGGLE] want: cava=${want.cava} buttons=${want.buttons} ir=${want.ir} safety=${want.safety} ` +
    `(btnUnit=${this.buttonsLedsUnit || 'n/a'}) screen=${pref.display.screen} rotate=${pref.display.rotate} mcp=${pref.controls.mcp23017_address}`
  );

  // Pre-change snapshot (don’t fail the run if snapshot errors)
  try { await logSnapshot(this, 'pre-toggles'); }
  catch (e) { this.logger.warn('[Quadify][SNAPSHOT] pre failed: ' + friendlyErr(e)); }

  // Build task list with labels so we can summarize results cleanly
  const tasks  = [];
  const labels = [];

  tasks.push(this.cavaStartStop(want.cava)); labels.push('cava');

  if (this.buttonsLedsUnit) {
    tasks.push(this.controlButtonsLeds(want.buttons));
    labels.push(this.buttonsLedsUnit);
  } else {
    this.logger.warn('[Quadify][TOGGLE] Buttons/LEDs service not installed; skipping toggle');
  }

  tasks.push(this.controlService('lircd',       want.ir)); labels.push('lircd');
  tasks.push(this.controlService('ir_listener', want.ir)); labels.push('ir_listener');

  // Safety toggles only enable/disable installed units
  tasks.push(this.controlSafeShutdown(want.safety));      labels.push('safe-shutdown');

  const results = await libQ.allSettled(tasks);

  // Per-task summary (fulfilled/rejected)
  results.forEach((r, i) => {
    const name = labels[i];
    if (r.state === 'fulfilled') {
      const v = r.value || {};
      // controlService() returns {active, enabled}; others may not
      if (name === this.buttonsLedsUnit || name === 'lircd' || name === 'ir_listener') {
        this.logger.info(`[Quadify][RESULT] ${name}: active=${v.active || 'n/a'} enabled=${v.enabled || 'n/a'}`);
      } else {
        this.logger.info(`[Quadify][RESULT] ${name}: ok`);
      }
    } else {
      this.logger.warn(`[Quadify][RESULT] ${name}: ERROR ${friendlyErr(r.reason)}`);
    }
  });

  // Post-change snapshot across the key units
  try {
    const cava  = await systemdSnapshot('cava');
    const btn   = this.buttonsLedsUnit
      ? await systemdSnapshot(this.buttonsLedsUnit)
      : { unit: this.buttonsLedsUnit || 'n/a', active: 'n/a', enabled: 'n/a' };
    const lircd = await systemdSnapshot('lircd');
    const irl   = await systemdSnapshot('ir_listener');

    this.logger.info(
      `[Quadify][SNAPSHOT] post-toggles :: ` +
      `cava=${cava.active}/${cava.enabled}, ` +
      `${btn.unit}=${btn.active}/${btn.enabled}, ` +
      `lircd=${lircd.active}/${lircd.enabled}, ` +
      `ir_listener=${irl.active}/${irl.enabled}`
    );
  } catch (e) {
    this.logger.warn('[Quadify][SNAPSHOT] post failed: ' + friendlyErr(e));
  }

  this.logger.info('[Quadify] Applied service toggles from preference');
  return results;
};


// ---------- Buttons & LEDs (save) ----------
ControllerQuadify.prototype.saveButtons_leds = function (data) {
  const self = this;
  const flat = getFlatConfig(data || {});
  self.logger.info('[Quadify] saveButtons_leds flat: ' + JSON.stringify(flat));

  // helpers: UI uses hex-without-0x (e.g. "20")
  const hexStrip0x = (s) => {
    const t = String(s ?? '').trim().toLowerCase();
    return t.startsWith('0x') ? t.slice(2) : t;
  };
  const hexNoPrefixToInt = (s, fb = 32) => {
    const v = parseInt(hexStrip0x(s), 16);
    return Number.isFinite(v) ? v : fb;
  };

  // --- 1) Address handling (optional field) ---
  // Accept either flat.mcp23017_address or data.mcp23017_address; default to keep-as-is if absent
  const addrProvided = flat.mcp23017_address !== undefined || (data && data.mcp23017_address !== undefined);
  const rawUi = addrProvided ? (flat.mcp23017_address ?? data.mcp23017_address) : undefined;

  let hexUi = null;
  let i2cInt = null;

  if (addrProvided) {
    hexUi  = hexStrip0x(rawUi || '20');      // "20"
    i2cInt = hexNoPrefixToInt(hexUi, 32);    // 32

    // YAML (daemon) wants a NUMBER
    const cfg = self.loadConfigYaml();
    cfg.mcp23017_address = i2cInt;
    self.saveConfigYaml(cfg);
    self.logger.info(`[Quadify][YAML] mcp23017_address <= ${i2cInt} (from UI ${hexUi})`);

    // v-conf mirror keeps no-prefix hex so UI shows "20"
    self.config.set('mcp23017_address', hexUi);
    self.config.save();
  }

  const hwCfg = self.loadConfigYaml();

  // --- 2) Persist preference.json (nested canonical + flat mirrors) ---
  return loadRawPreferenceJSON()
    .then(raw => {
      const prefObj = buildCanonicalFromAny(raw, hwCfg);

      if (flat.enableButtonsLED !== undefined) {
        prefObj.controls.buttons_led_service = logicValue(flat.enableButtonsLED);
      }
      if (addrProvided && hexUi !== null) {
        // keep prefs as no-prefix hex "20"
        prefObj.controls.mcp23017_address = hexUi;
      }

      const merged = withFlatMirrors(raw, prefObj);
      return saveCanonicalPreference(merged).then(() => prefObj);
    })
    .then(prefObj => {
      // --- 3) Mirror to v-conf for UI reflection ---
      applyPreferenceToVconfInstance(self.config, prefObj);
      // ensure the mcp address in v-conf remains no-prefix hex if we changed it
      if (addrProvided && hexUi !== null) self.config.set('mcp23017_address', hexUi);
      self.config.save();

      // Ensure we know the unit name before toggling
      const ready = self.buttonsLedsUnit ? libQ.resolve() : self.detectButtonsLedsUnit();
      return ready.then(() => prefObj);
    })
    .then(prefObj => {
      // --- 4) Toggle the service according to preference ---
      const want = !!prefObj.controls.buttons_led_service;
      self.logger.info(`[Quadify][TOGGLE] Buttons/LEDs => ${want ? 'enable' : 'disable'} (unit=${self.buttonsLedsUnit || 'n/a'})`);
      return self.controlButtonsLeds(want);
    })
    .then(state => {
      const msg = `Buttons & LEDs ${state?.active === 'active' ? 'enabled' : 'disabled'} (unitfile=${state?.enabled || 'unknown'})`;
      self.logger.info(`[Quadify] ${msg}`);
      self.commandRouter.pushToastMessage('success', 'Quadify', msg);
      return {};
    })
    .catch(err => {
      const msg = friendlyErr(err);
      self.logger.error('[Quadify] saveButtons_leds error: ' + msg);
      self.commandRouter.pushToastMessage('error', 'Quadify', 'Buttons & LEDs save failed: ' + msg);
      return {};
    });
};



ControllerQuadify.prototype.saveSafety_controls = function (data) {
  const self = this;
  const flat = getFlatConfig(data || {});
  const hwCfg = self.loadConfigYaml();

  return loadRawPreferenceJSON()
    .then(raw => {
      const pref = buildCanonicalFromAny(raw, hwCfg);
      if (flat.safe_shutdown_enabled !== undefined) {
        pref.safety.safe_shutdown = logicValue(flat.safe_shutdown_enabled);
      }
      const merged = withFlatMirrors(raw, pref);
      return saveCanonicalPreference(merged).then(() => pref);
    })
    .then(async (pref) => {
      applyPreferenceToVconfInstance(self.config, pref);
      self.config.save();

      const want = !!pref.safety.safe_shutdown;

      // IMPORTANT: only enable/disable units; DO NOT start them (avoids LEDs turning off now)
      await self.controlSafeShutdown(want);

      self.commandRouter.pushToastMessage(
        'success',
        'Quadify',
        want ? 'Safety: clean shutdown enabled' : 'Safety: clean shutdown disabled'
      );
      return {};
    })
    .catch(err => {
      self.logger.error('[Quadify] saveSafety_controls: ' + (err?.message || err));
      self.commandRouter.pushToastMessage('error', 'Quadify', 'Failed to save Safety settings');
      return {};
    });
};

// ---------- Detect Buttons/LEDs unit ----------
ControllerQuadify.prototype.detectButtonsLedsUnit = function () {
  const self = this;
  const systemctl = SYSTEMCTL;

  const tryOne = (name) => {
    const unit = `${name}.service`;
    // show returns 0 even if disabled/inactive; LoadState tells us if it exists
    return pExec(`${systemctl} show -p LoadState -p FragmentPath ${unit}`, self.logger)
      .then(({ stdout }) => {
        const lines = (stdout || '').split('\n').filter(Boolean);
        const map = {};
        lines.forEach(l => {
          const i = l.indexOf('=');
          if (i > -1) map[l.slice(0, i)] = l.slice(i + 1);
        });

        // High-signal per-candidate log (values are in scope here)
        self.logger.info(
          `[Quadify][DETECT] ${unit} load=${map.LoadState || 'unknown'} path=${map.FragmentPath || 'n/a'}`
        );

        return map.LoadState === 'loaded' ? name : null;
      })
      .fail((e) => {
        self.logger.warn(
          `[Quadify][DETECT] ${unit} query failed: ${e?.err?.message || e?.message || e}`
        );
        return null;
      });
  };

  let chain = libQ.resolve(null);
  BUTTONSLEDS_UNIT_CANDIDATES.forEach((name) => {
    chain = chain.then(found => (found ? found : tryOne(name)));
  });

  return chain.then((found) => {
    self.buttonsLedsUnit = found || null;
    if (self.buttonsLedsUnit) {
      self.logger.info(`[Quadify][DETECT] using unit=${self.buttonsLedsUnit}`);
    } else {
      self.logger.warn('[Quadify] Buttons/LEDs unit not found (quadify-buttonsleds).');
      self.commandRouter.pushToastMessage('warning', 'Quadify', 'Buttons/LEDs service not installed');
    }
    return self.buttonsLedsUnit;
  });
};


ControllerQuadify.prototype.applyAllServiceToggles = function () {
  const flatConfig = getFlatConfig(this.config.get() || {});
  const spectrumOn = logicValue(
    (flatConfig.enableSpectrum !== undefined ? flatConfig.enableSpectrum : flatConfig.enableCava)
  );
  const buttonsOn = logicValue(flatConfig.enableButtonsLED);
  const irOn      = logicValue(flatConfig.enableIR);
  const safeOn    = logicValue(flatConfig.safe_shutdown_enabled); // NEW

  return libQ.allSettled([
    this.cavaStartStop(spectrumOn),
    this.controlButtonsLeds(buttonsOn),
    this.controlService('lircd', irOn),
    this.controlService('ir_listener', irOn),
    this.controlSafeShutdown(safeOn) // NEW
  ]);
};


module.exports = ControllerQuadify;

// ---------- Vconf → Canonical preference (builder) ----------
function buildPreferenceFromVconf(conf) {
  const get = (k, fb) => {
    const v = conf.get(k);
    return (v === undefined || v === null) ? fb : v;
  };

  return {
    display: {
      spectrum: !!get('enableSpectrum', get('enableCava', true)), // back-compat
      screen:    get('display_screen', get('display_mode', 'modern')),
      rotate:    parseInt(get('display_rotate', 180), 10) || 0
    },
    controls: {
      buttons_led_service: !!get('enableButtonsLED', true),
      mcp23017_address:     get('mcp23017_address', '20')
    },
    ir: {
      enabled:  !!get('enableIR', true),
      profile:   get('ir_remote_profile', 'Xiaomi IR for TV box'),
      gpio_bcm:  parseInt(get('ir_gpio_pin', 27), 10) || 27
    },
    safety: {
      safe_shutdown: !!get('safe_shutdown_enabled', true),
    }
  };
}
