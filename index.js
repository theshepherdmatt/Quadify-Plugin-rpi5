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

const USERCONFIG_TXT = '/boot/userconfig.txt';
const IR_OVERLAY_LINE = 'dtoverlay=gpio-ir,gpio_pin=27';

const CONFIG_DIR = '/data/configuration/system_hardware/quadify';
const CONFIG_PATH = CONFIG_DIR + '/config.json';

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

// --- IR overlay helper (kept, required by Volumio IR plugin) ---
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

// -------- Plugin Controller ----------
function ControllerQuadify(context) {
  this.context = context;
  this.commandRouter = context.coreCommand;
  this.logger = context.logger;
  this.config = new Vconf();
}

// -------- On Start ----------
ControllerQuadify.prototype.onStart = function () {
  const d = libQ.defer();

  try {
    this.config.loadFile(CONFIG_PATH);
  } catch (e) {}

  this.applyAllServiceToggles()
    .then(() => {
      this.logger.info('[Quadify] Applied saved toggles on start');
      d.resolve();
    })
    .fail(err => {
      this.logger.error('[Quadify] apply toggles failed: ' + err.message);
      d.resolve();
    });

  return d.promise;
};

// ------------- Volumio Plugin Methods -------------
ControllerQuadify.prototype.onVolumioStart = function () {
  const defer = libQ.defer();
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
    oled_brightness: 200
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

  defer.resolve();
  return defer.promise;
};

// ----------- UI Config -----------
ControllerQuadify.prototype.getUIConfig = function () {
  const defer = libQ.defer();
  const flatConfig = getFlatConfig(this.config.get() || {});
  const lang_code = this.commandRouter.sharedVars.get('language_code');

  this.commandRouter.i18nJson(
    path.join(__dirname, 'i18n/strings_' + lang_code + '.json'),
    path.join(__dirname, 'i18n/strings_en.json'),
    path.join(__dirname, 'UIConfig.json')
  ).then(uiconf => {
    const displaySection = uiconf.sections.find(s => s.id === 'display_controls');
    if (displaySection) {
      displaySection.content.forEach(row => {
        if (row.id in flatConfig) row.value = flatConfig[row.id];
      });
    }

    const mcpSection = uiconf.sections.find(s => s.id === 'mcp23017_config');
    if (mcpSection) {
      mcpSection.content.forEach(row => {
        if (row.id in flatConfig) row.value = flatConfig[row.id];
      });
    }

    defer.resolve(uiconf);
  }).fail(err => defer.reject(err));

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

  this.commandRouter.pushToastMessage('success', 'Quadify', 'Configuration saved');
  return Promise.resolve({});
};

// ----------- Service Toggles ------------
ControllerQuadify.prototype.controlService = function (service, enable) {
  const action = enable ? 'start' : 'stop';
  const cmd = `sudo systemctl ${action} ${service}.service`;
  return libQ.nfcall(exec, cmd)
    .then(() => this.logger.info(`[Quadify] ${service}.service ${action}ed`))
    .fail(err => this.logger.error(`[Quadify] ${service}.service failed to ${action}: ${err.message}`));
};

ControllerQuadify.prototype.applyAllServiceToggles = function () {
  const flatConfig = getFlatConfig(this.config.get() || {});
  return libQ.all([
    this.controlService('cava', logicValue(flatConfig.enableCava)),
    this.controlService('early_led8', logicValue(flatConfig.enableButtonsLED))
  ]);
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
  return libQ.resolve();
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

module.exports = ControllerQuadify;
