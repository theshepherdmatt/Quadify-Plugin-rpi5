'use strict';

const libQ = require('kew');
const fs = require('fs-extra');
const yaml = require('js-yaml');
const path = require('path');
const exec = require('child_process').exec;
const Vconf = require('v-conf');

// --------- Paths ----------
const PLUGIN_ROOT = __dirname;
const PREF_PATH = path.join(PLUGIN_ROOT, 'quadifyapp', 'src', 'preference.json');
const YAML_PATH = path.join(PLUGIN_ROOT, 'quadifyapp', 'config.yaml');

// --- Service names / detection for Buttons & LEDs ---
const BUTTONSLEDS_UNIT_CANDIDATES = [
  'quadify-buttonsleds',
  'buttonsleds'
];

const USERCONFIG_TXT = '/boot/userconfig.txt';
const IR_OVERLAY_LINE = 'dtoverlay=gpio-ir,gpio_pin=27';

const CONFIG_DIR = '/data/configuration/system_hardware/quadify';
const CONFIG_PATH = CONFIG_DIR + '/config.json';

// Preference path candidates + helpers
const PREF_CANDIDATES = [
  path.join(PLUGIN_ROOT, 'quadifyapp', 'src', 'preference.json'),
  path.join(PLUGIN_ROOT, 'quadifyapp', 'scr', 'preference.json')
];

async function ensureDir(dir) {
  try { await fs.ensureDir(dir); } catch (_) {}
}

async function atomicWriteJSON(file, obj) {
  const tmp = file + '.tmp';
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

// ------------- Helpers ---------------
function logicValue(val) {
  if (typeof val === 'boolean') return val;
  if (typeof val === 'string') return val === 'true' || val === 'on' || val === '1';
  if (typeof val === 'number') return !!val;
  return false;
}

function flatten(val) {
  while (val && typeof val === 'object' && 'value' in val) val = val.value;
  return val;
}

function getFlatConfig(config) {
  const flat = {};
  Object.keys(config || {}).forEach(key => {
    flat[key] = flatten(config[key]);
  });
  return flat;
}

// Prevent storing the literal string "undefined"
const pick = (v, fallback) =>
  (v === undefined || v === null || v === '' || v === 'undefined') ? fallback : v;

// IR overlay helper (kept, required by Volumio IR plugin)
function ensureIrOverlayGpio27() {
  let content = '';
  try {
    content = fs.readFileSync(USERCONFIG_TXT, 'utf8');
  } catch (e) {
    content = '';
  }
  const lines = content.split('\n').filter(line => !/^dtoverlay=gpio-ir/.test(line));
  lines.push(IR_OVERLAY_LINE);
  fs.writeFileSync(USERCONFIG_TXT, lines.filter(Boolean).join('\n') + '\n', 'utf8');
}

// Build preference payload (maps legacy keys too)
function buildPreferenceFromVconf(conf) {
  const get = (k, fb) => {
    const v = conf.get(k);
    return (v === undefined || v === null) ? fb : v;
  };

  return {
    display: {
      spectrum: !!get('enableSpectrum', get('enableCava', false)), // back-compat with enableCava
      screen: get('display_screen', get('display_mode', 'vu')),
      rotate: parseInt(get('display_rotate', 0), 10) || 0
    },
    controls: {
      buttons_led_service: !!get('enableButtonsLED', false),
      mcp23017_address: get('mcp23017_address', '0x20')
    },
    ir: {
      enabled: !!get('enableIR', false),
      profile: get('ir_remote_profile', ''),
      gpio_bcm: parseInt(get('ir_gpio_pin', 27), 10) || 27
    },
    safety: {
      safe_shutdown: !!get('safe_shutdown_enabled', false),
      clean_mode: !!get('clean_mode_enabled', false)
    }
  };
}

// Non-destructive merge (so we don’t wipe unknown keys)
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

// Legacy → Canonical preference migration / IO
const PREF_DEFAULTS = {
  display: { spectrum: false, screen: 'vu', rotate: 0 },
  controls: { buttons_led_service: false, mcp23017_address: '0x20' },
  ir: { enabled: false, profile: '', gpio_bcm: 27 },
  safety: { safe_shutdown: false, clean_mode: false }
};

function coerceHexAddr(a, fb='0x20') {
  if (a === undefined || a === null || a === '') return fb;
  let s = String(a).trim().toLowerCase();
  if (!s.startsWith('0x')) s = '0x' + s;
  return s;
}

async function loadRawPreferenceJSON() {
  const p = await resolvePreferencePath();
  try { return await fs.readJson(p); } catch { return {}; }
}

async function saveCanonicalPreference(pref) {
  const p = await resolvePreferencePath();
  await atomicWriteJSON(p, pref);
}

// Build canonical (nested) object from any input (keeps legacy keys intact)
function buildCanonicalFromAny(raw, hwYaml = {}) {
  const nestedInput = (raw.display && raw.controls) ? raw : {};
  const out = shallowMerge(PREF_DEFAULTS, nestedInput);

  // display
  const legacyScreen = raw.display_screen || raw.display_mode;
  if (legacyScreen) out.display.screen = String(legacyScreen);
  if ('cava_enabled' in raw) out.display.spectrum = !!raw.cava_enabled;
  if ('oled_brightness' in raw) out.display.oled_brightness = parseInt(raw.oled_brightness, 10) || out.display.oled_brightness || 0;

  // YAML overrides for hardware
  if (hwYaml.display_rotate !== undefined) out.display.rotate = parseInt(hwYaml.display_rotate, 10) || 0;

  // controls
  const yamlAddr = hwYaml.mcp23017_address;
  out.controls.mcp23017_address = coerceHexAddr(yamlAddr ?? out.controls.mcp23017_address);

  // IR
  const yamlIrPin = hwYaml.ir_gpio_pin;
  out.ir.gpio_bcm = parseInt(yamlIrPin ?? out.ir.gpio_bcm ?? 27, 10) || 27;

  return out;
}

// Canonical preference getter + mirror into v-conf
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
  vconf.set('enableIR', !!pref.ir.enabled);
  vconf.set('ir_remote_profile', String(pref.ir.profile || ''));
  vconf.set('ir_gpio_pin', parseInt(pref.ir.gpio_bcm, 10) || 27);

  // safety
  vconf.set('safe_shutdown_enabled', !!pref.safety.safe_shutdown);
  vconf.set('clean_mode_enabled', !!pref.safety.clean_mode);
}

