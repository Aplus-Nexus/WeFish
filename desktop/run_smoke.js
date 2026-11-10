const { spawnSync } = require('child_process');

const python = process.platform === 'win32' ? 'python' : 'python3';
const result = spawnSync(python, ['smoke_package.py'], { stdio: 'inherit' });
if (result.error) {
  console.error(`unable to run ${python}: ${result.error.message}`);
  process.exit(1);
}
process.exit(result.status ?? 1);
