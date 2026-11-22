const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {METHODS,validateRequest,assetPath} = require('./boundary.cjs');
test('IPC permits only explicit business operations and bounded objects',()=>{
  assert.equal(validateRequest('status',{}).method,'status');
  assert.equal(validateRequest('voice/turn',{audio_wav_b64:'A'.repeat(120000),request_id:'req-voice-turn-0001'}).method,'voice/turn');
  for(const [method,params] of [['exec',{}],['status',[]],['connect',{value:'x'.repeat(33000)}]]) assert.throws(()=>validateRequest(method,params));
});
test('preload allowlist covers every boundary method including fx/convert, bootstrap and voice routes',()=>{
  const preload = fs.readFileSync(path.join(__dirname,'preload.cjs'),'utf8');
  const match = preload.match(/new Set\(\[(.*?)\]\)/);
  assert.ok(match);
  const allowed = new Set(match[1].split(',').map(s=>s.trim().replace(/^['"]|['"]$/g,'')));
  for (const method of METHODS) assert.ok(allowed.has(method), `Missing ${method} in preload.cjs`);
  for (const extra of ['window/theme','bootstrap','fx/currencies','fx/world','fx/history','voice/turn','voice/speak','voice/voices','voice/arm','voice/disarm']) {
    assert.ok(allowed.has(extra), `Missing ${extra}`);
  }
});
test('custom protocol cannot expose application code or foreign files',()=>{
  const root=path.resolve('renderer');
  assert.equal(assetPath('app://jarvis/index.html',root),path.join(root,'index.html'));
  for(const url of ['https://jarvis/index.html','app://evil/index.html','app://jarvis/%2e%2e%2fmain.cjs','app://jarvis/%5c..%5cmain.cjs','app://user@jarvis/index.html']) assert.equal(assetPath(url,root),null);
});
test('renderer index loads modular assets cleanly under CSP with zero inline scripts or styles',()=>{
  const html = fs.readFileSync(path.join(__dirname,'renderer','index.html'),'utf8');
  const refs = [...html.matchAll(/(?:src|href)="([^"]+)"/g)].map(m=>m[1]);
  assert.ok(refs.includes('theme.css') && refs.includes('core.js') && refs.includes('fx-studio.js') && refs.includes('views.js') && refs.includes('voice-filter.js') && refs.includes('jarvis-hud.js'));
  for (const ref of refs) {
    assert.ok(!ref.startsWith('http'), `External ref not permitted: ${ref}`);
    assert.ok(fs.existsSync(path.join(__dirname,'renderer',ref)), `Missing renderer asset: ${ref}`);
  }
  assert.equal(/<script(?![^>]*\bsrc=)[^>]*>/i.test(html), false);
  assert.equal(/<style\b/i.test(html), false);
});
