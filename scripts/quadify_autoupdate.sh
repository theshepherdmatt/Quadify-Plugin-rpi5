#!/bin/bash
set -euo pipefail
IFS=$'\n\t'

echo "🔄 Starting Quadify update..."

# 1) Stop playback and related services
echo "Stopping Quadify service..."
sudo systemctl stop quadify
sleep 2

# 2) Backup preferences
PREF_SRC="/data/plugins/system_hardware/quadify/quadifyapp/src/preference.json"
PREF_BAK="/tmp/preference.json.bak"
if [ -f "$PREF_SRC" ]; then
    echo "Backing up preference.json..."
    cp "$PREF_SRC" "$PREF_BAK"
fi

# 3) Clone latest beta version
echo "Cloning latest Quadify-Beta..."
rm -rf /home/volumio/Quadify_new
git clone https://github.com/theshepherdmatt/Quadify-Beta.git /home/volumio/Quadify_new

# 4) Replace old code
echo "Replacing old Quadify directory..."
mv /home/volumio/Quadify /home/volumio/Quadify_old 2>/dev/null || true
mv /home/volumio/Quadify_new /home/volumio/Quadify

# 5) Restore preferences
if [ -f "$PREF_BAK" ]; then
    echo "Restoring preference.json..."
    mv "$PREF_BAK" "$PREF_SRC"
    chown volumio:volumio "$PREF_SRC"
    chmod 664 "$PREF_SRC"
fi

# 6) Optional cleanup
echo "Cleaning up backup directory..."
rm -rf /home/volumio/Quadify_old

# 7) Wait for Volumio API to become responsive
echo "Waiting for Volumio API to become responsive..."
for i in {1..15}; do
    if curl -s http://localhost:3000/api/v1/getstate | grep -q \"status\"; then
        echo "Volumio is up!"
        break
    fi
    sleep 1
done

# 8) Restart Quadify
echo "Restarting Quadify..."
sudo systemctl start quadify

echo "✅ Quadify has been updated and restarted successfully."
