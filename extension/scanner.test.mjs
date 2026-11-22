import test from "node:test";
import assert from "node:assert/strict";
import {sourceIndex,readRendered,hasHumanDraft,discoverChats,openDiscoveredChat,readAccountProfile,readWppStoreProfile,readWppStoreMessages,resolveLidPhoneViaContactInfo} from "./scanner.mjs";

test("Store maps a configured LID and retains separate group participant identity", async () => {
  const previousWindow = globalThis.window;
  const lid = "12345678901234@lid";
  const group = "12036300000000@g.us";
  const phone = "919811112222@c.us";
  try {
    globalThis.window = {WPP: {
      isReady: true,
      conn: {isAuthenticated: () => true},
      contact: {getPnLidEntry: async () => ({phoneNumber: {_serialized: phone}})},
      chat: {
        list: async () => [lid, group].map(id => ({id, unreadCount: 1})),
        getMessages: async id => [
          {id: {id: "in", fromMe: false, participant: lid}, body: "WTB iPhone", t: 1800001000},
          {id: `true_${id}_out`, body: "Outbound", t: 1800001000},
        ],
      },
    }};
    const configured = await readWppStoreMessages(1800000000, ["+919811112222"]);
    assert.deepEqual(configured.events.map(e => [e.conversation_id, e.phone, !!e.outgoing]), [
      [lid, "+919811112222", false],
      [lid, "+919811112222", true],
    ]);
    assert.equal(configured.events[1].message_id, `true_${lid}_out`);
    assert.equal((await readWppStoreMessages(1800000000, [])).events.length, 0);
    const all = await readWppStoreMessages(1800000000, null);
    assert.equal(all.events.length, 3);
    assert.equal(all.events.find(e => e.conversation_id === group).sender_id, lid);
  } finally {
    globalThis.window = previousWindow;
  }
});
test("fast capture uses locally cached text and defers attachment download", async t => {
  const previous = globalThis.window;
  t.after(() => { globalThis.window = previous; });
  const jid = "12025550199@c.us";
  const phone = "+12025550199";
  let fetches = 0;
  let downloads = 0;
  globalThis.window = {WPP: {
    isReady: true,
    conn: {isAuthenticated: () => true},
    chat: {
      list: async () => [{id: jid, unreadCount: 1, msgs: {getModelsArray: () => [
        {id: {id:"txt1"}, type:"chat", body:"WTB an iPhone", t:1800001000},
        {id: {id:"stock1"}, type:"document", filename:"stock.xlsx", t:1800001001},
      ]}}],
      getMessages: async () => { fetches++; return []; },
      downloadMedia: async () => { downloads++; return null; },
    },
  }};
  const result = await readWppStoreMessages(1800000000, [phone], "Jarvis", phone, false);
  assert.equal(result.needs_media, true);
  assert.equal(result.events[0].text, "WTB an iPhone");
  assert.equal(result.events[1].stock_file_name, "stock.xlsx");
  assert.equal(fetches, 0);
  assert.equal(downloads, 0);
});
function row(id,text,name="Trader"){
  return {getAttribute:key=>key==="data-id"?id:null,querySelector:selector=>selector===".selectable-text"?{textContent:text}:selector==="[data-pre-plain-text]"?{getAttribute:()=>`[10:30, 9/11/2026] ${name}: `}:null};
}
function page(rows,draft="",title="Trader"){
  const main={querySelector:selector=>selector==="header"?{querySelector:()=>({getAttribute:()=>title})}:{textContent:draft},querySelectorAll:()=>rows};
  globalThis.document={getElementById:()=>main,querySelector:()=>({textContent:draft})};
}
test("configured source excludes outgoing, unrelated, group and LID messages",()=>{
  page([row("true_919876543210@c.us_a","self reply"),row("false_911111111111@c.us_b","other"),row("false_123456789@lid_c","LID"),row("false_123456789@g.us_d","group"),row("false_919876543210@c.us_e","WTB S25")]);
  assert.deepEqual(readRendered("+919876543210").events.map(e=>e.text),["WTB S25"]);
});
test("all-chat mode preserves stable LID identity without inventing a phone",()=>{
  page([row("false_123456789@lid_ABC123","WTS S25","Sender name")]);
  const e=readRendered().events[0];
  assert.equal(e.phone,null);assert.equal(e.conversation_id,"123456789@lid");assert.equal(e.sender_id,e.conversation_id);assert.equal(e.sender_name,"Sender name");
});
test("group message binds phone to participant and retains separate conversation",()=>{
  page([row("false_123456789@g.us_ABC123_919876543210@c.us","WTB","Supplier")]);
  const e=readRendered().events[0];assert.equal(e.chat_kind,"group");assert.equal(e.phone,"+919876543210");assert.equal(e.sender_id,"919876543210@c.us");
  page([row("false_123456789@g.us_ABC123","WTB")]);assert.equal(readRendered().events[0].phone,null);
});
test("mixed chats, human draft, loading header and malformed identity fail closed",()=>{
  page([row("false_919876543210@c.us_a","WTB")],"unfinished");
  assert.equal(hasHumanDraft(),true);assert.equal(readRendered().verified,false);
  page([row("false_919876543210@c.us_a","WTB")]);assert.equal(readRendered(null,"Other").verified,false);
  page([row("false_919876543210@c.us_a","WTB"),row("false_919876543211@c.us_b","WTS")]);assert.equal(readRendered().verified,false);
  page([row("false_display-name@lid_a","private")]);assert.equal(readRendered().verified,false);assert.equal(readRendered(".*").verified,false);
});
test("stable DOM message identity deduplicates nested records",()=>{
  page([row("false_919876543210@c.us_a","WTB"),row("false_919876543210@c.us_a","WTB")]);assert.equal(readRendered().events.length,1);
});
test("source rotation remains bounded after corrupt storage",()=>{
  assert.equal(sourceIndex(999,3),0);assert.equal(sourceIndex(-1,3),0);assert.equal(sourceIndex("bad",3),0);assert.throws(()=>sourceIndex(0,0));
});
test("discovery reports limited coverage and click rechecks visibility, draft and row fingerprint",()=>{
  let clicks=0;const candidate={getBoundingClientRect:()=>({height:60}),textContent:"Trader WTB",querySelector:s=>s==='span[title]'?{getAttribute:()=>"Trader"}:null,click:()=>clicks++};
  const pane={querySelectorAll:()=>[candidate]};
  globalThis.document={getElementById:()=>pane,querySelector:()=>null,hasFocus:()=>false};
  const result=discoverChats();assert.equal(result.complete,false);assert.equal(result.chats.length,1);
  assert.equal(openDiscoveredChat(result.chats[0]).opened,true);assert.equal(clicks,1);
  document.hasFocus=()=>true;assert.equal(openDiscoveredChat(result.chats[0]).opened,false);
  document.hasFocus=()=>false;candidate.textContent="Changed";assert.equal(openDiscoveredChat(result.chats[0]).opened,false);
});

