import test from "node:test";
import assert from "node:assert/strict";
import { probeLiveWhatsAppDom, checkSessionAndResolveConflict, openChatSameTab, supervisedWhatsApp } from "./actions.mjs";

test("probeLiveWhatsAppDom inspects selectors and reports LOGGED_IN / QR_REQUIRED", () => {
  const prevDoc = globalThis.document;
  const prevLoc = globalThis.location;
  const prevWin = globalThis.window;
  try {
    globalThis.location = { href: "https://web.whatsapp.com/" };
    globalThis.window = { WPP: { isReady: true, chat: { sendTextMessage: () => {}, sendFileMessage: () => {}, openChatBottom: () => {} } } };
    const foundMap = {
      "#pane-side": { tagName: "DIV", getAttribute: () => "" },
      '#main footer div[contenteditable="true"][role="textbox"]': { tagName: "DIV", getAttribute: (k) => k === "role" ? "textbox" : "" },
      '#main footer button[aria-label="Send"]': { tagName: "BUTTON", getAttribute: () => "Send" },
    };
    globalThis.document = {
      querySelector: (s) => foundMap[s] || null,
      querySelectorAll: () => [],
    };
    const res = probeLiveWhatsAppDom();
    assert.equal(res.ok, true);
    assert.equal(res.sessionState, "LOGGED_IN");
    assert.equal(res.wpp.hasChatSend, true);
    assert.equal(res.selectors.composeInput.found, true);
    assert.equal(res.selectors.sendButton.found, true);
  } finally {
    globalThis.document = prevDoc;
    globalThis.location = prevLoc;
    globalThis.window = prevWin;
  }
});

test("checkSessionAndResolveConflict clicks Use Here when takeover allowed", () => {
  const prevDoc = globalThis.document;
  const prevXPath = globalThis.XPathResult;
  let clicked = 0;
  try {
    globalThis.XPathResult = { FIRST_ORDERED_NODE_TYPE: 9 };
    globalThis.document = {
      getElementById: () => null,
      querySelector: () => null,
      evaluate: (expr) => ({
        singleNodeValue: expr.includes("USEHERE") ? { click: () => { clicked++; } } : null,
      }),
    };
    assert.equal(checkSessionAndResolveConflict(false).state, "USE_HERE_CONFLICT");
    assert.equal(clicked, 0);
    assert.equal(checkSessionAndResolveConflict(true).state, "CONFLICT_CLICKED");
    assert.equal(clicked, 1);
  } finally {
    globalThis.document = prevDoc;
    globalThis.XPathResult = prevXPath;
  }
});

test("openChatSameTab uses WPP openChatBottom without page reload", async () => {
  const prevDoc = globalThis.document;
  const prevWin = globalThis.window;
  const opened = [];
  try {
    globalThis.document = { getElementById: (id) => (id === "main" ? {} : null) };
    globalThis.window = {
      WPP: {
        isReady: true,
        chat: {
          openChatBottom: async (jid) => { opened.push(jid); },
        },
      },
    };
    const res = await openChatSameTab({ phone: "+919876543210" });
    assert.equal(res.opened, true);
    assert.equal(res.via, "wpp_open");
    assert.deepEqual(opened, ["919876543210@c.us"]);
  } finally {
    globalThis.document = prevDoc;
    globalThis.window = prevWin;
  }
});

const approved = {
  account_phone: "+12025550101",
  recipient_phone: "+12025550102",
  recipient_jid: "12025550102@c.us",
  text_parts: ["Your approved quote"],
};
const authorizedUntil = () => Date.now() / 1000 + 10;

function whatsappPage(t, {account = "12025550101@c.us", query, chat, send, draft} = {}) {
  const oldWindow = globalThis.window;
  const oldDocument = globalThis.document;
  t.after(() => { globalThis.window = oldWindow; globalThis.document = oldDocument; });
  globalThis.document = {querySelector: () => draft ? {textContent: draft} : null};
  const sent = [];
  globalThis.window = {
    location: {origin: "https://web.whatsapp.com"},
    WPP: {
      isReady: true,
      conn: {isAuthenticated: () => true, getMyUserWid: () => ({_serialized: account})},
      contact: {queryWidExists: query ?? (async jid => ({wid: {_serialized: jid}}))},
      chat: {
        get: chat ?? (jid => ({id: {_serialized: jid}})),
        sendTextMessage: async (jid, text, options) => {
          sent.push({jid, text, options});
          return send ? send(jid) : {to: jid, ack: 1, id: "true_" + jid + "_MSG123"};
        },
      },
    },
  };
  return sent;
}

