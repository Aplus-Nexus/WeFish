const { execFileSync } = require('child_process');
const fs = require('fs');
const path = require('path');

const npx = process.platform === 'win32' ? 'npx.cmd' : 'npx';
const requestedTarget = process.argv.find((arg) => arg.startsWith('--target='))?.split('=', 2)[1] || '';

if (requestedTarget && requestedTarget !== (process.platform === 'darwin' ? 'mac' : process.platform === 'win32' ? 'win' : '')) {
  console.error(`Cannot build ${requestedTarget} on ${process.platform}; use the matching host runner.`);
  process.exit(2);
}

function run(args) {
  // Prefer the Electron distribution installed by npm. This keeps local
  // packaging reproducible when the public Electron download endpoint is
  // unavailable; CI still installs the same version before packaging.
  const localElectron = path.join('.', 'node_modules', 'electron', 'dist');
  const configArgs = fs.existsSync(localElectron) ? [`--config.electronDist=${localElectron}`] : [];
  execFileSync(npx, ['electron-builder', ...args, ...configArgs], {
    stdio: 'inherit',
    // Windows exposes npx as a .cmd shim, which must be launched through a shell.
    shell: process.platform === 'win32',
  });
}

function findMacApp() {
  const release = path.join(__dirname, 'release');
  const entries = fs.readdirSync(release, { withFileTypes: true });
  for (const entry of entries) {
    if (!entry.isDirectory() || !entry.name.startsWith('mac')) continue;
    const app = path.join(release, entry.name, '上班墨鱼.app');
    if (fs.existsSync(app)) return app;
  }
  throw new Error('electron-builder did not produce a macOS .app directory');
}

function validateBundledResources(target) {
  const resources = path.join(__dirname, '..', 'dist', 'WeFishService');
  const executable = path.join(resources, target === 'win' ? 'WeFishService.exe' : 'WeFishService');
  if (!fs.existsSync(executable)) {
    throw new Error(
      `Missing ${executable}; build the matching PyInstaller sidecar first with `
      + '`python -m PyInstaller --noconfirm --clean WeFishService.spec`',
    );
  }
  for (const name of ['LICENSE', 'NOTICE']) {
    const file = path.join(__dirname, '..', name);
    if (!fs.existsSync(file)) throw new Error(`Missing required distribution notice: ${file}`);
  }
}

const target = requestedTarget || (process.platform === 'darwin' ? 'mac' : process.platform === 'win32' ? 'win' : '');

if (target === 'mac') {
  validateBundledResources(target);
  run(['--mac', 'dir', '--publish', 'never']);
  const app = findMacApp();
  execFileSync('ditto', [
    '-c', '-k', '--sequesterRsrc', '--keepParent',
    app,
    'release/上班墨鱼-mac.zip',
  ], { stdio: 'inherit' });
} else if (target === 'win') {
  validateBundledResources(target);
  run(['--win', 'dir', '--publish', 'never']);
  run(['--win', 'zip', '--publish', 'never']);
} else {
  console.error(`WeFish desktop packaging requires macOS or Windows (got ${process.platform})`);
  process.exit(2);
}
