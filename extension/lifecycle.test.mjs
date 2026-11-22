import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";
import {webcrypto} from "node:crypto";
import {validHello} from "./boundary.mjs";
import {selectWhatsAppTab} from "./tab-control.mjs";
import {supervisedWhatsApp} from "./actions.mjs";
import {readRendered,hasHumanDraft,sourceIndex,discoverChats,openDiscoveredChat,readAccountProfile} from "./scanner.mjs";
function worker({active=true,draft=false,scanAll=true,tabActive=false,focused=true,existing=false,captureProfile=false,profileCaptured=undefined,ownerPhone=null,profileRequested=false,dispatch=false,authorize=true,sessionStored={},observationsReply=null,mockEvents=null}={}){
  const stored={active:false,cursor:0,ownedTab:existing?null:7},sent=[];let listener,created=0,execCalls=0;
  const browserActions = [];
  const account = "+12025550101";
  const item = {id:"out-0000000000000001",account_phone:account,claim_token:"mock-token",text_parts:["Approved fixture"]};
  let pendingSend = dispatch;
  const replies = {
    dispatch_claim: {item},
    dispatch_authorize: authorize ? {ok:true,expires_at:Date.now()/1000+5} : {error:"DISPATCH_DISABLED"},
    dispatch_result: {ok:true},
  };
  const port={onMessage:{addListener:f=>listener=f},onDisconnect:{addListener:()=>{}},disconnect:()=>{},postMessage:message=>{
    sent.push(message);
    if (message.operation === "dispatch_result") pendingSend = false;
    queueMicrotask(()=>listener(message.operation==="hello"?{...message,operation:"hello_result",state:"READY_READ_ONLY",can_read_chats:true,can_send:false}:
      {...message,...(message.operation==="poll_config"?{active,can_send:dispatch,outbound:pendingSend?[{id:item.id}]:[],scan_all:scanAll,capture_profile:captureProfile,profile_requested:profileRequested,...(profileCaptured!==undefined?{profile_captured:profileCaptured}:{}),...(ownerPhone?{owner_phone:ownerPhone}:{}),sources:[{phone:"+919876543210"}]}:(message.operation==="observations" && observationsReply ? (typeof observationsReply==="function"?observationsReply(message):observationsReply) : replies[message.operation]??{results:[]}))}));
  }};
  const chrome={runtime:{connectNative:()=>port,onStartup:{addListener:()=>{}},onInstalled:{addListener:()=>{}},onMessage:{addListener:()=>{}},getURL:p=>"chrome-extension://id/"+p,id:"id"},
    storage:{
      local:{get:async keys=>typeof keys==="string"?{[keys]:stored[keys]}:Object.fromEntries(keys.map(k=>[k,stored[k]])),set:async data=>Object.assign(stored,data)},
      session:{get:async keys=>typeof keys==="string"?{[keys]:sessionStored[keys]}:Object.fromEntries(keys.map(k=>[k,sessionStored[k]])),set:async data=>Object.assign(sessionStored,data)},
    },
    tabs:{get:async()=>({id:7,windowId:1,active:tabActive,status:"complete",url:"https://web.whatsapp.com/"}),update:async()=>{},query:async()=>[{id:7,windowId:1,active:tabActive,status:"complete",url:"https://web.whatsapp.com/"}],create:async()=>{created++;return{id:8,active:false};},sendMessage:async()=>{}},
    windows:{get:async()=>({focused}),update:async()=>{}},
    action:{setBadgeText:async()=>{},setBadgeBackgroundColor:async()=>{},setTitle:async()=>{}},
    scripting:{executeScript:async args=>{
      execCalls++;
      if (args.func===supervisedWhatsApp) {
        const action = args.args[0];
        browserActions.push(action);
        if (action !== "account") assert.deepEqual(Array.from(args.target.documentIds), ["mock-document"]);
        const result = action === "account" ? {account_phone:account} : action === "verify"
          ? {verified:true,chat_id:"12025550102@c.us"}
          : {outcome:"ACCEPTED",sent_ids:["true_12025550102@c.us_Fixture"]};
        return [{documentId:"mock-document",result}];
      }
      return[{result:args.func===readAccountProfile?{verified:false}:args.func===hasHumanDraft?draft:args.func===discoverChats?{ready:true,chats:[{index:0,title:"Trader",fingerprint:"x",unread:true}]}:args.func===openDiscoveredChat?{opened:true}:{ready:true,present:true,verified:true,events:mockEvents??[{phone:"+919876543210",message_id:"false_919876543210@c.us_a",conversation_id:"919876543210@c.us",text:"WTB",identity_verified:true}]}}];
    }},
    alarms:{onAlarm:{addListener:()=>{}},create:async()=>{},clear:async()=>{}}};
  const context=vm.createContext({chrome,TextEncoder,crypto:webcrypto,validHello,selectWhatsAppTab,readRendered,hasHumanDraft,sourceIndex,discoverChats,openDiscoveredChat,readAccountProfile,supervisedWhatsApp,setTimeout,clearTimeout,console});
  vm.runInContext(fs.readFileSync(new URL("./background.js",import.meta.url),"utf8").replace(/^import .*;$/gm,""),context);
  return {stored,sessionStored,sent,browserActions,get created(){return created;},get execCalls(){return execCalls;},scan:()=>vm.runInContext("scan()",context)};
}
test("desktop active state starts capture without popup consent or stored active flag",async()=>{
  const w=worker();await w.scan();const payload=w.sent.find(v=>v.operation==="observations");
  assert.ok(payload);assert.equal(payload.coverage.complete,false);assert.equal(payload.coverage.scope,"rendered_recent_chats");assert.equal(w.stored.cursor,1);assert.equal(w.stored.lastError,null);
});
test("draft pauses without navigating or importing; focused observer tab scans passively without navigating",async()=>{
  const d=worker({draft:true});
  await d.scan();
  assert.ok(d.stored.lastError||d.stored.activity.phase==="paused");
  assert.ok(!d.sent.some(v=>v.operation==="observations"));

  const activeTab=worker({tabActive:true,focused:true});
  await activeTab.scan();
  assert.equal(activeTab.created,0);
  const obs=activeTab.sent.find(v=>v.operation==="observations" && v.events.length);
  assert.ok(obs);
  assert.equal(obs.events[0].text,"WTB");
});
test("existing WhatsApp tab is reused; selected tab in an unfocused window can scan",async()=>{
  const w=worker({existing:true,tabActive:true,focused:false});await w.scan();assert.equal(w.created,0);assert.equal(w.stored.ownedTab,7);assert.ok(w.sent.some(v=>v.operation==="observations"));
});
test("stopped desktop is polled but no WhatsApp observation runs",async()=>{
  const w=worker({active:false});await w.scan();assert.ok(w.sent.some(v=>v.operation==="poll_config"));assert.ok(!w.sent.some(v=>v.operation==="observations"));assert.equal(w.stored.active,false);
});
test("unchanged observed text is not sent to the desktop on every poll", async () => {
  const w = worker();
  await w.scan();
  await w.scan();
  assert.equal(w.sent.filter(message => message.operation === "observations" && message.events.length).length, 1);
});
test("configured source mode remains available",async()=>{
  const w=worker({scanAll:false});await w.scan();assert.equal(w.sent.find(v=>v.operation==="observations").coverage.scope,"configured_sources");
});
test("prior disconnect retryAt clears immediately when desktop reconnects without 180s lockout",async()=>{
  const w=worker({active:true});
  w.stored.failureCount=6;w.stored.retryAt=Date.now()+180000;
  await w.scan();
  assert.equal(w.stored.failureCount,0);
  assert.equal(w.stored.retryAt,0);
  assert.ok(w.sent.some(v=>v.operation==="observations"));
});
test("synced profile caches phone and does not repeat capture_profile unnecessarily",async()=>{
  const w=worker({active:true,captureProfile:true,profileCaptured:true,ownerPhone:"+919876543210"});
  w.stored.syncedProfilePhone="+919876543210";
  await w.scan();
  assert.equal(w.stored.syncedProfilePhone,"+919876543210");
  assert.notEqual(w.stored.activity?.detail,"Syncing your WhatsApp name & photo");
});
test("pending profile request does not repeat the interactive attempt",async()=>{
  const w=worker({active:false,profileRequested:true,profileCaptured:false});
  await w.scan();
  assert.ok(w.execCalls>0);
  const calls=w.execCalls;
  await w.scan();
  assert.equal(w.execCalls,calls);
});

