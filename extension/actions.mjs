// Self-contained WhatsApp Web same-tab action, XPath probe, conflict recovery & dispatch helpers.
// Every exported function runs inside chrome.scripting.executeScript and MUST remain self-contained.

export function probeLiveWhatsAppDom() {
  const firstMatch = (candidates) => {
    for (const [kind, expr] of candidates) {
      try {
        if (kind === "css") {
          const el = document.querySelector(expr);
          if (el) return { found: true, kind, expr, tag: el.tagName, aria: el.getAttribute("aria-label") || "", testid: el.getAttribute("data-testid") || "" };
        } else if (typeof document.evaluate === "function" && typeof XPathResult !== "undefined") {
          const res = document.evaluate(expr, document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null);
          const el = res.singleNodeValue;
          if (el) return { found: true, kind, expr, tag: el.tagName, aria: el.getAttribute("aria-label") || "", testid: el.getAttribute("data-testid") || "" };
        }
      } catch {}
    }
    return { found: false };
  };

  const w = typeof window !== "undefined" ? window.WPP : null;
  const qrEl = firstMatch([
    ["css", 'canvas[aria-label*="QR" i], canvas[aria-label*="Scan" i]'],
    ["xpath", "//div[@data-ref]/canvas | //canvas[@aria-label]"],
    ["xpath", "//button[contains(translate(.,'PHONENUMBER','phonenumber'),'phone number')]"],
  ]);
  const useHereEl = firstMatch([
    ["xpath", "//div[@role='dialog']//button[contains(translate(.,'USEHERE','usehere'),'use here')]"],
    ["xpath", "//button[contains(translate(.,'USEHERE','usehere'),'use here')]"],
  ]);
  const paneSide = firstMatch([["css", "#pane-side"], ["xpath", "//*[@id='pane-side']"]]);
  let sessionState = "LOADING";
  if (useHereEl.found) sessionState = "USE_HERE_CONFLICT";
  else if (qrEl.found) sessionState = "QR_REQUIRED";
  else if (paneSide.found) sessionState = "LOGGED_IN";

  const selectors = {
    chatList: paneSide,
    newChat: firstMatch([["xpath", "//button[@aria-label='New chat']"], ["css", 'button[aria-label="New chat"]']]),
    searchInput: firstMatch([
      ["css", '#side input[aria-label="Search or start a new chat"]'],
      ["xpath", "//input[@role='textbox' and @aria-label='Search or start a new chat']"],
      ["css", 'div[data-testid="chat-list-search"] div[contenteditable="true"]'],
      ["css", '#side div[contenteditable="true"][role="textbox"]'],
      ["css", 'div[aria-label="Search input textbox"]'],
      ["xpath", "//*[@id='side']//div[@contenteditable='true' and @role='textbox']"],
      ["xpath", "//div[@role='textbox' and contains(translate(@aria-label,'SEARCH','search'),'search')]"],
    ]),
    chatRow: firstMatch([
      ["css", '#pane-side [role="listitem"]'],
      ["css", '#pane-side [role="row"]'],
      ["css", '#pane-side [data-testid="cell-frame-container"]'],
      ["xpath", "//*[@id='pane-side']//*[@role='listitem' or @role='row']"],
    ]),
    mainPane: firstMatch([["css", "#main"], ["xpath", "//*[@id='main']"]]),
    chatHeader: firstMatch([["css", "#main header"], ["xpath", "//*[@id='main']//header"]]),
    composeInput: firstMatch([
      ["css", '#main footer div[contenteditable="true"][role="textbox"]'],
      ["css", 'div[data-testid="conversation-compose-box-input"]'],
      ["xpath", "//*[@id='main']//footer//*[@contenteditable='true' and @role='textbox']"],
    ]),
    sendButton: firstMatch([
      ["css", '#main footer button[aria-label="Send"]'],
      ["css", '#main footer span[data-testid="send"]'],
      ["css", '#main footer span[data-icon="send"]'],
      ["xpath", "//*[@id='main']//footer//button[@aria-label='Send' or .//*[@data-testid='send' or @data-icon='send']]"],
    ]),
    attachButton: firstMatch([
      ["css", '#main footer button[aria-label="Attach"]'],
      ["css", '#main footer span[data-testid="clip"], #main footer span[data-testid="attach-menu-plus"], #main footer span[data-icon="plus"]'],
      ["xpath", "//*[@id='main']//footer//button[@aria-label='Attach' or .//*[@data-testid='clip' or @data-icon='plus']]"],
    ]),
    fileInputs: (() => {
      const inputs = [...document.querySelectorAll('input[type="file"]')];
      return { found: inputs.length > 0, count: inputs.length, accepts: inputs.map((i) => i.getAttribute("accept") || "*/*") };
    })(),
    announcementFooter: firstMatch([
      ["xpath", "//*[@id='main']//footer//*[contains(translate(.,'ONLYADMINS','onlyadmins'),'only admins')]"],
    ]),
    profileTrigger: firstMatch([
      ["css", '[data-testid="menu-bar-profile"]'],
      ["css", 'nav button[aria-label*="profile" i], header button[aria-label*="profile" i]'],
      ["xpath", '//img[@width="24" and @height="24"]'],
    ]),
    qrCanvas: qrEl,
    useHereButton: useHereEl,
  };

  return {
    ok: true,
    sessionState,
    url: location.href,
    wpp: {
      loaded: Boolean(w),
      isReady: Boolean(w?.isReady || w?.loader?.isReady || w?.webpack?.isReady),
      hasChatSend: Boolean(w?.chat?.sendTextMessage),
      hasFileSend: Boolean(w?.chat?.sendFileMessage),
      hasOpenChat: Boolean(w?.chat?.openChatBottom || w?.chat?.find),
    },
    selectors,
    probedAt: Date.now(),
  };
}

