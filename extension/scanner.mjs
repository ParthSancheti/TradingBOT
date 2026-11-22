export function sourceIndex(cursor,count){
  if(!Number.isInteger(count) || count<1)throw Error("No sources");
  return Number.isSafeInteger(cursor) && cursor>=0?cursor%count:0;
}

export async function readWppStoreProfile(ownerPhone = null) {
  const w = window.WPP;
  if (!w) return { verified: false, reason: "WPP_UNLOADED" };
  const deadline = Date.now() + 2800;
  while (Date.now() < deadline && !w.isReady && !(w.webpack && w.webpack.isReady) && !w.loader?.isReady && !w.whatsapp?.Conn?.pushname) {
    await new Promise((r) => setTimeout(r, 120));
  }
  const withTimeout = (promise, ms = 1500) => Promise.race([
    Promise.resolve(promise).catch(() => null),
    new Promise((r) => setTimeout(() => r(null), ms)),
  ]);
  try {
    if (w.conn?.isAuthenticated && !w.conn.isAuthenticated()) {
      return { verified: false, reason: "NOT_AUTHENTICATED" };
    }
    const wid = w.conn?.getMyUserWid ? w.conn.getMyUserWid() : (w.conn?.getMyUserId ? w.conn.getMyUserId() : null);
    const rawWid = wid?.user || (typeof wid === "string" ? wid : wid?._serialized || "");
    const userDigits = String(rawWid).split("@")[0].replace(/\D/g, "");
    const phone = /^\d{7,15}$/.test(userDigits) ? "+" + userDigits : null;
    const ownerDigits = String(ownerPhone || "").replace(/\D/g, "");
    if (ownerDigits && userDigits && userDigits !== ownerDigits) {
      return { verified: false, reason: "PHONE_MISMATCH" };
    }
    let name = "";
    if (w.profile?.getMyProfileName) {
      name = String((await withTimeout(w.profile.getMyProfileName(), 1500)) || "").trim();
    }
    if (!name && w.whatsapp?.Conn?.pushname) {
      name = String(w.whatsapp.Conn.pushname || "").trim();
    }
    if (!name && wid && w.contact?.get) {
      const c = await withTimeout(w.contact.get(wid._serialized || wid), 1500);
      name = String(c?.pushname || c?.verifiedName || c?.name || "").trim();
    }
    let picUrl = null;
    try {
      const thumbStore = w.whatsapp?.ProfilePicThumbStore;
      const thumb = thumbStore?.get?.(wid?._serialized || wid);
      picUrl = thumb?.imgFull || thumb?.eurl || thumb?.img || null;
    } catch {}
    if (!picUrl && w.profile?.getMyProfilePicture) {
      const pic = await withTimeout(w.profile.getMyProfilePicture(), 1500);
      picUrl = pic?.imgFull || pic?.eurl || pic?.img || null;
    }
    if (!picUrl && wid && w.contact?.getProfilePicUrl) {
      picUrl = await withTimeout(w.contact.getProfilePicUrl(wid._serialized || wid), 1500);
    }
    if (!picUrl && typeof document !== "undefined" && typeof document.querySelectorAll === "function") {
      for (const img of document.querySelectorAll('img')) {
        if (img.closest?.('#main, #pane-side')) continue;
        const s = img.currentSrc || img.src || "";
        const wPx = Number(img.getAttribute?.('width') || img.width || 0);
        if ((wPx > 0 && wPx <= 48) && /^https:\/\/[^/]+\.(?:whatsapp\.net|fbcdn\.net)\//i.test(s)) {
          picUrl = s;
          break;
        }
      }
    }
    if (!name && !picUrl) return { verified: false, reason: "EMPTY_WPP_PROFILE" };
    return {
      verified: Boolean(name),
      phone,
      name: name.slice(0, 100),
      avatar_url: typeof picUrl === "string" && /^https:\/\//i.test(picUrl) ? picUrl : null,
      avatar_checked: true,
      source: "wpp_store",
    };
  } catch (err) {
    return { verified: false, reason: String(err?.message || err) };
  }
}

