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
const BUTTONSLEDS_UNIT_CANDIDATES = ['quadify-buttonsleds', 'buttonsleds'];

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

function coerceHexAddr(a, fb = '0x20') {
  if (a === undefined || a === null || a === '') return fb;
  let s = String(a).trim().toLowerCase();
  if (!s.startsWith('0x')) s = '0x' + s;
  return s;
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
    res.display_mode = s || 'vu';
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

// --------- Preference IO / Canonicalisation ---------
const PREF_DEFAULTS = {
  display:  { spectrum: false, screen: 'vu', rotate: 0 },
  controls: { buttons_led_service: false, mcp23017_address: '0x20' },
  ir:       { enabled: false, profile: '', gpio_bcm: 27 },
  safety:   { safe_shutdown: false, clean_mode: false }
};

async function loadRawPreferenceJSON() {
  const p = await resolvePreferencePath();
  try { return await fs.readJson(p); } catch { return {}; }
}

async function saveCanonicalPreference(prefObj) {
  const p = await resolvePreferencePath();
  await atomicWriteJSON(p, prefObj);
}

// Merge in canonical, then assert flat mirrors for legacy code
function withFlatMirrors(raw, canonical) {
  const merged = shallowMerge(raw, canonical);
  merged.cava_enabled = !!canonical.display?.spectrum;
  merged.display_mode = String(canonical.display?.screen || 'vu');
  return merged;
}

// Build canonical (nested) preference from any raw + YAML overrides
function buildCanonicalFromAny(raw, hwYaml = {}) {
  const nestedInput = (raw.display && raw.controls) ? raw : {};
  const out = shallowMerge(PREF_DEFAULTS, nestedInput);

  // display
  const legacyScreen = raw.display_screen || raw.display_mode;
  if (legacyScreen) out.display.screen = String(legacyScreen);
  if ('cava_enabled' in raw) out.display.spectrum = !!raw.cava_enabled;
  if ('oled_brightness' in raw) {
    const ob = parseInt(raw.oled_brightness, 10);
    if (!Number.isNaN(ob)) out.display.oled_brightness = ob;
  }

  // YAML overrides
  if (hwYaml.display_rotate !== undefined) out.display.rotate = parseInt(hwYaml.display_rotate, 10) || 0;

  // controls
  const yamlAddr = hwYaml.mcp23017_address;
  out.controls.mcp23017_address = coerceHexAddr(yamlAddr ?? out.controls.mcp23017_address);

  // IR
  const yamlIrPin = hwYaml.ir_gpio_pin;
  out.ir.gpio_bcm = parseInt(yamlIrPin ?? out.ir.gpio_bcm ?? 27, 10) || 27;

  return out;
}

async function getCanonicalPreference(hwYaml) {
  const raw = await loadRawPreferenceJSON();
  return buildCanonicalFromAny(raw, hwYaml);
}

function applyPreferenceToVconfInstance(vconf, pref) {
  // display
  vconf.set('enableSpectrum', !!pref.display.spectrum);
  vconf.set('enableCava',     !!pref.display.spectrum); // legacy mirror
  vconf.set('display_screen', String(pref.display.screen || 'vu'));
  vconf.set('display_rotate', String(pref.display.rotate ?? '0'));

  // buttons/LEDs
  vconf.set('enableButtonsLED', !!pref.controls.buttons_led_service);
  vconf.set('mcp23017_address', String(pref.controls.mcp23017_address || '0x20'));

  // IR
  vconf.set('enableIR',          !!pref.ir.enabled);
  vconf.set('ir_remote_profile', String(pref.ir.profile || ''));
  vconf.set('ir_gpio_pin',        parseInt(pref.ir.gpio_bcm, 10) || 27);

  // safety
  vconf.set('safe_shutdown_enabled', !!pref.safety.safe_shutdown);
  vconf.set('clean_mode_enabled',    !!pref.safety.clean_mode);
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

  const base      = path.join(LIRC_PROFILES_DIR, safe);
  const srcLircd  = path.join(base, 'lircd.conf');
  const srcLircrc = path.join(base, 'lircrc');

  if (!(await fs.pathExists(srcLircd))) {
    throw new Error(`IR profile incomplete: ${safe} (missing lircd.conf)`);
  }
  // lircrc is optional in your flow (ir_listener doesn’t use it), so don’t fail if missing
  const hasLircrc = await fs.pathExists(srcLircrc);

  async function doCopies(useSudo) {
    if (useSudo) {
      await pExec(`${SUDO} -n mkdir -p "${LIRC_DST_DIR}"`, this.logger).fail(() => libQ.resolve());
      await pExec(`${SUDO} -n cp -f "${srcLircd}" "${LIRCD_CONF_DST}"`, this.logger);
      if (hasLircrc) {
        await pExec(`${SUDO} -n cp -f "${srcLircrc}" "${LIRCRC_DST}"`, this.logger).fail(() => libQ.resolve());
      }
      // Ensure the home symlink always exists and points to /etc version
      await pExec(`${SUDO} -n ln -sf "${LIRCD_CONF_DST}" "/home/volumio/lircd.conf"`, this.logger).fail(() => libQ.resolve());
    } else {
      await fs.ensureDir(LIRC_DST_DIR);
      await fs.copy(srcLircd, LIRCD_CONF_DST, { overwrite: true });
      if (hasLircrc) {
        await fs.copy(srcLircrc, LIRCRC_DST, { overwrite: true });
      }
      try {
        await fs.unlink('/home/volumio/lircd.conf').catch(() => {});
        await fs.symlink(LIRCD_CONF_DST, '/home/volumio/lircd.conf');
      } catch (_) { /* non-fatal */ }
    }
  }

  try {
    await doCopies.call(this, /*useSudo=*/false);
  } catch (e) {
    if (e.code !== 'EACCES' && !/permission denied/i.test(e.message || '')) throw e;
    await doCopies.call(this, /*useSudo=*/true);
  }

  this.logger.info(`[Quadify] Installed IR profile: ${safe}`);
};

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
  const d = libQ.defer();

  try { this.config.loadFile(CONFIG_PATH); } catch (_) {}

  this.detectButtonsLedsUnit()
    .then(() => this.applyAllServiceToggles())
    .then(() => { this.logger.info('[Quadify] Applied saved toggles on start'); d.resolve(); })
    .fail(err => { this.logger.error('[Quadify] apply toggles failed: ' + (err?.message || err)); d.resolve(); });

  return d.promise;
};

