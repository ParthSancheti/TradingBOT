'use strict';
const {app, BrowserWindow, ipcMain, protocol, session, Notification, shell, Tray, Menu, nativeImage, dialog, clipboard} = require('electron');
const fs = require('node:fs/promises');
const {existsSync} = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const {spawn} = require('node:child_process');
const {validateRequest, assetPath} = require('./boundary.cjs');
const {WaDirectClient} = require('./wa-direct.cjs');
const ORIGIN = 'app://jarvis';
const PAGE = ORIGIN + '/index.html';
const CSP = "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; font-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'; object-src 'none'";
protocol.registerSchemesAsPrivileged([{scheme:'app', privileges:{standard:true, secure:true, supportFetchAPI:true}}]);
app.enableSandbox();
let window, tray = null, engine, starting, quitting = false, nextId = 0, buffer = '', livePermission=false, currentAgentName='Jarvis', workspaceRunning=false, trayNotified=false, cachedAutonomyMode=null;
const shownNotifications=new Set();
app.setAppUserModelId('com.jarvis.trade.desktop');
const pending = new Map();

function sanitizeAgentName(raw){
  const cleaned = String(raw || 'Jarvis').replace(/[<>{}\r\n\t]+/g, ' ').trim();
  return (cleaned || 'Jarvis').slice(0, 40);
}

let directPumpBusy = false;
async function triggerDirectPump() {
  if (directPumpBusy || !waClient || waClient.state !== 'connected' || !engine || engine.exitCode !== null) return;
  directPumpBusy = true;
  try {
    const listRes = await request('outbound/list', { statuses: ['READY'], limit: 10 }).catch(() => null);
    const items = listRes?.result?.items || listRes?.result || [];
    if (!Array.isArray(items) || !items.length) return;
    const accountPhone = waClient.profile?.phone;
    if (!accountPhone) return;
    const sessionId = `direct-${Date.now()}`;

    for (const item of items) {
      if (item.status !== 'READY') continue;
      const oid = item.id;
      const claimRes = await request('outbound/dispatch', {
        operation: 'dispatch_claim',
        outbound_id: oid,
        session_id: sessionId,
        account_phone: accountPhone,
      }).catch(() => null);

      const claimData = claimRes?.result;
      if (!claimData?.item) continue;

      const claimToken = claimData.item.claim_token;
      const targetJid = claimData.item.recipient_jid;
      const textParts = claimData.item.text_parts;
      const attachmentPath = claimData.item.attachment_path;
      const caption = claimData.item.caption;
      const meta = claimData.item.meta || {};

      const authRes = await request('outbound/dispatch', {
        operation: 'dispatch_authorize',
        outbound_id: oid,
        session_id: sessionId,
        claim_token: claimToken,
        account_phone: accountPhone,
      }).catch(() => null);

      if (!authRes?.result?.ok) {
        await request('outbound/dispatch', {
          operation: 'dispatch_result',
          outbound_id: oid,
          session_id: sessionId,
          claim_token: claimToken,
          outcome: 'FAILED_BEFORE_SEND',
          reason: 'AUTHORIZATION_DENIED',
        }).catch(() => {});
        continue;
      }

      const expiresAt = authRes.result.expires_at;
      if (expiresAt && Date.now() >= expiresAt * 1000) {
        await request('outbound/dispatch', {
          operation: 'dispatch_result',
          outbound_id: oid,
          session_id: sessionId,
          claim_token: claimToken,
          outcome: 'FAILED_BEFORE_SEND',
          reason: 'DISPATCH_AUTHORIZATION_EXPIRED',
        }).catch(() => {});
        continue;
      }

      let attachmentObj = null;
      if (attachmentPath && existsSync(attachmentPath)) {
        try {
          const fileBuf = await fs.readFile(attachmentPath);
          attachmentObj = {
            buffer: fileBuf,
            name: meta.attachment_name || path.basename(attachmentPath),
            mime: meta.attachment_mime || 'application/octet-stream',
            caption: caption || '',
            type: meta.attachment_type || 'document',
          };
        } catch {}
      }

      let sendRes;
      try {
        sendRes = await waClient.sendDirect(targetJid, textParts, attachmentObj);
      } catch (err) {
        sendRes = { ok: false, ambiguous: true, reason: String(err?.message || err) };
      }

      if (sendRes?.ok && Array.isArray(sendRes.sent_ids) && sendRes.sent_ids.length > 0) {
        await request('outbound/dispatch', {
          operation: 'dispatch_result',
          outbound_id: oid,
          session_id: sessionId,
          claim_token: claimToken,
          outcome: 'ACCEPTED',
          sent_ids: sendRes.sent_ids,
        }).catch(() => {});
      } else if (sendRes?.ambiguous) {
        await request('outbound/dispatch', {
          operation: 'dispatch_result',
          outbound_id: oid,
          session_id: sessionId,
          claim_token: claimToken,
          outcome: 'UNKNOWN',
          reason: sendRes?.reason || 'SEND_AMBIGUOUS_AFTER_INVOCATION',
        }).catch(() => {});
      } else {
        await request('outbound/dispatch', {
          operation: 'dispatch_result',
          outbound_id: oid,
          session_id: sessionId,
          claim_token: claimToken,
          outcome: 'FAILED_BEFORE_SEND',
          reason: sendRes?.reason || 'SEND_DIRECT_FAILED',
        }).catch(() => {});
      }
    }
  } catch {} finally {
    directPumpBusy = false;
  }
}