test("readAccountProfile extracts owner name from nested title span and SVG mask profile image",async()=>{
  let closed=0;
  const backBtn={click:()=>{closed++;}};
  const svgImg={
    closest:()=>({getAttribute:k=>k==="width"?"120":null}),
    getAttribute:k=>k==="xlink:href"?"https://media-bom5-2.cdn.whatsapp.net/v/t61.24694-24/avatar.jpg":null,
  };
  const labelSpan={textContent:"Your name",children:[],parentElement:null};
  const nameSpan={getAttribute:k=>k==="title"?"Parth Sancheti":null,textContent:"Parth Sancheti"};
  const wrapper={querySelectorAll:s=>s.includes("span[dir=\"auto\"]")?[nameSpan]:[]};
  labelSpan.parentElement=wrapper;
  wrapper.parentElement=wrapper;
  const pane={
    querySelector:s=>s==='button[aria-label="Back"], [data-testid="btn-closer-drawer"], header button'?backBtn:null,
    querySelectorAll:s=>{
      if(s==="svg image")return [svgImg];
      if(s==="img")return [];
      if(s==="span, div")return [labelSpan];
      if(s.includes("span[dir=\"auto\"]"))return [nameSpan];
      return [];
    },
  };
  globalThis.document={querySelector:s=>s.includes("profile-drawer")?pane:null,querySelectorAll:()=>[]};
  const prof=await readAccountProfile(null,false);
  assert.equal(prof.verified,true);
  assert.equal(prof.name,"Parth Sancheti");
  assert.equal(prof.avatar_url,"https://media-bom5-2.cdn.whatsapp.net/v/t61.24694-24/avatar.jpg");
});