export async function readWppStoreMessages(sinceTs = 0, allowedPhones = null, agentName = "Jarvis", databasePhone = null, captureMedia = true) {
  const deadline = Date.now() + 4000;
  while (Date.now() < deadline && !(window.WPP?.chat?.list && window.WPP?.chat?.getMessages)) {
    await new Promise((r) => setTimeout(r, 150));
  }
  const w = window.WPP;
  if (!w || !w.chat?.list || !w.chat?.getMessages) return { verified: false, reason: "WPP_UNLOADED" };
  while (Date.now() < deadline && !w.isReady && !(w.webpack && w.webpack.isReady) && !w.loader?.isReady) {
    await new Promise((r) => setTimeout(r, 120));
  }
  try {
    if (w.conn?.isAuthenticated && !w.conn.isAuthenticated()) {
      return { verified: false, reason: "UNAUTHENTICATED" };
    }
    // Event-driven inbox: every new message (in any chat, even one outside the
    // recent-chat sweep) is remembered here until the next scan reads it.
    if (!Array.isArray(window.__jarvisInbox)) window.__jarvisInbox = [];
    if (typeof document !== "undefined" && typeof w.on === "function" && window.__jarvisWakeStore !== w) {
      try {
        w.on("chat.new_message", (msg) => {
          try {
            const remote = msg?.id?.remote;
            const chatId = String(remote?._serialized || remote || msg?.from?._serialized || msg?.from || "");
            if (chatId) {
              window.__jarvisInbox.push({ chatId, at: Date.now() });
              if (window.__jarvisInbox.length > 500) window.__jarvisInbox.splice(0, window.__jarvisInbox.length - 500);
            }
          } catch {}
          document.dispatchEvent(new Event("jarvis-whatsapp-message"));
        });
        window.__jarvisWakeStore = w;
      } catch {}
    }
    const inboxChatIds = new Set((window.__jarvisInbox || []).map((entry) => entry.chatId).filter(Boolean));
    const processedChatIds = new Set();
    const minTs = Number(sinceTs || 0);
    const allowedSet = Array.isArray(allowedPhones)
      ? new Set(allowedPhones.map((p) => String(p || "").replace(/\D/g, "")).filter(Boolean))
      : null;
    const bounded = async (operation, timeout, fallback) => {
      let timer;
      try {
        return await Promise.race([
          Promise.resolve().then(operation),
          new Promise(resolve => { timer = setTimeout(() => resolve(fallback), timeout); }),
        ]);
      } catch {
        return fallback;
      } finally {
        clearTimeout(timer);
      }
    };
    const allChats = await bounded(() => w.chat.list({count: 60}), 3500, null);
    if (!Array.isArray(allChats)) return {verified:false, reason:"CHAT_LIST_UNAVAILABLE"};
    if (!allChats.length && !inboxChatIds.size) {
      return { verified: true, source: "wpp_store", discovered: 0, events: [], agent_commands: [], human_replies: [], lid_map: {} };
    }
    let myWid = null;
    try {
      myWid = w.conn?.getMyUserWid ? w.conn.getMyUserWid() : (w.conn?.getMyUserId ? w.conn.getMyUserId() : null);
    } catch {}
    let myLid = null;
    try {
      myLid = w.conn?.getMyUserLid ? w.conn.getMyUserLid() : null;
    } catch {}
    const mySerialized = String(myWid?._serialized || myWid || "");
    const myLidSerialized = String(myLid?._serialized || myLid || "");
    const agentTokenRe = new RegExp(`^@(?:agent|${String(agentName || "Jarvis").replace(/[.*+?^${}()|[\]\\]/g, "\\$&")})\\b`, "i");
    const isLikelyBase64 = (val) => {
      if (typeof val !== "string") return false;
      const t = val.trim();
      if (t.length < 80 || /\s/.test(t)) return false;
      return /^(?:\/9j\/|iVBORw0KGgo|R0lGOD|UklGR)/.test(t) || /^[A-Za-z0-9+/]{100,}={0,2}$/.test(t);
    };
    const isSelfChatId = (cid, chatObj) => Boolean(
      (mySerialized && cid === mySerialized)
      || (myLidSerialized && cid === myLidSerialized)
      || chatObj?.contact?.isMe
      || chatObj?.isMe
    );

    const candidates = allChats.filter((c) => {
      const cid = String(c?.id?._serialized || c?.id || "");
      if (!/^[0-9-]{5,80}@(?:c\.us|s\.whatsapp\.net|g\.us|lid)$/.test(cid)) return false;
      const selfChat = isSelfChatId(cid, c);
      if (allowedSet && !selfChat) {
        if (cid.endsWith("@g.us")) return false;
        const digits = cid.split("@")[0].replace(/\D/g, "");
        return allowedSet.has(digits) || cid.endsWith("@lid");
      }
      const chatTs = Number(c?.t || c?.lastReceivedKey?.t || 0);
      return inboxChatIds.has(cid) || Number(c?.unreadCount || 0) > 0 || selfChat || (minTs > 0 ? chatTs >= minTs - 30 : true);
    }).sort((a, b) => {
      // Chats that just received a live message are always read first.
      const ia = inboxChatIds.has(String(a?.id?._serialized || a?.id || "")) ? 1 : 0;
      const ib = inboxChatIds.has(String(b?.id?._serialized || b?.id || "")) ? 1 : 0;
      if (ia !== ib) return ib - ia;
      return Number(b.t || b.lastReceivedKey?.t || 0) - Number(a.t || a.lastReceivedKey?.t || 0);
    }).slice(0, 24);
    // A live message can arrive in a chat that is not among the 60 most recent
    // list entries (archived, pinned order changes). Fetch those chats directly.
    const listedIds = new Set(candidates.map((c) => String(c?.id?._serialized || c?.id || "")));
    for (const missingId of inboxChatIds) {
      if (listedIds.has(missingId) || candidates.length >= 40) continue;
      const extra = await bounded(() => (w.chat.get ? w.chat.get(missingId) : null), 1500, null);
      if (extra && /^[0-9-]{5,80}@(?:c\.us|s\.whatsapp\.net|g\.us|lid)$/.test(missingId)) {
        if (allowedSet && !isSelfChatId(missingId, extra)) {
          if (missingId.endsWith("@g.us")) continue;
          const digits = missingId.split("@")[0].replace(/\D/g, "");
          if (!allowedSet.has(digits) && !missingId.endsWith("@lid")) continue;
        }
        candidates.push(extra);
        listedIds.add(missingId);
      }
    }

    const events = [];
    const agentCommands = [];
    const humanReplies = [];
    const lidMap = {};
    const seenIds = new Set();
    let mediaFetchedCount = 0;
    const skipTypes = new Set(["e2e_notification", "notification_template", "gp2", "call_log", "revoked", "protocol", "ciphertext", "sticker"]);
    const messagesByChat = new Map();
    await Promise.all(candidates.map(async chat => {
        const id = String(chat.id?._serialized || chat.id || "");
        processedChatIds.add(id);
        const cached = chat.msgs?.getModelsArray?.();
        const unread = Number(chat?.unreadCount || 0);
        // The in-memory list of an unopened chat often holds only its last
        // message. Load from WhatsApp's store when it cannot cover every unread
        // message (several messages arrived while the chat was closed).
        const cacheCovers = Array.isArray(cached) && cached.length && cached.length > unread;
        let messages = cacheCovers ? cached.slice(-30) : null;
        if (!messages) {
          const want = Math.min(200, Math.max(30, unread + 5));
          const loaded = await bounded(() => w.chat.getMessages(id, {count: want}), 3500, null);
          messages = Array.isArray(loaded) && loaded.length ? loaded : (Array.isArray(cached) ? cached.slice(-30) : []);
        }
        if (Array.isArray(messages)) {
          messages.sort((a, b) => Number(a?.t || a?.timestamp || 0) - Number(b?.t || b?.timestamp || 0));
        }
        messagesByChat.set(id, messages);
    }));
    const phoneLookups = new Map();
    const resolvePhone = sender => {
      if (!phoneLookups.has(sender)) {
        phoneLookups.set(sender, bounded(async () => {
          const entry = await w.contact?.getPnLidEntry?.(sender);
          const mapped = /^([1-9]\d{6,14})@(?:c\.us|s\.whatsapp\.net)$/.exec(entry?.phoneNumber?._serialized || "");
          if (mapped) return "+" + mapped[1];
          const value = await w.contact?.getPhoneNumber?.(sender);
          const number = /^\+?([1-9]\d{6,14})(?:@(?:c\.us|s\.whatsapp\.net))?$/.exec(String(value?._serialized || value || ""));
          return number ? "+" + number[1] : null;
        }, 1000, null));
      }
      return phoneLookups.get(sender);
    };
    for (const [id, messages] of messagesByChat) {
      if (id.endsWith("@lid")) resolvePhone(id);
      if (id.endsWith("@g.us") && Array.isArray(messages)) {
        for (const message of messages) {
          const author = message?.author || message?.id?.participant || message?.sender;
          const participant = author?._serialized || (typeof author === "string" ? author : "");
          if (/^\d{5,30}@lid$/.test(participant)) resolvePhone(participant);
        }
      }
    }
    for (const chat of candidates) {
      const conversationId = String(chat.id?._serialized || chat.id || "");
      const selfChat = isSelfChatId(conversationId, chat);
      const isGroup = conversationId.endsWith("@g.us");
      const directNumber = /^([1-9]\d{6,14})@(?:c\.us|s\.whatsapp\.net)$/.exec(conversationId);
      const directPhone = directNumber ? "+" + directNumber[1] : conversationId.endsWith("@lid") ? await resolvePhone(conversationId) : null;
      if (!isGroup && !selfChat && allowedSet && (!directPhone || !allowedSet.has(directPhone.slice(1)))) continue;
      const chatName = String(chat.formattedTitle || chat.name || chat.contact?.pushname || chat.contact?.name || "").slice(0, 160);
      const msgs = messagesByChat.get(conversationId);
      if (!Array.isArray(msgs)) continue;
      for (const m of msgs) {
        if (!m || skipTypes.has(m.type)) continue;
        const hasMedia = Boolean(m.isMedia || m.mimetype || ["image", "video", "document", "ptt", "audio"].includes(m.type));
        const rawCap = String(m.caption || "").trim();
        const bodyStr = String(m.body || "").trim();
        const bodyIsB64 = isLikelyBase64(bodyStr);
        let text = rawCap || ((!hasMedia && !bodyIsB64) ? bodyStr : (hasMedia && !bodyIsB64 && m.type === "chat" ? bodyStr : ""));
        if (!text && hasMedia) {
          if (m.type === "image" || /^image\//i.test(m.mimetype || "")) text = "[Photo / Stock Image]";
          else if (m.type === "document") text = `[Document: ${String(m.filename || "Attachment").slice(0, 60)}]`;
          else if (m.type === "ptt" || m.type === "audio") text = "[Voice note]";
          else if (m.type === "video") text = "[Video]";
          else text = "[Media attachment]";
        }
        const rawT = Number(m.t || m.timestamp || 0);
        const ts = rawT > 1e12 ? Math.floor(rawT / 1000) : rawT;
        if (minTs > 0 && (!(ts > 0) || ts < minTs)) continue;

        const serializedId = typeof m.id === "string" ? m.id : m.id?._serialized;
        if (m.id?.fromMe === true || m.fromMe === true || serializedId?.startsWith("true_")) {
          const rawOutId = String(m.id?.id || (m.id?._serialized || "").split("_")[2] || ts).slice(0, 140);
          if (text && agentTokenRe.test(text)) {
            agentCommands.push({
              conversation_id: conversationId,
              self_chat: selfChat,
              message_id: serializedId?.startsWith("true_") ? serializedId : `true_${conversationId}_${rawOutId}`,
              text,
              timestamp: ts || Math.floor(Date.now() / 1000),
              has_media: hasMedia,
              media_type: m.type || null,
            });
          } else if (!selfChat) {
            humanReplies.push({
              conversation_id: conversationId,
              message_id: serializedId?.startsWith("true_") ? serializedId : `true_${conversationId}_${rawOutId}`,
              timestamp: ts || Math.floor(Date.now() / 1000),
            });
            if (!isGroup && directPhone && text && !hasMedia && text.length <= 12000
                && (!allowedSet || allowedSet.has(directPhone.slice(1)))) {
              events.push({
                conversation_id: conversationId, sender_id: conversationId, phone: directPhone,
                chat_name: chatName, sender_name: chatName, chat_kind: "direct",
                message_id: serializedId?.startsWith("true_") ? serializedId : `true_${conversationId}_${rawOutId}`,
                text, outgoing: true, identity_verified: true,
                ...(ts > 0 ? {timestamp: ts} : {}),
              });
              if (conversationId.endsWith("@lid")) lidMap[conversationId] = directPhone;
            }
          }
          continue;
        }

        if (!text || text.length > 12000) continue;
        let mediaB64 = null;
        let mediaMime = null;
        const isImageMsg = m.type === "image" || /^image\//i.test(m.mimetype || "");
        if (captureMedia && isImageMsg && mediaFetchedCount < 3) {
          mediaFetchedCount++;
          if (w.chat?.downloadMedia && typeof createImageBitmap === "function" && typeof document !== "undefined") {
            try {
              const blob = await Promise.race([
                w.chat.downloadMedia(m.id?._serialized || m.id).catch(() => null),
                new Promise((r) => setTimeout(() => r(null), 2800)),
              ]);
              if (blob && blob.size > 0 && blob.size < 8_000_000) {
                const bmp = await createImageBitmap(blob);
                const scale = Math.min(1, 896 / Math.max(bmp.width || 1, bmp.height || 1));
                const cw = Math.max(64, Math.round((bmp.width || 120) * scale));
                const ch = Math.max(64, Math.round((bmp.height || 120) * scale));
                const canvas = document.createElement("canvas");
                canvas.width = cw;
                canvas.height = ch;
                canvas.getContext("2d").drawImage(bmp, 0, 0, cw, ch);
                bmp.close();
                const dataUrl = canvas.toDataURL("image/jpeg", 0.78);
                const b64 = dataUrl.split(",", 2)[1] || "";
                if (b64 && b64.length <= 220000) {
                  mediaB64 = b64;
                  mediaMime = "image/jpeg";
                }
              }
            } catch {}
          }
          if (!mediaB64 && bodyIsB64 && bodyStr.startsWith("/9j/") && bodyStr.length <= 120000) {
            mediaB64 = bodyStr;
            mediaMime = "image/jpeg";
          }
        }
        const author = m.author || m.id?.participant || m.sender;
        const sender = isGroup ? (author?._serialized || (typeof author === "string" ? author : null)) : conversationId;
        if (sender && !/^[0-9-]{5,80}@(?:c\.us|s\.whatsapp\.net|lid)$/.test(sender)) continue;
        const numMatch = sender ? /^([1-9]\d{6,14})@(?:c\.us|s\.whatsapp\.net)$/.exec(sender) : null;
        const phone = !isGroup ? directPhone : numMatch ? "+" + numMatch[1] : sender?.endsWith("@lid") ? await resolvePhone(sender) : null;
        if (phone && sender?.endsWith("@lid")) lidMap[sender] = phone;
        if (allowedSet && (!phone || !allowedSet.has(phone.slice(1)))) continue;
        const rawId = String(m.id?.id || (m.id?._serialized || "").split("_")[2] || ts).replace(/[^A-Za-z0-9-]/g, "").slice(0, 140) || String(ts);
        const messageId = (isGroup && sender) ? `false_${conversationId}_${rawId}_${sender}` : `false_${conversationId}_${rawId}`;
        if (seenIds.has(messageId)) continue;
        seenIds.add(messageId);
        const senderName = String(m.notifyName || m.senderObj?.pushname || m.senderObj?.name || (!isGroup ? chatName : "")).slice(0, 160);
        const stockFile = !isGroup && phone && phone === databasePhone && m.type === "document" &&
          /\.(xlsx|csv|tsv|txt|json)$/i.test(String(m.filename || ""));
        let stockFileText = null;
        if (captureMedia && stockFile && mediaFetchedCount < 3 && w.chat?.downloadMedia && typeof FileReader === "function") {
          mediaFetchedCount++;
          try {
            const blob = await Promise.race([
              w.chat.downloadMedia(serializedId || m.id),
              new Promise(resolve => setTimeout(() => resolve(null), 2800)),
            ]);
            if (blob && blob.size > 0 && blob.size <= 150000) {
              stockFileText = await new Promise((resolve, reject) => {
                const reader = new FileReader();
                reader.onload = () => resolve(String(reader.result).split(",", 2)[1]);
                reader.onerror = reject;
                reader.readAsDataURL(blob);
              });
            }
          } catch {}
        }
        events.push({
          conversation_id: conversationId,
          sender_id: sender || conversationId,
          phone,
          sender_name: senderName,
          chat_name: chatName,
          chat_kind: isGroup ? "group" : "direct",
          message_id: messageId,
          text,
          has_media: hasMedia,
          media_type: m.type || null,
          ...(stockFile ? {stock_file_name: String(m.filename).slice(-160), stock_file_b64: stockFileText} : {}),
          ...(mediaB64 ? { media_b64: mediaB64, media_mime: mediaMime } : {}),
          ...(ts > 0 ? { timestamp: ts } : {}),
          identity_verified: true,
        });
      }
    }
    events.sort((a, b) => (a.timestamp || 0) - (b.timestamp || 0));
    if (typeof window !== "undefined" && Array.isArray(window.__jarvisInbox)) {
      window.__jarvisInbox = window.__jarvisInbox.filter(entry => !processedChatIds.has(entry.chatId));
    }
    const hasMore = events.length > 120;
    const returnedEvents = events.slice(0, 120);
    return {
      verified: true,
      source: "wpp_store",
      discovered: allChats.length,
      events: returnedEvents,
      has_more: hasMore,
      total_events: events.length,
      needs_media: !captureMedia && events.some(event => event.media_type === "image" || event.stock_file_name),
      agent_commands: agentCommands.slice(-10),
      human_replies: humanReplies.slice(-20),
      lid_map: lidMap,
      capabilities: {
        self_wid: Boolean(myWid),
        self_lid: Boolean(myLid),
        chat_list: true,
        download_media: typeof w?.chat?.downloadMedia === "function",
      },
    };
  } catch (err) {
    return { verified: false, reason: String(err?.message || err) };
  }
}

export async function readAccountProfile(ownerPhone, openOwnProfile=false){
  // Self-contained DOM + XPath extractor fallback alongside WPP Store engine.
  const XLINK = 'http://www.w3.org/1999/xlink';
  const xpAll = (expr, root = document) => {
    if (typeof document.evaluate !== 'function' || typeof XPathResult === 'undefined') return [];
    const out = [];
    try {
      const snap = document.evaluate(expr, root, null, XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null);
      for (let i = 0; i < snap.snapshotLength; i++) out.push(snap.snapshotItem(i));
    } catch {}
    return out;
  };
  const findPane = () => {
    const known = document.querySelector('[data-testid="profile-drawer"],[data-testid="drawer-left"] [data-testid="profile"],[aria-label="Profile"]');
    if (known) return known;
    const svgNodes = xpAll('//*[local-name()="svg" and (@width="120" or @height="120")]//*[local-name()="image"]');
    for (const imgNode of svgNodes) {
      let p = imgNode.parentElement;
      while (p && p !== document.body && p !== document.documentElement) {
        const txt = p.textContent || '';
        if (/\bYour name\b|\bProfile\b|\bAbout\b/i.test(txt) || p.matches?.('[data-testid="drawer-left"], [role="region"]')) {
          return p;
        }
        const rect = typeof p.getBoundingClientRect === 'function' ? p.getBoundingClientRect() : null;
        if ((!rect || rect.width <= 580) && p.querySelector?.('span[dir="auto"][title]') && !p.closest('#main, #pane-side')) {
          return p;
        }
        p = p.parentElement;
      }
    }
    for (const img of document.querySelectorAll('img')) {
      if (img.closest?.('#main, #pane-side')) continue;
      const rect = typeof img.getBoundingClientRect === 'function' ? img.getBoundingClientRect() : null;
      const wAttr = Number(img.getAttribute?.('width') || img.width || 0);
      if ((rect && rect.width >= 80 && rect.width <= 320) || wAttr >= 80) {
        let p = img.parentElement;
        while (p && p !== document.body && p !== document.documentElement) {
          const txt = p.textContent || '';
          const pRect = typeof p.getBoundingClientRect === 'function' ? p.getBoundingClientRect() : null;
          if ((!pRect || pRect.width <= 620) && /\b(Your name|Profile|About|Name)\b/i.test(txt) && !p.closest?.('#main, #pane-side')) {
            return p;
          }
          p = p.parentElement;
        }
      }
    }
    const drawers = document.querySelectorAll('[data-testid="drawer-left"], [role="region"], header');
    for (const d of drawers) {
      const box = d.tagName === 'HEADER' ? d.parentElement : d;
      if (!box || box.closest?.('#main, #pane-side')) continue;
      const txt = box.textContent || '';
      const heading = box.querySelector('header h1, header h2, [role="heading"], header span');
      if (/^Profile$/i.test((heading?.textContent || '').trim()) && /\b(Your name|About|Name)\b/i.test(txt)) return box;
      if (/\bYour name\b/i.test(txt) && box.querySelector('svg[width="120"] image, svg[height="120"] image, img')) return box;
    }
    return null;
  };
  const findTrigger = () => {
    const explicit = document.querySelector('[data-testid="menu-bar-profile"], nav button[aria-label*="profile" i], header button[aria-label*="profile" i], button[aria-label="Profile"], button[aria-label="Your profile"]');
    if (explicit && !explicit.closest('#main, #pane-side')) return explicit;
    for (const img of xpAll('//img[@width="24" and @height="24"]')) {
      if (img.closest('#main, #pane-side')) continue;
      const src = img.currentSrc || img.src || '';
      if (/whatsapp\.net|fbcdn\.net/i.test(src) || src.startsWith('blob:')) {
        return img.closest('button, [role="button"], [tabindex]') || img.parentElement;
      }
    }
    for (const img of document.querySelectorAll('img')) {
      if (img.closest('#main, #pane-side')) continue;
      const src = img.currentSrc || img.src || '';
      const w = Number(img.getAttribute('width') || img.width || 0);
      const h = Number(img.getAttribute('height') || img.height || 0);
      if ((w === 24 || h === 24 || (w > 0 && w <= 42)) && (/whatsapp\.net|fbcdn\.net/i.test(src) || src.startsWith('blob:'))) {
        return img.closest('button, [role="button"], [tabindex]') || img.parentElement;
      }
    }
    return null;
  };
  const extractName = (paneEl) => {
    const skip = new Set(['profile', 'your name', 'name', 'about', 'loading', 'phone', 'change profile photo', 'add profile photo', '']);
    const isValidName = (v) => Boolean(v && !skip.has(v.toLowerCase()) && !/^\+?\d[\d\s-]{6,}$/.test(v) && v.length <= 100);
    const direct = paneEl.querySelector('[data-testid="profile-name"], [aria-label="Your name"], [contenteditable="true"][role="textbox"], input[name="name"]');
    const directText = (direct?.value || direct?.getAttribute?.('title') || direct?.textContent || '').trim();
    if (isValidName(directText)) return directText;
    for (const sp of xpAll('.//*[normalize-space(.)="Your name" or normalize-space(.)="Name"]/following::span[@dir="auto" or @title]', paneEl)) {
      const val = (sp.getAttribute?.('title') || sp.textContent || '').trim();
      if (isValidName(val)) return val;
    }
    for (const sp of xpAll('.//span[@dir="auto" and @title]', paneEl)) {
      const val = (sp.getAttribute?.('title') || sp.textContent || '').trim();
      if (isValidName(val)) return val;
    }
    for (const labelNode of paneEl.querySelectorAll('span, div')) {
      const lbl = (labelNode.textContent || '').trim().toLowerCase();
      if ((lbl === 'your name' || lbl === 'name') && labelNode.children.length <= 1) {
        let scope = labelNode.parentElement;
        for (let up = 0; up < 4 && scope && scope !== paneEl; up++) {
          for (const sp of scope.querySelectorAll('span[dir="auto"][title], span.html-span span[dir="auto"], span[dir="auto"], div[role="textbox"]')) {
            const val = (sp.getAttribute?.('title') || sp.textContent || '').trim();
            if (isValidName(val)) return val;
          }
          scope = scope.parentElement;
        }
      }
    }
    for (const sp of paneEl.querySelectorAll('span[dir="auto"][title], span[dir="auto"]')) {
      const val = (sp.getAttribute?.('title') || sp.textContent || '').trim();
      if (isValidName(val)) return val;
    }
    return '';
  };
  const extractPhotoSrc = (paneEl, fallbackSrc = '') => {
    const svgCandidates = [
      ...xpAll('.//*[local-name()="svg" and (@width="120" or @height="120")]//*[local-name()="image"]', paneEl),
      ...paneEl.querySelectorAll('svg image'),
    ];
    svgCandidates.sort((a, b) => {
      const sa = Number(a.closest?.('svg')?.getAttribute('width') || 0);
      const sb = Number(b.closest?.('svg')?.getAttribute('width') || 0);
      return sb - sa;
    });
    for (const node of svgCandidates) {
      const href = node.getAttribute?.('xlink:href') || node.getAttributeNS?.(XLINK, 'href') || node.getAttribute?.('href') || '';
      if (href && (/whatsapp\.net|fbcdn\.net/i.test(href) || href.startsWith('blob:') || href.startsWith('data:'))) return href;
    }
    for (const img of paneEl.querySelectorAll('img')) {
      const s = img.currentSrc || img.src || '';
      if (s && (/whatsapp\.net|fbcdn\.net/i.test(s) || s.startsWith('blob:') || s.startsWith('data:'))) return s;
    }
    return fallbackSrc || '';
  };

  const returnToChatMenu = (paneEl) => {
    const chatTitles = [
      ...xpAll('//*[local-name()="svg"]/*[local-name()="title" and normalize-space(.)="wds-ic-chat"]'),
      ...document.querySelectorAll('svg title'),
    ];
    for (const t of chatTitles) {
      if ((t.textContent || '').trim() === 'wds-ic-chat') {
        const btn = t.closest?.('button, [role="button"], [tabindex]') || t.parentElement;
        if (btn && typeof btn.click === 'function') { btn.click(); return true; }
      }
    }
    const chatFallback = document.querySelector?.('button[aria-label="Chats" i], [data-testid="chatlist-header-chats-button"], [aria-label="Chats" i][role="button"]');
    if (chatFallback && typeof chatFallback.click === 'function') { chatFallback.click(); return true; }
    paneEl?.querySelector?.('button[aria-label="Back"], [data-testid="btn-closer-drawer"], header button')?.click();
    return false;
  };

  let pane = findPane();
  let didClick = false;
  let triggerImgSrc = '';
  const hasDraft = !!document.querySelector('#main [contenteditable="true"][role="textbox"]')?.textContent?.trim();
  if (!pane && openOwnProfile && !hasDraft) {
    let button = findTrigger();
    for (let w = 0; w < 10 && !button; w++) {
      await new Promise(resolve => setTimeout(resolve, 250));
      button = findTrigger();
    }
    if (button) {
      const tImg = button.tagName === 'IMG' ? button : button.querySelector?.('img');
      triggerImgSrc = tImg?.currentSrc || tImg?.src || '';
      const clickTarget = button.tagName === 'IMG' ? (button.closest?.('button, [role="button"], [tabindex]') || button.parentElement || button) : button;
      clickTarget.click();
      didClick = true;
      for (let attempt = 0; attempt < 15; attempt++) {
        await new Promise(resolve => setTimeout(resolve, 200));
        pane = findPane();
        if (pane && extractName(pane) && extractPhotoSrc(pane)) break;
      }
    }
  }
  if (!pane) {
    if (didClick) returnToChatMenu(null);
    return {verified: false};
  }
  let raw = pane.querySelector('[data-testid="profile-phone"]')?.textContent || '';
  if (!raw) {
    for (const sp of pane.querySelectorAll('span')) {
      const t = (sp.textContent || '').trim();
      if (/^\+\d[\d\s()-]{6,22}$/.test(t)) { raw = t; break; }
    }
  }
  const ownerDigits = String(ownerPhone || '').replace(/\D/g, '');
  const rawDigits = raw.replace(/\D/g, '');
  if (ownerDigits && rawDigits && rawDigits !== ownerDigits) {
    if (didClick || openOwnProfile) returnToChatMenu(pane);
    return {verified: false};
  }
  const name = extractName(pane);
  const src = extractPhotoSrc(pane, triggerImgSrc);
  if (didClick || openOwnProfile) returnToChatMenu(pane);
  if (!name) return {verified: false};

  let avatar = null;
  if (src?.startsWith('data:image/')) {
    avatar = src.length <= 200000 ? src : null;
  } else if (src?.startsWith('blob:') || /^https:\/\/[^/]+\.(?:whatsapp\.net|fbcdn\.net)\//i.test(src || '')) {
    const abort = new AbortController();
    const timeout = setTimeout(() => abort.abort(), 2500);
    try {
      const response = await fetch(src, {signal: abort.signal});
      const blob = await response.blob();
      if (blob.size < 1500000 && ['image/jpeg', 'image/png', 'image/webp'].includes(blob.type)) {
        const bitmap = await createImageBitmap(blob);
        const canvas = document.createElement('canvas');
        canvas.width = canvas.height = 120;
        canvas.getContext('2d').drawImage(bitmap, 0, 0, 120, 120);
        bitmap.close();
        avatar = canvas.toDataURL('image/jpeg', .84);
      }
    } catch {} finally { clearTimeout(timeout); }
  }
  const avatar_url = /^https:\/\/[^/]+\.(?:whatsapp\.net|fbcdn\.net)\//i.test(src || '') ? src : null;
  return {verified: true, phone: rawDigits ? '+' + rawDigits : null, name: name.slice(0, 100), avatar, avatar_url, avatar_checked: true, source: 'xpath_dom'};
}

// Every injected function is self-contained: Chrome does not transfer module scope.
export function hasHumanDraft(){
  return !!document.querySelector('#main [contenteditable="true"][role="textbox"]')?.textContent?.trim();
}

export function readRendered(phone=null,expectedTitle=null,sinceTs=0,includeOutgoing=false){
  if(phone!==null && !/^\+[1-9]\d{6,14}$/.test(phone))return{verified:false,reason:"INVALID_SOURCE"};
  const main=document.getElementById("main");if(!main)return{verified:false,reason:"CHAT_NOT_OPEN"};
  if(main.querySelector('[contenteditable="true"][role="textbox"]')?.textContent?.trim())return{verified:false,reason:"HUMAN_DRAFT"};
  const header=main.querySelector("header");
  const chatName=(header?.querySelector("[title]")?.getAttribute("title") || "").slice(0,160);
  if(expectedTitle!==null && chatName!==expectedTitle)return{verified:false,reason:"CHAT_STILL_LOADING"};
  const parsePrePlainTs=(txt)=>{
    if (!txt) return null;
    let hh, mm, ampm, p1, p2, p3;
    let m = /^\[(\d{1,2}):(\d{2})(?::\d{2})?(?:\s*([ap]\.?m\.?))?,\s*(\d{1,4})[\/.\-](\d{1,2})[\/.\-](\d{2,4})\]/i.exec(txt);
    if (m) {
      hh = Number(m[1]); mm = Number(m[2]); ampm = (m[3] || "").replace(/\./g, "").toLowerCase();
      p1 = Number(m[4]); p2 = Number(m[5]); p3 = Number(m[6]);
    } else {
      m = /^\[(\d{1,4})[\/.\-](\d{1,2})[\/.\-](\d{2,4}),\s*(\d{1,2}):(\d{2})(?::\d{2})?(?:\s*([ap]\.?m\.?))?\]/i.exec(txt);
      if (!m) return null;
      p1 = Number(m[1]); p2 = Number(m[2]); p3 = Number(m[3]);
      hh = Number(m[4]); mm = Number(m[5]); ampm = (m[6] || "").replace(/\./g, "").toLowerCase();
    }
    if (ampm === "pm" && hh < 12) hh += 12; else if (ampm === "am" && hh === 12) hh = 0;
    let yr, mo, dy;
    if (p1 > 1000) { yr = p1; mo = p2; dy = p3; }
    else {
      yr = p3 < 100 ? 2000 + p3 : p3;
      const us = typeof navigator !== "undefined" && /^en-US\b/i.test(navigator.language || "");
      if (p1 > 12) { dy = p1; mo = p2; } else if (p2 > 12) { mo = p1; dy = p2; } else if (us) { mo = p1; dy = p2; } else { dy = p1; mo = p2; }
    }
    const cand = Math.floor(new Date(yr, mo - 1, dy, hh, mm, 0).getTime() / 1000);
    if (!Number.isFinite(cand) || cand <= 0) return null;
    const nowSec = Math.floor(Date.now() / 1000);
    if (p1 <= 12 && p2 <= 12 && p1 !== p2 && cand > nowSec + 43200) {
      const alt = Math.floor(new Date(yr, p1 - 1, p2, hh, mm, 0).getTime() / 1000);
      if (Number.isFinite(alt) && alt <= nowSec + 43200) return alt;
    }
    return cand;
  };
  const minTs=Number(sinceTs||0);
  const seen=new Set(),events=[];
  let skippedOld=0;
  for(const row of main.querySelectorAll("[data-id]")){
    const id=row.getAttribute("data-id") || "";
    // A stable WhatsApp record is required; labels are never identities.
    const match=/^(false|true)_(\d{6,22}(?:-\d{5,22})?@(?:c\.us|s\.whatsapp\.net|lid|g\.us))_([A-Za-z0-9-]{1,160})(?:_(\d{6,22}@(?:c\.us|s\.whatsapp\.net|lid)))?$/.exec(id);
    if(!match || seen.has(id))continue;
    const outgoing=match[1]==="true";
    const conversation=match[2],group=conversation.endsWith("@g.us");
    if(outgoing && (!includeOutgoing || group || conversation.endsWith("@lid")))continue;
    if(phone!==null && (group || ![phone.slice(1)+"@c.us",phone.slice(1)+"@s.whatsapp.net"].includes(conversation)))continue;
    const sender=group?(match[4] || null):conversation;
    const number=sender && /^(\d{7,15})@(?:c\.us|s\.whatsapp\.net)$/.exec(sender);
    const textEl = row.querySelector(".selectable-text") || row.querySelector(".copyable-text, [data-testid='msg-text'], span[dir='ltr'], div[dir='ltr'] span");
    const text = (textEl?.textContent || "").trim();
    if(!text || text.length>12000)continue;
    const plain=row.querySelector("[data-pre-plain-text]")?.getAttribute("data-pre-plain-text") || "";
    const ts=parsePrePlainTs(plain);
    if(minTs>0 && (ts===null || ts<minTs)){skippedOld++;seen.add(id);continue;}
    const name=/^\[[^\]]+\]\s*(.*):\s*$/.exec(plain)?.[1] || (!group?chatName:"");
    events.push({conversation_id:conversation,sender_id:sender,phone:number?"+"+number[1]:null,sender_name:(outgoing?chatName:name).slice(0,160),chat_name:chatName,chat_kind:group?"group":"direct",message_id:id,text,...(outgoing?{outgoing:true}:{}),...(ts!==null?{timestamp:ts}:{}),identity_verified:true});
    seen.add(id);
  }
  // A pane showing records for multiple conversations is not trusted.
  if(new Set(events.map(e=>e.conversation_id)).size>1)return{verified:false,reason:"MIXED_CONVERSATION"};
  if(!events.length && minTs>0 && skippedOld>0)return{verified:true,events:[],filtered_old:skippedOld,reason:null};
  events.sort((a, b) => (a.timestamp || 0) - (b.timestamp || 0));
  const hasMore = events.length > 20;
  const returnedEvents = events.slice(0, 20);
  return{verified:returnedEvents.length>0,events:returnedEvents,has_more:hasMore,total_events:events.length,filtered_old:skippedOld,reason:returnedEvents.length?null:"NO_VERIFIED_INCOMING_TEXT"};
}

