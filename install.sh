#!/bin/sh
# Quadify install script — first-install only (no cleanup), POSIX /bin/sh

set -eu

# -----------------------------
# Basics
# -----------------------------
cd "$(dirname "$0")" || exit 1
LOG_FILE="install.log"
: > "$LOG_FILE"

log()  { echo "[Quadify Install] $*" | tee -a "$LOG_FILE"; }
warn() { echo "[Quadify Install] WARN: $*" | tee -a "$LOG_FILE"; }

PLUGIN_DIR="$(pwd)"

run() {
  echo "\$ $*" | tee -a "$LOG_FILE"
  "$@" || { warn "Command failed: $*"; exit 1; }
}

# -----------------------------
# CAVA (optional local build)
# -----------------------------
install_cava_from_fork() {
  log "Installing CAVA (local build)…"
  REPO="https://github.com/theshepherdmatt/cava.git"
  BUILD="/tmp/cava_build_$$"
  PREFIX="$PLUGIN_DIR/cava"

  run rm -rf "$BUILD" "$PREFIX"
  run apt-get install -y \
    git libfftw3-dev libasound2-dev libncursesw5-dev libpulse-dev \
    libtool automake autoconf gcc make pkg-config libiniparser-dev

  run git clone "$REPO" "$BUILD"
  ( cd "$BUILD" && run autoreconf -fi && run ./configure --prefix="$PREFIX" && run make && run make install )

  run mkdir -p "$PREFIX/config"
  if [ -f "$BUILD/config/default_config" ]; then
    run cp "$BUILD/config/default_config" "$PREFIX/config/default_config"
  elif [ -f "$PLUGIN_DIR/cava_default_config" ]; then
    run cp "$PLUGIN_DIR/cava_default_config" "$PREFIX/config/default_config"
  elif [ -f "$PLUGIN_DIR/quadifyapp/cava_default_config" ]; then
    run cp "$PLUGIN_DIR/quadifyapp/cava_default_config" "$PREFIX/config/default_config"
  else
    warn "No default cava config found to copy"
  fi

  run rm -rf "$BUILD"
  log "CAVA installed at $PREFIX"
}

# -----------------------------
# MPD FIFO (template append, idempotent)
# -----------------------------
configure_mpd_fifo() {
  log "Configuring MPD FIFO…"

  MPD_TMPL="/volumio/app/plugins/music_service/mpd/mpd.conf.tmpl"
  START="# --- QUADIFY_CAVA_FIFO_START ---"

  if [ ! -f "$MPD_TMPL" ]; then
    warn "MPD template not found: $MPD_TMPL"
    return 0
  fi

  if ! grep -q "$START" "$MPD_TMPL"; then
    sudo tee -a "$MPD_TMPL" >/dev/null <<'EOF'

# --- QUADIFY_CAVA_FIFO_START ---
audio_output {
    type            "fifo"
    name            "my_fifo"
    path            "/tmp/cava.fifo"
    format          "44100:16:2"
}
# --- QUADIFY_CAVA_FIFO_END ---
EOF
    log "FIFO block appended to MPD template."
  else
    log "FIFO block already present; skipping."
  fi
}

