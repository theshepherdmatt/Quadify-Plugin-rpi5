#!/bin/sh
# Quadify install script — POSIX /bin/sh compatible

set -eu

cd "$(dirname "$0")"
LOG_FILE="install.log"
: > "$LOG_FILE"  # truncate

log()  { echo "[Quadify Install] $*" | tee -a "$LOG_FILE"; }
warn() { echo "[Quadify Install] WARN: $*" | tee -a "$LOG_FILE"; }

# Absolute install path
PLUGIN_DIR="$(pwd)"

# ----- helpers -----
check_cmd() { command -v "$1" >/dev/null 2>&1; }

run() {
  # echo the command, run it; never hard-fail the installer
  echo "\$ $*" | tee -a "$LOG_FILE"
  if ! "$@"; then
    warn "'$*' failed (continuing)"
    return 0
  fi
}

install_cava_from_fork() {
  log "Installing CAVA from fork into plugin folder..."
  REPO="https://github.com/theshepherdmatt/cava.git"
  BUILD="/tmp/cava_build_$$"
  PREFIX="$PLUGIN_DIR/cava"

  run rm -rf "$BUILD" "$PREFIX"
  log "Installing build deps for CAVA..."
  run apt-get install -y libfftw3-dev libasound2-dev libncursesw5-dev libpulse-dev libtool automake autoconf gcc make pkg-config libiniparser-dev

  run git clone "$REPO" "$BUILD" || return 0

  ( cd "$BUILD" && run autoreconf -fi && run ./configure --prefix="$PREFIX" && run make && run make install ) || true

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

configure_mpd_fifo() {
  log "Configuring MPD FIFO..."
  MPD_TMPL="/volumio/app/plugins/music_service/mpd/mpd.conf.tmpl"
  FIFO_BLOCK='
audio_output {
    type            "fifo"
    name            "my_fifo"
    path            "/tmp/cava.fifo"
    format          "44100:16:2"
}'
  if [ -f "$MPD_TMPL" ]; then
    if grep -q "/tmp/cava.fifo" "$MPD_TMPL"; then
      log "FIFO already present in MPD template"
    else
      # shellcheck disable=SC2016
      run sh -c "printf '%s\n' \"$FIFO_BLOCK\" >> \"$MPD_TMPL\""
      log "Added FIFO block to MPD template"
    fi
    run systemctl restart mpd
  else
    warn "MPD template not found: $MPD_TMPL"
  fi
}

write_unit() {
  # write_unit <name> <Description> <WorkingDir REL or -> <ExecStart CMD or REL>
  NAME="$1"; shift
  DESC="$1"; shift
  WDIR_REL="$1"; shift
  EXEC_CMD="$1"; shift

  UNIT="/etc/systemd/system/$NAME"
  WDIR_LINE=""
  EXEC_LINE=""

  if [ "$WDIR_REL" != "-" ]; then
    WDIR_LINE="WorkingDirectory=${PLUGIN_DIR}/${WDIR_REL}"
  fi

  case "$EXEC_CMD" in
    /*) EXEC_LINE="ExecStart=${EXEC_CMD}" ;;
    *)
      # If it starts with "./", expand to absolute; else run via env
      case "$EXEC_CMD" in
        ./*) EXEC_LINE="ExecStart=${PLUGIN_DIR}/${EXEC_CMD#./}" ;;
        *)   EXEC_LINE="ExecStart=/usr/bin/env ${EXEC_CMD}" ;;
      esac
    ;;
  esac

  # Create unit
  umask 022
  {
    echo "[Unit]"
    echo "Description=${DESC}"
    echo "After=network.target"
    echo
    echo "[Service]"
    echo "Type=simple"
    echo "User=volumio"
    [ -n "$WDIR_LINE" ] && echo "$WDIR_LINE"
    echo "$EXEC_LINE"
    echo "Restart=always"
    echo "RestartSec=5"
    echo
    echo "[Install]"
    echo "WantedBy=multi-user.target"
  } | tee "$UNIT" >/dev/null

  run chmod 644 "$UNIT"
  log "Installed unit: $NAME → $UNIT"
}

rewrite_or_generate_unit_from_template() {
  SVC="$1"
  TEMPLATE="$PLUGIN_DIR/quadifyapp/service/$SVC"
  DST="/etc/systemd/system/$SVC"

  if [ -f "$TEMPLATE" ]; then
    log "Installing $SVC from template (path-corrected)"
    sed -e "s#/data/plugins/music_service/quadify#${PLUGIN_DIR}#g" \
        -e "s#/data/plugins/system_hardware/quadify#${PLUGIN_DIR}#g" \
        "$TEMPLATE" | tee "$DST" >/dev/null
    run chmod 644 "$DST"
    return 0
  fi

  case "$SVC" in
    ir_listener.service)
      write_unit "ir_listener.service" \
        "IR Listener Service for Quadify" \
        "quadifyapp/src/hardware" \
        "python3 ${PLUGIN_DIR}/quadifyapp/src/hardware/ir_listener.py"
    ;;
    early_led8.service)
      write_unit "early_led8.service" \
        "Early LED8 Buttons/LED Service for Quadify" \
        "quadifyapp/src/hardware" \
        "python3 ${PLUGIN_DIR}/quadifyapp/src/hardware/early_led8.py"
    ;;
    cava.service)
      CAVA_BIN="${PLUGIN_DIR}/cava/bin/cava"
      CAVA_CFG="${PLUGIN_DIR}/cava/config/default_config"
      if [ -x "$CAVA_BIN" ]; then
        write_unit "cava.service" "CAVA Visualizer for Quadify" "-" "${CAVA_BIN} -p ${CAVA_CFG}"
      else
        write_unit "cava.service" "CAVA Visualizer (system) for Quadify" "-" "cava -p ${CAVA_CFG}"
      fi
    ;;
    quadify.service)
      warn "No template for quadify.service and no fallback specified; skipping"
      return 0
    ;;
    *)
      warn "Unknown service $SVC; skipping"
      return 0
    ;;
  esac
}

# ----- install flow -----
log "Installing system dependencies..."
run apt-get update
run apt-get install -y python3 python3-pip python3-venv \
  libjpeg-dev zlib1g-dev libfreetype6-dev \
  i2c-tools python3-smbus libgirepository1.0-dev \
  pkg-config libcairo2-dev libffi-dev build-essential \
  libxml2-dev libxslt1-dev libssl-dev lirc lsof \
  python3-gi python3-cairo gir1.2-gtk-3.0 \
  libcairo2 libpango-1.0-0 libgdk-pixbuf-2.0-0

log "Cleaning up Python packaging conflicts..."
run pip3 uninstall -y importlib-metadata setuptools python-socketio socketio socketIO-client >/dev/null 2>&1 || true

log "Upgrading pip and setuptools..."
run python3 -m pip install --upgrade pip setuptools importlib-metadata

log "Installing Python deps from requirements.txt..."
REQ_PATH=""
if [ -f "./quadifyapp/requirements.txt" ]; then
  REQ_PATH="./quadifyapp/requirements.txt"
elif [ -f "./requirements.txt" ]; then
  REQ_PATH="./requirements.txt"
fi
if [ -n "$REQ_PATH" ]; then
  run python3 -m pip install --upgrade --ignore-installed -r "$REQ_PATH"
else
  warn "requirements.txt missing, skipping Python deps"
fi

log "Installing Node.js dependencies..."
if [ -f package.json ]; then
  run npm install --production --silent
  log "Node.js dependencies installed."
else
  warn "package.json not found, skipping npm install."
fi

log "Enabling I2C/SPI overlays..."
CONFIG_FILE="/boot/userconfig.txt"
run touch "$CONFIG_FILE"
grep -qxF 'dtparam=spi=on' "$CONFIG_FILE"  || echo 'dtparam=spi=on'  | tee -a "$CONFIG_FILE" >/dev/null
grep -qxF 'dtparam=i2c_arm=on' "$CONFIG_FILE" || echo 'dtparam=i2c_arm=on' | tee -a "$CONFIG_FILE" >/dev/null
run modprobe i2c-dev || true
run modprobe spi-bcm2835 || true

log "Adding IR overlay (gpio27) to userconfig.txt..."
grep -qxF 'dtoverlay=gpio-ir,gpio_pin=27' "$CONFIG_FILE" || echo 'dtoverlay=gpio-ir,gpio_pin=27' | tee -a "$CONFIG_FILE" >/dev/null

log "Configuring LIRC options for GPIO IR..."
LIRC_DIR="/etc/lirc"
LIRC_OPTIONS="$LIRC_DIR/lirc_options.conf"
run mkdir -p "$LIRC_DIR"
if [ -f "$LIRC_OPTIONS" ]; then
  run sed -i 's|^driver\s*=.*|driver = default|' "$LIRC_OPTIONS"
  run sed -i 's|^device\s*=.*|device = /dev/lirc0|' "$LIRC_OPTIONS"
  log "Updated existing lirc_options.conf"
else
  umask 022
  cat > "$LIRC_OPTIONS" <<EOF
[lircd]
nodaemon        = False
driver          = default
device          = /dev/lirc0
EOF
  log "Created new lirc_options.conf"
fi
run systemctl restart lircd || warn "lircd restart failed (continuing)"

# ----- systemd units -----
log "Removing any old units..."
run systemctl stop ir_listener.service early_led8.service cava.service quadify.service || true
run systemctl disable ir_listener.service early_led8.service cava.service quadify.service || true
run rm -f /etc/systemd/system/ir_listener.service \
          /etc/systemd/system/early_led8.service \
          /etc/systemd/system/cava.service \
          /etc/systemd/system/quadify.service \
          /etc/systemd/system/ir_listener.service.service \
          /etc/systemd/system/early_led8.service.service \
          /etc/systemd/system/cava.service.service || true
run systemctl daemon-reload || true

log "Installing systemd units (path-corrected to $PLUGIN_DIR)..."
for svc in quadify.service ir_listener.service early_led8.service cava.service; do
  rewrite_or_generate_unit_from_template "$svc"
done

run systemctl daemon-reload || true
if [ -f /etc/systemd/system/quadify.service ]; then
  run systemctl enable quadify.service
  run systemctl restart quadify.service || true
  log "quadify.service enabled & restarted"
else
  log "quadify.service not present (skipping enable/start)"
fi
log "IR/LED/CAVA services will be controlled from the plugin UI."

# ----- MPD & CAVA -----
configure_mpd_fifo

log "Installing CAVA (local copy into plugin dir)..."
install_cava_from_fork

# Fallback to system cava if build failed
if [ ! -x "$PLUGIN_DIR/cava/bin/cava" ]; then
  log "Local CAVA missing; installing system package fallback"
  run apt-get update
  run apt-get install -y cava
fi

# ----- perms & sanity -----
log "Setting permissions on plugin folder..."
run chown -R volumio:volumio "$PLUGIN_DIR"
run chmod -R 755 "$PLUGIN_DIR"

log "Packaging sanity check..."
python3 - <<'PY' || true
try:
    import importlib_metadata, setuptools
    print("OK:", hasattr(importlib_metadata, 'version'), setuptools.__version__)
except Exception as e:
    print("WARN: packaging check failed:", e)
PY

log "Install complete."
exit 0