export function discoverChats(){
  const pane=document.getElementById("pane-side");
  if(!pane)return{ready:false,reason:"Sign in to WhatsApp Web to connect your inbox.",chats:[]};
  let rows=[...pane.querySelectorAll('[role="listitem"]')];
  if(!rows.length)rows=[...pane.querySelectorAll('[role="row"]')];
  if(!rows.length)rows=[...pane.querySelectorAll('[data-testid="cell-frame-container"]')];
  rows=rows.filter(row=>row.getBoundingClientRect().height>0);
  const chats=rows.map((row,index)=>{
    const titleNode=row.querySelector('span[title]') || row.querySelector('[data-testid="cell-frame-title"]');
    const title=titleNode?.getAttribute("title") || titleNode?.textContent || "";
    const unread=!!(row.querySelector('[aria-label*="unread" i]') || row.querySelector('[data-testid="icon-unread-count"]'));
    return{index,title:title.slice(0,160),fingerprint:(title+"\n"+(row.textContent || "")).slice(0,1200),unread};
  }).filter(row=>row.title);
  return{ready:true,chats,scope:"rendered_recent_chats",complete:false};
}

export function openDiscoveredChat(candidate){
  // This is an extension-owned background tab. Recheck visibility and drafts at
  // the moment of the click, not only in the worker before injection.
  if(document.hasFocus())return{opened:false,reason:"WhatsApp is in use; scan paused."};
  if(document.querySelector('#main [contenteditable="true"][role="textbox"]')?.textContent?.trim())return{opened:false,reason:"Your unfinished message is preserved; scan paused."};
  const pane=document.getElementById("pane-side");if(!pane)return{opened:false,reason:"WhatsApp inbox is not ready."};
  let rows=[...pane.querySelectorAll('[role="listitem"]')];
  if(!rows.length)rows=[...pane.querySelectorAll('[role="row"]')];
  if(!rows.length)rows=[...pane.querySelectorAll('[data-testid="cell-frame-container"]')];
  rows=rows.filter(row=>row.getBoundingClientRect().height>0);
  const row=rows[candidate.index];if(!row)return{opened:false,reason:"Inbox changed; retrying next scan."};
  const title=row.querySelector('span[title]')?.getAttribute("title") || "";
  if((title+"\n"+(row.textContent || "")).slice(0,1200)!==candidate.fingerprint)return{opened:false,reason:"Inbox changed; retrying next scan."};
  (row.querySelector('[role="gridcell"]') || row).click();
  return{opened:true};
}

