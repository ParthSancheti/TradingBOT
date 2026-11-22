'use strict';

const fs = require('node:fs');
const path = require('node:path');

function syncAndPatchWaJs() {
  const root = path.resolve(__dirname, '..', '..');
  const src = path.join(root, 'desktop', 'node_modules', '@wppconnect', 'wa-js', 'dist', 'wppconnect-wa.js');
  const dst = path.join(root, 'extension', 'vendor', 'wppconnect-wa.js');
  if (!fs.existsSync(src)) {
    if (fs.existsSync(dst)) return { ok: true, patched: false, reason: 'dist_missing_kept_existing' };
    throw new Error('Missing @wppconnect/wa-js dist bundle.');
  }

  let code = fs.readFileSync(src, 'utf8');

  // 1. Add drifted WAWeb 2026 LID migration utilities to IGNORE_FAIL_MODULES so deprecated
  //    optional internal modules never raise uncaught extension console errors.
  code = code.replace(
    'IGNORE_FAIL_MODULES=new Set([',
    'IGNORE_FAIL_MODULES=new Set(["isLidMigrated","Lid1X1MigrationUtils","isSyncdSessionMigrated",'
  );

  // 2. Guard boot-time wrapModuleFunction calls when optional internal modules are absent.
  code = code.replace(
    '(0,c.wrapModuleFunction)(l.isLidMigrated,',
    '"function"==typeof l.isLidMigrated&&(0,c.wrapModuleFunction)(l.isLidMigrated,'
  );
  code = code.replace(
    'e=>e.Lid1X1MigrationUtils.isLidMigrated',
    'e=>e.Lid1X1MigrationUtils&&"function"==typeof e.Lid1X1MigrationUtils.isLidMigrated'
  );

  // 3. Downgrade missing-module diagnostics in Chrome MV3 MAIN world from console.error
  //    to console.warn (and drop telemetry exception reports) so WhatsApp Web rollouts
  //    do not surface noisy Chrome extension error badges.
  code = code.replace(
    'console.error(e),(0,l.trackException)(e)',
    'console.warn(e)'
  );
  code = code.replaceAll(
    'return console.error(e),(0,l.trackException)(e),void Object.defineProperty',
    'return console.warn(e),void Object.defineProperty'
  );
  code = code.replace(
    'if("function"!=typeof e)return void console.error("func is not a function")',
    'if("function"!=typeof e)return'
  );
  code = code.replace(
    'if(!n)return void console.error("func is not an exported function")',
    'if(!n)return'
  );
  code = code.replace(
    'if(!i)return void console.error("function path was not found")',
    'if(!i)return'
  );
  code = code.replace(
    'return console.error(e),void(0,l.trackException)(e)',
    'return console.warn(e)'
  );
  code = code.replace(
    'if(!e)return void console.error("WAWebChatSearchQuery module was not found")',
    'if(!e)return void console.warn("WAWebChatSearchQuery module was not found")'
  );
  code = code.replace(
    'void console.error("WA-JS: Stream events could not be registered after 60s")',
    'void console.warn("WA-JS: Stream events deferred")'
  );

  fs.mkdirSync(path.dirname(dst), { recursive: true });
  fs.writeFileSync(dst, code, 'utf8');
  return { ok: true, patched: true, bytes: Buffer.byteLength(code, 'utf8') };
}

if (require.main === module) {
  const res = syncAndPatchWaJs();
  console.log('Synced & patched wppconnect-wa.js:', JSON.stringify(res));
}

module.exports = { syncAndPatchWaJs };