// -------- Plugin Controller ----------
function ControllerQuadify(context) {
  this.context = context;
  this.commandRouter = context.coreCommand;
  this.logger = context.logger;
  this.config = new Vconf();
  this.buttonsLedsUnit = null; // resolved at runtime
}

// -------- On Start ----------
ControllerQuadify.prototype.onStart = function () {
  const d = libQ.defer();

  try {
    this.config.loadFile(CONFIG_PATH);
  } catch (e) {}

  this.detectButtonsLedsUnit()
    .then(() => this.applyAllServiceToggles())
    .then(() => {
      this.logger.info('[Quadify] Applied saved toggles on start');
      d.resolve();
    })
    .fail(err => {
      this.logger.error('[Quadify] apply toggles failed: ' + err?.message);
      d.resolve();
    });

  return d.promise;
};

// ------------- Volumio Plugin Methods -------------
ControllerQuadify.prototype.onVolumioStart = function () {
  const defer = libQ.defer();
  (async () => {
    this.logger.info('[Quadify] onVolumioStart – ensure config');

    try {
      fs.ensureDirSync(CONFIG_DIR);
      if (!fs.existsSync(CONFIG_PATH)) {
        fs.writeJsonSync(CONFIG_PATH, {}, { spaces: 2 });
      }
    } catch (e) {
      this.logger.error('[Quadify] ensure config dir/file failed: ' + e.message);
    }

    try {
      this.config.loadFile(CONFIG_PATH);
    } catch (e) {
      this.logger.warn('[Quadify] loadFile failed, starting fresh: ' + e.message);
    }

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
    if (changed) {
      this.logger.info('[Quadify] writing default config.json');
      this.config.save();
    }

    // Preference import/migration BEFORE returning
    const hwCfg = this.loadConfigYaml();
    try {
      const raw = await loadRawPreferenceJSON();
      const canonical = buildCanonicalFromAny(raw, hwCfg);
      const merged = shallowMerge(raw, canonical); // keep legacy keys, add nested
      // Flat mirrors for easy tooling/back-compat
      merged.cava_enabled = !!canonical.display.spectrum;
      merged.display_mode = String(canonical.display.screen || 'vu');
      await saveCanonicalPreference(merged);

      // Mirror canonical → v-conf (sets enableSpectrum AND enableCava)
      applyPreferenceToVconfInstance(this.config, canonical);
      this.config.save();

      // Resolve Buttons/LEDs unit, then make services match
      await this.detectButtonsLedsUnit();
      await this.applyAllServiceToggles();
    } catch (e) {
      this.logger.warn('[Quadify] pref migrate/import on boot: ' + e.message);
    }

    // Optional: also sync v-conf → preference (no-op after above)
    try {
      await this.syncPreferenceToQuadify();
    } catch (e) {
      this.logger.warn('[Quadify] pref sync on start: ' + e.message);
    }

    defer.resolve();
  })();
  return defer.promise;
};