let directEventQueue = [];
let directIntakeBusy = false;

async function enqueueDirectEvents(events) {
  for (const ev of events) {
    if (directEventQueue.length < 500) {
      directEventQueue.push({ event: ev, retries: 0 });
    }
  }
  drainDirectEventQueue();
}

async function drainDirectEventQueue() {
  if (directIntakeBusy || !directEventQueue.length || !engine || engine.exitCode !== null || engine.killed) return;
  directIntakeBusy = true;
  try {
    while (directEventQueue.length > 0) {
      const batch = [];
      let batchBytes = 0;
      const MAX_BATCH_BYTES = 20 * 1024;
      while (directEventQueue.length > 0 && batch.length < 15) {
        const item = directEventQueue[0];
        const itemBytes = Buffer.byteLength(JSON.stringify(item.event), 'utf8');
        if (batch.length > 0 && (batchBytes + itemBytes > MAX_BATCH_BYTES)) break;
        batch.push(directEventQueue.shift());
        batchBytes += itemBytes;
      }
      if (!batch.length) break;

      try {
        const res = await request('wa/sync', { events: batch.map(b => b.event) });
        const syncRes = res?.result || res;
        if (typeof syncRes?.scan_since_ts === 'number') waClient.scanSinceTs = syncRes.scan_since_ts;
        if (Array.isArray(syncRes?.results)) {
          for (let i = 0; i < syncRes.results.length; i++) {
            const r = syncRes.results[i];
            if (r?.status === 'RETRY_REQUIRED' && batch[i]?.retries < 5) {
              batch[i].retries++;
              directEventQueue.push(batch[i]);
            }
          }
        }
      } catch (err) {
        for (const item of batch) {
          if (item.retries < 5) {
            item.retries++;
            directEventQueue.unshift(item);
          }
        }
        await new Promise(r => setTimeout(r, 1000));
        break;
      }
    }
  } finally {
    directIntakeBusy = false;
  }
}

