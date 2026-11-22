'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const {validateRequest, METHODS} = require('./boundary.cjs');

test('A03: boundary.cjs must allow extension/pair and bridge/pair methods', () => {
  assert.ok(METHODS.has('extension/pair'), 'METHODS must include extension/pair');
  assert.ok(METHODS.has('bridge/pair'), 'METHODS must include bridge/pair');
  assert.doesNotThrow(() => validateRequest('extension/pair', {}));
  assert.doesNotThrow(() => validateRequest('bridge/pair', {}));
});

test('A17: source desktop/main.cjs must match release packaged ASAR main.cjs', () => {
  const asarCandidates = [
    process.env.JARVIS_RELEASE_ASAR,
    path.resolve(__dirname, '..', 'artifacts', 'release-20261121', 'desktop', 'Jarvis Trade-win32-x64', 'resources', 'app.asar'),
    path.resolve(__dirname, '..', 'release', 'Jarvis-Trade-Production-Package', 'Jarvis Trade', 'resources', 'app.asar'),
  ].filter(Boolean);
  const asarPath = asarCandidates.find(p => fs.existsSync(p));
  if (!asarPath) {
    return;
  }
  const asar = require('./node_modules/@electron/asar/lib/asar.js');
  const asarMain = asar.extractFile(asarPath, 'main.cjs');
  const srcMain = fs.readFileSync(path.resolve(__dirname, 'main.cjs'));
  const asarHash = crypto.createHash('sha256').update(asarMain).digest('hex');
  const srcHash = crypto.createHash('sha256').update(srcMain).digest('hex');
  assert.equal(asarHash, srcHash, 'Release ASAR main.cjs must match source desktop/main.cjs');
});