test("readAccountProfile clicks wds-ic-chat menu button back to chats after reading profile",async()=>{
  let chatMenuClicks=0;
  const chatBtn={click:()=>{chatMenuClicks++;}};
  const chatTitleNode={textContent:"wds-ic-chat",closest:()=>chatBtn,parentElement:chatBtn};
  const svgImg={
    closest:()=>({getAttribute:k=>k==="width"?"120":null}),
    getAttribute:k=>k==="xlink:href"?"https://media-bom5-2.cdn.whatsapp.net/v/t61.24694-24/avatar.jpg":null,
  };
  const nameSpan={getAttribute:k=>k==="title"?"Parth Sancheti":null,textContent:"Parth Sancheti"};
  const pane={
    querySelector:()=>null,
    querySelectorAll:s=>{
      if(s==="svg image")return [svgImg];
      if(s==="img")return [];
      if(s==="span, div")return [];
      if(s.includes("span[dir=\"auto\"]"))return [nameSpan];
      return [];
    },
  };
  globalThis.document={
    querySelector:s=>s.includes("profile-drawer")?pane:null,
    querySelectorAll:s=>s==="svg title"?[chatTitleNode]:[],
  };
  const prof=await readAccountProfile(null,true);
  assert.equal(prof.verified,true);
  assert.equal(chatMenuClicks,1);
});

test("readAccountProfile findPane walks past immediate div[class] avatar wrapper to drawer",async()=>{
  const backBtn={click:()=>{}};
  const nameSpan={getAttribute:k=>k==="title"?"Parth Sancheti":null,textContent:"Parth Sancheti"};
  const svgParent={closest:()=>null,parentElement:null,textContent:""};
  const svgNode={getAttribute:k=>k==="width"?"120":null,parentElement:svgParent};
  const svgImg={
    parentElement:svgNode,
    closest:()=>svgNode,
    getAttribute:k=>k==="xlink:href"?"https://media-bom5-2.cdn.whatsapp.net/v/t61.24694-24/avatar.jpg":null,
  };
  const drawer={
    textContent:"Profile Your name Parth Sancheti",
    matches:()=>false,
    closest:()=>null,
    querySelector:()=>null,
    querySelectorAll:s=>{
      if(s==="svg image")return [svgImg];
      if(s==="img")return [];
      if(s==="span, div")return [];
      if(s.includes("span[dir=\"auto\"]"))return [nameSpan];
      return [];
    },
    parentElement:null,
  };
  svgParent.parentElement=drawer;
  svgParent.closest=()=>svgParent;
  globalThis.document={
    body:{},
    documentElement:{},
    querySelector:()=>null,
    querySelectorAll:()=>[],
    evaluate:expr=>{
      const arr=expr.includes("120")?[svgImg]:[];
      return {snapshotLength:arr.length,snapshotItem:i=>arr[i]};
    },
  };
  globalThis.XPathResult={ORDERED_NODE_SNAPSHOT_TYPE:7};
  try{
    const prof=await readAccountProfile(null,false);
    assert.equal(prof.verified,true);
    assert.equal(prof.name,"Parth Sancheti");
  }finally{
    delete globalThis.XPathResult;
  }
});