export function checkSessionAndResolveConflict(allowTakeover = true) {
  const findXPath = (expr) => {
    if (typeof document.evaluate !== "function" || typeof XPathResult === "undefined") return null;
    try {
      return document.evaluate(expr, document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue;
    } catch { return null; }
  };
  const useHere = findXPath("//div[@role='dialog']//button[contains(translate(.,'USEHERE','usehere'),'use here')]")
    || findXPath("//button[contains(translate(.,'USEHERE','usehere'),'use here')]");
  if (useHere) {
    if (allowTakeover && typeof useHere.click === "function") {
      useHere.click();
      return { state: "CONFLICT_CLICKED" };
    }
    return { state: "USE_HERE_CONFLICT" };
  }
  const hasQr = Boolean(
    document.querySelector('canvas[aria-label*="QR" i], canvas[aria-label*="Scan" i], div[data-ref]')
    || findXPath("//button[contains(translate(.,'PHONENUMBER','phonenumber'),'phone number')]")
  );
  if (hasQr && !document.getElementById("pane-side")) {
    return { state: "QR_REQUIRED" };
  }
  if (!document.getElementById("pane-side")) {
    return { state: "LOADING" };
  }
  return { state: "LOGGED_IN" };
}

export async function openChatSameTab(target) {
  // Runs in MAIN or ISOLATED world; uses WPP in-memory first, then Search Box DOM (never reloads URL).
  const phoneDigits = String(target?.phone || "").replace(/\D/g, "");
  const targetJid = String(target?.jid || (phoneDigits ? `${phoneDigits}@c.us` : "")).trim();
  const queryText = String(target?.query || (phoneDigits ? `+${phoneDigits}` : "") || target?.title || "").trim();

  if (document.querySelector?.('#main [contenteditable="true"][role="textbox"]')?.textContent?.trim()) {
    return { opened: false, reason: "HUMAN_DRAFT_PRESENT" };
  }

  const w = typeof window !== "undefined" ? window.WPP : null;
  if (w && targetJid && (w.isReady || w.loader?.isReady || w.webpack?.isReady)) {
    try {
      if (w.chat?.openChatBottom) {
        await w.chat.openChatBottom(targetJid);
        await new Promise((r) => setTimeout(r, 350));
        if (document.getElementById("main")) return { opened: true, via: "wpp_open", jid: targetJid };
      }
      if (w.chat?.find) {
        const chat = await w.chat.find(targetJid).catch(() => null);
        if (chat && w.chat?.openChatAt) {
          await w.chat.openChatAt(chat.id?._serialized || targetJid);
          await new Promise((r) => setTimeout(r, 350));
          if (document.getElementById("main")) return { opened: true, via: "wpp_find", jid: targetJid };
        }
      }
    } catch {}
  }

  if (!queryText) return { opened: false, reason: "NO_TARGET" };
  const findSearchBox = () => (
    document.querySelector('#side input[aria-label="Search or start a new chat"]')
    || document.querySelector('div[data-testid="chat-list-search"] div[contenteditable="true"]')
    || document.querySelector('#side div[contenteditable="true"][role="textbox"]')
    || document.querySelector('div[aria-label="Search input textbox"]')
  );
  const searchBox = findSearchBox();
  if (!searchBox) return { opened: false, reason: "SEARCH_BOX_NOT_FOUND" };

  const updateInput = (box, value) => {
    if (box.tagName !== "INPUT") return false;
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set;
    setter.call(box, value);
    box.dispatchEvent(new Event("input", { bubbles: true }));
    return true;
  };
  searchBox.focus();
  if (updateInput(searchBox, queryText)) {
    // The native input dispatched its own event.
  } else if (typeof document.execCommand === "function") {
    document.execCommand("selectAll", false, null);
    document.execCommand("insertText", false, queryText);
  } else {
    searchBox.textContent = queryText;
  }
  if (searchBox.tagName !== "INPUT") searchBox.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText", data: queryText }));

  const deadline = Date.now() + 2600;
  let matchedRow = null;
  while (Date.now() < deadline) {
    await new Promise((r) => setTimeout(r, 220));
    const pane = document.getElementById("pane-side");
    if (!pane) continue;
    const rows = [...pane.querySelectorAll('[role="listitem"], [role="row"], [data-testid="cell-frame-container"]')].filter((r) => r.getBoundingClientRect().height > 0);
    if (rows.length) {
      const needle = queryText.toLowerCase();
      const matches = rows.filter(row => {
        const title = row.querySelector('span[title]')?.getAttribute('title')?.trim() || "";
        return phoneDigits
          ? /^[+\d ()-]+$/.test(title) && title.replace(/\D/g, "") === phoneDigits
          : title.toLowerCase() === needle;
      });
      const distinctMatches = matches.filter(row => !matches.some(other => other !== row && row.contains(other)));
      if (distinctMatches.length === 1) matchedRow = distinctMatches[0];
      if (matchedRow) break;
    }
  }
  if (!matchedRow) {
    searchBox.focus();
    if (!updateInput(searchBox, "") && typeof document.execCommand === "function") {
      document.execCommand("selectAll", false, null);
      document.execCommand("delete", false, null);
    }
    return { opened: false, reason: "CHAT_NOT_FOUND_IN_SEARCH" };
  }
  (matchedRow.querySelector('[role="gridcell"]') || matchedRow).click();
  await new Promise((r) => setTimeout(r, 350));
  // Clear search filter cleanly so sidebar returns to normal
  const sb2 = findSearchBox();
  if (sb2) {
    sb2.focus();
    if (!updateInput(sb2, "") && typeof document.execCommand === "function") {
      document.execCommand("selectAll", false, null);
      document.execCommand("delete", false, null);
      sb2.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "deleteContentBackward" }));
    }
  }
  return { opened: Boolean(document.getElementById("main")), via: "dom_search" };
}

