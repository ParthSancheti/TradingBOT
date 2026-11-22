import {validHello} from "./boundary.mjs";
import {selectWhatsAppTab} from "./tab-control.mjs";
import {readRendered,hasHumanDraft,sourceIndex,discoverChats,openDiscoveredChat,readAccountProfile,readWppStoreProfile,readWppStoreMessages} from "./scanner.mjs";
import {probeLiveWhatsAppDom,checkSessionAndResolveConflict,openChatSameTab,supervisedWhatsApp} from "./actions.mjs";
let port=null,connection=null,session=null,sequence=0,pending=new Map(),scanning=false,fastTimer=null,agentName="Jarvis";
let mediaScan = null;
let dispatchScan = null;
let lastMediaScan = 0;
let lastWakeScan = 0;
let profileScan = null;
const savedText = new Map();
let savedTextLoaded = false;
let rescanRequested = false;
let lastWppReason = null;
const mediaEnriched = new Set();
const pendingQueue = new Map();
// MV3 service workers are suspended and restarted; keep the sent-message
// fingerprints in session storage so a restart does not resend 100+ messages.
async function loadSavedText(){
  if(savedTextLoaded)return;
  savedTextLoaded=true;
  try{
    const data = await chrome.storage.session.get(["savedTextEntries", "mediaEnrichedEntries", "pendingQueueEntries"]);
    if(Array.isArray(data?.savedTextEntries))for(const [k,v] of data.savedTextEntries)if(!savedText.has(k))savedText.set(k,v);
    if(Array.isArray(data?.mediaEnrichedEntries))for(const k of data.mediaEnrichedEntries)mediaEnriched.add(k);
    if(Array.isArray(data?.pendingQueueEntries))for(const [k,v] of data.pendingQueueEntries)if(!pendingQueue.has(k))pendingQueue.set(k,v);
  }catch{}
}
async function persistSavedText(){
  try{
    await chrome.storage.session.set({
      savedTextEntries: [...savedText.entries()].slice(-1500),
      mediaEnrichedEntries: [...mediaEnriched.values()].slice(-500),
      pendingQueueEntries: [...pendingQueue.entries()].slice(-200),
    });
  }catch{}
}
const pause=ms=>new Promise(resolve=>setTimeout(resolve,ms));
let activity={phase:"idle",detail:"Open desktop app to connect",at:Date.now()};
async function showActivity(phase,detail,tabId=null){
  activity={phase,detail,agentName,at:Date.now()};
  await chrome.storage.local.set({activity,agentName});
  const badge=phase==="scanning"?"•••":phase==="saving"?"↑":phase==="paused"?"Ⅱ":phase==="error"?"!":"";
  await chrome.action.setBadgeText({text:badge});
  await chrome.action.setBadgeBackgroundColor({color:phase==="error"?"#a84d54":"#6253c7"});
  await chrome.action.setTitle({title:`${agentName} · ${detail}`});
  if(tabId)await chrome.tabs.sendMessage(tabId,{operation:"jarvis_activity",...activity}).catch(()=>{});
}

function scheduleFastPoll(ms=2500){
  if(typeof chrome?.runtime?.getManifest!=="function")return;
  clearTimeout(fastTimer);
  fastTimer=setTimeout(()=>scan(false),ms);
}

