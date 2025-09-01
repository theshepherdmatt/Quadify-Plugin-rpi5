#!/usr/bin/env bash
set -euo pipefail
# Kill any irexec, blank the lircrc, and make the lircd socket world-readable.
# This prevents key “double handling” and lets your listener receive events.
pkill -x irexec 2>/dev/null || true
echo "# Quadify empty" > /etc/lirc/irexec.lircrc
[ -S /var/run/lirc/lircd ] && chmod 666 /var/run/lirc/lircd || true