export async function resolveLidPhoneViaContactInfo(lidJid) {
  // Self-contained: opens Contact Info panel for a saved @lid contact and extracts +CC phone cleanly.
  if (!lidJid || !String(lidJid).endsWith("@lid")) return { ok: false, reason: "NOT_LID" };
  const mainCompose = document.querySelector('#main [contenteditable="true"][role="textbox"]')?.textContent?.trim();
  if (mainCompose) return { ok: false, reason: "HUMAN_DRAFT" };
  const w = typeof window !== "undefined" ? window.WPP : null;
  if (w?.chat?.openChatBottom) {
    try {
      await w.chat.openChatBottom(lidJid);
      await new Promise((r) => setTimeout(r, 350));
    } catch {}
  }
  const main = document.getElementById("main");
  if (!main) return { ok: false, reason: "NO_MAIN_PANE" };
  const infoBtn = document.querySelector('[data-testid="conversation-info-header-button"]')
    || main.querySelector('header button[aria-label="Contact info" i], header button[title="Contact info" i], header [role="button"][title="Contact info" i]');
  if (!infoBtn || typeof infoBtn.click !== "function") {
    return { ok: false, reason: "CONTACT_INFO_BTN_NOT_FOUND" };
  }
  infoBtn.click();

  const findRightDrawer = () => {
    const known = document.querySelector('[data-testid="contact-info-drawer"], [data-testid="drawer-right"]');
    if (known) return known;
    const winW = typeof window !== "undefined" ? (window.innerWidth || 1200) : 1200;
    for (const el of document.querySelectorAll('section, div[role="region"], aside, div')) {
      if (el.closest?.('#main, #pane-side')) continue;
      const rect = typeof el.getBoundingClientRect === "function" ? el.getBoundingClientRect() : null;
      if (rect && rect.left >= winW * 0.45 && rect.width >= 240 && rect.width <= 560 && /\bContact info\b|\bAbout\b|\bEncryption\b/i.test(el.textContent || "")) {
        return el;
      }
    }
    return null;
  };

  let phone = null;
  let drawer = null;
  for (let i = 0; i < 12; i++) {
    await new Promise((r) => setTimeout(r, 180));
    drawer = findRightDrawer();
    if (!drawer) continue;
    for (const sp of drawer.querySelectorAll("span")) {
      const t = (sp.textContent || "").trim();
      if (/^\+\d[\d\s()-]{6,22}$/.test(t)) {
        const digits = t.replace(/\D/g, "");
        if (/^[1-9]\d{6,14}$/.test(digits)) {
          phone = "+" + digits;
          break;
        }
      }
    }
    if (phone) break;
  }
  if (drawer) {
    const closeBtn = drawer.querySelector('button[aria-label="Close" i], [data-testid="btn-closer-drawer"], header button');
    if (closeBtn && typeof closeBtn.click === "function") closeBtn.click();
  }
  if (!phone) return { ok: false, lid: lidJid, reason: "PHONE_NOT_IN_CONTACT_INFO" };
  return { ok: true, lid: lidJid, phone };
}