async function fetchAvatarDataUrl(avatarUrl){
  if(typeof fetch!=="function"||typeof OffscreenCanvas==="undefined"||typeof createImageBitmap!=="function")return null;
  if(!/^https:\/\/[^/]+\.(?:whatsapp\.net|fbcdn\.net)\//i.test(avatarUrl||""))return null;
  try{
    const ctrl=new AbortController();
    const timer=setTimeout(()=>ctrl.abort(),4000);
    try{
      const resp=await fetch(avatarUrl,{signal:ctrl.signal});
      if(!resp.ok)return null;
      const blob=await resp.blob();
      if(blob.size>1500000)return null;
      const bmp=await createImageBitmap(blob);
      const canvas=new OffscreenCanvas(120,120);
      const ctx=canvas.getContext("2d");
      ctx.drawImage(bmp,0,0,120,120);
      bmp.close();
      const jpegBlob=await canvas.convertToBlob({type:"image/jpeg",quality:0.85});
      const buf=await jpegBlob.arrayBuffer();
      let bin="";
      const bytes=new Uint8Array(buf);
      for(let i=0;i<bytes.length;i++)bin+=String.fromCharCode(bytes[i]);
      return "data:image/jpeg;base64,"+btoa(bin);
    }finally{clearTimeout(timer);}
  }catch{return null;}
}

async function waitTabComplete(tabId,timeoutMs=7000){
  const deadline=Date.now()+timeoutMs;
  while(Date.now()<deadline){
    const tab=await chrome.tabs.get(tabId).catch(()=>null);
    if(!tab)return null;
    if(tab.status==="complete" && tab.url?.startsWith("https://web.whatsapp.com/"))return tab;
    await pause(300);
  }
  return await chrome.tabs.get(tabId).catch(()=>null);
}

// Injects wa-js once per WhatsApp document and waits until its module loader
// is ready. Before this fix the scan ran immediately after injection, WPP was
// not ready, and every scan fell back to clicking chats in the DOM.
async function ensureWppInjected(tabId,waitMs=15000){
  try{
    const [chk]=await chrome.scripting.executeScript({
      target:{tabId,frameIds:[0]},
      world:"MAIN",
      func:()=>({
        present:Boolean(window.WPP),
        ready:Boolean(window.WPP&&(window.WPP.isReady||window.WPP.loader?.isReady)),
        version:window.WPP?.version||null,
      }),
    }).catch(()=>[null]);
    if(chk?.result?.ready && (!chk.result.version || chk.result.version === "4.8.2")) return {ready:true, version:chk.result.version||"4.8.2"};
    if(!chk?.result?.present){
      await chrome.scripting.executeScript({
        target:{tabId,frameIds:[0]},
        world:"MAIN",
        // No analytics, no "powered by" device name, no status sync.
        func:()=>{window.WPPConfig={...(window.WPPConfig||{}),disableGoogleAnalytics:true,googleAnalyticsId:null,poweredBy:null,deviceName:false,syncAllStatus:false,sendStatusToDevice:false};},
      }).catch(()=>{});
      await chrome.scripting.executeScript({
        target:{tabId,frameIds:[0]},
        world:"MAIN",
        files:["vendor/wppconnect-wa.js"],
      }).catch(()=>{});
    }
    const [wait]=await chrome.scripting.executeScript({
      target:{tabId,frameIds:[0]},
      world:"MAIN",
      func:async(ms)=>{
        const deadline=Date.now()+ms;
        while(Date.now()<deadline){
          const w=window.WPP;
          if(w&&(w.isReady||w.loader?.isReady))return {ready:true};
          await new Promise(r=>setTimeout(r,200));
        }
        return {ready:false,reason:window.WPP?"WPP_NOT_READY":"WPP_NOT_INJECTED"};
      },
      args:[waitMs],
    }).catch(()=>[null]);
    return wait?.result||{ready:false,reason:"INJECTION_FAILED"};
  }catch{return {ready:false,reason:"INJECTION_FAILED"};}
}

async function syncProfileFromTab(tabId,ownerPhone=null,forceDrawer=false){
  const readyTab=await waitTabComplete(tabId,7000);
  if(!readyTab?.url?.startsWith("https://web.whatsapp.com/")||readyTab.discarded)return null;
  let wppRes=null;
  if(typeof readWppStoreProfile==="function"){
    try{
      await ensureWppInjected(tabId);
      const [r]=await chrome.scripting.executeScript({
        target:{tabId,frameIds:[0]},
        world:"MAIN",
        func:readWppStoreProfile,
        args:[ownerPhone],
      }).catch(()=>[null]);
      if(r?.result)wppRes=r.result;
    }catch{}
  }
  let wppAvatar=wppRes?.avatar||null;
  if(!wppAvatar && wppRes?.avatar_url){
    wppAvatar=await fetchAvatarDataUrl(wppRes.avatar_url);
  }
  let domRes=null;
  if((!wppRes?.verified || (forceDrawer && !wppAvatar)) && typeof readAccountProfile==="function"){
    const [r]=await chrome.scripting.executeScript({
      target:{tabId,frameIds:[0]},
      func:readAccountProfile,
      args:[ownerPhone,forceDrawer],
    }).catch(()=>[null]);
    if(r?.result)domRes=r.result;
  }
  const merged={
    verified:Boolean(wppRes?.verified||domRes?.verified),
    phone:wppRes?.phone||domRes?.phone||null,
    name:wppRes?.name||domRes?.name||null,
    avatar:domRes?.avatar||wppAvatar||null,
    avatar_url:wppRes?.avatar_url||domRes?.avatar_url||null,
    avatar_checked:Boolean(wppRes?.avatar_checked||domRes?.avatar_checked||domRes?.avatar||wppAvatar),
    source:domRes?.verified&&!wppRes?.verified?"xpath_dom":(wppRes?.verified?"wpp_store":"xpath_dom"),
  };
  if(merged.verified && !merged.avatar && merged.avatar_url){
    merged.avatar=await fetchAvatarDataUrl(merged.avatar_url);
  }
  await chrome.storage.local.set({
    lastProfileDebug:{
      at:Date.now(),
      verified:merged.verified,
      wppOk:Boolean(wppRes?.verified),
      wppReason:wppRes?.reason||null,
      domOk:Boolean(domRes?.verified),
      phone:merged.phone||null,
      hasAvatar:Boolean(merged.avatar),
    },
  }).catch(()=>null);
  return merged;
}

async function captureWppMessages(tabId,sinceTs=0,allowedPhones=null,databasePhone=null,captureMedia=false){
  if(typeof readWppStoreMessages!=="function")return null;
  try{
    const injected=await ensureWppInjected(tabId);
    if(!injected?.ready){lastWppReason=injected?.reason||"WPP_NOT_READY";return null;}
    const [r]=await chrome.scripting.executeScript({
      target:{tabId,frameIds:[0]},
      world:"MAIN",
      func:readWppStoreMessages,
      args:[sinceTs,allowedPhones,agentName,databasePhone,captureMedia],
    }).catch(()=>[null]);
    if(r?.result?.verified){lastWppReason=null;return r.result;}
    lastWppReason=r?.result?.reason||"WPP_READ_FAILED";
  }catch{lastWppReason="WPP_READ_FAILED";}
  return null;
}

async function runLiveDomProbe(tabId){
  if(typeof probeLiveWhatsAppDom!=="function")return null;
  await ensureWppInjected(tabId);
  const [r]=await chrome.scripting.executeScript({
    target:{tabId,frameIds:[0]},
    world:"MAIN",
    func:probeLiveWhatsAppDom,
  }).catch(()=>[null]);
  return r?.result || null;
}

function connect(){
  if(connection)return connection;
  connection=new Promise((resolve,reject)=>{
    session=crypto.randomUUID();sequence=1;
    const native=chrome.runtime.connectNative("com.jarvis.trade");port=native;
    const timeout=setTimeout(()=>{reject(Error(`Open the ${agentName} desktop app to connect.`));native.disconnect();},10000);
    native.onMessage.addListener(result=>{
      if(result.operation==="hello_result"){
        clearTimeout(timeout);
        if(!validHello(result,session) || !result.can_read_chats){reject(Error("Desktop connection could not be verified."));native.disconnect();return;}
        resolve();return;
      }
      const callback=pending.get(result.sequence);
      if(result.session_id===session && callback){pending.delete(result.sequence);callback.resolve(result);}
    });
    native.onDisconnect.addListener(()=>{
      const ignored=chrome.runtime.lastError;port=null;connection=null;clearTimeout(timeout);
      reject(Error("Desktop app disconnected."));for(const v of pending.values())v.reject(Error("Desktop app disconnected."));pending.clear();
    });
    native.postMessage({protocol:"jarvis.bridge.v1",operation:"hello",sequence,session_id:session});
  });
  connection.catch(()=>{connection=null;});return connection;
}
async function callOnce(operation,data={}){
  await connect();const n=++sequence;
  return new Promise((resolve,reject)=>{
    const timer=setTimeout(()=>{pending.delete(n);reject(Error("Desktop request timed out. It will retry shortly."));},30000);
    pending.set(n,{resolve:r=>{clearTimeout(timer);resolve(r);},reject:e=>{clearTimeout(timer);reject(e);}});
    try{port.postMessage({protocol:"jarvis.bridge.v1",session_id:session,sequence:n,operation,activity,...data});}
    catch(error){pending.delete(n);clearTimeout(timer);reject(error);}
  });
}
async function call(operation,data={}){
  try{
    return await callOnce(operation,data);
  }catch(err){
    const msg=String(err?.message||err||"");
    if(/disconnected|closed|pipe|timeout/i.test(msg)){
      try{port?.disconnect?.();}catch{}
      port=null;connection=null;
      if(typeof chrome?.runtime?.getManifest==="function")await pause(300);
      return await callOnce(operation,data);
    }
    throw err;
  }
}
async function dispatchApprovedMessage(tabId, outboundId) {
  const currentTab = await chrome.tabs.get(tabId).catch(() => null);
  if (!currentTab || currentTab.discarded) {
    return;
  }
  if (currentTab.status !== "complete" || !currentTab.url?.startsWith("https://web.whatsapp.com/")) {
    return;
  }
  await ensureWppInjected(tabId);
  const [probe] = await chrome.scripting.executeScript({
    target: {tabId, frameIds: [0]},
    world: "MAIN",
    func: supervisedWhatsApp,
    args: ["account"],
  }).catch(() => [null]);
  const accountPhone = probe?.result?.account_phone;
  if (!accountPhone || !probe?.documentId) {
    return;
  }

  const claim = await callOnce("dispatch_claim", {
    outbound_id: outboundId,
    account_phone: accountPhone,
  });
  const item = claim?.item;
  if (claim?.error || item?.id !== outboundId || !item?.claim_token) return;

  const nativeArgs = {
    outbound_id: outboundId,
    claim_token: item.claim_token,
    account_phone: accountPhone,
  };
  const evaluate = async (...args) => {
    const [reply] = await chrome.scripting.executeScript({
      target: {tabId, documentIds: [probe.documentId]},
      world: "MAIN",
      func: supervisedWhatsApp,
      args,
    });
    return reply?.result;
  };
  let outcome = {outcome: "FAILED_BEFORE_SEND", reason: "WHATSAPP_CHECK_FAILED"};
  let sendStarted = false;
  try {
    const verification = await evaluate("verify", item);
    if (verification?.verified !== true) {
      outcome = verification ?? outcome;
    } else {
      const authorization = await callOnce("dispatch_authorize", nativeArgs);
      if (authorization?.ok === true) {
        sendStarted = true;
        outcome = await evaluate("send", item, verification.chat_id, authorization.expires_at)
          ?? {outcome: "UNKNOWN", reason: "WHATSAPP_RESULT_MISSING"};
      } else {
        outcome = {outcome: "FAILED_BEFORE_SEND", reason: "DISPATCH_NOT_AUTHORIZED"};
      }
    }
  } catch {
    if (sendStarted) outcome = {outcome: "UNKNOWN", reason: "WHATSAPP_RESULT_MISSING"};
  }
  if (!["FAILED_BEFORE_SEND", "UNKNOWN", "ACCEPTED"].includes(outcome.outcome)) {
    outcome = {outcome: "UNKNOWN", reason: "WHATSAPP_RESULT_INVALID"};
  }
  const recorded = await callOnce("dispatch_result", {...nativeArgs, ...outcome});
  if (recorded?.error) throw Error("The desktop could not confirm the dispatch result. Check the outbox.");
}

async function inject(tabId,func,args=[]){
  const tab=await chrome.tabs.get(tabId);
  if(tab.discarded)throw Error("Chrome put the observer tab to sleep. Reopen it, then return to your desk.");
  if(!tab.url?.startsWith("https://web.whatsapp.com/"))throw Error("Observer tab left WhatsApp. Close it and restart the desk.");
  const [result]=await chrome.scripting.executeScript({target:{tabId,frameIds:[0]},func,args});
  return result?.result;
}
async function humanUsing(tab){
  if(!tab.active)return false;
  return (await chrome.windows.get(tab.windowId)).focused;
}
async function capture(tabId,phone=null,title=null,sinceTs=0){
  const deadline=Date.now()+3500;
  let lastReason=null;
  while(Date.now()<deadline){
    const current=await chrome.tabs.get(tabId);
    if(current.status==="complete"){
      const result=await inject(tabId,readRendered,[phone,title,sinceTs,true]);
      if(result?.verified)return result;
      if(result?.reason==="HUMAN_DRAFT")throw Error("Your unfinished message is preserved. Scan paused.");
      lastReason=result?.reason||lastReason;
    }
    await pause(250);
  }
  // The chat loaded but holds only media/system rows: that is not a failure.
  if(lastReason==="NO_VERIFIED_INCOMING_TEXT")return {verified:true,events:[]};
  return null;
}
async function saveObservations(events, metadata) {
  const encoder = new TextEncoder();
  let batch = [];
  let savedCount = 0;
  let dupCount = 0;
  let ignoredCount = 0;
  let retryCount = 0;
  const allResults = [];

  const send = async report => {
    const answer = await call("observations", report);
    if (answer.error) {
      for (const event of report.events) {
        retryCount++;
        const prev = pendingQueue.get(event.message_id);
        pendingQueue.set(event.message_id, {
          event,
          attempts: (prev?.attempts || 0) + 1,
          lastAttempt: Date.now(),
        });
      }
      throw Error(answer.error);
    }
    const results = Array.isArray(answer.results) ? answer.results : [];
    const hasDetailedResults = results.length > 0;
    for (let i = 0; i < report.events.length; i++) {
      const event = report.events[i];
      const res = hasDetailedResults ? (results.find(r => r.message_id === event.message_id) || (results.length === report.events.length ? results[i] : null)) : null;
      if (res) allResults.push(res);
      const isTerminal = !hasDetailedResults || (res && res.status && res.status !== "RETRY_REQUIRED");
      if (isTerminal) {
        pendingQueue.delete(event.message_id);
        if (!event.media_b64 && !event.stock_file_b64) {
          savedText.set(event.message_id, observationFingerprint(event));
        } else {
          mediaEnriched.add(event.message_id);
        }
        if (!hasDetailedResults || ["SAVED", "RECEIVED", "CLASSIFIED", "CAPTURED_REVIEW"].includes(res?.status)) {
          savedCount++;
        } else if (res?.status === "DUPLICATE") {
          dupCount++;
        } else if (String(res?.status).startsWith("IGNORED") || res?.status === "OUTGOING") {
          ignoredCount++;
        }
      } else {
        retryCount++;
        const prev = pendingQueue.get(event.message_id);
        pendingQueue.set(event.message_id, {
          event,
          attempts: (prev?.attempts || 0) + 1,
          lastAttempt: Date.now(),
        });
      }
    }
    while (savedText.size > 1500) savedText.delete(savedText.keys().next().value);
    await persistSavedText();
    if (hasDetailedResults && results.some(result => result.status === "RETRY_REQUIRED")) {
      throw Error("Some messages need a retry. Check the desktop connection.");
    }
  };
  let nextMetadata = metadata;
  for (const event of events) {
    if (encoder.encode(JSON.stringify([event])).length > 230000) {
      throw Error("An observation exceeded the bridge size limit.");
    }
    const candidate = [...batch, event];
    if (batch.length === 20 || encoder.encode(JSON.stringify({events: candidate, ...nextMetadata})).length > 230000) {
      await send({events: batch, ...nextMetadata});
      batch = [];
      nextMetadata = null;
    }
    batch.push(event);
  }
  if (batch.length || nextMetadata) await send({events: batch, ...nextMetadata});
  return {
    total: events.length,
    saved: savedCount,
    duplicate: dupCount,
    ignored: ignoredCount,
    queued: retryCount,
    results: allResults,
  };
}
function observationFingerprint(event) {
  return JSON.stringify([event.conversation_id, event.phone, event.text, event.timestamp, event.outgoing]);
}
async function syncRequestedProfile(tab, config, openProfileDrawer, requestId, ownerPhoneKey) {
  if (openProfileDrawer) await chrome.storage.local.set(config.profile_requested
    ? {lastProfileRequestId: requestId}
    : {startupProfileOwner: ownerPhoneKey});
  const profile = await syncProfileFromTab(tab.id, config.owner_phone || null, openProfileDrawer);
  if (profile?.verified) {
    await call("poll_config", {profile});
    await chrome.storage.local.set({syncedProfilePhone: config.owner_phone || profile.phone || "__default__", profileRetryAfter: 0});
  } else {
    await chrome.storage.local.set({profileRetryAfter: Date.now() + 60000});
  }
}
async function scan(force=false){
  if(scanning){if(force)rescanRequested=true;return;}
  scanning=true;
  let nextCursor=null;
  try{
    const retry=await chrome.storage.local.get(["retryAt","lastConfigVersion","agentName","syncedProfilePhone","profileRetryAfter","lastProfileRequestId","startupProfileOwner"]);
    if(retry.agentName)agentName=retry.agentName;
    const config=await call("poll_config",{activity,dispatch_version:1});if(config.error)throw Error(config.error);
    if(config.agent_name)agentName=config.agent_name;
    const ownerPhoneKey=String(config.owner_phone||"__default__");
    const storePatch={active:config.active===true,agentName,lastConnected:Date.now(),failureCount:0,retryAt:0,lastError:null};
    if(config.profile_captured===true && !config.profile_requested){
      storePatch.syncedProfilePhone=ownerPhoneKey;
      storePatch.profileRetryAfter=0;
    }else if(config.profile_captured===false && config.profile_requested){
      storePatch.syncedProfilePhone="";
    }
    await chrome.storage.local.set(storePatch);
    if(config.config_version && retry.lastConfigVersion !== config.config_version){
      savedText.clear();
      await chrome.storage.local.set({cursor:0,lastConfigVersion:config.config_version,...(config.profile_captured===false?{syncedProfilePhone:""}:{})});
    }
    if(config.open_requested){
      const tab=await selectWhatsAppTab(chrome,{focus:true});
      await showActivity("waiting","WhatsApp Web active as main tab",tab.id);
      scheduleFastPoll(config.profile_requested || config.capture_profile ? 1200 : 2500);
      return;
    }
    const syncedAlreadyLocal = Boolean(!config.profile_requested && retry.syncedProfilePhone && retry.syncedProfilePhone === ownerPhoneKey && config.profile_captured !== false);
    const profileRetryDue = Date.now() >= Number(retry.profileRetryAfter || 0);
    const requestId = config.profile_request_id || ownerPhoneKey;
    const openProfileDrawer = config.profile_requested
      ? retry.lastProfileRequestId !== requestId
      : Boolean(config.active && config.capture_profile && !syncedAlreadyLocal && profileRetryDue && retry.startupProfileOwner !== ownerPhoneKey);
    const shouldCaptureProfile = openProfileDrawer || (profileRetryDue && Boolean(config.profile_requested || (config.capture_profile && !syncedAlreadyLocal)));
    if (shouldCaptureProfile && !profileScan) {
      const tab = await selectWhatsAppTab(chrome, {focus: Boolean(config.profile_only && openProfileDrawer)});
      if (!config.active) {
        await showActivity("scanning","Syncing your WhatsApp name & photo",tab.id);
        await syncRequestedProfile(tab, config, openProfileDrawer, requestId, ownerPhoneKey);
        if (config.profile_only || config.profile_requested) return;
      } else {
        profileScan = syncRequestedProfile(tab, config, false, requestId, ownerPhoneKey)
          .catch(() => {})
          .finally(() => { profileScan = null; });
      }
    }
    if(config.active!==true){
      await chrome.storage.local.set({lastError:null,state:"Connected · stopped in desktop app"});
      await showActivity("idle","Connected to desktop · Stopped in app");
      scheduleFastPoll(4000);
      return;
    }
    const persisted=await chrome.storage.local.get(["cursor","ownedTab"]);
    const tab=await selectWhatsAppTab(chrome,{dedupe:false});
    if(typeof checkSessionAndResolveConflict==="function"){
      const [ses]=await chrome.scripting.executeScript({
        target:{tabId:tab.id,frameIds:[0]},
        func:checkSessionAndResolveConflict,
        args:[false],
      }).catch(()=>[null]);
      if(ses?.result?.state==="QR_REQUIRED"){
        await showActivity("paused","Scan QR in WhatsApp Web to resume",tab.id);
        await chrome.storage.local.set({lastError:"WhatsApp Web needs QR login.",state:"Waiting for QR login"});
        scheduleFastPoll(4000);
        return;
      }
      if(ses?.result?.state==="USE_HERE_CONFLICT"){
        await showActivity("paused","WhatsApp is open in another window. Click \"Use here\" in this tab to resume.",tab.id);
        await chrome.storage.local.set({lastError:"WhatsApp is open in another window.",state:"Waiting for \"Use here\""});
        scheduleFastPoll(5000);
        return;
      }
      if(ses?.result?.state==="LOADING"){
        await showActivity("waiting","WhatsApp Web is loading…",tab.id);
        scheduleFastPoll(3000);
        return;
      }
      if(ses?.result?.state==="CONFLICT_CLICKED")await pause(700);
    }
    await loadSavedText();
    const nextMessage = config.can_send === true && config.outbound?.[0];
    const sinceTs=Number(config.scan_since_ts || 0);
    // Do not flash "Checking…" every few seconds; the panel only changes when
    // there is something new to report.
    if(activity.phase!=="waiting"&&activity.phase!=="scanning")await showActivity("scanning","Listening for new WhatsApp messages",tab.id);
    let captured=[],humanReplies=[],lidMap={},discovered=0,failed=0,usedFallback=false;
    const scanAll = config.scan_all !== false;
    const allowedPhones = scanAll ? null : (config.sources || []).map(s => s.phone).concat(config.database_phone || []);
    const wppFeed = await captureWppMessages(tab.id, sinceTs, allowedPhones, config.database_phone);
    if (wppFeed?.verified) {
      captured = wppFeed.events || [];
      humanReplies = wppFeed.human_replies || [];
      lidMap = wppFeed.lid_map || {};
      discovered = Number(wppFeed.discovered || captured.length || 0);
    } else {
      usedFallback = true;
      if (await inject(tab.id, hasHumanDraft)) throw Error("Your unfinished message is preserved. Scan paused.");
      const userActive = await humanUsing(tab);
      if (userActive) {
        if (scanAll || allowedPhones?.length) {
          const passive = await inject(tab.id, readRendered, [null, null, sinceTs, true]).catch(() => null);
          if (passive?.reason === "HUMAN_DRAFT") throw Error("Your unfinished message is preserved. Scan paused.");
          if (passive?.verified) {
            captured = (passive.events || []).filter(event => !allowedPhones || allowedPhones.includes(event.phone));
            discovered = 1;
          }
        }
      } else if (scanAll) {
        const listing = await inject(tab.id, discoverChats);
        if (!listing?.ready) throw Error(listing?.reason || "WhatsApp inbox is not ready.");
        const chats = listing.chats || []; discovered = chats.length;
        if (!chats.length) throw Error("No rendered chats found. Sign in or reopen the WhatsApp observer tab.");
        // Only chats with unread messages are opened. Cycling through every
        // read chat produced the endless "no verified incoming text" checks.
        const unread = chats.filter(c => c.unread);
        if (unread.length) {
          const index = sourceIndex(persisted.cursor, unread.length); nextCursor = index + 1;
          const candidate = unread[index];
          await showActivity("scanning", "Reading new messages from " + candidate.title, tab.id);
          const opened = await inject(tab.id, openDiscoveredChat, [candidate]);
          if (!opened?.opened) throw Error(opened?.reason || "Chat could not be opened.");
          const result = await capture(tab.id, null, candidate.title, sinceTs);
          if (result?.verified) captured = result.events || []; else if (result === null) failed = 1;
        } else {
          const passive = await inject(tab.id, readRendered, [null, null, sinceTs, true]).catch(() => null);
          if (passive?.verified) captured = passive.events || [];
        }
      } else if (allowedPhones?.length) {
        const sources = allowedPhones;
        const index = sourceIndex(persisted.cursor, sources.length); nextCursor = index + 1;
        const phone = sources[index]; discovered = sources.length;
        if(!/^\+[1-9]\d{6,14}$/.test(phone))throw Error("Stock source number is invalid.");
        if(typeof openChatSameTab==="function"){
          await ensureWppInjected(tab.id);
          await chrome.scripting.executeScript({target:{tabId:tab.id,frameIds:[0]},world:"MAIN",func:openChatSameTab,args:[{phone}]}).catch(()=>null);
        }else{
          const target="https://web.whatsapp.com/send?phone="+encodeURIComponent(phone.slice(1));
          if(tab.url!==target)await chrome.tabs.update(tab.id,{url:target,active:false});
        }
        const result=await capture(tab.id,phone,null,sinceTs);
        if(result)captured=result.events;else failed=1;
      }
    }
    const coverage={scope:scanAll?"rendered_recent_chats":"configured_sources",complete:false,discovered,scanned:failed?0:1,failed};
    const lidCacheState=await chrome.storage.local.get(["resolvedLidPhones"]);
    const resolvedLidPhones={...(lidCacheState.resolvedLidPhones||{})};
    for(const [lidKey,lidVal] of Object.entries(lidMap||{})){
      if(lidKey && lidVal)resolvedLidPhones[lidKey]=lidVal;
    }
    for(const ev of captured){
      if(!ev.phone && ev.sender_id && resolvedLidPhones[ev.sender_id]){
        ev.phone=resolvedLidPhones[ev.sender_id];
        lidMap[ev.sender_id]=ev.phone;
      }
    }
    if(Object.keys(lidMap||{}).length) await chrome.storage.local.set({resolvedLidPhones});
    if (!wppFeed) humanReplies = captured.filter(event => event.outgoing).map(event => ({
      conversation_id: event.conversation_id, message_id: event.message_id, timestamp: event.timestamp,
    }));
    const freshCaptured = captured.filter(event => savedText.get(event.message_id) !== observationFingerprint(event));
    const nowMs = Date.now();
    const dueRetries = [];
    for (const [mid, item] of pendingQueue.entries()) {
      const backoff = Math.min(30000, 2000 * Math.pow(2, (item.attempts || 1) - 1));
      if (nowMs - (item.lastAttempt || 0) >= backoff) {
        dueRetries.push(item.event);
      }
    }
    const seenNewIds = new Set();
    const newEvents = [];
    for (const ev of [...dueRetries, ...freshCaptured]) {
      if (!seenNewIds.has(ev.message_id)) {
        seenNewIds.add(ev.message_id);
        newEvents.push(ev);
      }
    }
    if (newEvents.length) await showActivity("saving",`Sending ${newEvents.length} message${newEvents.length===1?"":"s"} to ${agentName}`,tab.id);
    const saveSummary = await saveObservations(newEvents, {coverage, human_replies: humanReplies, lid_map: lidMap, agent_commands: (wppFeed?.agent_commands || []).slice(-10)});
    if (nextMessage?.id && !dispatchScan) {
      dispatchScan = dispatchApprovedMessage(tab.id, nextMessage.id)
        .catch(() => showActivity("error", "WhatsApp delivery status unavailable. Check the outbox.", tab.id))
        .finally(() => { dispatchScan = null; });
    }
    if (wppFeed?.needs_media && !mediaScan && Date.now() - lastMediaScan >= 8000) {
      lastMediaScan = Date.now();
      mediaScan = captureWppMessages(tab.id, sinceTs, allowedPhones, config.database_phone, true)
        .then(feed => {
          const mediaEvents = (feed?.events || []).filter(event => (event.media_b64 || event.stock_file_b64) && !mediaEnriched.has(event.message_id));
          if (mediaEvents.length) return saveObservations(mediaEvents, null);
        })
        .catch(() => {})
        .finally(() => { mediaScan = null; });
    }
    if(nextCursor!==null) await chrome.storage.local.set({cursor:nextCursor});
    const fresh = (saveSummary?.saved || 0) || newEvents.filter(event=>!event.outgoing).length;
    const engine=usedFallback?` · basic mode (${lastWppReason||"WPP unavailable"})`:"";
    if(fresh>0)await showActivity("waiting",`${fresh} new message${fresh===1?"":"s"} sent to ${agentName}${engine}`,tab.id);
    else if(activity.phase!=="waiting"||activity.detail.startsWith("Listening")||usedFallback!==Boolean(activity.detail.includes("basic mode")))await showActivity("waiting",`Listening for new WhatsApp messages${engine}`,tab.id);
    await chrome.storage.local.set({
      lastScan:Date.now(),
      lastError:null,
      coverage,
      state:usedFallback?"Connected · basic DOM mode":"Connected · 24x7 active",
      captured:captured.length,
      counts: {
        captured: captured.length,
        saved: saveSummary?.saved || 0,
        duplicate: saveSummary?.duplicate || 0,
        ignored: saveSummary?.ignored || 0,
        queued: pendingQueue.size,
      },
      engine:usedFallback?"dom":"wpp",
      wppReason:lastWppReason
    });
    await chrome.storage.local.set({failureCount:0,retryAt:0});
    if(rescanRequested){rescanRequested=false;scheduleFastPoll(250);}
    else scheduleFastPoll(usedFallback?5000:2800);
  }catch(e){
    const previous=await chrome.storage.local.get("failureCount");
    const failures=Math.min(6,(previous.failureCount||0)+1);
    const delay=Math.min(10000,3000+failures*1000);
    await chrome.storage.local.set({lastError:e.message,failureCount:failures,retryAt:Date.now()+delay});
    await showActivity("error",e.message+" · Reconnecting in "+Math.ceil(delay/1000)+"s.");
    scheduleFastPoll(delay);
  }
  finally{scanning=false;}
}
async function arm(){await chrome.alarms.create("jarvis-minute",{periodInMinutes:0.5});}
chrome.alarms.onAlarm.addListener(a=>{if(a.name==="jarvis-minute")scan();});
chrome.runtime.onStartup.addListener(()=>{arm();scan();});
chrome.runtime.onInstalled.addListener(()=>{arm();scan();});
chrome.runtime.onMessage.addListener((message,sender,reply)=>{
  const popup=sender.id===chrome.runtime.id && sender.url===chrome.runtime.getURL("status.html") && !sender.tab;
  const wake=sender.id===chrome.runtime.id && sender.tab && sender.frameId===0 && sender.url?.startsWith("https://web.whatsapp.com/") && message.operation==="wake";
  if(!popup && !wake)return false;
  (async()=>{
    if(wake){
      if(Date.now() - lastWakeScan >= 750){
        lastWakeScan = Date.now();
        scan(true);
      }
      return{ok:true};
    }
    if(message.operation==="sync"){await scan(true);return{text:"Connection refreshed. Start and Stop are managed by your desktop app."};}
    if(message.operation==="connection_proof"){const config=await call("poll_config");if(config.error)throw Error(config.error);return{text:config.active?(config.can_send?"Desktop connected · Supervised trading active.":"Desktop connected · Observation active."):"Desktop connected. Press Start in your desktop app."};}
    if(message.operation==="probe_dom"){const tab=await selectWhatsAppTab(chrome,{dedupe:false});const probe=await runLiveDomProbe(tab.id);if(!probe)throw Error("Open WhatsApp Web tab to probe live XPaths.");await chrome.storage.local.set({lastProbe:probe});await call("probe_result",{probe}).catch(()=>null);const okCount=Object.values(probe.selectors||{}).filter(v=>v?.found).length;const totalCount=Object.keys(probe.selectors||{}).length;return{text:`Live Probe (${probe.sessionState}): ${okCount}/${totalCount} XPaths verified · WPP:${probe.wpp?.isReady?"ready":"fallback"}`};}
    return{error:"Unsupported operation"};
  })().then(reply).catch(e=>reply({error:e.message}));return true;
});
if (typeof chrome?.runtime?.getManifest === "function") {
  chrome.storage.local.set({failureCount:0,retryAt:0,lastError:null}).catch(()=>null);
  arm();
  scan(true);
}