// ----------- UI Config -----------
ControllerQuadify.prototype.getUIConfig = function () {
  const defer = libQ.defer();
  const lang_code = this.commandRouter.sharedVars.get('language_code');
  const stringsPath = path.join(__dirname, 'i18n', 'strings_' + lang_code + '.json');
  const stringsEn   = path.join(__dirname, 'i18n', 'strings_en.json');
  const uiConfig    = path.join(__dirname, 'UIConfig.json');

  const populate = (uiconf) => {
    const hwCfg = this.loadConfigYaml();

    return getCanonicalPreference(hwCfg).then(pref => {
      const set = (sectionId, id, val) => {
        const sec = uiconf.sections.find(s => s.id === sectionId);
        if (!sec) return;
        const row = sec.content.find(c => c.id === id);
        if (row !== undefined) row.value = val;
      };

      // YAML truth for hardware
      set('display_settings', 'display_rotate', String(hwCfg.display_rotate ?? pref.display.rotate ?? '0'));
      set('buttons_leds', 'mcp23017_address', String(hwCfg.mcp23017_address || pref.controls.mcp23017_address || '0x20'));
      set('ir_controller', 'ir_gpio_pin', parseInt(hwCfg.ir_gpio_pin ?? pref.ir.gpio_bcm ?? 27, 10));

      // Preference truth for toggles/choices
      set('display_settings', 'enableSpectrum', !!pref.display.spectrum);
      set('display_settings', 'display_screen', String(pref.display.screen || 'vu'));
      set('buttons_leds', 'enableButtonsLED', !!pref.controls.buttons_led_service);
      set('ir_controller', 'enableIR', !!pref.ir.enabled);
      set('ir_controller', 'ir_remote_profile', String(pref.ir.profile || ''));
      set('safety_controls', 'safe_shutdown_enabled', !!pref.safety.safe_shutdown);
      set('safety_controls', 'clean_mode_enabled', !!pref.safety.clean_mode);

      // Legacy sections (back-compat) – optional
      const flatConfig = getFlatConfig(this.config.get() || {});
      const legacyApply = (sectionId, keys) => {
        const sec = uiconf.sections.find(s => s.id === sectionId);
        if (!sec) return;
        keys.forEach(k => {
          const row = sec.content.find(c => c.id === k);
          if (row && flatConfig[k] !== undefined) row.value = flatConfig[k];
        });
      };
      legacyApply('display_controls', ['display_mode','enableCava','enableButtonsLED']);
      legacyApply('mcp23017_config',  ['mcp23017_address']);

      return uiconf;
    });
  };

  // ✅ Correct order: strings, strings_en, UIConfig
  this.commandRouter.i18nJson(stringsPath, stringsEn, uiConfig)
    .then(uiconf => populate(uiconf))
    .then(uiconf => defer.resolve(uiconf))
    .fail(async (err) => {
      // Safe fallback if i18n files are missing
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

// ------------- UIConfig Save Handler -------------
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
    enableCava: logicValue(data.enableCava !== undefined ? flatten(data.enableCava) : oldConfig.enableCava),
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
    show_date: logicValue(data.show_date !== undefined ? flatten(data.show_date) : oldConfig.show_date),

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
    let a = String(mergedConfig.mcp23017_address).trim().toLowerCase();
    if (!a.startsWith('0x')) a = '0x' + a;
    mergedConfig.mcp23017_address = a;
  }

  Object.keys(mergedConfig).forEach(k => this.config.set(k, mergedConfig[k]));
  this.config.save();

  this.applyAllServiceToggles();

  const hwCfg = this.loadConfigYaml();
  return loadRawPreferenceJSON()
    .then(raw => {
      const canonical = buildCanonicalFromAny(raw, hwCfg);
      // update canonical from current v-conf snapshot
      const desired = buildPreferenceFromVconf(this.config);
      const mergedPref = shallowMerge(raw, shallowMerge(canonical, desired));
      return saveCanonicalPreference(mergedPref).then(() => mergedPref);
    })
    .then(() => {
      this.commandRouter.pushToastMessage('success', 'Quadify', 'Configuration saved');
      return {};
    })
    .catch(err => {
      this.commandRouter.pushToastMessage('error', 'Quadify', 'Saved, but preference sync failed');
      this.logger.error('[Quadify] preference sync failed: ' + err.message);
      return {};
    });
};

// ----------- Service Toggles ------------
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
  const logger = this.logger;
  const sudo = '/usr/bin/sudo';
  const systemctl = '/bin/systemctl'; // absolute path is safer on Volumio
  const unit = `${service}.service`;

  // Wrap pExec calls so verify never explodes even if a command fails
  const safeExec = (cmd) =>
    pExec(cmd, logger).fail(() => ({ stdout: '', stderr: '', cmd, failed: true }));

  const verify = () => {
    return libQ.all([
      safeExec(`${systemctl} is-active ${service}`),
      safeExec(`${systemctl} is-enabled ${service}`)
    ]).then(([a, e]) => {
      const active  = (a.stdout || '').trim() || 'unknown';
      const enabled = (e.stdout || '').trim() || 'unknown';
      logger.info(`[Quadify] VERIFY ${service}: active=${active} enabled=${enabled}`);
      return { active, enabled };
    });
  };

  const enableSeq = [
    `${sudo} ${systemctl} daemon-reload`,
    `${sudo} ${systemctl} enable --now ${unit}`
  ];
  const enableFallback = [
    `${sudo} ${systemctl} start ${unit}`
  ];

  const disableSeq = [
    `${sudo} ${systemctl} disable --now ${unit}`
  ];
  const disableFallback = [
    `${sudo} ${systemctl} stop ${unit}`,
    `${sudo} ${systemctl} disable ${unit}`
  ];

  // IMPORTANT: kew uses .fail, not .catch
  const runSeq = (seq) =>
    seq.reduce(
      (p, cmd) => p.then(() => pExec(cmd, logger).fail(() => libQ.resolve())),
      libQ.resolve()
    );

  const plan = enable ? [enableSeq, enableFallback] : [disableSeq, disableFallback];

  return runSeq(plan[0])
    .then(verify)
    .then(state => {
      const wantActive  = enable ? 'active'   : 'inactive';
      const wantEnabled = enable ? 'enabled'  : 'disabled';
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

// Toggle the resolved Buttons/LEDs unit (if present)
ControllerQuadify.prototype.controlButtonsLeds = function (enable) {
  if (!this.buttonsLedsUnit) {
    this.logger.warn('[Quadify] Skipping Buttons/LEDs toggle: no unit detected');
    return libQ.resolve();
  }
  return this.controlService(this.buttonsLedsUnit, enable);
};

// -------- MCP23017 Config, autodetect, YAML ---------
ControllerQuadify.prototype.updateMcpConfig = function (data) {
  let addr = data.mcp23017_address;
  if (addr && !String(addr).toLowerCase().startsWith('0x')) addr = '0x' + addr;

  const cfg = this.loadConfigYaml();
  cfg.mcp23017_address = addr;
  this.saveConfigYaml(cfg);

  this.config.set('mcp23017_address', addr);
  this.config.save();

  this.commandRouter.pushToastMessage('success', 'Quadify', `MCP23017 address saved: ${addr}`);

  // keep preference.json aligned as well
  const hwCfg = this.loadConfigYaml();
  return loadRawPreferenceJSON()
    .then(raw => {
      const canonical = buildCanonicalFromAny(raw, hwCfg);
      canonical.controls.mcp23017_address = addr;
      const merged = shallowMerge(raw, canonical);
      return saveCanonicalPreference(merged);
    })
    .then(() => ({}))
    .catch(e => { this.logger.warn('[Quadify] pref sync after updateMcpConfig: ' + e.message); return {}; });
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

    // update preference with detected address
    const hwCfg = this.loadConfigYaml();
    loadRawPreferenceJSON()
      .then(raw => {
        const canonical = buildCanonicalFromAny(raw, hwCfg);
        if (foundAddr) canonical.controls.mcp23017_address = foundAddr;
        const merged = shallowMerge(raw, canonical);
        return saveCanonicalPreference(merged);
      })
      .catch(e => this.logger.warn('[Quadify] pref sync after autoDetect: ' + e.message));

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
  try {
    return yaml.load(fs.readFileSync(YAML_PATH, 'utf8')) || {};
  } catch {
    return {};
  }
};

ControllerQuadify.prototype.saveConfigYaml = function (cfg) {
  fs.writeFileSync(YAML_PATH, yaml.dump(cfg), 'utf8');
};

// --------- Misc/Stub handlers --------
ControllerQuadify.prototype.restartQuadify = function () {
  this.logger.info('[Quadify] Restart requested via UI.');
  return libQ.nfcall(exec, 'sudo systemctl restart quadify.service')
    .then(() => {
      this.commandRouter.pushToastMessage('success', 'Quadify', 'Quadify service restarted.');
      return {};
    })
    .fail((err) => {
      this.logger.error('[Quadify] Failed to restart quadify.service: ' + err.message);
      this.commandRouter.pushToastMessage('error', 'Quadify', 'Failed to restart Quadify service.');
      throw err;
    });
};

ControllerQuadify.prototype.updateRotaryConfig = function () {
  return libQ.resolve();
};

// --------- Section SAVE handlers --------
ControllerQuadify.prototype.saveDisplay_settings = function (data) {
  const self = this; // lock context
  const flat = getFlatConfig(data || {});
  const cfg = self.loadConfigYaml();

  // YAML truth for rotate
  if (flat.display_rotate !== undefined) {
    cfg.display_rotate = String(flat.display_rotate);
    self.saveConfigYaml(cfg);
  }

  const hwCfg = self.loadConfigYaml();
  return loadRawPreferenceJSON()
    .then(raw => {
      const pref = buildCanonicalFromAny(raw, hwCfg);

      // Spectrum: prefer enableSpectrum; fall back to legacy enableCava
      let spectrumVal;
      if (flat.enableSpectrum !== undefined) spectrumVal = logicValue(flat.enableSpectrum);
      else if (flat.enableCava !== undefined) spectrumVal = logicValue(flat.enableCava);
      if (spectrumVal !== undefined) pref.display.spectrum = spectrumVal;

      // Screen (guard against typos)
      if (flat.display_screen !== undefined) {
        const scr = String(flat.display_screen);
        const allowed = ['vu','minimal','digitalvuscreen','vuscreen','modern','original'];
        pref.display.screen = allowed.includes(scr) ? scr : 'vu';
      }

      // Write nested + flat mirrors
      const writeObj = shallowMerge(raw, pref);
      writeObj.cava_enabled = !!pref.display.spectrum;
      writeObj.display_mode = String(pref.display.screen || 'vu');

      return saveCanonicalPreference(writeObj).then(() => pref);
    })
    .then(pref => {
      // mirror to v-conf
      applyPreferenceToVconfInstance(self.config, pref);
      self.config.save();

      // INLINE service toggles
      const spectrumOn = !!pref.display.spectrum;
      const buttonsOn  = !!pref.controls.buttons_led_service;
      return libQ.all([
        self.controlService('cava', spectrumOn),
        self.controlButtonsLeds(buttonsOn)
      ]);
    })
    .then(() => {
      self.commandRouter.pushToastMessage('success', 'Quadify', 'Display settings saved');
      return {};
    })
    .catch(err => {
      self.logger.error('[Quadify] saveDisplay_settings: ' + err.message);
      self.commandRouter.pushToastMessage('error', 'Quadify', 'Failed to save display settings');
      return {};
    });
};

ControllerQuadify.prototype.saveIr_controller = function (data) {
  const self = this;
  const flat = getFlatConfig(data || {});
  const cfg = self.loadConfigYaml();

  if (flat.ir_gpio_pin !== undefined) {
    cfg.ir_gpio_pin = parseInt(flat.ir_gpio_pin, 10) || 27;
    self.saveConfigYaml(cfg);
    try { ensureIrOverlayGpio27(); } catch (e) { self.logger.warn('[Quadify] ensureIrOverlay failed: ' + e.message); }
  }

  const hwCfg = self.loadConfigYaml();
  return loadRawPreferenceJSON()
    .then(raw => {
      const pref = buildCanonicalFromAny(raw, hwCfg);
      if (flat.enableIR !== undefined) pref.ir.enabled = logicValue(flat.enableIR);
      if (flat.ir_remote_profile !== undefined) pref.ir.profile = String(flat.ir_remote_profile || '');
      const merged = shallowMerge(raw, pref);
      return saveCanonicalPreference(merged).then(() => pref);
    })
    .then(pref => {
      applyPreferenceToVconfInstance(self.config, pref);
      self.config.save();
      self.commandRouter.pushToastMessage('success', 'Quadify', 'IR settings saved');
      return {};
    })
    .catch(err => { self.logger.error('[Quadify] saveIr_controller: ' + err.message); return {}; });
};

ControllerQuadify.prototype.saveButtons_leds = function (data) {
  const self = this;
  const flat = getFlatConfig(data || {});
  const cfg = self.loadConfigYaml();

  // YAML truth for MCP address
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
      const merged = shallowMerge(raw, pref);
      return saveCanonicalPreference(merged).then(() => pref);
    })
    .then(pref => {
      applyPreferenceToVconfInstance(self.config, pref);
      self.config.save();

      // INLINE service toggles
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
    .catch(err => { self.logger.error('[Quadify] saveButtons_leds: ' + err.message); return {}; });
};

// Detect which systemd unit we should control for Buttons & LEDs.
ControllerQuadify.prototype.detectButtonsLedsUnit = function () {
  const self = this;
  const sudo = '/usr/bin/sudo';
  const systemctl = '/bin/systemctl';

  const tryOne = (name) =>
    pExec(`${sudo} ${systemctl} status ${name}.service`, self.logger)
      .then(() => name)
      .catch(() => null);

  let chain = libQ.resolve(null);
  BUTTONSLEDS_UNIT_CANDIDATES.forEach((name) => {
    chain = chain.then((found) => (found ? found : tryOne(name)));
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

  return libQ.all([
    this.controlService('cava', spectrumOn),
    this.controlButtonsLeds(buttonsOn)
  ]);
};

// ---- Spectrum (CAVA) controls ----
ControllerQuadify.prototype.startCava = function () {
  const self = this;
  return pExec('/usr/bin/sudo /bin/systemctl start cava.service', self.logger)
    .then(() => self.commandRouter.pushToastMessage('success', 'Quadify', 'Spectrum started'))
    .catch((e) => {
      self.logger.error('[Quadify] startCava failed: ' + (e?.err?.message || e));
      self.commandRouter.pushToastMessage('error', 'Quadify', 'Failed to start Spectrum');
    });
};

ControllerQuadify.prototype.stopCava = function () {
  const self = this;
  return pExec('/usr/bin/sudo /bin/systemctl stop cava.service', self.logger)
    .then(() => self.commandRouter.pushToastMessage('success', 'Quadify', 'Spectrum stopped'))
    .catch((e) => {
      self.logger.error('[Quadify] stopCava failed: ' + (e?.err?.message || e));
      self.commandRouter.pushToastMessage('error', 'Quadify', 'Failed to stop Spectrum');
    });
};

ControllerQuadify.prototype.restartCava = function () {
  const self = this;
  return pExec('/usr/bin/sudo /bin/systemctl restart cava.service', self.logger)
    .then(() => self.commandRouter.pushToastMessage('success', 'Quadify', 'Spectrum restarted'))
    .catch((e) => {
      self.logger.error('[Quadify] restartCava failed: ' + (e?.err?.message || e));
      self.commandRouter.pushToastMessage('error', 'Quadify', 'Failed to restart Spectrum');
    });
};


module.exports = ControllerQuadify;