test("readWppStoreProfile & readWppStoreMessages filter by timestamp watermark cleanly",async()=>{
  const prevWindow=globalThis.window;
  try{
    globalThis.window={
      WPP:{
        isReady:true,
        conn:{isAuthenticated:()=>true,getMyUserId:()=>({_serialized:"919876543210@c.us"})},
        profile:{
          getMyProfileName:async()=>"Parth Sancheti",
          getMyProfilePicture:async()=>({eurl:"https://media-bom5-2.cdn.whatsapp.net/v/t61.24694-24/pfp.jpg"}),
        },
        chat:{
          list:async()=>[{id:{_serialized:"919811112222@c.us"},name:"Buyer One",unreadCount:1,t:1800001000}],
          getMessages:async()=>[
            {id:{id:"old1",fromMe:false},body:"Old offer",t:1800000100},
            {id:{id:"self1",fromMe:true},body:"My reply",t:1800000900},
            {id:{id:"new1",fromMe:false},body:"WTB 10 iPhone 17 Pro",t:1800000800,notifyName:"Rohan"},
          ],
        },
      },
    };
    const p=await readWppStoreProfile(null);
    assert.equal(p.verified,true);
    assert.equal(p.name,"Parth Sancheti");
    assert.equal(p.phone,"+919876543210");
    assert.equal(p.source,"wpp_store");

    const feed=await readWppStoreMessages(1800000500,null);
    assert.equal(feed.verified,true);
    assert.equal(feed.events.length,2);
    assert.equal(feed.events[0].text,"WTB 10 iPhone 17 Pro");
    assert.equal(feed.events[0].timestamp,1800000800);
    assert.equal(feed.events[0].message_id,"false_919811112222@c.us_new1");
    assert.equal(feed.events[1].outgoing,true);
    assert.equal(feed.events[1].message_id,"true_919811112222@c.us_self1");

    globalThis.window.WPP.conn.getMyUserLid=()=>({_serialized:"12345678901234@lid"});
    globalThis.window.WPP.chat.list=async()=>[
      {id:{_serialized:"12345678901234@lid"},name:"Parth (You)",unreadCount:0,t:1800001000},
      {id:{_serialized:"919811112222@c.us"},name:"Buyer One",unreadCount:1,t:1800001000},
    ];
    globalThis.window.WPP.chat.getMessages=async cid=>cid.endsWith("@lid")
      ? [
          {id:{id:"cmd1",fromMe:true},body:"@Jarvis Make a banner",type:"chat",t:1800000950},
          {id:{id:"reply1",fromMe:true},body:"Here is your banner.",type:"chat",t:1800000960},
        ]
      : [{id:{id:"img1",fromMe:false},type:"image",isMedia:true,mimetype:"image/jpeg",body:"/9j/"+"A".repeat(120),t:1800000850}];
    const mediaFeed=await readWppStoreMessages(1800000500,null);
    assert.equal(mediaFeed.events.length,1);
    assert.equal(mediaFeed.events[0].text,"[Photo / Stock Image]");
    assert.ok(mediaFeed.events[0].media_b64?.startsWith("/9j/"));
    assert.equal(mediaFeed.agent_commands.length,1);
    assert.equal(mediaFeed.agent_commands[0].text,"@Jarvis Make a banner");
    assert.equal(mediaFeed.agent_commands[0].self_chat,true);

    const rowOld=row("false_919876543210@c.us_olddom","Ancient message");
    const rowNew={
      getAttribute:k=>k==="data-id"?"false_919876543210@c.us_newdom":null,
      querySelector:s=>s===".selectable-text"?{textContent:"Fresh message"}:s==="[data-pre-plain-text]"?{getAttribute:()=>"[10:30, 11/11/2026] Buyer: "}:null,
    };
    page([rowOld,rowNew]);
    const cutoff=Math.floor(new Date(2026,10,10,12,0,0).getTime()/1000);
    const rendered=readRendered(null,null,cutoff);
    assert.equal(rendered.verified,true);
    assert.deepEqual(rendered.events.map(e=>e.text),["Fresh message"]);
  }finally{
    globalThis.window=prevWindow;
  }
});

