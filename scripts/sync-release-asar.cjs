'use strict';

const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const crypto = require('node:crypto');
const asar = require('../desktop/node_modules/@electron/asar/lib/asar.js');

const ROOT = path.resolve(__dirname, '..');

const asarTargets = [
  path.join(ROOT, 'artifacts', 'release-20261121', 'desktop', 'Jarvis Trade-win32-x64'),
  path.join(ROOT, 'release', 'Jarvis-Trade-Production-Package', 'Jarvis Trade')
];

const sourceFiles = [
  'boundary.cjs',
  'main.cjs',
  'preload.cjs',
  'wa-direct.cjs',
  'package.json',
  'renderer/index.html',
  'renderer/layout.css',
  'renderer/views.js',
  'renderer/core.js',
  'renderer/fx-studio.js',
  'renderer/voice-filter.js',
  'renderer/jarvis-hud.js'
];

async function syncAsar() {
  for (const dir of asarTargets) {
    const asarPath = path.join(dir, 'resources', 'app.asar');
    if (!fs.existsSync(asarPath)) {
      console.log(`Skipping non-existent: ${asarPath}`);
      continue;
    }

    console.log(`Processing ASAR at: ${asarPath}`);
    const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), 'asar-sync-'));

    try {
      // 1. Extract existing asar
      asar.extractAll(asarPath, tempDir);

      // 2. Overwrite files from desktop source
      for (const rel of sourceFiles) {
        const srcPath = path.join(ROOT, 'desktop', rel);
        const destPath = path.join(tempDir, rel);
        if (fs.existsSync(srcPath)) {
          fs.mkdirSync(path.dirname(destPath), { recursive: true });
          fs.copyFileSync(srcPath, destPath);
        }
      }

      // 3. Repack asar
      await asar.createPackage(tempDir, asarPath);
      console.log(`Successfully repacked: ${asarPath}`);

      // 4. Update SHA256SUMS.txt
      const manifestPath = path.join(dir, 'SHA256SUMS.txt');
      if (fs.existsSync(manifestPath)) {
        const asarBytes = fs.readFileSync(asarPath);
        const newHash = crypto.createHash('sha256').update(asarBytes).digest('hex');
        const lines = fs.readFileSync(manifestPath, 'utf8').split('\n');
        const updated = lines.map(line => {
          if (line.includes('resources/app.asar') || line.includes('resources\\app.asar')) {
            const parts = line.trim().split(/\s+/);
            return `${newHash}  ${parts[1]}`;
          }
          return line;
        });
        fs.writeFileSync(manifestPath, updated.join('\n'));
        console.log(`Updated SHA256SUMS.txt in ${dir} with hash ${newHash}`);
      }
    } finally {
      fs.rmSync(tempDir, { recursive: true, force: true });
    }
  }
}

syncAsar().then(() => {
  console.log('ASAR sync complete.');
}).catch(err => {
  console.error('ASAR sync failed:', err);
  process.exit(1);
});