# -----------------------------
# systemd helper
# -----------------------------
install_unit_from_template_or_simple() {
  # install_unit_from_template_or_simple <svc> <desc> <workdir_rel_or_-> <exec>
  SVC="$1"; DESC="$2"; WORKDIR_REL="$3"; EXEC_CMD="$4"
  TEMPLATE="$PLUGIN_DIR/quadifyapp/service/$SVC"
  DST="/etc/systemd/system/$SVC"

  if [ -f "$TEMPLATE" ]; then
    log "Installing $SVC from template"
    run cp "$TEMPLATE" "$DST"
    run chmod 644 "$DST"
    return 0
  fi

  WDIR_LINE=""
  [ "$WORKDIR_REL" != "-" ] && WDIR_LINE="WorkingDirectory=$PLUGIN_DIR/$WORKDIR_REL"

  case "$EXEC_CMD" in
    /*) EXEC_LINE="$EXEC_CMD" ;;
    ./*) EXEC_LINE="$PLUGIN_DIR/${EXEC_CMD#./}" ;;
    *)   EXEC_LINE="/usr/bin/env $EXEC_CMD" ;;
  esac

  umask 022
  cat <<EOF | sudo tee "$DST" >/dev/null
[Unit]
Description=$DESC
After=network.target

[Service]
Type=simple
User=volumio
$WDIR_LINE
ExecStart=$EXEC_LINE
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
EOF
  run chmod 644 "$DST"
}

# -----------------------------
# 1) APT: core packages
# -----------------------------
log "Installing system dependencies…"
run apt-get update
run apt-get install -y \
  python3 python3-pip python3-venv \
  i2c-tools python3-smbus \
  lsof \
  libjpeg-dev zlib1g-dev libfreetype6-dev \
  libgirepository1.0-dev libcairo2-dev libffi-dev build-essential \
  libxml2-dev libxslt1-dev libssl-dev \
  python3-gi python3-cairo gir1.2-gtk-3.0 \
  pkg-config

# -----------------------------
# 2) Python deps
# -----------------------------
REQ_PATH=""
[ -f "$PLUGIN_DIR/quadifyapp/requirements.txt" ] && REQ_PATH="$PLUGIN_DIR/quadifyapp/requirements.txt"
[ -z "$REQ_PATH" ] && [ -f "$PLUGIN_DIR/requirements.txt" ] && REQ_PATH="$PLUGIN_DIR/requirements.txt"

log "Upgrading pip/setuptools…"
run python3 -m pip install --no-cache-dir --upgrade pip setuptools wheel

if [ -n "$REQ_PATH" ]; then
  log "Installing Python requirements from: $REQ_PATH"
  run python3 -m pip install --no-cache-dir --upgrade --ignore-installed -r "$REQ_PATH"
else
  warn "requirements.txt not found; skipping Python bulk install"
fi

# CairoSVG safety net
python3 - <<'PY' || true
try:
    import cairosvg
    print("CairoSVG present:", getattr(cairosvg, "__version__", "?"))
except Exception:
    import sys, subprocess
    print("Installing CairoSVG stack…")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "--no-cache-dir",
                           "cairosvg", "cssselect2", "tinycss2", "defusedxml", "cairocffi"])
PY

# -----------------------------
# 3) Node (optional)
# -----------------------------
if [ -f "$PLUGIN_DIR/package.json" ]; then
  log "Installing Node.js deps (production)…"
  run npm install --production --silent
else
  log "No package.json found; skipping npm install."
fi

# -----------------------------
# 4) Enable I²C/SPI + IR overlay (no LIRC install)
# -----------------------------
log "Enabling I2C/SPI & IR overlays…"
CONFIG_FILE="/boot/userconfig.txt"
run touch "$CONFIG_FILE"

# Keep these if your hardware needs them
grep -qxF 'dtparam=spi=on' "$CONFIG_FILE"  || echo 'dtparam=spi=on'  | sudo tee -a "$CONFIG_FILE" >/dev/null
grep -qxF 'dtparam=i2c_arm=on' "$CONFIG_FILE" || echo 'dtparam=i2c_arm=on' | sudo tee -a "$CONFIG_FILE" >/dev/null

# This is the important one for the Volumio IR plugin (gpio 27)
grep -qxF 'dtoverlay=gpio-ir,gpio_pin=27' "$CONFIG_FILE" || echo 'dtoverlay=gpio-ir,gpio_pin=27' | sudo tee -a "$CONFIG_FILE" >/dev/null

run modprobe i2c-dev || true
run modprobe spi-bcm2835 || true


# -----------------------------
# 5) systemd services
# -----------------------------
log "Installing systemd services…"

# quadify.service
install_unit_from_template_or_simple \
  "quadify.service" \
  "Main Quadify Service" \
  "quadifyapp" \
  "/usr/bin/python3 $PLUGIN_DIR/quadifyapp/src/main.py"

# quadify-ir-listener.service (if script exists)
if [ -f "$PLUGIN_DIR/quadifyapp/src/hardware/ir_listener.py" ]; then
  install_unit_from_template_or_simple \
    "quadify-ir-listener.service" \
    "Quadify IR Listener" \
    "quadifyapp/src/hardware" \
    "/usr/bin/python3 $PLUGIN_DIR/quadifyapp/src/hardware/ir_listener.py"
fi

# early_led8.service (if script exists)
if [ -f "$PLUGIN_DIR/quadifyapp/scripts/early_led8.py" ]; then
  install_unit_from_template_or_simple \
    "early_led8.service" \
    "Early LED8 Buttons/LED Service for Quadify" \
    "quadifyapp/scripts" \
    "/usr/bin/python3 $PLUGIN_DIR/quadifyapp/scripts/early_led8.py"
fi

# cava.service — install unconditionally now (points to local-build path)
install_unit_from_template_or_simple \
  "cava.service" \
  "CAVA Visualizer for Quadify" \
  "-" \
  "/data/plugins/system_hardware/quadify/cava/bin/cava -p /data/plugins/system_hardware/quadify/cava/config/default_config"

run systemctl daemon-reload
run systemctl enable --now quadify.service || true
[ -f /etc/systemd/system/quadify-ir-listener.service ] && run systemctl enable --now quadify-ir-listener.service || true
[ -f /etc/systemd/system/early_led8.service ] && run systemctl enable early_led8.service || true

# -----------------------------
# 6) MPD FIFO + CAVA
# -----------------------------
configure_mpd_fifo

log "Installing CAVA (local build); will fall back to system package if build fails…"
FALLBACK=0
if install_cava_from_fork; then
  log "Local CAVA built."
else
  warn "Local CAVA build failed; installing system cava as fallback."
  FALLBACK=1
  run apt-get update
  run apt-get install -y cava
fi

# If fallback to system cava, point ExecStart to system binary
if [ "$FALLBACK" -eq 1 ]; then
  CAVA_SYS_BIN="$(command -v cava || true)"
  if [ -n "$CAVA_SYS_BIN" ]; then
    sudo sed -i "s|^ExecStart=.*|ExecStart=$CAVA_SYS_BIN -p /data/plugins/system_hardware/quadify/cava/config/default_config|" \
      /etc/systemd/system/cava.service || true
  fi
fi

run systemctl daemon-reload
run systemctl enable --now cava.service || true

# -----------------------------
# 7) Permissions
# -----------------------------
log "Setting permissions on plugin folder…"
run chown -R volumio:volumio "$PLUGIN_DIR"
run chmod -R 755 "$PLUGIN_DIR"

# -----------------------------
# 8) Sanity ping
# -----------------------------
python3 - <<'PY' || true
import importlib
mods = ["RPi.GPIO","smbus2","yaml","cairosvg","PIL","luma.core","luma.oled"]
for m in mods:
    try:
        importlib.import_module(m)
        print(f"{m:12s}: OK")
    except Exception as e:
        print(f"{m:12s}: MISSING ({e.__class__.__name__})")
PY

log "Install complete. Reboot recommended."
exit 0