test("resolveLidPhoneViaContactInfo clicks contact info button and normalizes +CC phone",async()=>{
  const prevWindow=globalThis.window;
  let infoClicked=false,closed=false,drawerOpen=false;
  const drawer={
    textContent:"Contact info PR. RAHUL AJIT BURAD",
    querySelectorAll:s=>s==="span"?[{textContent:"PR. RAHUL AJIT BURAD"},{textContent:"+91 90287 94222"}]:[],
    querySelector:s=>s.includes("Close")?{click:()=>{closed=true;drawerOpen=false;}}:null,
  };
  const infoBtn={click:()=>{infoClicked=true;drawerOpen=true;}};
  globalThis.window={WPP:{chat:{openChatBottom:async()=>{}}}};
  globalThis.document={
    getElementById:id=>id==="main"?{querySelector:()=>null}:null,
    querySelector:s=>{
      if(s.includes("conversation-info-header-button"))return infoBtn;
      if(s.includes("contact-info-drawer") && drawerOpen)return drawer;
      return null;
    },
    querySelectorAll:()=>[],
  };
  try{
    const res=await resolveLidPhoneViaContactInfo("179126040305838@lid");
    assert.equal(infoClicked,true);
    assert.equal(closed,true);
    assert.equal(res.ok,true);
    assert.equal(res.phone,"+919028794222");
  }finally{
    globalThis.window=prevWindow;
  }
});

test("live new_message event pulls in a chat outside the recent list and loads unread history", async t => {
  const previous = globalThis.window;
  t.after(() => { globalThis.window = previous; });
  let handler = null;
  let fetched = 0;
  const archived = "919811111111@c.us";
  const archivedChat = {id: archived, unreadCount: 3, t: 1800002000, msgs: {getModelsArray: () => [
    {id: {id: "m3"}, type: "chat", body: "WTS S25 Ultra 256GB 40 pcs", t: 1800002000},
  ]}};
  globalThis.window = {WPP: {
    isReady: true,
    on: (name, fn) => { if (name === "chat.new_message") handler = fn; },
    conn: {isAuthenticated: () => true},
    chat: {
      list: async () => [],
      get: async id => id === archived ? archivedChat : null,
      getMessages: async () => { fetched++; return [
        {id: {id: "m1"}, type: "chat", body: "Ready stock iPhone 16 128GB", t: 1800001900},
        {id: {id: "m2"}, type: "chat", body: "Price USD 610", t: 1800001950},
        {id: {id: "m3"}, type: "chat", body: "WTS S25 Ultra 256GB 40 pcs", t: 1800002000},
      ]; },
    },
  }};
  globalThis.document = {dispatchEvent: () => {}};
  // First read registers the listener; the chat list is empty.
  const first = await readWppStoreMessages(1800000000, null, "Jarvis", null, false);
  assert.equal(first.events.length, 0);
  handler({id: {remote: archived}});
  const second = await readWppStoreMessages(1800000000, null, "Jarvis", null, false);
  assert.equal(fetched, 1);
  assert.deepEqual(second.events.map(e => e.text), ["Ready stock iPhone 16 128GB", "Price USD 610", "WTS S25 Ultra 256GB 40 pcs"]);
  assert.equal(second.events[0].phone, "+919811111111");
});

test("missing or throwing getMyUserLid leaves readWppStoreMessages working and reports capabilities", async () => {
  const previousWindow = globalThis.window;
  try {
    globalThis.window = {
      WPP: {
        isReady: true,
        conn: {
          isAuthenticated: () => true,
          getMyUserId: () => ({ _serialized: "919876543210@c.us" }),
          getMyUserLid: () => { throw new Error("The account LID is unavailable"); },
        },
        chat: {
          list: async () => [{ id: "919811112222@c.us", unreadCount: 1, t: 1800001000 }],
          getMessages: async () => [
            { id: { id: "m1", fromMe: false }, body: "WTB 10 iPhone 16", t: 1800001000 },
          ],
        },
      },
    };
    const result = await readWppStoreMessages(1800000000, null);
    assert.equal(result.verified, true);
    assert.equal(result.events.length, 1);
    assert.equal(result.events[0].text, "WTB 10 iPhone 16");
    assert.equal(result.capabilities.self_wid, true);
    assert.equal(result.capabilities.self_lid, false);
  } finally {
    globalThis.window = previousWindow;
  }
});

