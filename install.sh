#!/bin/sh
# Quadify install script — first-install only (no cleanup), POSIX /bin/sh
set -eu

cd "$(dirname "$0")" || exit 1
LOG_FILE="install.log"; : > "$LOG_FILE"
log()  { echo "[Quadify Install] $*" | tee -a "$LOG_FILE"; }
warn() { echo "[Quadify Install] WARN: $*" | tee -a "$LOG_FILE"; }
run()  { echo "\$ $*" | tee -a "$LOG_FILE"; "$@" || { warn "Command failed: $*"; exit 1; }; }
PLUGIN_DIR="$(pwd)"

# --- APT non-interactive + dpkg heal ---
export DEBIAN_FRONTEND=noninteractive
APT_OPTS='-y -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold'

# If a previous run left dpkg half-configured (like lirc), heal it first
sudo dpkg --configure -a || true
sudo apt-get -f $APT_OPTS install || true

# -----------------------------
# LIRC: force raw /dev/lirc0 + install our remote
# -----------------------------
configure_lirc_default() {
  log "Configuring LIRC (default + /dev/lirc0)…"

  LIRC_OPTIONS="/etc/lirc/lirc_options.conf"

  sudo tee "$LIRC_OPTIONS" >/dev/null <<'EOF'
[lircd]
nodaemon = False
driver   = default
device   = /dev/lirc0
output   = /run/lirc/lircd
EOF

  if [ -f "$PLUGIN_DIR/quadifyapp/lirc/lircd.conf" ]; then
    run install -m 644 "$PLUGIN_DIR/quadifyapp/lirc/lircd.conf" /etc/lirc/lircd.conf
  else
    warn "Missing $PLUGIN_DIR/quadifyapp/lirc/lircd.conf — cannot install remote"
  fi

  run mkdir -p /etc/lirc/lircd.conf.d
  run sh -c 'for f in /etc/lirc/lircd.conf.d/*.conf; do mv "$f" "$f.disabled"; done 2>/dev/null || true'

  run systemctl restart lircd || true
  journalctl -u lircd -n 30 --no-pager | egrep 'Initial device|Options: driver|Using remote|ready' || true

  log "LIRC set to driver=default device=/dev/lirc0 and remote installed."
}

ensure_lirc_symlink() {
  log "Ensuring /home/volumio/lircd.conf → /etc/lirc/lircd.conf"
  run install -d -m 755 /etc/lirc
  # If you shipped a default profile, put it in place once:
  if [ -f "$PLUGIN_DIR/quadifyapp/lirc/lircd.conf" ]; then
    run install -m 644 "$PLUGIN_DIR/quadifyapp/lirc/lircd.conf" /etc/lirc/lircd.conf
  fi
  # Point home file to /etc version (this is what lircd logs as 'Using remote:')
  sudo ln -sf /etc/lirc/lircd.conf /home/volumio/lircd.conf
  sudo chown volumio:volumio /home/volumio/lircd.conf || true
}

