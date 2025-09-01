// uninstall.js for Quadify-Plugin
// This script will be called by Volumio on uninstall

const execSync = require('child_process').execSync;

function run(cmd) {
  try {
    execSync(cmd, { stdio: 'inherit' });
  } catch (e) {
    // Ignore errors so uninstall continues
  }
}

console.log('[Quadify] Uninstall: Stopping systemd service...');
run('systemctl stop quadify.service');
run('systemctl disable quadify.service');
run('rm -f /etc/systemd/system/quadify.service');

console.log('[Quadify] Uninstall: Reloading systemd...');
run('systemctl daemon-reload');

// Optional: Remove plugin data directory
// run('rm -rf /home/volumio/Quadify');

console.log('[Quadify] Uninstall: Complete.');

module.exports = function() {
  // nothing else to do; all uninstall actions are above
};