const waClient = new WaDirectClient({
  onState: (st) => {
    if (window && !window.isDestroyed()) {
      window.webContents.send('jarvis:event', {event: 'wa-direct', data: st});
    }
    if (engine && engine.exitCode === null && !engine.killed) {
      request('wa/sync', {
        status: {state: st.state, connected: st.connected, has_session: st.has_session, error: st.error},
        ...(st.profile ? {profile: st.profile} : {}),
      }).then((res) => {
        const syncRes = res?.result || res;
        if (typeof syncRes?.scan_since_ts === 'number') waClient.scanSinceTs = syncRes.scan_since_ts;
        if (st.connected) triggerDirectPump();
      }).catch(() => {});
    }
  },
  onMessages: async (events) => {
    if (engine && engine.exitCode === null && !engine.killed && Array.isArray(events) && events.length) {
      await enqueueDirectEvents(events);
    }
  },
});
function serviceState(state, message) {
  if (window && !window.isDestroyed()) window.webContents.send('jarvis:service-state', {state, ...(message ? {message} : {})});
}
function failPending(message) {
  for (const entry of pending.values()) { clearTimeout(entry.timer); entry.reject(new Error(message)); }
  pending.clear();
}
async function request(method, params) {
  validateRequest(method,params);
  if (pending.size >= 12) throw new Error('Another operation is still running. Please wait.');
  await startEngine();
  const id = ++nextId;
  return new Promise((resolve,reject) => {
    const timer = setTimeout(() => { pending.delete(id); reject(new Error('The operation took too long. Check Connections and retry.')); },120000);
    pending.set(id,{resolve,reject,timer});
    engine.stdin.write(JSON.stringify({id,method,params})+'\n');
  });
}
function startEngine() {
  if (engine && engine.exitCode === null && !engine.killed) return Promise.resolve();
  if (starting) return starting;
  starting = new Promise((resolve, reject) => {
    let executable, args, cwd;
    if (app.isPackaged) {
      cwd = process.resourcesPath;
      executable = path.join(cwd, 'jarvis-desktop-service', process.platform === 'win32' ? 'jarvis-desktop-service.exe' : 'jarvis-desktop-service');
      args = [];
    } else {
      cwd = path.resolve(__dirname, '..');
      const rootVenvPython = path.join(cwd, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
      const fallbackVenvPython = path.join('D:\\Projects\\TradingBOT', '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
      if (existsSync(rootVenvPython)) {
        executable = rootVenvPython;
      } else if (existsSync(fallbackVenvPython)) {
        cwd = 'D:\\Projects\\TradingBOT';
        executable = fallbackVenvPython;
      } else {
        executable = rootVenvPython;
      }
      args = ['-u', '-m', 'jarvis.desktop_service'];
    }
    if (!existsSync(executable)) { reject(new Error('The bundled engine is missing. Repair or reinstall Jarvis Trade.')); return; }
    serviceState('starting');
    const child = spawn(executable, args, {cwd, windowsHide:true, stdio:['pipe','pipe','pipe'],
      env:{...process.env, PYTHONPATH: app.isPackaged ? '' : path.join(cwd,'backend'), PYTHONUNBUFFERED:'1', PYTHONIOENCODING:'utf-8', PYDANTIC_DISABLE_PLUGINS:'__all__'}});
    engine = child; buffer = '';
    child.once('spawn', () => { serviceState('ready'); resolve(); });
    child.once('error', () => { serviceState('failed','The local engine could not start.'); reject(new Error('The local engine could not start.')); });
    child.stdout.setEncoding('utf8');
    child.stdout.on('data', chunk => {
      buffer += chunk;
      let newline;
      while ((newline = buffer.indexOf('\n')) !== -1) {
        const line = buffer.slice(0, newline);
        buffer = buffer.slice(newline + 1);
        if (!line.trim()) continue;
        let response;
        try { response = JSON.parse(line); } catch { failPending('The engine returned an invalid response.'); child.kill(); return; }
        if(response.event){
          if(response.event==='refresh'){serviceState('ready');continue;}
          if(response.event==='data-updated'&&Array.isArray(response.data?.collections)){
            if (response.data.collections.includes('wa-reset')) {
              waClient.disconnect({wipe:true}).catch(()=>{});
            }
            if (response.data.collections.includes('outbound')) {
              triggerDirectPump();
            }
          }
          if(response.event==='bridge-status' && typeof response.data?.running==='boolean'){
            workspaceRunning = response.data.running;
            refreshTrayMenu();
          }
          if(['notification','live','voice','assistant','data-updated','ui-action','wa-direct','profile-updated','bridge-status'].includes(response.event)&&window&&!window.isDestroyed())window.webContents.send('jarvis:event',{event:response.event,data:response.data});
          if(response.event==='live'&&response.data?.type==='closed')livePermission=false;
          if(response.event==='notification'&&Notification.isSupported()&&!shownNotifications.has(response.data.id)){
            const n=new Notification({title:String(response.data.title).slice(0,120),body:String(response.data.body).slice(0,300),icon:path.join(__dirname,'renderer','icon.png')});
            n.on('show',()=>{
              try {
                shownNotifications.add(response.data.id);
                if (typeof request === 'function') {
                  request('alerts/action',{id:response.data.id,action:'ack'}).catch(()=>{});
                }
              } catch {}
            });
            n.on('click',()=>{
              try {
                if (window && !window.isDestroyed()) {
                  window.show();
                  window.focus();
                  window.webContents.send('jarvis:event',{event:'notification-click',data:response.data});
                }
              } catch {}
            });
            n.show();
            n.on('failed',()=>window?.webContents.send('jarvis:event',{event:'notification-error',data:{message:'Windows could not show a notification. Alerts remain in your dashboard inbox.'}}));
          }
          continue;
        }
        const pendingEntry = pending.get(response.id);
        if (!pendingEntry) continue;
        clearTimeout(pendingEntry.timer); pending.delete(response.id);
        pendingEntry.resolve(response.error ? {error:response.error} : {result:response.result});
      }
    });
    // Never persist backend stderr: provider errors can contain account data.
    child.stderr.on('data', () => {});
    child.stdin.on('error', () => failPending('The local engine connection closed.'));
    child.once('exit', () => {
      livePermission=false;
      if (engine === child) engine = null;
      failPending('The local engine stopped. Retry the action to reconnect.');
      serviceState(quitting ? 'stopped' : 'failed','The local engine stopped. Retry to reconnect.');
    });
  }).finally(() => { starting = null; });
  return starting;
}
function showMainWindow() {
  if (!window || window.isDestroyed()) createWindow();
  if (window.isMinimized()) window.restore();
  try { window.setSkipTaskbar(false); } catch {}
  window.show();
  try {
    window.moveTop();
    window.setAlwaysOnTop(true);
    window.setAlwaysOnTop(false);
    app.focus({ steal: true });
  } catch {}
  window.focus();
}

function refreshTrayMenu() {
  if (!tray) return;
  try {
    tray.setToolTip(`${currentAgentName} Trade · ${workspaceRunning ? 'Running 24x7 in background' : 'Stopped (background ready)'}`);
    tray.setContextMenu(Menu.buildFromTemplate([
      { label: `Open ${currentAgentName} Trade`, click: showMainWindow },
      { label: workspaceRunning ? 'Workspace: Running 24x7' : 'Workspace: Stopped', enabled: false },
      { type: 'separator' },
      {
        label: workspaceRunning ? 'Stop Workspace' : 'Start 24x7 Workspace',
        click: () => {
          request(workspaceRunning ? 'stop' : 'start', {}).then((res) => {
            if (typeof res?.result?.running === 'boolean') {
              workspaceRunning = res.result.running;
              refreshTrayMenu();
              if (window && !window.isDestroyed()) window.webContents.send('jarvis:event', { event: 'bridge-status', data: { ...res.result.bridge, running: workspaceRunning } });
            }
          }).catch(() => showMainWindow());
        },
      },
      { type: 'separator' },
      { label: `Quit ${currentAgentName} Completely`, click: () => { app.quit(); } },
    ]));
  } catch {}
}

function createTray() {
  if (tray || process.argv.includes('--capture-desktop')) return;
  try {
    const iconImg = nativeImage.createFromPath(path.join(__dirname, 'renderer', 'icon.png')).resize({ width: 16, height: 16 });
    tray = new Tray(iconImg);
    tray.on('click', showMainWindow);
    tray.on('double-click', showMainWindow);
    refreshTrayMenu();
  } catch {}
}

async function stopEngine() {
  const child = engine;
  if (!child) return;
  child.stdin.end();
  await Promise.race([new Promise(resolve=>child.once('exit',resolve)), new Promise(resolve=>setTimeout(resolve,3500))]);
  if (child.exitCode === null) child.kill();
}
function createWindow() {
  window = new BrowserWindow({width:1440,height:940,minWidth:1000,minHeight:700,title:'Jarvis Trade',icon:path.join(__dirname,'renderer','icon.png'),backgroundColor:'#e9eff9',titleBarStyle:'hidden',titleBarOverlay:{color:'#00000000',symbolColor:'#0f172a',height:40},show:false,
    autoHideMenuBar:true,webPreferences:{preload:path.join(__dirname,'preload.cjs'),nodeIntegration:false,contextIsolation:true,sandbox:true,
      webSecurity:true,allowRunningInsecureContent:false,webviewTag:false,spellcheck:false}});
  window.webContents.setWindowOpenHandler(()=>({action:'deny'}));
  window.webContents.on('will-navigate', event=>event.preventDefault());
  window.webContents.on('will-attach-webview',event=>event.preventDefault());
  window.once('ready-to-show',()=>window.show());
  setTimeout(() => {
    if (window && !window.isDestroyed() && !window.isVisible() && !quitting) {
      window.show();
    }
  }, 1800);
  window.on('close', (event) => {
    if (!quitting && workspaceRunning && !process.argv.includes('--capture-desktop')) {
      event.preventDefault();
      window.hide();
      refreshTrayMenu();
      if (!trayNotified && Notification.isSupported()) {
        trayNotified = true;
        try {
          new Notification({
            title: `${currentAgentName} Trade active in tray`,
            body: 'Running 24x7 in the background. Click Stop in the app or Quit in tray to stop.',
            icon: path.join(__dirname, 'renderer', 'icon.png'),
          }).show();
        } catch {}
      }
    } else if (!quitting) {
      quitting = true;
    }
  });
  window.loadURL(PAGE);
  if (!app.isPackaged && process.argv.includes('--capture-desktop')) {
    const consoleErrors = [];
    window.webContents.on('console-message', (_e, level, message) => {
      if (level >= 2) consoleErrors.push(message);
    });
    window.webContents.once('did-finish-load', () => setTimeout(async () => {
      try {
        for (let i = 0; i < 60; i++) {
          const ready = await window.webContents.executeJavaScript("typeof JarvisApp !== 'undefined' && !JarvisApp.bootstrapping && document.getElementById('service-label')?.textContent !== 'Connecting to desktop'").catch(() => false);
          if (ready) break;
          await new Promise(resolve=>setTimeout(resolve,400));
        }
        const themeDefault = await window.webContents.executeJavaScript("document.documentElement.dataset.theme");
        const shot = await window.webContents.capturePage();
        await fs.writeFile(path.join(__dirname,'..','.runtime','desktop-overview.png'),shot.toPNG());
        await window.webContents.executeJavaScript("document.getElementById('open-jarvis').click()");
        await new Promise(resolve=>setTimeout(resolve,500));
        const overflowXSplit = await window.webContents.executeJavaScript("document.documentElement.scrollWidth>window.innerWidth");
        await fs.writeFile(path.join(__dirname,'..','.runtime','desktop-split-jarvis.png'),(await window.webContents.capturePage()).toPNG());
        let assistantDebug = null;
        for (const page of ['activity','stock','market','assistant','setup','settings','alerts','connections']) {
          if(page!=='assistant')await window.webContents.executeJavaScript("document.getElementById('close-jarvis')?.click()");
          await window.webContents.executeJavaScript(`view("${page}"); new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)))`);
          if (page === 'market') {
            for (let w = 0; w < 30; w++) {
              const fxReady = await window.webContents.executeJavaScript("Boolean(document.getElementById('view-market')?.hidden === false && document.querySelector('#fx-top-cards .fx-top-card-rate')?.textContent && document.querySelector('#fx-top-cards .fx-top-card-rate').textContent !== '—' && document.querySelector('#pair-chart-box svg'))").catch(() => false);
              if (fxReady) break;
              await new Promise(resolve=>setTimeout(resolve,350));
            }
          }
          await window.webContents.executeJavaScript("new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)))");
          await new Promise(resolve=>setTimeout(resolve,900));
          if(page==='assistant'){
            assistantDebug = await window.webContents.executeJavaScript("JSON.stringify({studioHidden:document.getElementById('jarvis-studio')?.hidden, display:getComputedStyle(document.getElementById('jarvis-studio')).display, zIndex:getComputedStyle(document.getElementById('jarvis-studio')).zIndex, selected:[...document.querySelectorAll('.nav.selected')].map(n=>n.dataset.view)})");
          }
          await fs.writeFile(path.join(__dirname,'..','.runtime',`desktop-${page}.png`),(await window.webContents.capturePage()).toPNG());
          if(page==='market'){
            await window.webContents.executeJavaScript("document.querySelector('[data-market-tab=\"world\"]')?.click(); new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)))");
            for (let w = 0; w < 12; w++) {
              const worldReady = await window.webContents.executeJavaScript("Boolean(document.querySelector('#world-matrix-wrap table') && document.querySelector('#world-compare-chart svg'))").catch(() => false);
              if (worldReady) break;
              await new Promise(resolve=>setTimeout(resolve,300));
            }
            await window.webContents.executeJavaScript("new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)))");
            await fs.writeFile(path.join(__dirname,'..','.runtime','desktop-market-world.png'),(await window.webContents.capturePage()).toPNG());
            await window.webContents.executeJavaScript("document.querySelector('[data-market-tab=\"desk\"]')?.click()");
          }
          if(page==='assistant')await window.webContents.executeJavaScript("document.getElementById('studio-back')?.click()");
        }
        await window.webContents.executeJavaScript("document.getElementById('close-jarvis')?.click();view('desk');document.getElementById('theme-toggle').click()");
        await new Promise(resolve=>setTimeout(resolve,800));
        await fs.writeFile(path.join(__dirname,'..','.runtime','desktop-dark.png'),(await window.webContents.capturePage()).toPNG());
        await window.webContents.executeJavaScript("document.getElementById('cmdk-open').click()");
        await new Promise(resolve=>setTimeout(resolve,400));
        await fs.writeFile(path.join(__dirname,'..','.runtime','desktop-cmdk.png'),(await window.webContents.capturePage()).toPNG());
        await window.webContents.executeJavaScript("document.getElementById('cmdk-dialog').close();document.getElementById('theme-toggle').click()");
        const summary = await window.webContents.executeJavaScript("JSON.stringify({title:document.title,theme:document.documentElement.dataset.theme,service:document.getElementById('service-label').textContent,sheets:document.getElementById('health-sheet').textContent,error:document.getElementById('notice').textContent,overflowX:document.documentElement.scrollWidth>window.innerWidth,sidebarOverflow:(()=>{const n=document.querySelector('.sidebar-nav');return n?n.scrollHeight>n.clientHeight:false;})(),marketTopCards:document.querySelectorAll('#fx-top-cards > *').length,tabsInControls:Boolean(document.querySelector('.market-controls .market-tabs')),fxAge:document.getElementById('fx-board-age')?.textContent,convOutput:document.getElementById('conv-output')?.textContent})");
        const report = {...JSON.parse(summary), themeDefault, overflowXSplit, assistantDebug: assistantDebug ? JSON.parse(assistantDebug) : null, consoleErrors};
        await fs.writeFile(path.join(__dirname,'..','.runtime','desktop-smoke.json'),JSON.stringify(report,null,2));
      } catch (err) {
        await fs.writeFile(path.join(__dirname,'..','.runtime','desktop-smoke.json'),JSON.stringify({captureError:String(err),consoleErrors})).catch(()=>{});
      } finally {
        if (process.argv.includes('--capture-exit')) app.quit();
      }
    },4500));
  }
}
if (!app.isPackaged && process.argv.includes('--capture-desktop')) {
  app.setPath('userData', path.join(__dirname, '..', '.runtime', 'capture-userdata'));
}
if (!app.requestSingleInstanceLock()) app.quit();
else {
  app.on('second-instance',()=>{ if (app.isReady()) showMainWindow(); });
  app.whenReady().then(async()=>{
    if(process.platform==='win32'&&app.isPackaged){
      try{
        const directory=path.join(app.getPath('appData'),'Microsoft','Windows','Start Menu','Programs');await fs.mkdir(directory,{recursive:true});
        shell.writeShortcutLink(path.join(directory,'Jarvis Trade.lnk'),'create',{target:process.execPath,cwd:path.dirname(process.execPath),description:'Jarvis Trade operations',appUserModelId:'com.jarvis.trade.desktop',icon:process.execPath,iconIndex:0});
      }catch{}
    }
    session.defaultSession.setPermissionRequestHandler((contents,permission,callback,details)=>callback(!!(livePermission&&contents===window?.webContents&&permission==='media'&&details.mediaTypes?.length&&details.mediaTypes.every(t=>t==='audio'))));
    session.defaultSession.setPermissionCheckHandler((contents,permission,origin,details)=>!!(livePermission&&contents===window?.webContents&&permission==='media'&&details.mediaType==='audio'&&origin.startsWith(ORIGIN)));
    session.defaultSession.on('will-download',event=>event.preventDefault());
    protocol.handle('app', async req=>{
      const file = assetPath(req.url,path.join(__dirname,'renderer'));
      if (!file || req.method !== 'GET') return new Response('Not found',{status:404});
      const mime = {'.html':'text/html; charset=utf-8','.js':'text/javascript; charset=utf-8','.css':'text/css; charset=utf-8','.svg':'image/svg+xml','.png':'image/png','.woff2':'font/woff2'}[path.extname(file)];
      if (!mime) return new Response('Not found',{status:404});
      try {return new Response(await fs.readFile(file),{headers:{'Content-Type':mime,'Content-Security-Policy':CSP,'X-Content-Type-Options':'nosniff','Cache-Control':'no-store'}});}
      catch {return new Response('Not found',{status:404});}
    });
    ipcMain.handle('jarvis:request',async(event,envelope)=>{
      if (!window || event.sender !== window.webContents || event.senderFrame !== window.webContents.mainFrame || event.senderFrame.url !== PAGE)
        return {error:{code:'UNTRUSTED_SENDER',message:'This window is not authorized.'}};
      try {
        if(envelope?.method==='window/theme'){
          const light = envelope?.params?.theme !== 'dark';
          try {
            window.setBackgroundColor(light ? '#e9eff9' : '#050814');
            if (typeof window.setTitleBarOverlay === 'function') {
              window.setTitleBarOverlay({color:'#00000000',symbolColor:light ? '#0f172a' : '#e2e8f0',height:40});
            }
          } catch {}
          return {result:{theme: light ? 'light' : 'dark'}};
        }
        if(envelope?.method==='window/title'){
          const name=sanitizeAgentName(envelope?.params?.agent_name);
          currentAgentName=name;
          const fullTitle=`${name} Trade`;
          try{ window.setTitle(fullTitle); }catch{}
          refreshTrayMenu();
          return {result:{agent_name:name,title:fullTitle}};
        }
        if(envelope?.method==='window/zoom'){
          const scale = Number(envelope?.params?.scale ?? envelope?.params?.zoom ?? 100);
          const factor = Math.max(0.5, Math.min(2.0, (scale || 100) / 100));
          if (window && !window.isDestroyed()) {
            try {
              window.webContents.setZoomFactor(factor);
            } catch {}
          }
          return {result:{scale: Math.round(factor * 100), factor}};
        }
        if (envelope?.method === 'outbound/create' && envelope.params?.approve_now) {
          const p = envelope.params || {};
          let isTurbo = (cachedAutonomyMode === 'turbo');
          if (!isTurbo) {
            try {
              const currentSettings = await request('settings/get', {});
              const mode = currentSettings?.result?.autonomy_mode || currentSettings?.autonomy_mode;
              if (mode) cachedAutonomyMode = mode;
              if (mode === 'turbo') isTurbo = true;
            } catch {}
          }
          if (!isTurbo) {
            const target = p.recipient_phone || p.recipient_jid || p.conversation_id || 'recipient';
            let bodyPreview = (p.text || '').trim();
            if (p.attachment_name) {
              bodyPreview += (bodyPreview ? '\n\n' : '') + `[Attachment: ${p.attachment_name}]`;
            }
            const confirmation = await dialog.showMessageBox(window, {
              type: 'question',
              title: 'Send WhatsApp message',
              message: `Send to ${target}?`,
              detail: bodyPreview || '(empty message)',
              buttons: ['Cancel', 'Send'],
              defaultId: 1,
              cancelId: 0,
            });
            if (confirmation.response !== 1) {
              return { result: { cancelled: true } };
            }
          }
          return await request('outbound/create', envelope.params);
        }
        if (envelope?.method === 'attachment/stage') {
          const params = envelope.params || {};
          const spoolDir = path.join(app.getPath('userData'), 'spool');
          await fs.mkdir(spoolDir, { recursive: true });
          const id = `att_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`;
          let destPath, name, size, mimeType, mediaType, sha256;

          if (params.file_path) {
            const srcPath = path.resolve(String(params.file_path));
            if (!existsSync(srcPath)) {
              return { error: { code: 'FILE_NOT_FOUND', message: 'Attachment file not found.' } };
            }
            const stat = await fs.stat(srcPath);
            if (stat.isDirectory()) {
              return { error: { code: 'INVALID_FILE', message: 'Directories cannot be attached.' } };
            }
            if (stat.size > 50 * 1024 * 1024) {
              return { error: { code: 'FILE_TOO_LARGE', message: 'Attachment exceeds 50MB limit.' } };
            }
            name = path.basename(srcPath);
            size = stat.size;
            const ext = path.extname(name).toLowerCase();
            mediaType = ['.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp'].includes(ext) ? 'image' :
                        ['.mp3', '.wav', '.ogg', '.m4a', '.aac'].includes(ext) ? 'audio' :
                        ['.mp4', '.webm', '.mkv', '.mov'].includes(ext) ? 'video' : 'document';
            mimeType = {
              '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.webp': 'image/webp',
              '.gif': 'image/gif', '.pdf': 'application/pdf', '.xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
              '.csv': 'text/csv', '.txt': 'text/plain', '.mp3': 'audio/mpeg', '.mp4': 'video/mp4'
            }[ext] || 'application/octet-stream';

            destPath = path.join(spoolDir, `${id}_${name.replace(/[^a-zA-Z0-9._-]/g, '_')}`);
            await fs.copyFile(srcPath, destPath);
            const content = await fs.readFile(destPath);
            sha256 = crypto.createHash('sha256').update(content).digest('hex');
          } else if (params.buffer || params.base64) {
            const buf = params.buffer ? Buffer.from(params.buffer) : Buffer.from(params.base64, 'base64');
            if (buf.length > 50 * 1024 * 1024) {
              return { error: { code: 'FILE_TOO_LARGE', message: 'Attachment exceeds 50MB limit.' } };
            }
            name = String(params.name || 'attachment.dat').replace(/[^a-zA-Z0-9._-]/g, '_');
            size = buf.length;
            const ext = path.extname(name).toLowerCase();
            mediaType = params.media_type || (['.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp'].includes(ext) ? 'image' : 'document');
            mimeType = params.mime_type || 'application/octet-stream';
            destPath = path.join(spoolDir, `${id}_${name}`);
            await fs.writeFile(destPath, buf);
            sha256 = crypto.createHash('sha256').update(buf).digest('hex');
          } else {
            return { error: { code: 'INVALID_INPUT', message: 'No file path or buffer provided.' } };
          }

          return {
            result: {
              descriptor: {
                id,
                file_path: destPath,
                name,
                size,
                mime_type: mimeType,
                media_type: mediaType,
                sha256,
              }
            }
          };
        }
        if (envelope?.method === 'clipboard/read-image') {
          const nativeImg = clipboard.readImage();
          if (nativeImg.isEmpty()) {
            return { result: { empty: true } };
          }
          const pngBuf = nativeImg.toPNG();
          if (pngBuf.length > 50 * 1024 * 1024) {
            return { error: { code: 'FILE_TOO_LARGE', message: 'Pasted image exceeds 50MB limit.' } };
          }
          const spoolDir = path.join(app.getPath('userData'), 'spool');
          await fs.mkdir(spoolDir, { recursive: true });
          const id = `clip_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`;
          const name = `screenshot-${new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-')}.png`;
          const destPath = path.join(spoolDir, `${id}.png`);
          await fs.writeFile(destPath, pngBuf);
          const sha256 = crypto.createHash('sha256').update(pngBuf).digest('hex');
          return {
            result: {
              descriptor: {
                id,
                file_path: destPath,
                name,
                size: pngBuf.length,
                mime_type: 'image/png',
                media_type: 'image',
                sha256,
              }
            }
          };
        }
        if (envelope?.method === 'outbound/action' && envelope.params?.action === 'approve') {
          const {id, expected_updated_at} = envelope.params;
          const outbox = await request('outbound/list', {});
          if (outbox.error) return outbox;
          const draft = outbox.result.items.find(item => item.id === id && (item.status === 'DRAFT' || item.status === 'NEEDS_PRICE_REVIEW') &&
            item.updated_at === expected_updated_at);
          if (!draft || !outbox.result.can_send) {
            return {error:{code:'APPROVAL_UNAVAILABLE',message:'Start supervised mode and refresh the draft.'}};
          }
          const confirmation = await dialog.showMessageBox(window, {
            type: 'question', title: 'Approve WhatsApp message', message: `Send to ${draft.recipient_phone || draft.recipient_jid}?`,
            detail: draft.text_parts.join('\n\n'), buttons: ['Cancel', 'Approve & Send'], defaultId: 0, cancelId: 0,
          });
          if (confirmation.response !== 1) return {result:{cancelled:true}};
          return await request('outbound/action', {id, action:'approve', expected_updated_at});
        }
        if(envelope?.method==='wa/sync') return {error:{code:'FORBIDDEN',message:'This operation is reserved for the connector.'}};
         if(envelope?.method==='wa/connect'){
          return {result: await waClient.connect(`${currentAgentName} Trade`)};
        }
        if(envelope?.method==='wa/disconnect'){
          const wipe = !!envelope?.params?.wipe;
          const st = await waClient.disconnect({wipe});
          request('wa/sync',{status:{state:st.state,connected:st.connected,has_session:st.has_session,error:st.error}}).catch(()=>{});
          return {result:st};
        }
        if(envelope?.method==='wa/status'){
          return {result:waClient.status()};
        }
        if (envelope?.method === 'diagnostics/clear') {
          const confirmation = await dialog.showMessageBox(window, {
            type: 'warning', buttons: ['Cancel', 'Clear log'], defaultId: 0, cancelId: 0,
            message: 'Clear the local diagnostic log?',
            detail: 'Previously saved copies will stay on disk.',
          });
          if (confirmation.response !== 1) return {result: {canceled: true}};
          return await request('diagnostics/clear', {});
        }
        if (envelope?.method === 'diagnostics/save-log') {
          const log = await request('diagnostics/logs', {});
          if (log.error) return log;
          const today = new Date().toISOString().slice(0, 10);
          const destination = await dialog.showSaveDialog(window, {
            title: 'Save diagnostic log', defaultPath: `jarvis-diagnostics-${today}.json`,
            filters: [{name: 'JSON', extensions: ['json']}],
          });
          if (destination.canceled || !destination.filePath) return {result: {canceled: true}};
          try {
            const report = {app_version: app.getVersion(), exported_at: new Date().toISOString(), ...log.result};
            await fs.writeFile(destination.filePath, JSON.stringify(report, null, 2) + '\n', 'utf8');
          } catch {
            return {error: {code: 'LOG_SAVE_FAILED', message: 'The diagnostic log could not be saved.'}};
          }
          return {result: {saved: true, entries: log.result.entries.length}};
        }
        if(envelope?.method==='notifications/test'){
          if(!Notification.isSupported())throw Error('Windows notifications are not available.');
          return await new Promise(resolve=>{const n=new Notification({title:`${currentAgentName} notification check`,body:'Price movement and sourcing alerts will appear here.',icon:path.join(__dirname,'renderer','icon.png')});const timeout=setTimeout(()=>resolve({error:{code:'NOTIFICATION_TIMEOUT',message:'No notification delivery confirmation. Check Windows notification settings.'}}),5000);n.once('show',()=>{clearTimeout(timeout);resolve({result:{shown:true}});});n.once('failed',()=>{clearTimeout(timeout);resolve({error:{code:'NOTIFICATION_FAILED',message:'Windows blocked the notification. Check Windows notification settings.'}});});n.show();});
        }
        if(envelope?.method==='sheet/open'){
          const r=await request('sheet/link',{});if(r.error)return r;
          const u=new URL(r.result.url);if(u.protocol!=='https:'||u.hostname!=='docs.google.com'||!u.pathname.startsWith('/spreadsheets/d/'))throw Error('Invalid Sheet link');
          await shell.openExternal(u.href);return {result:{opened:true}};
        }
        const result=await request(envelope?.method,envelope?.params || {});
        if(envelope?.method==='settings/save'&&envelope?.params?.autonomy_mode)cachedAutonomyMode=envelope.params.autonomy_mode;
        if((envelope?.method==='live/start'||envelope?.method==='voice/arm')&&!result.error)livePermission=true;
        if(envelope?.method==='live/stop'||envelope?.method==='voice/disarm')livePermission=false;
        if(typeof result?.result?.running==='boolean'){
          workspaceRunning = result.result.running;
          refreshTrayMenu();
        }
        return result;
      }
      catch(error){return {error:{code:'DESKTOP_UNAVAILABLE',message:error.message}};}
    });
    createTray();
    createWindow();
    startEngine().catch(()=>{});
  });
  app.on('window-all-closed',(e)=>{ if(!quitting && !process.argv.includes('--capture-desktop')) e.preventDefault(); });
  let isQuittingCleanly = false;
  app.on('before-quit', (event) => {
    if (!isQuittingCleanly) {
      event.preventDefault();
      isQuittingCleanly = true;
      quitting = true;
      try { tray?.destroy(); tray = null; } catch {}
      Promise.allSettled([waClient.disconnect({ wipe: false }), stopEngine()]).finally(() => app.quit());
    }
  });
}
