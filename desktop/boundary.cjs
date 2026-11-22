'use strict';
const path = require('node:path');
const METHODS = new Set(['status','connect','setup/save','start','stop','activity','diagnostic','provider/save','provider/test','provider/discover','dashboard','inventory/save','market/snapshot','market/refresh','market/save','assistant/ask','assistant/history','settings/get','settings/save','alerts/list','alerts/action','sheet/link','sheet/open','live/start','live/audio','live/text','live/stop','inventory/bulk','notifications/test','provider/reveal','profile/sync','sheet/prepare','bridge/open']);
const MAX_REQUEST = 32 * 1024;
const MAX_AUDIO_REQUEST = 2 * 1024 * 1024;
METHODS.add('live/mic-end');
METHODS.add('fx/convert');
METHODS.add('fx/currencies');
METHODS.add('fx/world');
METHODS.add('fx/history');
METHODS.add('bootstrap');
METHODS.add('voice/turn');
METHODS.add('voice/speak');
METHODS.add('voice/voices');
METHODS.add('voice/arm');
METHODS.add('voice/disarm');
METHODS.add('window/title');
METHODS.add('wa/connect');
METHODS.add('wa/scan-mode');
METHODS.add('wa/disconnect');
METHODS.add('wa/status');
METHODS.add('wa/sync');
METHODS.add('outbound/list');
METHODS.add('outbound/action');
METHODS.add('outbound/create');
METHODS.add('outbound/dispatch');
METHODS.add('inventory/daily-stock');
METHODS.add('contacts/list');
METHODS.add('contacts/tag');
METHODS.add('scheduler/list');
METHODS.add('inventory/parse');
METHODS.add('diagnostics/logs');
METHODS.add('diagnostics/clear');
METHODS.add('diagnostics/save-log');
METHODS.add('extension/pair');
METHODS.add('bridge/pair');
METHODS.add('attachment/stage');
METHODS.add('clipboard/read-image');
METHODS.add('message/trace');
METHODS.add('window/zoom');
function validateRequest(method, params) {
  if (!METHODS.has(method) || !params || typeof params !== 'object' || Array.isArray(params)) throw new Error('Unsupported desktop request');
  const encoded = JSON.stringify(params);
  const limit = (method === 'voice/turn' || method === 'inventory/parse') ? MAX_AUDIO_REQUEST : MAX_REQUEST;
  if (Buffer.byteLength(encoded, 'utf8') > limit) throw new Error('Desktop request is too large');
  return {method, params};
}
function assetPath(url, directory) {
  const parsed = new URL(url);
  if (parsed.protocol !== 'app:' || parsed.hostname !== 'jarvis' || parsed.username || parsed.password || parsed.search) return null;
  let name;
  try { name = decodeURIComponent(parsed.pathname); } catch { return null; }
  if (name.includes('\\') || name.includes('\0')) return null;
  const root = path.resolve(directory);
  const target = path.resolve(root, '.' + (name === '/' ? '/index.html' : name));
  return target.startsWith(root + path.sep) ? target : null;
}
module.exports = {METHODS, validateRequest, assetPath};