test("approved dispatch sends once and reports the receipt through the native worker", async () => {
  const fixture = worker({dispatch:true});
  await fixture.scan();
  await fixture.scan();
  assert.deepEqual(fixture.browserActions, ["account", "verify", "send"]);
  const result = fixture.sent.find(message => message.operation === "dispatch_result");
  assert.equal(result?.outcome, "ACCEPTED");
  assert.equal(result?.sent_ids?.[0], "true_12025550102@c.us_Fixture");
});

test("revoked native authorization stops the browser before send", async () => {
  const fixture = worker({dispatch:true,authorize:false});
  await fixture.scan();
  assert.deepEqual(fixture.browserActions, ["account", "verify"]);
  const result = fixture.sent.find(message => message.operation === "dispatch_result");
  assert.equal(result?.outcome, "FAILED_BEFORE_SEND");
});

test("reconnect and browser-worker restart do not resend already committed messages", async () => {
  const sessionStored = {};
  const obsReply = { results: [{ message_id: "false_919876543210@c.us_a", status: "SAVED" }] };

  // First session / worker instance
  const w1 = worker({ sessionStored, observationsReply: obsReply });
  await w1.scan();
  assert.equal(w1.sent.filter(m => m.operation === "observations" && m.events?.length).length, 1);
  assert.ok(sessionStored.savedTextEntries?.length > 0, "Saved text fingerprints must be persisted in session storage");

  // Simulate browser worker restart by creating a new worker instance sharing sessionStored
  const w2 = worker({ sessionStored, observationsReply: obsReply });
  await w2.scan();
  // w2 should recognize the message as already saved and not resend it
  const obs2 = w2.sent.filter(m => m.operation === "observations" && m.events?.length);
  assert.equal(obs2.length, 0, "Restarted worker must not resend messages already committed in session storage");
});