// Chrome serializes this function when it runs it in the WhatsApp page.
export async function supervisedWhatsApp(action, item = null, expected = null, expiresAt = null) {
  const beforeSend = reason => ({outcome: "FAILED_BEFORE_SEND", reason});
  const unknown = reason => ({outcome: "UNKNOWN", reason});
  const idOf = id => typeof id === "string" ? id : id?._serialized;
  const wpp = window.WPP;
  let invoked = false;
  let timer;
  try {
    if (window.location.origin !== "https://web.whatsapp.com") {
      return beforeSend("TAB_MISSING");
    }
    if (document?.querySelector?.('#main [contenteditable="true"][role="textbox"]')?.textContent?.trim()) {
      return beforeSend("HUMAN_DRAFT_PRESENT");
    }
    const isAuthed = Boolean(wpp?.conn?.isAuthenticated?.() === true);
    if (!isAuthed) {
      const findXPath = (expr) => {
        if (typeof document?.evaluate !== "function" || typeof XPathResult === "undefined") return null;
        try {
          return document.evaluate(expr, document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue;
        } catch { return null; }
      };
      const useHere = findXPath("//div[@role='dialog']//button[contains(translate(.,'USEHERE','usehere'),'use here')]")
        || findXPath("//button[contains(translate(.,'USEHERE','usehere'),'use here')]");
      if (useHere) {
        return beforeSend("USE_HERE_CONFLICT");
      }
      const hasQr = Boolean(
        document?.querySelector?.('canvas[aria-label*="QR" i], canvas[aria-label*="Scan" i], div[data-ref]')
        || findXPath("//button[contains(translate(.,'PHONENUMBER','phonenumber'),'phone number')]")
      );
      if (hasQr && !document?.getElementById?.("pane-side")) {
        return beforeSend("QR_REQUIRED");
      }
      if (document?.getElementById?.("pane-side")) {
        return beforeSend("WHATSAPP_SESSION_UNVERIFIED");
      }
      return beforeSend("LOADING");
    }
    if (!wpp?.isReady && !wpp?.loader?.isReady && !wpp?.webpack?.isReady) {
      return beforeSend("WHATSAPP_SESSION_UNVERIFIED");
    }
    const currentAccount = () => {
      const id = idOf(wpp.whatsapp?.UserPrefs?.getMaybeMePnUser?.() ?? wpp.conn.getMyUserWid?.());
      const match = /^([1-9][0-9]{6,14})@(?:c\.us|s\.whatsapp\.net)$/.exec(id ?? "");
      return wpp.conn.isAuthenticated() === true && match ? "+" + match[1] : null;
    };
    const accountPhone = currentAccount();
    if (!accountPhone) return beforeSend("WHATSAPP_ACCOUNT_UNVERIFIED");
    if (action === "account") return {account_phone: accountPhone};
    if (action !== "verify" && action !== "send") return beforeSend("INVALID_DISPATCH_ACTION");
    if (accountPhone !== item?.account_phone) return beforeSend("WHATSAPP_ACCOUNT_CHANGED");
    const phone = item.recipient_phone;
    const jid = item.recipient_jid;
    const parts = item.text_parts;
    if (typeof phone !== "string" || !/^\+[1-9][0-9]{6,14}$/.test(phone) ||
        jid !== phone.slice(1) + "@c.us" || phone === accountPhone ||
        !Array.isArray(parts) || parts.length !== 1 || typeof parts[0] !== "string" || !parts[0].trim()) {
      return beforeSend("INVALID_APPROVED_MESSAGE");
    }
    if (action === "send" && (!Number.isFinite(expiresAt) || Date.now() >= expiresAt * 1000)) {
      return beforeSend("DISPATCH_AUTHORIZATION_EXPIRED");
    }
    const verification = await Promise.race([
      wpp.contact?.queryWidExists?.(jid),
      new Promise(resolve => { timer = setTimeout(() => resolve(null), 4500); }),
    ]);
    clearTimeout(timer);
    if (idOf(verification?.wid) !== jid) return beforeSend("RECIPIENT_PHONE_UNVERIFIED");
    const lid = idOf(verification.lid);
    const exactChat = id => {
      const chat = wpp.chat?.get?.(id);
      return chat && idOf(chat.id) === id ? chat : null;
    };
    const recipient = exactChat(jid) ??
      (/^[0-9]{5,30}@lid$/.test(lid ?? "") ? exactChat(lid) : null);
    if (recipient && (recipient.isGroup || recipient.isNewsletter)) {
      return beforeSend("RECIPIENT_CHAT_UNVERIFIED");
    }
    const chatId = idOf(recipient?.id) || jid;
    if (currentAccount() !== accountPhone) return beforeSend("WHATSAPP_ACCOUNT_CHANGED");
    if (action === "verify") return {verified: true, chat_id: chatId};
    if (chatId !== expected && jid !== expected || window.WPP !== wpp) return beforeSend("WHATSAPP_CHAT_CHANGED");
    if (document.querySelector('#main [contenteditable="true"][role="textbox"]')?.textContent?.trim()) {
      return beforeSend("HUMAN_DRAFT_PRESENT");
    }
    if (Date.now() >= expiresAt * 1000) return beforeSend("DISPATCH_AUTHORIZATION_EXPIRED");
    if (typeof wpp.chat?.sendTextMessage !== "function") return beforeSend("WHATSAPP_SEND_UNAVAILABLE");
    invoked = true;
    const receipt = await Promise.race([
      wpp.chat.sendTextMessage(chatId, parts[0], {
        createChat: !recipient,
        waitForAck: true,
        markIsRead: false,
        linkPreview: false,
        detectMentioned: false,
      }),
      new Promise(resolve => { timer = setTimeout(() => resolve(null), 18000); }),
    ]);
    const receiptId = idOf(receipt?.id);
    const prefix = "true_" + chatId + "_";
    if (!Number.isInteger(receipt?.ack) || receipt.ack < 1 || (receipt?.to !== undefined && receipt.to !== chatId) ||
        typeof receiptId !== "string" || !receiptId.startsWith(prefix) ||
        !/^[a-zA-Z0-9-]{1,160}$/.test(receiptId.slice(prefix.length))) {
      return unknown("WHATSAPP_RECEIPT_UNCONFIRMED");
    }
    return {outcome: "ACCEPTED", sent_ids: [receiptId]};
  } catch {
    return invoked ? unknown("WHATSAPP_SEND_RESULT_UNAVAILABLE") : beforeSend("WHATSAPP_CHECK_FAILED");
  } finally {
    clearTimeout(timer);
  }
}
