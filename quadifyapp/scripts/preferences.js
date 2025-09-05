// /data/plugins/system_hardware/quadify/quadifyapp/scripts/preferences.js
const fs = require('fs');
const path = require('path');

const PREF_PATH = '/data/plugins/system_hardware/quadify/quadifyapp/src/preference.json';
const TMP_PATH  = PREF_PATH + '.tmp';

const DEFAULTS = {
  display: { spectrum: true, screen: 'modern', rotate: 0, oled_brightness: 255 },
  controls: { buttons_led_service: false, mcp23017_address: '0x20' },
  ir: { enabled: false, profile: '', gpio_bcm: 27 },
  safety: { safe_shutdown: false, clean_mode: false },
  // legacy root keys normalised below
  display_mode: undefined,
  clock_font_key: 'clock_sans',
  show_seconds: false,
  show_date: false,
  screensaver_enabled: true,
  screensaver_type: 'geo',
  screensaver_timeout: 3600,
  oled_brightness: 255, // legacy duplication
  cava_enabled: true,
  modern_spectrum_mode: 'bars'
};

function coerceHexAddr(v) {
  if (v == null || v === '') return '0x20';
  let s = String(v).trim().toLowerCase();
  if (!s.startsWith('0x')) {
    const n = parseInt(s, 10);
    if (!Number.isNaN(n)) return '0x' + n.toString(16);
    return '0x20';
  }
  return s;
}

function normalise(rawIn) {
  const raw = rawIn || {};
  const out = JSON.parse(JSON.stringify(DEFAULTS));

  // Deep sections
  out.display = Object.assign({}, out.display, raw.display || {});
  out.controls = Object.assign({}, out.controls, raw.controls || {});
  out.ir = Object.assign({}, out.ir, raw.ir || {});
  out.safety = Object.assign({}, out.safety, raw.safety || {});

  // Legacy root keys
  if (raw.display_mode && !out.display.screen) out.display.screen = raw.display_mode;
  if (typeof raw.oled_brightness === 'number') out.display.oled_brightness = raw.oled_brightness;

  // Flat keys
  out.clock_font_key = raw.clock_font_key ?? out.clock_font_key;
  out.show_seconds = raw.show_seconds ?? out.show_seconds;
  out.show_date = raw.show_date ?? out.show_date;
  out.screensaver_enabled = raw.screensaver_enabled ?? out.screensaver_enabled;
  out.screensaver_type = raw.screensaver_type ?? out.screensaver_type;
  out.screensaver_timeout = raw.screensaver_timeout ?? out.screensaver_timeout;
  out.cava_enabled = raw.cava_enabled ?? out.cava_enabled;
  out.modern_spectrum_mode = raw.modern_spectrum_mode ?? out.modern_spectrum_mode;

  // Coercions
  out.controls.mcp23017_address = coerceHexAddr(out.controls.mcp23017_address);
  out.display.rotate = Number(out.display.rotate) || 0;
  out.display.oled_brightness = Math.max(0, Math.min(255, Number(out.display.oled_brightness) || 255));

  return out;
}

function read() {
  try {
    const txt = fs.readFileSync(PREF_PATH, 'utf8');
    return normalise(JSON.parse(txt));
  } catch {
    return normalise({});
  }
}

function write(obj) {
  const data = JSON.stringify(obj, null, 2);
  fs.writeFileSync(TMP_PATH, data);
  fs.renameSync(TMP_PATH, PREF_PATH); // atomic
}

module.exports = { PREF_PATH, read, write, normalise };