test("supervised send invokes one text send on the exact verified recipient and requires the server receipt", async t => {
  const sent = whatsappPage(t);
  assert.deepEqual(await supervisedWhatsApp("account"), {account_phone: approved.account_phone});
  const verification = await supervisedWhatsApp("verify", approved);
  assert.deepEqual(verification, {verified: true, chat_id: approved.recipient_jid});
  assert.equal(sent.length, 0);
  const result = await supervisedWhatsApp("send", approved, verification.chat_id, authorizedUntil());
  assert.deepEqual(result, {outcome: "ACCEPTED", sent_ids: ["true_12025550102@c.us_MSG123"]});
  assert.equal(sent.length, 1);
  assert.equal(sent[0].text, approved.text_parts[0]);
  assert.equal(sent[0].options.waitForAck, true);
  assert.equal(sent[0].options.createChat, false);
});

test("supervised send can use the existing LID chat returned by the exact phone lookup", async t => {
  const lid = "1234567890@lid";
  const sent = whatsappPage(t, {
    query: async jid => ({wid: jid, lid}),
    chat: jid => jid === lid ? {id: lid} : undefined,
  });
  assert.deepEqual(await supervisedWhatsApp("verify", approved), {verified: true, chat_id: lid});
  assert.equal((await supervisedWhatsApp("send", approved, lid, authorizedUntil())).outcome, "ACCEPTED");
  assert.equal(sent[0].jid, lid);
});

test("a changed account, recipient, expired grant and multipart text fail before sending", async t => {
  const sent = whatsappPage(t, {query: async () => ({wid: "12025550103@c.us"})});
  assert.equal((await supervisedWhatsApp("send", {...approved, account_phone: "+12025550104"}, approved.recipient_jid, authorizedUntil())).reason, "WHATSAPP_ACCOUNT_CHANGED");
  assert.equal((await supervisedWhatsApp("send", approved, approved.recipient_jid, 1)).reason, "DISPATCH_AUTHORIZATION_EXPIRED");
  assert.equal((await supervisedWhatsApp("send", {...approved, text_parts: ["one", "two"]}, approved.recipient_jid, authorizedUntil())).reason, "INVALID_APPROVED_MESSAGE");
  assert.equal((await supervisedWhatsApp("send", approved, approved.recipient_jid, authorizedUntil())).reason, "RECIPIENT_PHONE_UNVERIFIED");
  assert.equal(sent.length, 0);
});

test("supervised send preserves a human draft", async t => {
  const sent = whatsappPage(t, {draft: "my pending message"});
  assert.equal((await supervisedWhatsApp("send", approved, approved.recipient_jid, authorizedUntil())).reason, "HUMAN_DRAFT_PRESENT");
  assert.equal(sent.length, 0);
});

test("an ambiguous reply is quarantined after one send call", async t => {
  const sent = whatsappPage(t, {send: jid => ({to: jid, ack: 0, id: "true_" + jid + "_MSG123"})});
  assert.deepEqual(
    await supervisedWhatsApp("send", approved, approved.recipient_jid, authorizedUntil()),
    {outcome: "UNKNOWN", reason: "WHATSAPP_RECEIPT_UNCONFIRMED"},
  );
  assert.equal(sent.length, 1);
});

test("unauthenticated session blocks send with structured blocker reason", async t => {
  const oldWindow = globalThis.window;
  const oldDocument = globalThis.document;
  const oldXPath = globalThis.XPathResult;
  t.after(() => { globalThis.window = oldWindow; globalThis.document = oldDocument; globalThis.XPathResult = oldXPath; });
  
  globalThis.XPathResult = { FIRST_ORDERED_NODE_TYPE: 9 };
  globalThis.window = {
    location: { origin: "https://web.whatsapp.com" },
    WPP: { isReady: false, conn: { isAuthenticated: () => false } },
  };

  // Test USE_HERE_CONFLICT
  globalThis.document = {
    querySelector: () => null,
    evaluate: (expr) => ({
      singleNodeValue: expr.includes("USEHERE") ? { click: () => {} } : null,
    }),
  };
  const useHereRes = await supervisedWhatsApp("verify", approved);
  assert.equal(useHereRes.outcome, "FAILED_BEFORE_SEND");
  assert.equal(useHereRes.reason, "USE_HERE_CONFLICT");

  // Test QR_REQUIRED
  globalThis.document = {
    getElementById: (id) => null,
    querySelector: (sel) => sel.includes("canvas") ? {} : null,
    evaluate: () => ({ singleNodeValue: null }),
  };
  const qrRes = await supervisedWhatsApp("verify", approved);
  assert.equal(qrRes.outcome, "FAILED_BEFORE_SEND");
  assert.equal(qrRes.reason, "QR_REQUIRED");

  // Test LOADING
  globalThis.document = {
    getElementById: (id) => null,
    querySelector: () => null,
    evaluate: () => ({ singleNodeValue: null }),
  };
  const loadingRes = await supervisedWhatsApp("verify", approved);
  assert.equal(loadingRes.outcome, "FAILED_BEFORE_SEND");
  assert.equal(loadingRes.reason, "LOADING");
});