ControllerQuadify.prototype.onVolumioStart = function () {
  const defer = libQ.defer();

  (async () => {
    this.logger.info('[Quadify] onVolumioStart – ensure config');

    try {
      fs.ensureDirSync(CONFIG_DIR);
      if (!fs.existsSync(CONFIG_PATH)) fs.writeJsonSync(CONFIG_PATH, {}, { spaces: 2 });
    } catch (e) {
      this.logger.error('[Quadify] ensure config dir/file failed: ' + e.message);
    }

    try { this.config.loadFile(CONFIG_PATH); }
    catch (e) { this.logger.warn('[Quadify] loadFile failed, starting fresh: ' + e.message); }

    const defaults = {
      enableCava: false,
      enableButtonsLED: false,
      mcp23017_address: '0x20',
      display_mode: 'modern',
      clock_font_key: 'clock_sans',
      show_seconds: false,
      show_date: false,
      screensaver_enabled: false,
      screensaver_type: 'geo',
      screensaver_timeout: 3600,
      oled_brightness: 200,
      // New UI keys
      enableSpectrum: false,
      display_screen: 'vu',
      display_rotate: '0',
      enableIR: false,
      ir_remote_profile: '',
      ir_gpio_pin: 27,
      safe_shutdown_enabled: false,
      clean_mode_enabled: false
    };

    let changed = false;
    Object.keys(defaults).forEach(k => {
      if (this.config.get(k) === undefined) {
        this.config.set(k, defaults[k]);
        changed = true;
      }
    });
    if (changed) { this.logger.info('[Quadify] writing default config.json'); this.config.save(); }

    // Preference import/migration BEFORE returning
    const hwCfg = this.loadConfigYaml();
    try {
      const raw       = await loadRawPreferenceJSON();
      const canonical = buildCanonicalFromAny(raw, hwCfg);
      const merged    = withFlatMirrors(raw, canonical);

      await saveCanonicalPreference(merged);

      // Mirror canonical → v-conf (sets enableSpectrum AND enableCava)
      applyPreferenceToVconfInstance(this.config, canonical);
      this.config.save();

      // Resolve Buttons/LEDs unit, then apply service toggles
      await this.detectButtonsLedsUnit();
      await this.applyAllServiceToggles();
    } catch (e) {
      this.logger.warn('[Quadify] pref migrate/import on boot: ' + e.message);
    }

    // Optional: sync hook (kept as no-op)
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
    const hwCfg = this.loadConfigYaml();
    const pref  = await getCanonicalPreference(hwCfg);

    const set = (sectionId, id, val) => {
      const sec = uiconf.sections.find(s => s.id === sectionId);
      if (!sec) return;
      const row = sec.content.find(c => c.id === id);
      if (row !== undefined) row.value = val;
    };

    // YAML truth for hardware
    set('display_settings', 'display_rotate',  String(hwCfg.display_rotate ?? pref.display.rotate ?? '0'));
    set('buttons_leds',     'mcp23017_address', coerceHexAddr(hwCfg.mcp23017_address || pref.controls.mcp23017_address || '0x20'));
    set('ir_controller',    'ir_gpio_pin',      parseInt(hwCfg.ir_gpio_pin ?? pref.ir.gpio_bcm ?? 27, 10));

    // Preference truth for toggles/choices
    set('display_settings', 'enableSpectrum',        !!pref.display.spectrum);
    set('display_settings', 'display_screen',        String(pref.display.screen || 'vu'));
    set('buttons_leds',     'enableButtonsLED',      !!pref.controls.buttons_led_service);
    set('ir_controller',    'enableIR',              !!pref.ir.enabled);
    set('ir_controller',    'ir_remote_profile',     String(pref.ir.profile || ''));
    set('safety_controls',  'safe_shutdown_enabled', !!pref.safety.safe_shutdown);
    set('safety_controls',  'clean_mode_enabled',    !!pref.safety.clean_mode);

    // ---- IR profile options (from filesystem)
    const irSec = uiconf.sections.find(s => s.id === 'ir_controller');
    if (irSec) {
      const profileEl = irSec.content.find(c => c.id === 'ir_remote_profile');
      if (profileEl) {
        const opts = await this.listIrProfiles();
        profileEl.options = [{ label: '— none —', value: '' }, ...opts];

        const stored = String(pref.ir.profile || '');
        profileEl.value = opts.some(o => o.value === stored) ? stored : '';
      }
    }

    // Legacy back-compat (optional)
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
      oldConfig.mcp23017_address || '0x20'
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
    mergedConfig.mcp23017_address = coerceHexAddr(mergedConfig.mcp23017_address);
  }

  Object.keys(mergedConfig).forEach(k => this.config.set(k, mergedConfig[k]));
  this.config.save();

  this.applyAllServiceToggles();

  const hwCfg = this.loadConfigYaml();
  return loadRawPreferenceJSON()
    .then(raw => {
      const canonical  = buildCanonicalFromAny(raw, hwCfg);
      const desired    = buildPreferenceFromVconf(this.config);
      const mergedPref = withFlatMirrors(raw, shallowMerge(canonical, desired));
      return saveCanonicalPreference(mergedPref).then(() => mergedPref);
    })
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

  const safeExec = (cmd) =>
    pExec(cmd, logger).fail(() => ({ stdout: '', stderr: '', cmd, failed: true }));

  const verify = () => libQ.all([
      safeExec(`${systemctl} is-active ${service}`),
      safeExec(`${systemctl} is-enabled ${service}`)
    ]).then(([a, e]) => {
      const active  = (a.stdout || '').trim() || 'unknown';
      const enabled = (e.stdout || '').trim() || 'unknown';
      logger.info(`[Quadify] VERIFY ${service}: active=${active} enabled=${enabled}`);
      return { active, enabled };
    });

  // Use sudo -n (non-interactive) so it fails fast if not allowed
  const enableSeq       = [`${sudo} -n ${systemctl} daemon-reload`, `${sudo} -n ${systemctl} enable --now ${unit}`];
  const enableFallback  = [`${sudo} -n ${systemctl} start ${unit}`];
  const disableSeq      = [`${sudo} -n ${systemctl} disable --now ${unit}`];
  const disableFallback = [`${sudo} -n ${systemctl} stop ${unit}`, `${sudo} -n ${systemctl} disable ${unit}`];

  const runSeq = (seq) => seq.reduce(
    (p, cmd) => p.then(() => pExec(cmd, logger).fail(() => libQ.resolve())),
    libQ.resolve()
  );

  const plan = enable ? [enableSeq, enableFallback] : [disableSeq, disableFallback];

  return runSeq(plan[0])
    .then(verify)
    .then(state => {
      const wantActive  = enable ? 'active'  : 'inactive';
      const wantEnabled = enable ? 'enabled' : 'disabled';
      if (state.active !== wantActive || state.enabled !== wantEnabled) {
        logger.warn(`[Quadify] ${service} not in desired state (have active=${state.active} enabled=${state.enabled}), applying fallback...`);
        return runSeq(plan[1]).then(verify);
      }
      return state;
    })
    .then(finalState => {
      logger.info(`[Quadify] ${service} final state: active=${finalState.active} enabled=${finalState.enabled}`);
      return finalState;
    })
    .fail(err => {
      logger.error(`[Quadify] controlService(${service}, ${enable}) ERROR: ${err?.err?.message || err}`);
    });
};


ControllerQuadify.prototype.controlButtonsLeds = function (enable) {
  const self = this;
  return libQ.resolve()
    .then(() => {
      if (!self.buttonsLedsUnit) {
        return self.detectButtonsLedsUnit();
      }
    })
    .then(() => {
      if (!self.buttonsLedsUnit) {
        self.logger.warn('[Quadify] Skipping Buttons/LEDs toggle: no unit detected');
        return;
      }
      return self.controlService(self.buttonsLedsUnit, enable);
    });
};

// ---------- MCP23017 / YAML ----------
ControllerQuadify.prototype.updateMcpConfig = function (data) {
  let addr = data.mcp23017_address;
  if (addr && !String(addr).toLowerCase().startsWith('0x')) addr = '0x' + addr;

  const cfg = this.loadConfigYaml();
  cfg.mcp23017_address = addr;
  this.saveConfigYaml(cfg);

  this.config.set('mcp23017_address', addr);
  this.config.save();

  this.commandRouter.pushToastMessage('success', 'Quadify', `MCP23017 address saved: ${addr}`);

  const hwCfg = this.loadConfigYaml();
  return loadRawPreferenceJSON()
    .then(raw => {
      const canonical = buildCanonicalFromAny(raw, hwCfg);
      canonical.controls.mcp23017_address = addr;
      return saveCanonicalPreference(withFlatMirrors(raw, canonical));
    })
    .then(() => ({}))
    .catch(e => {
      this.logger.warn('[Quadify] pref sync after updateMcpConfig: ' + (e?.message || e));
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
    cfg.mcp23017_address = foundAddr || '';
    this.saveConfigYaml(cfg);

    this.config.set('mcp23017_address', foundAddr || '');
    this.config.save();

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
  const flat = getFlatConfig(data || {});
  const cfg  = self.loadConfigYaml();

  // YAML truth for rotate
  if (flat.display_rotate !== undefined) {
    cfg.display_rotate = String(flat.display_rotate);
    self.saveConfigYaml(cfg);
  }

  const hwCfg = self.loadConfigYaml();
  return loadRawPreferenceJSON()
    .then(raw => {
      const pref = buildCanonicalFromAny(raw, hwCfg);

      // Spectrum: prefer enableSpectrum; fallback to legacy enableCava
      let spectrumVal;
      if (flat.enableSpectrum !== undefined)      spectrumVal = logicValue(flat.enableSpectrum);
      else if (flat.enableCava !== undefined)     spectrumVal = logicValue(flat.enableCava);
      if (spectrumVal !== undefined)              pref.display.spectrum = spectrumVal;

      // Screen (allow all variants you use)
      if (flat.display_screen !== undefined) {
        const scr = String(flat.display_screen);
        const allowed = [
          'vu','minimal','digitalvuscreen','vuscreen',
          'modern','modern-bars','modern-dots','modern-osci',
          'fm4','original'
        ];
        pref.display.screen = allowed.includes(scr) ? scr : 'vu';
      }

      // Write with flat mirrors
      const writeObj = mergedCanonicalWithMinimalMirrors(raw, pref);
      return saveCanonicalPreference(writeObj).then(() => pref);

    })
    .then(pref => {
      // mirror to v-conf
      applyPreferenceToVconfInstance(self.config, pref);
      self.config.save();

      // Toggle services inline
      const spectrumOn = !!pref.display.spectrum;
      const buttonsOn  = !!pref.controls.buttons_led_service;
      return libQ.all([
        self.controlService('cava', spectrumOn),
        self.controlButtonsLeds(buttonsOn)
      ]);
    })
    .then(() => {
      self.commandRouter.pushToastMessage('success', 'Quadify', 'Display settings saved, please restart Quadify');
      return {};
    })
    .catch(err => {
      self.logger.error('[Quadify] saveDisplay_settings: ' + (err?.message || err));
      self.commandRouter.pushToastMessage('error', 'Quadify', 'Failed to save display settings');
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


ControllerQuadify.prototype.saveButtons_leds = function (data) {
  const self = this;
  const flat = getFlatConfig(data || {});
  const cfg  = self.loadConfigYaml();

  if (flat.mcp23017_address !== undefined) {
    let a = String(flat.mcp23017_address).trim().toLowerCase();
    if (!a.startsWith('0x')) a = '0x' + a;
    cfg.mcp23017_address = a;
    self.saveConfigYaml(cfg);
  }

  const hwCfg = self.loadConfigYaml();
  return loadRawPreferenceJSON()
    .then(raw => {
      const pref = buildCanonicalFromAny(raw, hwCfg);
      if (flat.enableButtonsLED !== undefined) {
        pref.controls.buttons_led_service = logicValue(flat.enableButtonsLED);
      }
      const merged = withFlatMirrors(raw, pref);
      return saveCanonicalPreference(merged).then(() => pref);
    })
    .then(pref => {
      applyPreferenceToVconfInstance(self.config, pref);
      self.config.save();

      // NEW: ensure we know the exact unit name before toggling it
      return libQ.resolve()
        .then(() => (!self.buttonsLedsUnit ? self.detectButtonsLedsUnit() : null))
        .then(() => pref);
    })
    .then(pref => {
      // Toggle services inline
      const spectrumOn = !!pref.display.spectrum;
      const buttonsOn  = !!pref.controls.buttons_led_service;
      return libQ.all([
        self.controlService('cava', spectrumOn),
        self.controlButtonsLeds(buttonsOn)
      ]);
    })
    .then(() => {
      self.commandRouter.pushToastMessage('success', 'Quadify', 'Buttons & LEDs saved');
      return {};
    })
    .catch(err => {
      self.logger.error('[Quadify] saveButtons_leds: ' + (err?.message || err));
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
      if (flat.safe_shutdown_enabled !== undefined) pref.safety.safe_shutdown = logicValue(flat.safe_shutdown_enabled);
      if (flat.clean_mode_enabled   !== undefined)  pref.safety.clean_mode    = logicValue(flat.clean_mode_enabled);

      const merged = withFlatMirrors(raw, pref);
      return saveCanonicalPreference(merged).then(() => pref);
    })
    .then(pref => {
      applyPreferenceToVconfInstance(self.config, pref);
      self.config.save();
      self.commandRouter.pushToastMessage('success', 'Quadify', 'Safety settings saved please restart Quadify');
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
  const systemctl = SYSTEMCTL; // uses the resolver you added

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
        return map.LoadState === 'loaded' ? name : null;
      })
      .fail(() => null);
  };

  let chain = libQ.resolve(null);
  BUTTONSLEDS_UNIT_CANDIDATES.forEach((name) => {
    chain = chain.then(found => (found ? found : tryOne(name)));
  });

  return chain.then((found) => {
    self.buttonsLedsUnit = found || null;
    if (self.buttonsLedsUnit) {
      self.logger.info(`[Quadify] Buttons/LEDs unit resolved: ${self.buttonsLedsUnit}`);
    } else {
      self.logger.warn('[Quadify] Buttons/LEDs unit not found (quadify-buttonsleds/buttonsleds).');
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

  return libQ.allSettled([
    this.controlService('cava', spectrumOn),
    this.controlButtonsLeds(buttonsOn),
    this.controlService('lircd', irOn),
    this.controlService('ir_listener', irOn)
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
      spectrum: !!get('enableSpectrum', get('enableCava', false)), // back-compat
      screen:    get('display_screen', get('display_mode', 'vu')),
      rotate:    parseInt(get('display_rotate', 0), 10) || 0
    },
    controls: {
      buttons_led_service: !!get('enableButtonsLED', false),
      mcp23017_address:     get('mcp23017_address', '0x20')
    },
    ir: {
      enabled:  !!get('enableIR', false),
      profile:   get('ir_remote_profile', ''),
      gpio_bcm:  parseInt(get('ir_gpio_pin', 27), 10) || 27
    },
    safety: {
      safe_shutdown: !!get('safe_shutdown_enabled', false),
      clean_mode:    !!get('clean_mode_enabled', false)
    }
  };
}