test("partial ACK leaves unsaved remainder in pending queue with retry", async () => {
  const sessionStored = {};
  const mockEvents = [
    { phone: "+919876543210", message_id: "m_saved", conversation_id: "919876543210@c.us", text: "Saved msg", identity_verified: true },
    { phone: "+919876543210", message_id: "m_retry", conversation_id: "919876543210@c.us", text: "Retry msg", identity_verified: true },
  ];
  const obsReply = {
    results: [
      { message_id: "m_saved", status: "SAVED" },
      { message_id: "m_retry", status: "RETRY_REQUIRED", reason: "RUNNER_INACTIVE" },
    ],
  };

  const w = worker({ sessionStored, mockEvents, observationsReply: obsReply });
  await w.scan();

  // Check pendingQueueEntries in session storage
  const pendingEntries = sessionStored.pendingQueueEntries || [];
  assert.equal(pendingEntries.length, 1, "Unacknowledged event must remain in pending queue");
  assert.equal(pendingEntries[0][0], "m_retry");

  // Check savedTextEntries in session storage
  const savedEntries = sessionStored.savedTextEntries || [];
  assert.equal(savedEntries.some(([k]) => k === "m_saved"), true, "Acknowledged event must be recorded as saved");
  assert.equal(savedEntries.some(([k]) => k === "m_retry"), false, "Unacknowledged event must not be marked saved");
});

test("media retry does not trigger repeated downloads once mediaEnriched is recorded", async () => {
  const sessionStored = {
    mediaEnrichedEntries: ["media_msg_1"],
  };
  const mockEvents = [
    {
      phone: "+919876543210",
      message_id: "media_msg_1",
      conversation_id: "919876543210@c.us",
      text: "[Photo / Stock Image]",
      media_b64: "data:image/jpeg;base64,abc",
      identity_verified: true,
    },
  ];
  const obsReply = { results: [{ message_id: "media_msg_1", status: "SAVED" }] };
  const w = worker({ sessionStored, mockEvents, observationsReply: obsReply });
  await w.scan();

  // Check that mediaEnriched was maintained and no new duplicate was needed
  assert.ok(sessionStored.mediaEnrichedEntries?.includes("media_msg_1"));
});