test("old unread message before floor is excluded even when unread is true", async () => {
  const previousWindow = globalThis.window;
  try {
    globalThis.window = {
      WPP: {
        isReady: true,
        conn: { isAuthenticated: () => true },
        chat: {
          list: async () => [{ id: "919811112222@c.us", unreadCount: 5, t: 1700000000 }],
          getMessages: async () => [
            { id: { id: "m_old", fromMe: false }, body: "Old offer", t: 1700000000 },
          ],
        },
      },
    };
    const result = await readWppStoreMessages(1800000000, null);
    assert.equal(result.verified, true);
    assert.equal(result.events.length, 0, "Old unread message must be excluded when before floor");
  } finally {
    globalThis.window = previousWindow;
  }
});

test("missing or unparseable DOM timestamp is excluded when floor is active", () => {
  const rowValid = {
    getAttribute: k => k === "data-id" ? "false_919876543210@c.us_validdom" : null,
    querySelector: s => s === ".selectable-text" ? { textContent: "Valid timestamp msg" } : s === "[data-pre-plain-text]" ? { getAttribute: () => "[10:30, 11/11/2026] Buyer: " } : null,
  };
  const rowMissingTs = {
    getAttribute: k => k === "data-id" ? "false_919876543210@c.us_notsdom" : null,
    querySelector: s => s === ".selectable-text" ? { textContent: "No timestamp msg" } : null,
  };
  const rowUnparseableTs = {
    getAttribute: k => k === "data-id" ? "false_919876543210@c.us_badtsdom" : null,
    querySelector: s => s === ".selectable-text" ? { textContent: "Bad timestamp msg" } : s === "[data-pre-plain-text]" ? { getAttribute: () => "[unparseable] Buyer: " } : null,
  };
  page([rowValid, rowMissingTs, rowUnparseableTs]);
  const cutoff = Math.floor(new Date(2026, 10, 10, 12, 0, 0).getTime() / 1000);
  const rendered = readRendered(null, null, cutoff);
  assert.equal(rendered.verified, true);
  assert.equal(rendered.events.length, 1);
  assert.equal(rendered.events[0].text, "Valid timestamp msg");
  assert.equal(rendered.filtered_old, 2, "Uncertain and missing timestamps must fail closed and be filtered");
});

test("failure after extracting dirty inbox does not lose unread chat IDs in __jarvisInbox", async () => {
  const previousWindow = globalThis.window;
  try {
    globalThis.window = {
      __jarvisInbox: [
        { chatId: "dirty1@c.us", at: Date.now() },
        { chatId: "dirty2@c.us", at: Date.now() },
      ],
      WPP: {
        isReady: true,
        conn: { isAuthenticated: () => true },
        chat: {
          list: async () => { throw new Error("Network transient failure"); },
        },
      },
    };
    const result = await readWppStoreMessages(1800000000, null);
    assert.equal(result.verified, false);
    assert.equal(globalThis.window.__jarvisInbox.length, 2, "Inbox dirty chats must not be cleared on failure");
    assert.deepEqual(globalThis.window.__jarvisInbox.map(e => e.chatId), ["dirty1@c.us", "dirty2@c.us"]);
  } finally {
    globalThis.window = previousWindow;
  }
});

test("batch of >120 new WPP messages drains oldest-first with continuation", async () => {
  const previousWindow = globalThis.window;
  try {
    const totalMsgs = 150;
    const msgs = [];
    for (let i = 1; i <= totalMsgs; i++) {
      msgs.push({
        id: { id: `msg_${i}`, fromMe: false },
        body: `Order ${i}`,
        t: 1800000000 + i,
      });
    }
    msgs.sort(() => Math.random() - 0.5);
    globalThis.window = {
      WPP: {
        isReady: true,
        conn: { isAuthenticated: () => true },
        chat: {
          list: async () => [{ id: "919811112222@c.us", unreadCount: totalMsgs, t: 1800000150 }],
          getMessages: async () => msgs,
        },
      },
    };
    const result = await readWppStoreMessages(1800000000, null);
    assert.equal(result.verified, true);
    assert.equal(result.events.length, 120, "Batch is bounded to 120");
    assert.equal(result.has_more, true);
    assert.equal(result.total_events, 150);
    assert.equal(result.events[0].text, "Order 1");
    assert.equal(result.events[0].timestamp, 1800000001);
    assert.equal(result.events[119].text, "Order 120");
    assert.equal(result.events[119].timestamp, 1800000120);
  } finally {
    globalThis.window = previousWindow;
  }
});