write_sudoers() {
  log "Installing sudoers drop-in for Quadify…"
  SUDOERS="/etc/sudoers.d/quadify-lirc"
  # Allow volumio to run these without a password (exactly what index.js uses)
  sudo tee "$SUDOERS" >/dev/null <<'EOF'
volumio ALL=(ALL) NOPASSWD: /bin/systemctl, /usr/bin/systemctl, /bin/mkdir, /usr/bin/mkdir, /bin/cp, /usr/bin/cp, /bin/ln, /usr/bin/ln
EOF
  sudo chmod 0440 "$SUDOERS"
  sudo visudo -cf "$SUDOERS" >/dev/null
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
  (
    cd "$BUILD"
    run autoreconf -fi
    run ./configure --prefix="$PREFIX"
    run make
    run make install
  )

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
# MPD FIFO (append once)
# -----------------------------
configure_mpd_fifo() {
  log "Configuring MPD FIFO…"
  MPD_TMPL="/volumio/app/plugins/music_service/mpd/mpd.conf.tmpl"
  START="# --- QUADIFY_CAVA_FIFO_START ---"
  if [ ! -f "$MPD_TMPL" ]; then warn "MPD template not found: $MPD_TMPL"; return 0; fi
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
  # <svc> <desc> <workdir_rel_or_-> <exec>
  SVC="$1"; DESC="$2"; WORKDIR_REL="$3"; EXEC_CMD="$4"
  TEMPLATE="$PLUGIN_DIR/quadifyapp/service/$SVC"
  [ -f "$TEMPLATE" ] || TEMPLATE="$PLUGIN_DIR/quadifyapp/services/$SVC"
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
    *) EXEC_LINE="/usr/bin/env $EXEC_CMD" ;;
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
# 1) APT: core packages (+ LIRC)
# -----------------------------

if [ -f /etc/lirc/irexec.lircrc ]; then
  sudo cp -a /etc/lirc/irexec.lircrc /etc/lirc/irexec.lircrc.quadify.bak || true
  sudo rm -f /etc/lirc/irexec.lircrc
fi

log "Installing system dependencies…"
run apt-get update
run apt-get $APT_OPTS install \
  python3 python3-pip python3-venv python3-dev \
  i2c-tools python3-smbus \
  lirc lsof \
  libjpeg-dev zlib1g-dev libfreetype6-dev \
  libgirepository1.0-dev libcairo2-dev libffi-dev build-essential \
  libxml2-dev libxslt1-dev libssl-dev \
  python3-gi python3-cairo gir1.2-gtk-3.0 \
  pkg-config \
  libopenjp2-7 libtiff5 liblcms2-dev libwebp-dev

# -----------------------------
# 2) Python deps
# -----------------------------
REQ_PATH=""
[ -f "$PLUGIN_DIR/quadifyapp/requirements.txt" ] && REQ_PATH="$PLUGIN_DIR/quadifyapp/requirements.txt"
[ -z "$REQ_PATH" ] && [ -f "$PLUGIN_DIR/requirements.txt" ] && REQ_PATH="$PLUGIN_DIR/requirements.txt"

log "Upgrading pip/setuptools…"
export PIP_DISABLE_PIP_VERSION_CHECK=1
export PIP_EXTRA_INDEX_URL="https://www.piwheels.org/simple"

run python3 -m pip install --no-cache-dir --upgrade pip setuptools wheel

if [ -n "$REQ_PATH" ]; then
  log "Installing Python requirements from: $REQ_PATH"
  run python3 -m pip install --no-cache-dir --prefer-binary cffi cairocffi
  run python3 -m pip install --no-cache-dir --prefer-binary -r "$REQ_PATH"
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
# 4) I²C/SPI overlays + IR overlay (GPIO 27)
# -----------------------------
log "Enabling I2C/SPI & IR overlays…"

CONFIG_FILE="/boot/userconfig.txt"
run touch "$CONFIG_FILE"

# helpers
ensure_cfg() { grep -qxF "$1" "$CONFIG_FILE" || echo "$1" | sudo tee -a "$CONFIG_FILE" >/dev/null; }

ensure_cfg 'dtparam=spi=on'
ensure_cfg 'dtparam=i2c_arm=on'

# Keep only ONE gpio-ir line; update if it already exists
if grep -q '^dtoverlay=gpio-ir' "$CONFIG_FILE"; then
  run sed -i 's/^dtoverlay=gpio-ir.*/dtoverlay=gpio-ir,gpio_pin=27/' "$CONFIG_FILE"
else
  echo 'dtoverlay=gpio-ir,gpio_pin=27' | sudo tee -a "$CONFIG_FILE" >/dev/null
fi

# Load modules now (overlays still need a reboot to take effect)
run modprobe i2c-dev || true
run modprobe spi-bcm2835 || true

# Auto-pick LIRC driver/device (prefers devinput, falls back to /dev/lirc0)
configure_lirc_default
ensure_lirc_symlink

# -----------------------------
# 5) LIRC post-step (kill irexec, blank lircrc, relax socket perms)
# -----------------------------
log "Configuring LIRC post-step…"
run tee /usr/local/bin/quadify-blank-irexec.sh >/dev/null <<'SH'
#!/usr/bin/env bash
set -euo pipefail
pkill -x irexec 2>/dev/null || true
echo "# Quadify empty" | tee /etc/lirc/irexec.lircrc >/dev/null
[ -S /run/lirc/lircd ] && chmod 666 /run/lirc/lircd || true
SH
run chmod +x /usr/local/bin/quadify-blank-irexec.sh

cat <<'UNIT' | sudo tee /etc/systemd/system/quadify-lirc-post.service >/dev/null
[Unit]
Description=Quadify LIRC post-setup (kill irexec, blank lircrc, chmod socket)
After=lircd.service
Wants=lircd.service

[Service]
Type=oneshot
ExecStart=/usr/local/bin/quadify-blank-irexec.sh
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
UNIT

# Don’t fail if CLI isn’t present
volumio plugin disable ir_controller >/dev/null 2>&1 || true

# ============================
# On/Off SHIM overlays (kernel)
# ============================
configure_onoff_shim_overlays() {
  log "Configuring kernel overlays for On/Off SHIM…"

  # ensure userconfig is included
  if ! grep -q '^include userconfig.txt' /boot/config.txt 2>/dev/null; then
    echo 'include userconfig.txt' | sudo tee -a /boot/config.txt >/dev/null
  fi

  UCFG="/boot/userconfig.txt"
  run touch "$UCFG"

  grep -q '^dtparam=i2c_arm=on' "$UCFG" || echo 'dtparam=i2c_arm=on' | sudo tee -a "$UCFG" >/dev/null
  grep -q '^dtoverlay=gpio-shutdown' "$UCFG" || \
    echo 'dtoverlay=gpio-shutdown,gpio_pin=17,active_low=1,gpio_pull=up' | sudo tee -a "$UCFG" >/dev/null
  grep -q '^dtoverlay=gpio-poweroff' "$UCFG" || \
    echo 'dtoverlay=gpio-poweroff,gpiopin=4,active_low=1' | sudo tee -a "$UCFG" >/dev/null

  log "On/Off SHIM overlays ensured (BCM17 shutdown, BCM4 poweroff). Reboot required to take effect."
}

# =========================================
# Shutdown helpers (copy scripts + units)
# =========================================
install_shutdown_assets() {
  log "Installing LED-off and clean-poweroff assets…"

  # Source files in your repo
  SRC_LED_OFF="$PLUGIN_DIR/quadifyapp/scripts/quadify-leds-off.py"
  SRC_CLEAN_PO="$PLUGIN_DIR/quadifyapp/scripts/clean-poweroff.sh"
  SRC_UNIT_LED="$PLUGIN_DIR/quadifyapp/service/quadify-leds-off.service"
  SRC_UNIT_CPO="$PLUGIN_DIR/quadifyapp/service/volumio-clean-poweroff.service"

  # Destinations on the system
  DST_LED_OFF="/usr/local/bin/quadify-leds-off.py"
  DST_CLEAN_PO="/usr/local/bin/clean-poweroff.sh"
  DST_UNIT_LED="/etc/systemd/system/quadify-leds-off.service"
  DST_UNIT_CPO="/etc/systemd/system/volumio-clean-poweroff.service"

  # Verify sources exist (warn, don’t abort the whole install)
  missing=0
  for f in "$SRC_LED_OFF" "$SRC_CLEAN_PO" "$SRC_UNIT_LED" "$SRC_UNIT_CPO"; do
    if [ ! -f "$f" ]; then
      warn "Missing $f"
      missing=1
    fi
  done

  # Copy what we have; skip missing ones
  [ -f "$SRC_LED_OFF" ] && run install -m 755 "$SRC_LED_OFF" "$DST_LED_OFF"
  [ -f "$SRC_CLEAN_PO" ] && run install -m 755 "$SRC_CLEAN_PO" "$DST_CLEAN_PO"
  [ -f "$SRC_UNIT_LED" ] && run install -m 644 "$SRC_UNIT_LED" "$DST_UNIT_LED"
  [ -f "$SRC_UNIT_CPO" ] && run install -m 644 "$SRC_UNIT_CPO" "$DST_UNIT_CPO"

  # Default env (UI may override later)
  run install -d -m 755 /etc/quadify
  sudo tee /etc/quadify/clean-poweroff.env >/dev/null <<'ENV'
# Written by Quadify (edit if needed)
SERVICES="quadify early_led8 ir_listener cava"
PRE_HOOK=""
POST_HOOK=""
STOP_VOL="1"
ENV

  run systemctl daemon-reload

  # Enable what exists; don’t fail if absent
  [ -f "$DST_UNIT_LED" ] && run systemctl enable quadify-leds-off.service || true
  [ -f "$DST_UNIT_CPO" ] && run systemctl enable volumio-clean-poweroff.service || true

  log "Shutdown helpers installed."
}


# Apply kernel overlays for the On/Off SHIM and install shutdown helpers
configure_onoff_shim_overlays
install_shutdown_assets

# -----------------------------
# 6) systemd services
# -----------------------------
log "Installing systemd services…"

# quadify.service
install_unit_from_template_or_simple \
  "quadify.service" \
  "Main Quadify Service" \
  "quadifyapp" \
  "/usr/bin/python3 $PLUGIN_DIR/quadifyapp/src/main.py"

install_unit_from_template_or_simple \
 "quadify-buttonsleds.service" \
  "Quadify Buttons & LEDs" \
  "-" \
  "/usr/bin/python3 /data/plugins/system_hardware/quadify/quadifyapp/scripts/buttonsleds_daemon.py"

# ir_listener.service (if script exists)
if [ -f "$PLUGIN_DIR/quadifyapp/src/hardware/ir_listener.py" ]; then
  install_unit_from_template_or_simple \
    "ir_listener.service" \
    "Quadify IR Listener (LIRC → ModeManager)" \
    "quadifyapp/src/hardware" \
    "/usr/bin/python3 $PLUGIN_DIR/quadifyapp/src/hardware/ir_listener.py"

  run systemctl daemon-reload
  run systemctl enable --now ir_listener.service || true
fi

# early_led8.service (if script exists)
if [ -f "$PLUGIN_DIR/quadifyapp/scripts/early_led8.py" ]; then
  install_unit_from_template_or_simple \
    "early_led8.service" \
    "Early LED8 Buttons/LED Service for Quadify" \
    "quadifyapp/scripts" \
    "/usr/bin/python3 $PLUGIN_DIR/quadifyapp/scripts/early_led8.py"
fi

# cava.service — install unconditionally (local-build path)
install_unit_from_template_or_simple \
  "cava.service" \
  "CAVA Visualizer for Quadify" \
  "-" \
  "/data/plugins/system_hardware/quadify/cava/bin/cava -p /data/plugins/system_hardware/quadify/cava/config/default_config"

# Enable services
run systemctl daemon-reload
run systemctl enable --now lircd.service || true
run systemctl enable --now quadify-lirc-post.service || true
run systemctl enable --now quadify.service || true
run systemctl disable --now quadify-buttonsleds.service || true
systemctl disable --now buttonsleds.service >/dev/null 2>&1 || true

[ -f /etc/systemd/system/ir-listener.service ] && run systemctl enable --now ir-listener.service || true
[ -f /etc/systemd/system/early_led8.service ] && run systemctl enable early_led8.service || true

write_sudoers

# -----------------------------
# 7) MPD FIFO + CAVA (from fork)
# -----------------------------
install_cava_from_fork
configure_mpd_fifo

# Pin ExecStart to plugin-local cava binary
sudo sed -i "s|^ExecStart=.*|ExecStart=$PLUGIN_DIR/cava/bin/cava -p $PLUGIN_DIR/cava/config/default_config|" \
  /etc/systemd/system/cava.service || true

run systemctl daemon-reload

# Enable only (do NOT start during install)
if [ -x "$PLUGIN_DIR/cava/bin/cava" ]; then
  run systemctl enable cava.service
else
  warn "CAVA binary missing ($PLUGIN_DIR/cava/bin/cava). Leaving service disabled."
  systemctl disable cava.service >/dev/null 2>&1 || true
fi

# -----------------------------
# 8) Permissions
# -----------------------------
log "Setting permissions on plugin folder…"
run chown -R volumio:volumio "$PLUGIN_DIR"
run chmod -R 755 "$PLUGIN_DIR"

# -----------------------------
# 9) Sanity ping
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
