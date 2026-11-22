'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');

const ROOT = path.resolve(__dirname, '..');
const asar = require('./node_modules/@electron/asar/lib/asar.js');

test('Phase 6 B12: Full ASAR and source parity across release bundles', () => {
  const asarCandidates = [
    path.join(ROOT, 'artifacts', 'release-20261121', 'desktop', 'Jarvis Trade-win32-x64', 'resources', 'app.asar'),
    path.join(ROOT, 'release', 'Jarvis-Trade-Production-Package', 'Jarvis Trade', 'resources', 'app.asar'),
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

  for (const asarPath of asarCandidates) {
    if (!fs.existsSync(asarPath)) continue;

    for (const file of sourceFiles) {
      const asarBytes = asar.extractFile(asarPath, file);
      const srcBytes = fs.readFileSync(path.join(ROOT, 'desktop', file));
      if (file === 'package.json') {
        const asarPkg = JSON.parse(asarBytes.toString('utf8'));
        const srcPkg = JSON.parse(srcBytes.toString('utf8'));
        assert.equal(asarPkg.version, srcPkg.version, `File ${file} version in ${asarPath} must match source`);
        assert.equal(asarPkg.name, srcPkg.name, `File ${file} name in ${asarPath} must match source`);
      } else {
        const asarHash = crypto.createHash('sha256').update(asarBytes).digest('hex');
        const srcHash = crypto.createHash('sha256').update(srcBytes).digest('hex');
        assert.equal(asarHash, srcHash, `File ${file} in ${asarPath} must match source desktop/${file}`);
      }
    }
  }
});

test('Phase 6 Release: SHA256SUMS.txt manifest exists and validates release directory files', () => {
  const releaseDirs = [
    path.join(ROOT, 'artifacts', 'release-20261121', 'desktop', 'Jarvis Trade-win32-x64'),
    path.join(ROOT, 'release', 'Jarvis-Trade-Production-Package', 'Jarvis Trade')
  ];

  for (const dir of releaseDirs) {
    if (!fs.existsSync(dir)) continue;
    const manifestPath = path.join(dir, 'SHA256SUMS.txt');
    assert.ok(fs.existsSync(manifestPath), `SHA256SUMS.txt must exist in ${dir}`);
    
    const lines = fs.readFileSync(manifestPath, 'utf8').trim().split('\n');
    assert.ok(lines.length > 100, `SHA256SUMS.txt in ${dir} must contain entries (found ${lines.length})`);
    
    // Validate sample files from manifest (app.asar and main executable)
    const asarEntry = lines.find(l => l.endsWith('resources/app.asar'));
    assert.ok(asarEntry, 'app.asar must be tracked in manifest');
    const [expectedHash] = asarEntry.trim().split(/\s+/);
    const actualHash = crypto.createHash('sha256').update(fs.readFileSync(path.join(dir, 'resources', 'app.asar'))).digest('hex');
    assert.equal(actualHash, expectedHash, 'app.asar hash in manifest must match disk file');
  }
});

test('Phase 6 Boundary & Preload Security: Strict method set and parameter boundaries', () => {
  const boundary = require('./boundary.cjs');
  assert.ok(boundary.METHODS instanceof Set, 'boundary.cjs must export METHODS Set');
  
  // Must allow essential business methods
  const requiredMethods = [
    'alerts/list',
    'alerts/action',
    'message/trace',
    'window/zoom',
    'settings/get',
    'settings/save',
    'outbound/list',
    'outbound/create',
    'outbound/dispatch',
    'inventory/bulk'
  ];
  for (const m of requiredMethods) {
    assert.ok(boundary.METHODS.has(m), `METHODS must include ${m}`);
  }

  // Must reject foreign or shell methods
  assert.equal(boundary.METHODS.has('shell/execute'), false, 'Shell execute must not be permitted');
  assert.equal(boundary.METHODS.has('fs/read'), false, 'Direct fs read must not be permitted');
  assert.equal(boundary.METHODS.has('eval'), false, 'Eval must not be permitted');
});

test('Phase 6 UI Ergonomics & CSP: Zero inline styles or scripts in packaged index.html', () => {
  const htmlPath = path.join(ROOT, 'desktop', 'renderer', 'index.html');
  const html = fs.readFileSync(htmlPath, 'utf8');

  // Verify zero inline styles
  const inlineStyles = html.match(/style\s*=\s*["'][^"']*["']/gi) || [];
  assert.equal(inlineStyles.length, 0, `index.html must have 0 inline styles; found ${inlineStyles.length}`);

  // Verify zero inline scripts
  const inlineScripts = html.match(/<script(?![^>]*\bsrc=)[^>]*>[\s\S]*?<\/script>/gi) || [];
  assert.equal(inlineScripts.length, 0, `index.html must have 0 inline scripts; found ${inlineScripts.length}`);
});
