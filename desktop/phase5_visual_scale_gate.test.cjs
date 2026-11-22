'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const ROOT = path.resolve(__dirname, '..');

test('Phase 5 B06: File inputs eliminate raw visibility risk and use class + hidden under CSP', () => {
  const htmlPath = path.join(ROOT, 'desktop', 'renderer', 'index.html');
  const html = fs.readFileSync(htmlPath, 'utf8');

  // Verify file inputs do not use inline style="display:none"
  const waAttMatch = html.match(/<input[^>]*id="wa-attachment-input"[^>]*>/);
  assert.ok(waAttMatch, 'wa-attachment-input must exist');
  assert.doesNotMatch(waAttMatch[0], /style\s*=\s*["'][^"']*display\s*:\s*none/i, 'wa-attachment-input must not have inline display:none');
  assert.match(waAttMatch[0], /class\s*=\s*["'][^"']*file-input-hidden[^"']*["']/, 'wa-attachment-input must have file-input-hidden class');
  assert.match(waAttMatch[0], /\bhidden\b/, 'wa-attachment-input must have hidden attribute');

  const outAttMatch = html.match(/<input[^>]*id="outbound-attachment-input"[^>]*>/);
  assert.ok(outAttMatch, 'outbound-attachment-input must exist');
  assert.doesNotMatch(outAttMatch[0], /style\s*=\s*["'][^"']*display\s*:\s*none/i, 'outbound-attachment-input must not have inline display:none');
  assert.match(outAttMatch[0], /class\s*=\s*["'][^"']*file-input-hidden[^"']*["']/, 'outbound-attachment-input must have file-input-hidden class');
  assert.match(outAttMatch[0], /\bhidden\b/, 'outbound-attachment-input must have hidden attribute');

  // Verify zero inline style="..." attributes in index.html for strict CSP
  const inlineStyleMatches = html.match(/style\s*=\s*["'][^"']*["']/gi) || [];
  assert.equal(inlineStyleMatches.length, 0, `index.html must have 0 inline styles for strict CSP; found ${inlineStyleMatches.length}`);

  // Verify layout.css defines .file-input-hidden
  const cssPath = path.join(ROOT, 'desktop', 'renderer', 'layout.css');
  const css = fs.readFileSync(cssPath, 'utf8');
  assert.match(css, /\.file-input-hidden\s*\{[^}]*display\s*:\s*none\s*!important/, 'layout.css must define .file-input-hidden with display:none !important');
});

test('Phase 5 B06: WhatsApp composer is auto-growing textarea with Enter submit, Shift+Enter newline, and IME guard', () => {
  const htmlPath = path.join(ROOT, 'desktop', 'renderer', 'index.html');
  const html = fs.readFileSync(htmlPath, 'utf8');

  // Verify wa-composer-input is a textarea with rows="1" and class wa-input
  assert.match(html, /<textarea[^>]*id="wa-composer-input"[^>]*class="[^"]*\bwa-input\b[^"]*"[^>]*rows="1"/, 'wa-composer-input must be a textarea with rows="1" and wa-input class');

  const viewsPath = path.join(ROOT, 'desktop', 'renderer', 'views.js');
  const views = fs.readFileSync(viewsPath, 'utf8');

  // Verify adjustComposerHeight clamps between 40px and 140px
  assert.match(views, /function\s+adjustComposerHeight\s*\(/, 'views.js must define adjustComposerHeight');
  assert.match(views, /Math\.max\(\s*40\s*,\s*Math\.min\(\s*el\.scrollHeight\s*,\s*140\s*\)\s*\)/, 'adjustComposerHeight must clamp scrollHeight between 40 and 140px');

  // Verify IME composition guard and Shift+Enter behavior
  assert.match(views, /e\.isComposing\s*\|\|\s*e\.keyCode\s*===\s*229/, 'views.js keydown listener must guard against IME composition');
  assert.match(views, /if\s*\(!e\.shiftKey\)\s*\{\s*e\.preventDefault\(\);\s*dispatchWaComposerSend\(true\);/s, 'views.js must submit on Enter without Shift and allow newline on Shift+Enter');

  // Verify per-thread drafts retain both text and staged attachments
  assert.match(views, /threadDrafts\.set\(activeWaGroupObj\.convId,\s*\{\s*text:\s*curText,\s*attachment:\s*stagedWaAttachment\s*\}\)/, 'views.js must save both text and attachment in thread drafts on switch');
  assert.match(views, /threadDrafts\.get\(grp\.convId\)/, 'views.js must restore draft on contact switch');
});

test('Phase 5 B10: IPC and Electron main native zoom registration and clamping (0.5 to 2.0)', () => {
  const boundaryPath = path.join(ROOT, 'desktop', 'boundary.cjs');
  const boundary = fs.readFileSync(boundaryPath, 'utf8');
  assert.match(boundary, /METHODS\.add\('window\/zoom'\)/, 'boundary.cjs must permit window/zoom method');

  const preloadPath = path.join(ROOT, 'desktop', 'preload.cjs');
  const preload = fs.readFileSync(preloadPath, 'utf8');
  assert.match(preload, /'window\/zoom'/, 'preload.cjs must include window/zoom in allowed method set');

  const mainPath = path.join(ROOT, 'desktop', 'main.cjs');
  const main = fs.readFileSync(mainPath, 'utf8');
  assert.match(main, /envelope\?\.method\s*===\s*'window\/zoom'/, 'main.cjs must handle window/zoom method');
  assert.match(main, /Math\.max\(0\.5,\s*Math\.min\(2\.0,/, 'main.cjs must clamp zoom factor between 0.5 and 2.0');
  assert.match(main, /setZoomFactor\(factor\)/, 'main.cjs must invoke webContents.setZoomFactor');
});

test('Phase 5 B10: Display & Scaling controls, keyboard shortcuts, and views wiring', () => {
  const htmlPath = path.join(ROOT, 'desktop', 'renderer', 'index.html');
  const html = fs.readFileSync(htmlPath, 'utf8');

  // Verify setting-ui-scale select and options (85, 90, 100, 110, 120)
  assert.match(html, /<select[^>]*id="setting-ui-scale"/, 'index.html must have setting-ui-scale select');
  assert.match(html, /<option\s+value="85"/, 'index.html must offer 85% scale');
  assert.match(html, /<option\s+value="90"/, 'index.html must offer 90% scale');
  assert.match(html, /<option\s+value="100"[^>]*selected/, 'index.html must offer 100% default scale');
  assert.match(html, /<option\s+value="110"/, 'index.html must offer 110% scale');
  assert.match(html, /<option\s+value="120"/, 'index.html must offer 120% scale');
  assert.match(html, /id="btn-reset-scale"/, 'index.html must have btn-reset-scale button');

  const viewsPath = path.join(ROOT, 'desktop', 'renderer', 'views.js');
  const views = fs.readFileSync(viewsPath, 'utf8');

  // Verify applyUiScale implementation and event bindings
  assert.match(views, /function\s+applyUiScale\s*\(/, 'views.js must define applyUiScale');
  assert.match(views, /call\('window\/zoom',\s*\{[^}]*factor[^}]*\}\)/, 'applyUiScale must call window/zoom IPC');
  assert.match(views, /localStorage\.setItem\('jarvis_ui_scale',/, 'applyUiScale must persist scale to localStorage');
  assert.match(views, /\$\('setting-ui-scale'\)\.onchange/, 'views.js must listen to setting-ui-scale change');
  assert.match(views, /\$\('btn-reset-scale'\)\.onclick/, 'views.js must listen to btn-reset-scale click');
  assert.match(views, /e\.key\s*===\s*'0'\s*\|\|\s*e\.code\s*===\s*'Digit0'\s*\|\|\s*e\.code\s*===\s*'Numpad0'/, 'views.js must bind Ctrl+0 reset');
  assert.match(views, /applyUiScale\(100,\s*true\)/, 'Ctrl+0 must reset scale to 100%');
});

test('Phase 5 B07: Stock & demand batch import uses Add to database as primary commit action with add/overwrite modes', () => {
  const htmlPath = path.join(ROOT, 'desktop', 'renderer', 'index.html');
  const html = fs.readFileSync(htmlPath, 'utf8');

  // Primary commit buttons and mode radios
  assert.match(html, /<button[^>]*id="stock-add-database-btn"[^>]*>.*Add to database<\/button>/s, 'index.html must have primary stock-add-database-btn');
  assert.match(html, /<button[^>]*id="bulk-commit"[^>]*>Add to database<\/button>/, 'bulk-commit must be labeled Add to database');
  assert.match(html, /<input[^>]*type="radio"[^>]*name="stock-import-mode"[^>]*value="add"/, 'import mode add must be present');
  assert.match(html, /<input[^>]*type="radio"[^>]*name="stock-import-mode"[^>]*value="overwrite"/, 'import mode overwrite must be present');

  const viewsPath = path.join(ROOT, 'desktop', 'renderer', 'views.js');
  const views = fs.readFileSync(viewsPath, 'utf8');

  // Verify diff badges and modes
  assert.match(views, /diff-badge-update/, 'views.js must define update diff badge');
  assert.match(views, /diff-badge-skip/, 'views.js must define skip diff badge');
  assert.match(views, /diff-badge-new/, 'views.js must define new diff badge');
  assert.match(views, /request_id:\s*`\$\{bulkRequest\.slice\(0,\s*50\)\}-\$\{Math\.floor\(i\s*\/\s*20\)\}`/, 'views.js must use chunked batch request IDs');
  assert.match(views, /Committed \$\{saved\} items to local database · Pending Google Sheets sync/, 'views.js must report truthful local-vs-Sheets status');
});

test('Phase 5 Real Stage Pills & Detail Drawer: Drawer markup, CSS pulse motion, and message/trace invocation', () => {
  const htmlPath = path.join(ROOT, 'desktop', 'renderer', 'index.html');
  const html = fs.readFileSync(htmlPath, 'utf8');

  assert.match(html, /<(?:div|aside)\s+id="message-detail-drawer"[^>]*class="message-detail-drawer"/, 'index.html must have message-detail-drawer');
  assert.match(html, /id="message-detail-overlay"/, 'index.html must have message-detail-overlay element');
  assert.match(html, /class="[^"]*message-detail-overlay[^"]*"/, 'index.html must have message-detail-overlay class');
  assert.match(html, /<button[^>]*id="message-detail-close"/, 'index.html must have message-detail-close');

  const cssPath = path.join(ROOT, 'desktop', 'renderer', 'layout.css');
  const css = fs.readFileSync(cssPath, 'utf8');

  assert.match(css, /@keyframes\s+stagePulse/, 'layout.css must define @keyframes stagePulse');
  assert.match(css, /\.stage-pill\.active\s*\{[^}]*animation:\s*stagePulse/, 'layout.css must apply stagePulse to .stage-pill.active');
  assert.match(css, /@media\s*\(\s*prefers-reduced-motion:\s*reduce\s*\)/, 'layout.css must respect prefers-reduced-motion');

  const viewsPath = path.join(ROOT, 'desktop', 'renderer', 'views.js');
  const views = fs.readFileSync(viewsPath, 'utf8');

  assert.match(views, /function\s+openMessageDetailDrawer\s*\(/, 'views.js must define openMessageDetailDrawer');
  assert.match(views, /call\('message\/trace',\s*\{\s*message_id:\s*messageId\s*\}\)/, 'openMessageDetailDrawer must call message/trace IPC');
  assert.match(views, /⚡ Gemini AI Provider/, 'openMessageDetailDrawer must show Gemini AI provenance badge');
  assert.match(views, /✓ Deterministic Local Regex/, 'openMessageDetailDrawer must show deterministic local regex badge');
});

test('Phase 5 Notification History: Filter bar, CSS status pills, and archive handling', () => {
  const htmlPath = path.join(ROOT, 'desktop', 'renderer', 'index.html');
  const html = fs.readFileSync(htmlPath, 'utf8');

  assert.match(html, /class="notification-filter-bar"/, 'index.html must have notification-filter-bar');
  assert.match(html, /data-alert-filter="ACTIVE"/, 'filter bar must have ACTIVE option');
  assert.match(html, /data-alert-filter="NEW"/, 'filter bar must have NEW unread option');
  assert.match(html, /data-alert-filter="sourcing_need"/, 'filter bar must have sourcing_need option');
  assert.match(html, /data-alert-filter="stock_match"/, 'filter bar must have stock_match option');
  assert.match(html, /data-alert-filter="system_failure"/, 'filter bar must have system_failure option');
  assert.match(html, /data-alert-filter="ALL"/, 'filter bar must have ALL history option');

  const cssPath = path.join(ROOT, 'desktop', 'renderer', 'layout.css');
  const css = fs.readFileSync(cssPath, 'utf8');

  assert.match(css, /\.notification-status-pill\.status-synced/, 'layout.css must define status-synced pill');
  assert.match(css, /\.notification-status-pill\.status-pending/, 'layout.css must define status-pending pill');
  assert.match(css, /\.notification-status-pill\.status-failed/, 'layout.css must define status-failed pill');

  const viewsPath = path.join(ROOT, 'desktop', 'renderer', 'views.js');
  const views = fs.readFileSync(viewsPath, 'utf8');

  assert.match(views, /call\('alerts\/action',\s*\{\s*action:\s*'clear_all'\s*\}\)/, 'clear-alerts must call alerts/action with clear_all');
  assert.match(views, /document\.querySelectorAll\('\.notification-filter-bar button'\)\.forEach/, 'views.js must wire notification-filter-bar button click events');
});
