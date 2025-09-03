#!/bin/bash
set -euo pipefail
log(){ logger -t clean-poweroff "$*"; }

# UI-configurable env (written by plugin on save)
ENV_FILE="/etc/quadify/clean-poweroff.env"
[ -f "$ENV_FILE" ] && . "$ENV_FILE"

# Defaults if env isn’t present
SERVICES="${SERVICES:-quadify early_led8 ir_listener cava}"
PRE_HOOK="${PRE_HOOK:-}"
POST_HOOK="${POST_HOOK:-}"
STOP_VOL="${STOP_VOL:-1}"

log "Starting clean poweroff"

# Pre-hook (e.g. persist state to disk)
if [ -n "${PRE_HOOK}" ]; then
  log "Running pre-hook: ${PRE_HOOK}"
  bash -c "${PRE_HOOK}" || true
fi

# Stop user-selected services
for s in $SERVICES; do
  systemctl stop "${s}.service" 2>/dev/null || true
done

# Force LEDs off via MCP23017
/usr/bin/python3 /usr/local/bin/quadify-leds-off.py || true

# Politely stop playback (if enabled)
if [ "${STOP_VOL}" = "1" ]; then
  curl -m 2 -s 'http://localhost:3000/api/v1/commands/?cmd=stop' >/dev/null 2>&1 || true
  sleep 1
fi

# Core audio/Volumio bits (skip if absent)
for s in volspotconnect2 shairport-sync upmpdcli mpd volumio; do
  systemctl stop "$s" 2>/dev/null || true
done

# Unmount NAS/USB quickly to avoid CIFS/NFS hangs
for m in /mnt/NAS/* /mnt/USB/*; do
  [ -e "$m" ] && umount -l "$m" 2>/dev/null || true
done

sync

# Post-hook (last chance before halt)
if [ -n "${POST_HOOK}" ]; then
  log "Running post-hook: ${POST_HOOK}"
  bash -c "${POST_HOOK}" || true
fi

log "Clean poweroff finished"