test("batch of >20 rendered DOM messages drains oldest-first with continuation", () => {
  const rows = [];
  for (let i = 1; i <= 25; i++) {
    const minStr = String(i).padStart(2, "0");
    rows.push({
      getAttribute: k => k === "data-id" ? `false_919876543210@c.us_m${i}` : null,
      querySelector: s => s === ".selectable-text" ? { textContent: `DOM Order ${i}` } : s === "[data-pre-plain-text]" ? { getAttribute: () => `[10:${minStr}, 11/11/2026] Trader: ` } : null,
    });
  }
  page(rows.reverse());
  const cutoff = Math.floor(new Date(2026, 10, 11, 10, 0, 0).getTime() / 1000);
  const rendered = readRendered(null, null, cutoff);
  assert.equal(rendered.verified, true);
  assert.equal(rendered.events.length, 20);
  assert.equal(rendered.has_more, true);
  assert.equal(rendered.total_events, 25);
  assert.equal(rendered.events[0].text, "DOM Order 1");
  assert.equal(rendered.events[19].text, "DOM Order 20");
});

test("chat with >80 unread messages requests an appropriately sized page covering unread", async () => {
  const previousWindow = globalThis.window;
  let requestedCount = 0;
  try {
    globalThis.window = {
      WPP: {
        isReady: true,
        conn: { isAuthenticated: () => true },
        chat: {
          list: async () => [{ id: "919811112222@c.us", unreadCount: 95, t: 1800001000 }],
          getMessages: async (id, opts) => {
            requestedCount = opts?.count;
            return [{ id: { id: "m1", fromMe: false }, body: "High unread msg", t: 1800001000 }];
          },
        },
      },
    };
    const result = await readWppStoreMessages(1800000000, null);
    assert.equal(result.verified, true);
    assert.equal(requestedCount, 100, "Count requested must cover unreadCount (95 + 5 = 100)");
  } finally {
    globalThis.window = previousWindow;
  }
});

test("configured-source LID chat is resolved to phone before filtering and included", async () => {
  const previousWindow = globalThis.window;
  const lid = "179126040305838@lid";
  const phone = "+919028794222";
  try {
    globalThis.window = {
      WPP: {
        isReady: true,
        conn: { isAuthenticated: () => true },
        contact: {
          getPnLidEntry: async (id) => id === lid ? { phoneNumber: { _serialized: "919028794222@c.us" } } : null,
        },
        chat: {
          list: async () => [{ id: lid, unreadCount: 1, t: 1800001000 }],
          getMessages: async () => [
            { id: { id: "lid_m1", fromMe: false }, body: "WTB 50 iPhone 16", t: 1800001000 },
          ],
        },
      },
    };
    const result = await readWppStoreMessages(1800000000, [phone]);
    assert.equal(result.verified, true);
    assert.equal(result.events.length, 1);
    assert.equal(result.events[0].conversation_id, lid);
    assert.equal(result.events[0].phone, phone);
    assert.equal(result.events[0].text, "WTB 50 iPhone 16");
  } finally {
    globalThis.window = previousWindow;
  }
});

test("DOM inclusive boundary accepts exact timestamp", () => {
  const targetDate = new Date(2026, 10, 11, 10, 30, 0);
  const cutoff = Math.floor(targetDate.getTime() / 1000);
  const rowExact = {
    getAttribute: k => k === "data-id" ? "false_919876543210@c.us_exact" : null,
    querySelector: s => s === ".selectable-text" ? { textContent: "Exact boundary msg" } : s === "[data-pre-plain-text]" ? { getAttribute: () => "[10:30, 11/11/2026] Trader: " } : null,
  };
  page([rowExact]);
  const rendered = readRendered(null, null, cutoff);
  assert.equal(rendered.verified, true);
  assert.equal(rendered.events.length, 1);
  assert.equal(rendered.events[0].text, "Exact boundary msg");
});
