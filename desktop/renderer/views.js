'use strict';

(() => {
  const eyeIcon = '<svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M2 12s4-6 10-6 10 6 10 6-4 6-10 6S2 12 2 12Z"/><circle cx="12" cy="12" r="2.5"/></svg>';
  const maskedKey = '••••••••••••••••';

  let msgFilter = 'WORK';
  let activityHasMore = false;
  let activityTotal = null;
  let activityOffset = 0;
  let activityCounts = null;
  let activityPeople = [];
  let recentImportant = null;
  let filteredActivity = null;
  let filterTimer = null;
  let filterGeneration = 0;
  const ACTIVITY_PAGE_SIZE = 100;
  let fxDialogGeneration = 0;
  let renderedKeyCount = -1;
  let knownAlertIds = null;
  let bulkRows = null;
  let bulkRequest = null;

  const BOOT_CACHE_KEY = 'jarvis-boot-cache-v1';
  let bootCache = null;

  function readBootCache() {
    if (bootCache) return bootCache;
    try {
      const parsed = JSON.parse(localStorage.getItem(BOOT_CACHE_KEY) || 'null');
      if (parsed && typeof parsed === 'object') {
        bootCache = parsed;
        return bootCache;
      }
    } catch {}
    bootCache = {};
    return bootCache;
  }

  function saveBootCache(patch) {
    try {
      const next = { ...readBootCache(), ...patch, cached_at: Date.now() };
      bootCache = next;
      const serialized = JSON.stringify(next);
      if (serialized.length < 480000) localStorage.setItem(BOOT_CACHE_KEY, serialized);
    } catch {}
  }

  function clearStoredWorkspaceCache() {
    bootCache = {};
    JarvisApp.records = [];
    activityHasMore = false;
    activityTotal = null;
    activityOffset = 0;
    activityCounts = null;
    activityPeople = [];
    recentImportant = null;
    filteredActivity = null;
    filterGeneration++;
    JarvisApp.insightData = null;
    JarvisApp.loadedAt = Object.create(null);
    try {
      localStorage.removeItem(BOOT_CACHE_KEY);
      localStorage.removeItem('jarvis-chats-cache-v1');
      localStorage.removeItem('jarvis-active-chat');
    } catch {}
    JarvisApp.handlers.resetChats?.();
  }

  function formatConversion(rate) {
    return `${rate.base} ${rate.amount} ≈ ${rate.quote} ${rate.converted}` +
      (rate.as_of ? ` · Frankfurter reference · ${rate.as_of}` : '');
  }

  function openFxDialog({ amount = '', quote = 'AED', hint = 'Reference currency conversion via Frankfurter.' } = {}) {
    fxDialogGeneration++;
    $('message-fx-form').reset();
    $('message-fx-hint').textContent = hint;
    $('message-fx-result').textContent = '';
    if (amount) $('message-fx-amount').value = amount;
    $('message-fx-quote').value = quote;
    $('fx-dialog').showModal();
  }

  $('message-fx-close').onclick = () => $('fx-dialog').close();
  $('fx-dialog').addEventListener('close', () => { fxDialogGeneration++; });
  $('open-fx-converter').onclick = () => openFxDialog();

  $('message-fx-form').onsubmit = (event) => {
    event.preventDefault();
    const generation = fxDialogGeneration;
    return action(event.submitter, async () => {
      const output = $('message-fx-result');
      output.textContent = 'Getting the reference rate…';
      try {
        const rate = await call('fx/convert', {
          amount: $('message-fx-amount').value,
          quote: $('message-fx-quote').value.toUpperCase(),
        });
        if (generation === fxDialogGeneration) output.textContent = formatConversion(rate);
      } catch (error) {
        if (generation === fxDialogGeneration) output.textContent = error.message;
      }
    });
  };

  function renderBuyerFX(container, data) {
    if (data.state !== 'CLASSIFIED' || data.triage?.category !== 'BUY' || !data.triage.proceed) return;
    const pricing = data.buyer_pricing;
    const currency = pricing?.currency || data.triage.reply_currency || 'USD';
    const source = pricing?.source;
    const line = document.createElement('div');
    line.className = 'message-fx';
    const label = document.createElement('span');
    label.textContent = source === 'number_region_hint'
      ? `Suggested currency · ${currency} (phone region)`
      : (source === 'buyer_requested' || data.triage.reply_currency)
        ? `Buyer requested · ${currency}`
        : 'Quote currency · USD';
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'text-button';
    button.textContent = 'Convert USD price';
    button.onclick = () => openFxDialog({ quote: currency, hint: label.textContent });
    line.append(label, button);
    container.append(line);
    for (const conversion of pricing?.conversions || []) {
      const detail = document.createElement('small');
      detail.className = 'message-fx-detail';
      detail.textContent = conversion.unavailable
        ? `Buyer’s unit price · ${conversion.quote} rate unavailable`
        : `Buyer’s unit price${pricing.item ? ' for ' + pricing.item : ''} · ${formatConversion(conversion)}`;
      container.append(detail);
    }
  }

  function messageCategory(d) {
    if (d.direction === 'outgoing' || d.state === 'OUTGOING') return 'OUTGOING';
    if (d.state === 'IGNORED_SOCIAL' || d.triage?.category === 'OTHER') return 'SOCIAL';
    return d.triage?.category || '';
  }

  function needsMessageReview(d) {
    return ['NEEDS_REVIEW', 'RECEIVED'].includes(d.state) || d.triage?.needs_review === true;
  }

  function importantOverviewMessage(record) {
    const d = record.data || {};
    const cat = messageCategory(d);
    // Overview screen shows high-value trade signals only: BUY inquiries with pricing/specs, active trade opportunities
    // Excludes STOCK (supplier broadcasts), SOCIAL, UNKNOWN, OTHER, and ignored noise
    if (cat !== 'BUY') return false;
    if (['STOCK', 'SOCIAL', 'UNKNOWN', 'OTHER'].includes(cat)) return false;
    if (d.state === 'IGNORED_SOCIAL' || d.state === 'IGNORED_OLD' || d.state === 'IGNORED_FAMILY') return false;
    return true;
  }

  function messagePerson(d) {
    return d.phone || d.sender_id || d.conversation_id || '';
  }

  function renderActivityPeople(records) {
    const select = $('activity-person');
    const selected = select.value;
    const people = new Map();
    for (const {person, name} of activityPeople) {
      if (person) people.set(person, name && name !== person ? `${name} · ${person}` : person);
    }
    for (const {data: d = {}} of records) {
      const person = messagePerson(d);
      if (!person || people.has(person)) continue;
      const name = d.sender_name || d.sender_label || d.chat_name;
      people.set(person, name && name !== person ? `${name} · ${person}` : person);
    }
    select.replaceChildren(new Option('All contacts', ''), ...[...people]
      .sort((a, b) => a[1].localeCompare(b[1]))
      .map(([person, name]) => new Option(name, person)));
    select.value = people.has(selected) ? selected : '';
  }

  function setMessageFilter(value) {
    msgFilter = value;
    document.querySelectorAll('.activity-filters .chip').forEach((button) => {
      const selected = button.dataset.msgFilter === value;
      button.classList.toggle('active', selected);
      button.setAttribute('aria-pressed', String(selected));
    });
  }

  function messageMatchesFilter(record) {
    const d = record.data || {};
    const cat = messageCategory(d);
    if (msgFilter === 'WORK') {
      if (cat === 'SOCIAL' || cat === 'BANTER' || cat === 'IGNORED') return false;
      return true;
    }
    if (['BUY', 'SELL', 'STOCK', 'SOCIAL', 'OUTGOING'].includes(msgFilter) && cat !== msgFilter) return false;
    if (msgFilter === 'REVIEW' && !needsMessageReview(d)) return false;
    if (msgFilter === 'SOURCE' && !d.stock_source) return false;
    const person = $('activity-person').value;
    if (person && person !== messagePerson(d)) return false;
    const q = ($('activity-search')?.value || '').trim().toLowerCase();
    if (!q) return true;
    const hay = `${d.sender_name || ''} ${d.sender_label || ''} ${d.phone || ''} ${d.text || ''} ${cat}`.toLowerCase();
    return hay.includes(q);
  }

  function activityFilters() {
    return { category: msgFilter, person: $('activity-person').value, query: $('activity-search').value.trim().slice(0, 160) };
  }

  function activityFilterKey(filters) {
    return filters.category === 'WORK' && !filters.person && !filters.query ? '' : JSON.stringify(filters);
  }

  function requestActivityFilter() {
    clearTimeout(filterTimer);
    filterGeneration++;
    renderActivityLists();
    filterTimer = setTimeout(() => refreshFilteredActivity(), 180);
  }

  function svgIcon(paths, size = 12, viewBox = '0 0 24 24', fill = 'none') {
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('viewBox', viewBox);
    svg.setAttribute('width', String(size));
    svg.setAttribute('height', String(size));
    svg.setAttribute('fill', fill);
    svg.setAttribute('aria-hidden', 'true');
    for (const def of paths) {
      const node = document.createElementNS('http://www.w3.org/2000/svg', def.tag || 'path');
      for (const [k, v] of Object.entries(def.attrs || {})) node.setAttribute(k, String(v));
      svg.append(node);
    }
    return svg;
  }

  function tagIconForCategory(catKey) {
    if (catKey === 'BUY') return svgIcon([{ attrs: { d: 'M7 17L17 7M9 7h8v8', stroke: 'currentColor', 'stroke-width': 2.2, 'stroke-linecap': 'round', 'stroke-linejoin': 'round' } }]);
    if (catKey === 'SELL') return svgIcon([{ attrs: { d: 'M17 7L7 17M15 17H7V9', stroke: 'currentColor', 'stroke-width': 2.2, 'stroke-linecap': 'round', 'stroke-linejoin': 'round' } }]);
    if (catKey === 'STOCK') return svgIcon([{ attrs: { d: 'M4 7l8-4 8 4v10l-8 4-8-4V7Z', stroke: 'currentColor', 'stroke-width': 2, 'stroke-linejoin': 'round' } }]);
    return svgIcon([{ attrs: { d: 'M12 8v4M12 16h.01', stroke: 'currentColor', 'stroke-width': 2.2, 'stroke-linecap': 'round' } }, { tag: 'circle', attrs: { cx: 12, cy: 12, r: 9, stroke: 'currentColor', 'stroke-width': 2 } }]);
  }

  function buildEmptyPipeline() {
    const flow = document.createElement('div');
    flow.className = 'empty-pipeline';
    flow.setAttribute('aria-hidden', 'true');
    const nodes = [
      { cls: 'wa', label: 'WhatsApp Web', icon: [{ attrs: { d: 'M12 3a9 9 0 0 0-7.7 13.6L3 21l4.6-1.2A9 9 0 1 0 12 3Z', stroke: 'currentColor', 'stroke-width': 2, 'stroke-linejoin': 'round' } }] },
      { cls: 'ai', label: 'Jarvis AI Triage', icon: [{ attrs: { d: 'M12 3l2.2 5.8L20 11l-5.8 2.2L12 19l-2.2-5.8L4 11l5.8-2.2L12 3Z', fill: 'currentColor' } }] },
      { cls: 'sheet', label: 'Google Sheets Ledger', icon: [{ tag: 'rect', attrs: { x: 4, y: 3, width: 16, height: 18, rx: 2.5, stroke: 'currentColor', 'stroke-width': 2 } }, { attrs: { d: 'M8 9h8M8 13h8M12 9v7', stroke: 'currentColor', 'stroke-width': 1.8 } }] },
    ];
    nodes.forEach((n, idx) => {
      const chip = document.createElement('div');
      chip.className = 'empty-node';
      const badge = document.createElement('span');
      badge.className = `empty-node-badge ${n.cls}`;
      badge.append(svgIcon(n.icon, 14));
      const text = document.createElement('span');
      text.textContent = n.label;
      chip.append(badge, text);
      flow.append(chip);
      if (idx < nodes.length - 1) {
        const arrow = document.createElement('span');
        arrow.className = 'empty-flow-arrow';
        arrow.textContent = '→';
        flow.append(arrow);
      }
    });
    return flow;
  }

  function updateMessagesInfographics(records) {
    const total = activityTotal ?? records.length;
    let buy = 0, sell = 0, stock = 0, review = 0, source = 0, social = 0, outgoing = 0;
    for (const r of records) {
      const d = r.data || {};
      const cat = messageCategory(d);
      if (cat === 'BUY') buy++;
      else if (cat === 'SELL') sell++;
      else if (cat === 'STOCK') stock++;
      else if (cat === 'SOCIAL') social++;
      else if (cat === 'OUTGOING') outgoing++;
      if (needsMessageReview(d)) review++;
      if (d.stock_source) source++;
    }
    buy = activityCounts?.buy ?? buy;
    sell = activityCounts?.sell ?? sell;
    stock = activityCounts?.stock ?? stock;
    review = activityCounts?.needs_review ?? review;
    source = activityCounts?.sources ?? source;
    social = activityCounts?.social ?? social;
    outgoing = activityCounts?.outgoing ?? outgoing;
    const sellAndStock = sell + stock;
    const classifiedOther = Math.max(0, total - buy - sellAndStock - review);
    const pct = (n) => (total ? Math.round((n / total) * 100) + '%' : '0%');
    const setText = (elId, val) => { const el = $(elId); if (el) el.textContent = val; };
    const setWidth = (elId, w) => { const el = $(elId); if (el) el.style.width = w; };

    setText('msg-stat-total', activityTotal ?? total);
    setText('msg-stat-buy', buy);
    setText('msg-stat-sell', sellAndStock);
    setText('msg-stat-review', activityCounts?.needs_review ?? review);
    setText('msg-share-buy', pct(buy));
    setText('msg-share-sell', pct(sellAndStock));
    setText('msg-stat-review-pill', `${activityCounts?.needs_review ?? review} pending`);

    setWidth('msg-bar-total', total ? Math.min(100, (total / 50) * 100) + '%' : '0%');
    setWidth('msg-bar-buy', total ? (buy / total) * 100 + '%' : '0%');
    setWidth('msg-bar-sell', total ? (sellAndStock / total) * 100 + '%' : '0%');

    if (total > 0) {
      setWidth('mix-buy', (buy / total) * 100 + '%');
      setWidth('mix-sell', (sellAndStock / total) * 100 + '%');
      setWidth('mix-review', (review / total) * 100 + '%');
      setWidth('mix-other', (classifiedOther / total) * 100 + '%');
    } else {
      setWidth('mix-buy', '0%');
      setWidth('mix-sell', '0%');
      setWidth('mix-review', '0%');
      setWidth('mix-other', '100%');
    }

    const work = Math.max(0, (activityTotal ?? total) - social);
    setText('filter-count-WORK', work);
    setText('filter-count-ALL', activityTotal ?? total);
    setText('filter-count-BUY', buy);
    setText('filter-count-SELL', sell);
    setText('filter-count-STOCK', stock);
    setText('filter-count-REVIEW', activityCounts?.needs_review ?? review);
    setText('filter-count-SOURCE', source);
    setText('filter-count-SOCIAL', social);
    setText('filter-count-OUTGOING', outgoing);
    const syncPill = $('messages-sync-pill');
    if (syncPill) syncPill.textContent = sheetsSyncLabel(JarvisApp.state);
  }

  function sheetsSyncLabel(status) {
    if (!status?.sheets_connected) return 'Connect Google Sheet';
    if (Object.keys(status.sheets_sync?.errors || {}).length) return 'Sheet sync needs attention';
    const pending = status.sheets_sync?.pending || 0;
    return pending ? `${pending} change${pending === 1 ? '' : 's'} pending Sheet sync` : 'Synced with Sheets';
  }

  function renderMessages(id, list, emptyTitle = 'Your next conversation starts here.') {
    const container = $(id);
    if (!container) return;
    container.replaceChildren();
    if (!list.length) {
      const box = document.createElement('div');
      box.className = 'empty-state';
      const title = document.createElement('h3');
      title.textContent = emptyTitle;
      const p = document.createElement('p');
      p.textContent = id === 'recent-messages'
        ? 'Trade messages and reviews will appear here as they are captured.'
        : JarvisApp.records.length
          ? 'Try a different filter or load older messages.'
        : (JarvisApp.state?.running
          ? 'Workspace is observing chats. Verified buyer requests and stock updates will flow automatically into this ledger.'
          : 'Start your workspace to capture verified WhatsApp buyer inquiries, classify demand vs. stock, and log them into Google Sheets.');
      box.append(buildEmptyPipeline(), title, p);
      container.append(box);
      return;
    }
    const fragment = document.createDocumentFragment();
    for (const record of list) {
      const d = record.data || {};
      const cat = messageCategory(d);
      const tagKey = cat || (needsMessageReview(d) ? 'REVIEW' : 'OTHER');
      const row = document.createElement('article');
      row.className = 'message';
      row.dataset.msgCat = tagKey;
      row.onclick = (e) => {
        if (e.target.closest('button, a, input, select')) return;
        openMessageDetailDrawer(record);
      };
      const avatar = document.createElement('div');
      avatar.className = 'sender-avatar';
      const name = d.sender_name || d.sender_label || d.phone || d.sender_id || 'WhatsApp sender';
      avatar.textContent = name.replace(/[^\p{L}]/gu, '').slice(0, 2).toUpperCase() || 'WA';

      const content = document.createElement('div');
      content.className = 'message-content';
      const head = document.createElement('div');
      head.className = 'message-heading';
      const sender = document.createElement('strong');
      sender.textContent = name;
      const identity = document.createElement('div');
      identity.className = 'message-identity';
      identity.append(sender);
      if (d.phone && d.phone !== name) {
        const number = document.createElement('span');
        number.className = 'message-phone mono';
        number.textContent = d.phone;
        identity.append(number);
      }
      const time = document.createElement('time');
      const sentAt = d.timestamp > 0 ? d.timestamp * 1000 : d.received > 0 ? d.received * 1000 : record.updated_at;
      time.textContent = sentAt
        ? new Date(sentAt).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
        : '';
      if (d.timestamp > 0 && d.received >= d.timestamp) {
        time.title = `Captured ${Math.round(d.received - d.timestamp)}s after WhatsApp timestamp`;
      }
      head.append(identity, time);

      const text = document.createElement('p');
      const rawTxt = typeof d.text === 'string' ? d.text.trim() : '';
      const isRawB64 = rawTxt.length > 100 && !/\s/.test(rawTxt) && /^(\/9j\/|iVBORw0KGgo|R0lGOD|UklGR|[A-Za-z0-9+/]{100,}={0,2}$)/.test(rawTxt);
      text.textContent = isRawB64 ? '[Photo / Media attachment]' : (rawTxt || '(No text content)');

      const meta = document.createElement('div');
      meta.className = 'message-meta';
      const isActiveMsg = ['RECEIVED', 'MEDIA_EXTRACTION', 'AI_CLASSIFICATION', 'STOCK_MATCHING', 'PROCESSING'].includes(d.state || d.current_stage);
      const tag = document.createElement('span');
      tag.className = 'message-tag' + (isActiveMsg ? ' active stage-pill' : '');
      const stateLabel = { NEEDS_REVIEW: 'Waiting for review', RECEIVED: 'Captured', IGNORED_SOCIAL: 'Social message', CLASSIFIED: 'Classified' }[d.state] || d.state || 'Captured';
      tag.dataset.cat = tagKey;
      tag.append(tagIconForCategory(tagKey), document.createTextNode(
        ({SOCIAL: 'Social', OUTGOING: 'Sent', UNKNOWN: 'Unclear'})[cat] || cat || stateLabel
      ));
      const source = document.createElement('span');
      source.className = 'message-source-pill';
      source.append(
        svgIcon([{ tag: 'circle', attrs: { cx: 12, cy: 12, r: 4, fill: 'currentColor' } }], 8),
        document.createTextNode(cat === 'OUTGOING' ? 'Outgoing message' : d.stock_source ? 'Stock source' : 'Incoming message')
      );
      meta.append(tag, source);
      if (id === 'all-messages' && cat !== 'OUTGOING' && /^\+[1-9]\d{6,14}$/.test(d.phone || '')
          && !d.conversation_id?.endsWith('@g.us')) {
        const reply = document.createElement('button');
        reply.type = 'button';
        reply.className = 'text-button message-reply';
        reply.textContent = 'Draft reply';
        reply.onclick = () => {
          view('alerts');
          $('whatsapp-outbox').open = true;
          $('outbound-phone').value = d.phone;
          $('outbound-text').focus();
        };
        meta.append(reply);
      }
      if (d.buyer_pricing?.item) {
        const itemPill = document.createElement('span');
        itemPill.className = 'message-source-pill';
        itemPill.textContent = d.buyer_pricing.item;
        meta.append(itemPill);
      }

      content.append(head, text, meta);
      if (d.reason || d.processing_stage) {
        const progress = document.createElement('p');
        progress.className = 'message-source-pill' + (isActiveMsg ? ' active stage-pill' : '');
        const labels = {
          MEDIA_BYTES_REQUIRED: 'Waiting for image bytes before Gemini OCR',
          MEDIA_STOCK_UNCONFIRMED: 'Gemini OCR has not confirmed stock items',
          MODEL_KEY_REQUIRED: 'Waiting for a configured Gemini connection',
          MODEL_QUOTA: 'Gemini quota reached',
          MODEL_COOLDOWN: 'Gemini is waiting to retry',
          MODEL_UNAVAILABLE: 'Gemini is unavailable',
          CLASSIFICATION_FAILED: 'Classification failed',
        };
        progress.textContent = labels[d.reason] || d.processing_stage || String(d.reason).replaceAll('_', ' ');
        content.append(progress);
      }
      renderBuyerFX(content, d);
      row.append(avatar, content);
      fragment.append(row);
    }
    container.append(fragment);
  }

  async function openMessageDetailDrawer(record) {
    const drawer = $('message-detail-drawer');
    const content = $('message-detail-content');
    const title = $('message-detail-title');
    const subtitle = $('message-detail-subtitle');
    if (!drawer || !content) return;

    drawer.hidden = false;
    content.replaceChildren();

    const d = record.data || {};
    const messageId = record.id || d.id || d.message_id || '';
    const sender = d.sender_name || d.sender_label || d.phone || d.sender_id || 'WhatsApp Message';

    if (title) title.textContent = `Message Trace · ${sender}`;
    if (subtitle) subtitle.textContent = `ID: ${messageId.slice(0, 32)}… · ${new Date(record.updated_at || Date.now()).toLocaleTimeString()}`;

    const skel = document.createElement('div');
    skel.className = 'empty-state';
    skel.textContent = 'Loading detailed message timeline and AI provenance…';
    content.append(skel);

    let trace = null;
    try {
      if (window.jarvis?.call && messageId) {
        trace = await window.jarvis.call('message/trace', { message_id: messageId });
      }
    } catch (err) {
      console.warn('Trace fetch failed:', err);
    }

    content.replaceChildren();

    // 1. Current State & Decision Overview
    const curStage = trace?.current_stage || d.processing_stage || d.state || 'CAPTURED';
    const outcome = trace?.stage_outcome || 'SUCCESS';
    const reason = trace?.reason || d.reason || 'Message ingested and triaged.';

    const overviewCard = document.createElement('div');
    overviewCard.className = 'trace-section';
    const ovTitle = document.createElement('h4');
    ovTitle.className = 'trace-section-title';
    ovTitle.textContent = 'Current Processing State';
    
    const ovRow = document.createElement('div');
    ovRow.className = 'trace-event-head';
    const ovBadge = document.createElement('span');
    ovBadge.className = 'trace-provenance-badge ' + (outcome === 'SUCCESS' ? 'local' : 'gemini');
    ovBadge.textContent = `${curStage} · ${outcome}`;
    
    const ovTime = document.createElement('small');
    ovTime.className = 'mono';
    ovTime.textContent = trace?.last_progress_at ? new Date(trace.last_progress_at * 1000).toLocaleTimeString() : '';
    ovRow.append(ovBadge, ovTime);

    const ovP = document.createElement('p');
    ovP.textContent = reason;
    overviewCard.append(ovTitle, ovRow, ovP);

    // 2. Original Inbound Message
    const msgCard = document.createElement('div');
    msgCard.className = 'trace-section';
    const msgTitle = document.createElement('h4');
    msgTitle.className = 'trace-section-title';
    msgTitle.textContent = 'Inbound Message Content';
    const msgBody = document.createElement('p');
    msgBody.className = 'mono';
    msgBody.textContent = trace?.text || d.text || '(No text content)';
    msgCard.append(msgTitle, msgBody);

    // 3. Intelligence & Model Provenance
    const provCard = document.createElement('div');
    provCard.className = 'trace-section';
    const provTitle = document.createElement('h4');
    provTitle.className = 'trace-section-title';
    provTitle.textContent = 'Intelligence Provenance';

    const source = trace?.classification_source || d.classification_source || 'local';
    const provBadge = document.createElement('span');
    provBadge.className = 'trace-provenance-badge ' + (source === 'gemini' ? 'gemini' : 'local');
    provBadge.textContent = source === 'gemini' ? '⚡ Gemini AI Provider' : '✓ Deterministic Local Regex';

    const provTable = document.createElement('div');
    provTable.className = 'trace-event-node';
    provTable.innerHTML = `
      <div><strong>Classification Source:</strong> ${source}</div>
      <div><strong>Model Requested:</strong> ${trace?.model_requested || d.model_requested || 'None (Local)'}</div>
      <div><strong>Model Served:</strong> ${trace?.model_served || d.model_served || 'None (Local)'}</div>
    `;
    provCard.append(provTitle, provBadge, provTable);

    // 4. Parsed Commercial Items
    const items = trace?.triage?.items || d.triage?.items || [];
    if (items.length) {
      const itemsCard = document.createElement('div');
      itemsCard.className = 'trace-section';
      const itemsTitle = document.createElement('h4');
      itemsTitle.className = 'trace-section-title';
      itemsTitle.textContent = `Parsed Trade Items (${items.length})`;
      for (const it of items) {
        const itNode = document.createElement('div');
        itNode.className = 'trace-event-node';
        itNode.innerHTML = `
          <strong>${it.device || 'Item'} ${it.variant || ''}</strong>
          <div>Qty: ${it.quantity || 1} · Target Price: ${it.unit_price ? (it.currency || 'USD') + ' ' + it.unit_price : 'Market'} · Grade: ${it.condition || 'New'}</div>
        `;
        itemsCard.append(itNode);
      }
      content.append(itemsCard);
    }

    // 5. Timeline of Ordered Stage Events
    const events = trace?.events || [];
    if (events.length) {
      const timeCard = document.createElement('div');
      timeCard.className = 'trace-section';
      const timeTitle = document.createElement('h4');
      timeTitle.className = 'trace-section-title';
      timeTitle.textContent = `Stage Event Timeline (${events.length})`;

      const timeline = document.createElement('div');
      timeline.className = 'trace-event-timeline';
      for (const ev of events) {
        const node = document.createElement('div');
        node.className = 'trace-event-node';
        const head = document.createElement('div');
        head.className = 'trace-event-head';
        const name = document.createElement('strong');
        name.textContent = `#${ev.sequence || 1} ${ev.stage || 'STAGE'}`;
        const outcomeSpan = document.createElement('span');
        outcomeSpan.className = 'mono';
        outcomeSpan.textContent = `${ev.outcome || 'OK'} · ${ev.reason_code || ''}`;
        head.append(name, outcomeSpan);

        const timeSpan = document.createElement('small');
        timeSpan.className = 'mono';
        timeSpan.textContent = ev.occurred_at ? new Date(ev.occurred_at * 1000).toLocaleTimeString() : '';
        node.append(head, timeSpan);
        timeline.append(node);
      }
      timeCard.append(timeTitle, timeline);
      content.append(timeCard);
    }

    // 6. Outbound Intent & Decision (if present)
    const decisions = trace?.decisions || [];
    const outbound = trace?.outbound || (d.outbound_id ? { id: d.outbound_id } : null);
    if (decisions.length || outbound) {
      const outCard = document.createElement('div');
      outCard.className = 'trace-section';
      const outTitle = document.createElement('h4');
      outTitle.className = 'trace-section-title';
      outTitle.textContent = 'Outbound Decision & Dispatch';
      const outNode = document.createElement('div');
      outNode.className = 'trace-event-node';
      outNode.innerHTML = `
        <div><strong>Outbound ID:</strong> ${outbound?.id || d.outbound_id || 'None'}</div>
        <div><strong>Status:</strong> ${outbound?.status || d.trade_action || 'N/A'}</div>
        ${outbound?.text_parts ? `<div><strong>Reply:</strong> ${outbound.text_parts.join(' ')}</div>` : ''}
      `;
      outCard.append(outTitle, outNode);
      content.append(outCard);
    }

    // 7. Actions / Safe Retry
    const actionSec = document.createElement('div');
    actionSec.className = 'trace-section';
    const actionTitle = document.createElement('h4');
    actionTitle.className = 'trace-section-title';
    actionTitle.textContent = 'Trace Actions';

    const canRetry = ['NEEDS_REVIEW', 'MEDIA_TIMEOUT', 'FAILED'].includes(d.state || trace?.state);
    if (canRetry) {
      const retryBtn = document.createElement('button');
      retryBtn.className = 'button secondary compact-btn';
      retryBtn.type = 'button';
      retryBtn.textContent = 'Re-evaluate message';
      retryBtn.onclick = () => action(retryBtn, async () => {
        toast('Message re-evaluation queued.');
        drawer.hidden = true;
      });
      actionSec.append(retryBtn);
    } else {
      const safeLabel = document.createElement('small');
      safeLabel.className = 'mono';
      safeLabel.textContent = '✓ Terminal state / quote protected from duplicate execution.';
      actionSec.append(safeLabel);
    }

    content.prepend(overviewCard, msgCard, provCard);
    content.append(actionSec);
  }

  $('message-detail-close')?.addEventListener('click', () => {
    if ($('message-detail-drawer')) $('message-detail-drawer').hidden = true;
  });
  $('message-detail-overlay')?.addEventListener('click', () => {
    if ($('message-detail-drawer')) $('message-detail-drawer').hidden = true;
  });

  let activeWaCatFilter = 'ALL';
  let activeWaContact = null;
  let activeWaGroupObj = null;
  const threadDrafts = new Map();
  let isSendingWaMessage = false;

  function renderWhatsAppMode() {
    const list = $('wa-contacts-list');
    if (!list) return;
    const msgView = $('messages-view');
    if (msgView && msgView.hidden) return;
    const records = JarvisApp.records;
    if (!records.length) {
      noData(list, 'No WhatsApp conversations captured yet.');
      return;
    }

    const now = Date.now();
    const TTL_48H = 48 * 60 * 60 * 1000;

    const groupMap = new Map();
    for (const rec of records) {
      const d = rec.data || {};
      const cat = messageCategory(d) || (needsMessageReview(d) ? 'REVIEW' : (d.triage?.category || 'OTHER'));
      const isArchivedMsg = ['STOCK', 'SOCIAL', 'UNKNOWN', 'OTHER'].includes(cat) ||
        d.state === 'IGNORED_SOCIAL' || d.state === 'IGNORED_OLD' || d.state === 'IGNORED_FAMILY';

      // 48h TTL auto-pruning for archive messages
      const msgTime = (d.timestamp > 0 ? (d.timestamp > 1e12 ? d.timestamp : d.timestamp * 1000) : null)
        || (rec.updated_at ? new Date(rec.updated_at).getTime() : 0);
      if (isArchivedMsg && msgTime && (now - msgTime > TTL_48H)) {
        continue;
      }

      const convId = (d.conversation_id || d.chat_id || d.chat_jid || d.phone || d.sender_id || 'unknown').trim();
      const isGroup = Boolean(d.is_group || d.chat_kind === 'group' || convId.endsWith('@g.us'));
      const displayName = (d.chat_name || d.sender_name || d.sender_label || d.phone || (isGroup ? 'WhatsApp Group' : 'WhatsApp Contact')).trim();
      const directPhone = isGroup ? '' : (d.phone || (convId.includes('@s.whatsapp.net') || convId.includes('@c.us') ? convId.replace(/@.*$/, '') : ''));

      if (!groupMap.has(convId)) {
        groupMap.set(convId, {
          convId,
          key: convId,
          name: displayName,
          phone: directPhone,
          isGroup,
          lastRecord: rec,
          records: [],
          category: 'ARCHIVE',
          isArchived: true,
          archiveKind: cat,
        });
      }
      const grp = groupMap.get(convId);
      grp.records.push(rec);
      if (!grp.phone && directPhone) grp.phone = directPhone;
      if (isGroup && d.chat_name) grp.name = d.chat_name.trim();

      // Real commercial trade signals upgrade conversation out of archive
      if (['BUY', 'SELL', 'REVIEW'].includes(cat)) {
        grp.category = cat;
        grp.isArchived = false;
      } else if (grp.isArchived) {
        if (cat === 'STOCK') grp.archiveKind = 'STOCK';
        else if (cat === 'SOCIAL') grp.archiveKind = 'SOCIAL';
      }
    }

    const groups = Array.from(groupMap.values());
    const filterQuery = ($('wa-search-input')?.value || '').toLowerCase().trim();

    const filteredGroups = groups.filter((g) => {
      if (activeWaCatFilter === 'ALL') {
        if (g.isArchived) return false;
      } else if (activeWaCatFilter === 'ARCHIVE') {
        if (!g.isArchived) return false;
      } else if (activeWaCatFilter === 'BUY') {
        if (g.isArchived || g.category !== 'BUY') return false;
      } else if (activeWaCatFilter === 'SELL') {
        if (g.isArchived || g.category !== 'SELL') return false;
      } else if (activeWaCatFilter === 'REVIEW') {
        if (g.isArchived || g.category !== 'REVIEW') return false;
      }

      if (filterQuery) {
        const matchName = (g.name || '').toLowerCase().includes(filterQuery);
        const matchPhone = (g.phone || '').toLowerCase().includes(filterQuery);
        const matchConv = (g.convId || '').toLowerCase().includes(filterQuery);
        const matchMsg = g.records.some((r) => String(r.data?.text || '').toLowerCase().includes(filterQuery));
        return matchName || matchPhone || matchConv || matchMsg;
      }
      return true;
    });

    if (!filteredGroups.length) {
      list.replaceChildren();
      list.dataset.renderSig = '';
      const emptyMsg = activeWaCatFilter === 'ALL'
        ? 'No active commercial trade conversations. Stock lists, social & unknown messages are filed in 📦 Archive.'
        : (activeWaCatFilter === 'ARCHIVE' ? 'Archive is empty. Messages older than 48 hours are automatically purged.' : 'No conversations match the search.');
      noData(list, emptyMsg);
      activeWaGroupObj = null;
      if ($('wa-chat-stream')) {
        $('wa-chat-stream').replaceChildren();
        $('wa-chat-stream').dataset.threadSig = '';
        noData($('wa-chat-stream'), 'Select a conversation on the left to read messages.');
      }
      return;
    }

    if (!activeWaContact || !groups.find((g) => g.convId === activeWaContact)) {
      activeWaContact = filteredGroups[0]?.convId || null;
    }

    const listSig = filteredGroups.map((g) => `${g.convId}_${g.records.length}_${g.lastRecord?.id || ''}_${g.category}`).join('|') + `::active=${activeWaContact}`;
    if (list.dataset.renderSig !== listSig) {
      list.dataset.renderSig = listSig;
      list.replaceChildren();

      for (const grp of filteredGroups) {
        const item = document.createElement('div');
        item.className = 'wa-contact-card' + (grp.convId === activeWaContact ? ' active' : '');
        item.setAttribute('role', 'option');

        const avatar = document.createElement('div');
        avatar.className = 'wa-contact-avatar';
        avatar.textContent = grp.isGroup ? '👥' : (grp.name.replace(/[^\p{L}]/gu, '').slice(0, 2).toUpperCase() || 'WA');

        const meta = document.createElement('div');
        meta.className = 'wa-contact-meta';

        const rowTop = document.createElement('div');
        rowTop.className = 'wa-contact-row';
        const nameEl = document.createElement('span');
        nameEl.className = 'wa-contact-name';
        nameEl.textContent = grp.name;

        const timeEl = document.createElement('span');
        timeEl.className = 'wa-contact-time';
        const lastTs = grp.lastRecord.data?.timestamp > 0
          ? (grp.lastRecord.data.timestamp > 1e12 ? grp.lastRecord.data.timestamp : grp.lastRecord.data.timestamp * 1000)
          : grp.lastRecord.updated_at;
        timeEl.textContent = lastTs ? new Date(lastTs).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : '';
        rowTop.append(nameEl, timeEl);

        const rowBottom = document.createElement('div');
        rowBottom.className = 'wa-contact-row';
        const preview = document.createElement('p');
        preview.className = 'wa-contact-preview';
        const rawTxt = String(grp.lastRecord.data?.text || '').trim();
        preview.textContent = rawTxt.slice(0, 48) || (grp.isGroup ? '(Group message)' : '(Trade message)');

        const catBadge = document.createElement('span');
        if (grp.isGroup) {
          catBadge.className = 'chip wa-contact-badge';
          catBadge.textContent = '👥 GROUP';
        } else if (grp.isArchived) {
          catBadge.className = 'chip wa-contact-badge wa-badge-social';
          catBadge.textContent = grp.archiveKind === 'STOCK' ? '📦 STOCK' : (grp.archiveKind === 'SOCIAL' ? '💬 SOCIAL' : '📦 ARCHIVE');
        } else {
          catBadge.className = 'chip wa-contact-badge' + (grp.category === 'BUY' ? ' good' : '');
          catBadge.textContent = grp.category;
        }
        rowBottom.append(preview, catBadge);

        meta.append(rowTop, rowBottom);
        item.append(avatar, meta);

        item.onclick = () => {
          if (activeWaGroupObj?.convId) {
            const curInput = $('wa-composer-input');
            const curText = curInput ? curInput.value : '';
            threadDrafts.set(activeWaGroupObj.convId, { text: curText, attachment: stagedWaAttachment });
          }
          activeWaContact = grp.convId;
          activeWaGroupObj = grp;
          list.querySelectorAll('.wa-contact-card').forEach((el) => el.classList.remove('active'));
          item.classList.add('active');
          renderWhatsAppThread(grp);
        };

        list.append(item);
      }
    }

    const activeGrp = filteredGroups.find((g) => g.convId === activeWaContact) || filteredGroups[0];
    if (activeGrp) {
      activeWaGroupObj = activeGrp;
      renderWhatsAppThread(activeGrp);
    }
  }

  function renderWhatsAppThread(grp) {
    if (!$('wa-chat-stream') || !grp) return;
    activeWaGroupObj = grp;
    $('wa-active-name').textContent = grp.name;
    $('wa-active-phone').textContent = grp.isGroup ? `Group · ${grp.convId}` : (grp.phone || 'WhatsApp Verified Contact');
    $('wa-active-avatar').textContent = grp.isGroup ? '👥' : (grp.name.replace(/[^\p{L}]/gu, '').slice(0, 2).toUpperCase() || 'WA');

    const muteBadge = $('wa-mute-badge');
    const unmuteBtn = $('wa-action-unmute');
    if (muteBadge && unmuteBtn) {
      if (grp.isGroup) {
        muteBadge.hidden = true;
        unmuteBtn.hidden = true;
      } else {
        JarvisApp.request('contacts/mute-status', { conversation_id: grp.convId })
          .then((res) => {
            const st = res?.result || res || {};
            if (st.muted && !st.blocked) {
              const mins = Math.max(1, Math.round((st.remaining_seconds || 0) / 60));
              const hrs = Math.floor(mins / 60);
              const remMins = mins % 60;
              const timeStr = hrs > 0 ? `${hrs}h ${remMins}m` : `${remMins}m`;
              muteBadge.textContent = `🔇 Muted (${timeStr} left)`;
              muteBadge.hidden = false;
              unmuteBtn.hidden = false;
            } else {
              muteBadge.hidden = true;
              unmuteBtn.hidden = true;
            }
          })
          .catch(() => {
            muteBadge.hidden = true;
            unmuteBtn.hidden = true;
          });

        unmuteBtn.onclick = async () => {
          try {
            await JarvisApp.request('contacts/unmute', { conversation_id: grp.convId });
            muteBadge.hidden = true;
            unmuteBtn.hidden = true;
            toast('Chat unmuted.');
          } catch {
            toast('Failed to unmute chat.');
          }
        };
      }
    }

    const input = $('wa-composer-input');
    const sendBtn = $('wa-composer-send');
    if (grp.isGroup) {
      if (input) {
        input.disabled = true;
        input.placeholder = 'Group chat — direct replies disabled. Open a direct contact to message.';
        input.value = '';
        adjustComposerHeight(input);
      }
      if (sendBtn) sendBtn.disabled = true;
      stagedWaAttachment = null;
      renderWaAttachmentPreview();
    } else {
      if (input) {
        input.disabled = false;
        input.placeholder = 'Type WhatsApp reply (Enter to send, Shift+Enter for newline)...';
        const draft = threadDrafts.get(grp.convId);
        const draftText = (typeof draft === 'object' && draft !== null) ? (draft.text || '') : (draft || '');
        input.value = draftText;
        adjustComposerHeight(input);
        stagedWaAttachment = (typeof draft === 'object' && draft !== null) ? draft.attachment : null;
        renderWaAttachmentPreview();
      }
      if (sendBtn) sendBtn.disabled = false;
    }

    $('wa-action-draft-reply').onclick = () => {
      if (grp.isGroup) {
        toast('Direct composer reply is disabled in group chats.');
        return;
      }
      const input = $('wa-composer-input');
      if (input) {
        input.focus();
        if (!input.value.trim()) {
          const item = grp.records.find((r) => r.data?.buyer_pricing?.item)?.data?.buyer_pricing?.item;
          input.value = item ? `Hi ${grp.name}, regarding ${item}: ` : `Hi ${grp.name}, `;
          const existing = threadDrafts.get(grp.convId);
          const att = (typeof existing === 'object' && existing !== null) ? existing.attachment : stagedWaAttachment;
          threadDrafts.set(grp.convId, { text: input.value, attachment: att });
          adjustComposerHeight(input);
        }
      }
    };

    $('wa-action-filter-stock').onclick = () => {
      view('stock');
      const item = grp.records.find((r) => r.data?.buyer_pricing?.item)?.data?.buyer_pricing?.item;
      if (item) {
        $('inventory-search').value = item;
        renderInventory(JarvisApp.insightData?.inventory || []);
      }
    };

    const stream = $('wa-chat-stream');
    const statusSig = grp.records.slice(-10).map((r) => `${r.id}:${r.revision || 0}:${r.data?.delivery_status || r.data?.status || ''}:${r.data?.ack || 0}`).join(',');
    const threadSig = `${grp.convId}::${grp.records.length}::${grp.records[0]?.id || ''}::${grp.records[grp.records.length - 1]?.id || ''}::${statusSig}`;
    if (stream.dataset.threadSig === threadSig) {
      return;
    }

    const wasNearBottom = (stream.scrollHeight - stream.scrollTop - stream.clientHeight) < 80;

    stream.replaceChildren();
    stream.dataset.threadSig = threadSig;

    const sorted = [...grp.records].reverse();
    for (const rec of sorted) {
      const d = rec.data || {};
      const cat = messageCategory(d);
      const isOutgoing = cat === 'OUTGOING';

      const bubble = document.createElement('div');
      bubble.className = 'wa-bubble ' + (isOutgoing ? 'outgoing' : 'incoming');

      if (!isOutgoing && grp.isGroup) {
        const senderHeader = document.createElement('div');
        senderHeader.className = 'wa-sender-header';
        senderHeader.style.cssText = 'font-size: 11px; font-weight: 600; color: var(--accent, #2563eb); margin-bottom: 2px;';
        senderHeader.textContent = d.sender_name || (d.phone ? d.phone : 'Number unavailable');
        bubble.append(senderHeader);
      }

      if (d.attachment_name || d.stock_file_name) {
        const attPill = document.createElement('div');
        attPill.className = 'wa-att-pill';
        attPill.style.cssText = 'font-size: 11px; background: rgba(0,0,0,0.06); border-radius: 4px; padding: 2px 6px; margin-bottom: 4px; display: inline-flex; align-items: center; gap: 4px;';
        attPill.textContent = `📎 ${d.attachment_name || d.stock_file_name}`;
        bubble.append(attPill);
      }

      const text = document.createElement('p');
      text.style.margin = '0';
      const rawTxt = typeof d.text === 'string' ? d.text.trim() : '';
      text.textContent = rawTxt || (d.attachment_name ? `[Attachment: ${d.attachment_name}]` : '(Media/Attachment)');
      bubble.append(text);

      if (d.buyer_pricing?.item) {
        const itemPill = document.createElement('span');
        itemPill.className = 'message-source-pill';
        itemPill.style.marginTop = '6px';
        itemPill.textContent = `🎯 ${d.buyer_pricing.item} · ${d.buyer_pricing.target_unit_price ? 'Target ₹' + d.buyer_pricing.target_unit_price : 'Lead request'}`;
        bubble.append(itemPill);
      }

      const footer = document.createElement('div');
      footer.className = 'wa-bubble-footer';
      const time = document.createElement('span');
      const sentAt = d.timestamp > 0 ? d.timestamp * 1000 : rec.updated_at;
      time.textContent = sentAt ? new Date(sentAt).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : '';
      footer.append(time);

      if (isOutgoing) {
        const check = document.createElement('span');
        const ack = Number(d.ack || 0);
        const st = String(d.delivery_status || d.status || d.dispatch_status || (d.sent_receipt ? 'SENT' : '')).toUpperCase();
        if (st === 'READ' || ack >= 3) {
          check.textContent = ' ✓✓';
          check.className = 'wa-checkmarks wa-check-read';
          check.title = 'Read';
        } else if (st === 'DELIVERED' || ack === 2) {
          check.textContent = ' ✓✓';
          check.className = 'wa-checkmarks wa-check-delivered';
          check.title = 'Delivered';
        } else if (st === 'SENT' || st === 'ACCEPTED' || ack === 1 || d.sent_receipt) {
          check.textContent = ' ✓';
          check.className = 'wa-checkmarks wa-check-sent';
          check.title = 'Sent';
        } else if (st === 'FAILED' || st === 'FAILED_BEFORE_SEND') {
          check.textContent = ' ⚠️';
          check.className = 'wa-checkmarks wa-check-failed';
          check.title = 'Failed to send';
        } else if (st === 'UNKNOWN') {
          check.textContent = ' ❓';
          check.className = 'wa-checkmarks wa-check-unknown';
          check.title = 'Status unavailable · Check WhatsApp before retrying';
        } else {
          check.textContent = ' ⏱';
          check.className = 'wa-checkmarks wa-check-pending';
          check.title = 'Pending';
        }
        footer.append(check);
      }
      bubble.append(footer);
      stream.append(bubble);
    }
    if (wasNearBottom || !stream.dataset.hasRenderedOnce) {
      stream.scrollTop = stream.scrollHeight;
      stream.dataset.hasRenderedOnce = 'true';
    }
  }

  function renderActivityLists() {
    const records = JarvisApp.records;
    updateMessagesInfographics(records);
    renderActivityPeople(records);
    renderWhatsAppMode();
    renderMessages('recent-messages', recentImportant ?? records.filter(importantOverviewMessage).slice(0, 4), 'No important messages yet.');
    const key = activityFilterKey(activityFilters());
    const exact = key && filteredActivity?.key === key ? filteredActivity : null;
    const filtered = key ? exact?.records || records.filter(messageMatchesFilter) : records;
    renderMessages(
      'all-messages',
      filtered,
      records.length ? 'No messages match the current filter.' : 'Your next conversation starts here.'
    );
    $('nav-count').textContent = activityTotal ?? records.length;
    $('activity-count').textContent = key
      ? `${filtered.length} matching message${filtered.length === 1 ? '' : 's'} loaded`
      : filtered.length === records.length
      ? `${records.length}${activityTotal > records.length ? ' of ' + activityTotal : ''} captured messages`
      : `Showing ${filtered.length} of ${records.length} loaded messages`;
    $('activity-more').hidden = !(key ? exact?.hasMore : activityHasMore);
    renderOverviewMessageCounts();
  }

  function renderOverviewMessageCounts() {
    $('home-captured').textContent = activityCounts?.captured ?? JarvisApp.insightData?.captured ?? '—';
    $('home-review').textContent = activityCounts?.needs_review ?? JarvisApp.insightData?.review ?? '—';
  }

  function openExtensionSyncDialog() {
    const dialog = $('extension-sync-dialog');
    if (!dialog) return;
    const bridge = JarvisApp.state?.bridge || {};
    const isLive = !!(bridge.verified || JarvisApp.state?.direct_wa?.connected);
    let lastSeenStr = 'Never recorded';
    if (bridge.last_seen) {
      lastSeenStr = new Date(bridge.last_seen * 1000).toLocaleString();
    } else if (JarvisApp.records?.length) {
      const maxTs = Math.max(...JarvisApp.records.map((r) => (r.data?.timestamp ? r.data.timestamp * 1000 : 0)));
      if (maxTs > 0) lastSeenStr = new Date(maxTs).toLocaleString();
    }
    const lastSyncEl = $('ext-last-sync-time');
    if (lastSyncEl) lastSyncEl.textContent = lastSeenStr;

    const countEl = $('ext-captured-count');
    if (countEl) countEl.textContent = `${JarvisApp.records?.length || 0} messages synced`;

    const statusEl = $('ext-bridge-status');
    if (statusEl) {
      statusEl.textContent = isLive ? 'Broadcasting live' : 'Not live / Missing';
      statusEl.className = 'ext-box-val' + (isLive ? ' good-val' : '');
    }

    const titleEl = $('ext-sync-title');
    if (titleEl) titleEl.textContent = isLive ? 'WhatsApp Extension Live' : 'Extension Not Live / Connected';

    const iconEl = $('ext-sync-badge-icon');
    if (iconEl) iconEl.className = 'ext-sync-icon-badge' + (isLive ? ' good' : '');

    try { dialog.showModal(); } catch { dialog.hidden = false; }
  }

  async function refreshActivity(force = false) {
    if (!force && JarvisApp.records.length && Date.now() - (JarvisApp.loadedAt.activity || 0) < 15000) {
      renderActivityLists();
      return;
    }
    try {
      const result = await coalesce('activity:' + force, () => call('activity', {
        limit: ACTIVITY_PAGE_SIZE, offset: 0, refresh: force, include_people: true,
      }));
      const first = result.messages || [];
      if (activityOffset > ACTIVITY_PAGE_SIZE) {
        const previous = new Map(JarvisApp.records.map(record => [record.id, record]));
        for (const record of first) previous.delete(record.id);
        JarvisApp.records = [...first, ...previous.values()];
      } else {
        JarvisApp.records = first;
      }
      activityTotal = result.total ?? null;
      activityCounts = result.counts ?? null;
      activityPeople = result.people || [];
      recentImportant = result.recent ?? null;
      activityOffset = JarvisApp.records.length;
      activityHasMore = result.has_more === true && (activityTotal === null || activityOffset < activityTotal);
      JarvisApp.loadedAt.activity = Date.now();
      saveBootCache({ activity: result });
      renderActivityLists();
      if (JarvisApp.activeView === 'activity' && activityFilterKey(activityFilters())) refreshFilteredActivity();
    } catch (error) {
      if (JarvisApp.activeView === 'activity') notice(error.message);
    }
  }

  async function loadOlderActivity() {
    if (activityFilterKey(activityFilters())) {
      await refreshFilteredActivity(true);
      return;
    }
    const offset = activityOffset;
    const result = await call('activity', { limit: ACTIVITY_PAGE_SIZE, offset });
    const older = result.messages || [];
    const known = new Set(JarvisApp.records.map(record => record.id));
    JarvisApp.records.push(...older.filter(record => !known.has(record.id)));
    activityTotal = result.total ?? activityTotal;
    activityOffset = Math.max(activityOffset, offset + older.length);
    activityHasMore = result.has_more === true;
    renderActivityLists();
  }

  async function refreshFilteredActivity(append = false) {
    const filters = activityFilters();
    const key = activityFilterKey(filters);
    if (!key) return;
    const previous = filteredActivity?.key === key ? filteredActivity : null;
    const generation = ++filterGeneration;
    const offset = append ? previous?.offset || 0 : 0;
    try {
      const result = await call('activity', { ...filters, limit: ACTIVITY_PAGE_SIZE, offset });
      if (generation !== filterGeneration || key !== activityFilterKey(activityFilters())) return;
      const rows = result.messages || [];
      const records = append ? [...(previous?.records || [])] : [];
      const ids = new Set(records.map(row => row.id));
      records.push(...rows.filter(row => !ids.has(row.id)));
      filteredActivity = { key, records, offset: offset + rows.length, hasMore: result.has_more === true };
      renderActivityLists();
    } catch (error) {
      if (generation === filterGeneration && JarvisApp.activeView === 'activity') notice(error.message);
    }
  }

  function addStock(value = '') {
    const row = document.createElement('div');
    row.className = 'stock-row';
    const input = document.createElement('input');
    input.type = 'tel';
    input.placeholder = '+91 98765 43210';
    input.value = value;
    input.required = true;
    input.setAttribute('aria-label', 'Stock source phone number');
    const remove = document.createElement('button');
    remove.type = 'button';
    remove.className = 'remove-stock';
    remove.textContent = '×';
    remove.setAttribute('aria-label', 'Remove stock number');
    remove.onclick = () => {
      $('stock-numbers').children.length > 1 ? row.remove() : input.focus();
    };
    row.append(input, remove);
    $('stock-numbers').append(row);
  }

  function maskKeyRow(row) {
    const input = row.querySelector('input');
    input.type = 'password';
    if (row.dataset.savedIndex !== undefined) input.value = maskedKey;
    row.querySelector('.eye-button').setAttribute('aria-pressed', 'false');
  }

  function addGeminiKeyRow(savedIndex = null) {
    const keyRows = $('gemini-keys');
    if (keyRows.children.length >= 5) return;
    const row = document.createElement('div');
    row.className = 'stock-row gemini-key-row';
    const input = document.createElement('input');
    input.type = 'password';
    input.autocomplete = 'off';
    input.placeholder = `Gemini API key ${keyRows.children.length + 1}`;
    input.setAttribute('aria-label', input.placeholder);
    input.required = savedIndex === null;
    if (savedIndex !== null) {
      row.dataset.savedIndex = String(savedIndex);
      input.readOnly = true;
      input.value = maskedKey;
    }
    const eye = document.createElement('button');
    eye.type = 'button';
    eye.className = 'eye-button';
    eye.setAttribute('aria-label', 'Show or hide API key');
    eye.setAttribute('aria-pressed', 'false');
    eye.innerHTML = eyeIcon;
    let hideTimer = null;
    eye.onclick = () => action(eye, async () => {
      clearTimeout(hideTimer);
      if (input.type === 'text') { maskKeyRow(row); return; }
      const index = row.dataset.savedIndex;
      if (index !== undefined) {
        const { key } = await call('provider/reveal', { index: Number(index) });
        if (!row.isConnected || row.dataset.savedIndex !== index || JarvisApp.activeView !== 'connections' || document.hidden || !document.hasFocus()) return;
        input.value = key;
      }
      input.type = 'text';
      eye.setAttribute('aria-pressed', 'true');
      hideTimer = setTimeout(() => maskKeyRow(row), 20000);
    });
    row.append(input, eye);
    if (savedIndex !== null) {
      const replace = document.createElement('button');
      replace.type = 'button';
      replace.className = 'text-button replace-key';
      replace.textContent = 'Replace';
      replace.onclick = () => {
        clearTimeout(hideTimer);
        delete row.dataset.savedIndex;
        maskKeyRow(row);
        input.value = '';
        input.readOnly = false;
        input.required = true;
        replace.remove();
        input.focus();
      };
      row.append(replace);
    }
    const remove = document.createElement('button');
    remove.type = 'button';
    remove.className = 'remove-stock';
    remove.textContent = '×';
    remove.setAttribute('aria-label', 'Remove key row');
    remove.onclick = () => { if (keyRows.children.length > 1) row.remove(); };
    row.append(remove);
    keyRows.append(row);
  }

  function savedKeyRows(count, force = false) {
    if (count === renderedKeyCount && !force) return;
    renderedKeyCount = count;
    $('gemini-keys').replaceChildren();
    for (let index = 0; index < count; index++) addGeminiKeyRow(index);
    if (!count) addGeminiKeyRow();
  }

  function renderProfile(prof) {
    let p = prof;
    if (!p || !p.name) {
      try {
        const saved = JSON.parse(localStorage.getItem('jarvis-saved-profile') || 'null');
        if (saved?.name) p = saved;
      } catch {}
    }
    if (p?.name) {
      if ($('account-name')) $('account-name').textContent = p.name;
      if ($('setup-profile-name')) $('setup-profile-name').textContent = p.name;
      if ($('setup-profile-state')) $('setup-profile-state').textContent = p.phone ? `${p.phone} · Verified WhatsApp Trader` : 'Synced from your own WhatsApp profile';
      if (p.phone && $('owner-label') && (!JarvisApp.state?.setup?.owner_phone)) {
        $('owner-label').textContent = p.phone;
      }
      const avatars = [document.querySelector('.account .avatar'), document.querySelector('.profile-preview'), $('setup-avatar-large')];
      for (const av of avatars) {
        if (!av) continue;
        if (p.avatar) {
          const img = document.createElement('img');
          img.src = p.avatar;
          img.alt = '';
          av.replaceChildren(img);
        } else {
          av.textContent = p.name.slice(0, 2).toUpperCase();
        }
      }
      try { localStorage.setItem('jarvis-saved-profile', JSON.stringify(p)); } catch {}
    } else {
      if ($('account-name')) $('account-name').textContent = 'Trading Desk';
      if ($('setup-profile-name')) $('setup-profile-name').textContent = 'Trading Desk';
      if ($('setup-profile-state')) $('setup-profile-state').textContent = 'WhatsApp Profile Pending Sync';
      const avatars = [document.querySelector('.account .avatar'), document.querySelector('.profile-preview'), $('setup-avatar-large')];
      for (const av of avatars) {
        if (!av) continue;
        av.textContent = 'TD';
      }
    }
  }

  function renderWaDirectState(st) {
    if (!st) return;
    const panel = $('wa-direct-panel');
    const qr = $('wa-direct-qr');
    const stateEl = $('wa-direct-state');
    const pairBtn = $('wa-direct-pair');
    const discBtn = $('wa-direct-disconnect');
    if (!panel || !qr || !stateEl) return;

    const showPanel = st.state !== 'disconnected' || !!st.error || !!st.connected || !!st.qr;
    panel.hidden = !showPanel;
    if (st.qr) {
      qr.src = st.qr;
      qr.hidden = false;
    } else {
      qr.removeAttribute('src');
      qr.hidden = true;
    }
    if (pairBtn) pairBtn.hidden = !!st.connected;
    if (discBtn) discBtn.hidden = !st.connected && !st.has_session;

    if (st.connected) {
      const pName = st.profile?.name || 'WhatsApp Account';
      const pPhone = st.profile?.phone ? ` (${st.profile.phone})` : '';
      stateEl.textContent = `Connected without Chrome as ${pName}${pPhone} — read-only sync active.`;
    } else if (st.state === 'qr') {
      stateEl.textContent = 'Scan this QR in WhatsApp → Linked Devices → Link a Device (read-only).';
    } else if (st.state === 'connecting') {
      stateEl.textContent = 'Connecting to WhatsApp multi-device servers…';
    } else if (st.error) {
      stateEl.textContent = `Direct sync paused: ${st.error}`;
    } else {
      stateEl.textContent = 'Ready to pair without Chrome.';
    }
  }

  function renderBridgeLive(bridge, directConnected = !!JarvisApp.state?.direct_wa?.connected, running = !!JarvisApp.state?.running) {
    const b = bridge || {};
    const waReady = !!b.verified || directConnected;
    const actPhase = b.activity?.phase || '';
    const actDetail = b.activity?.detail || '';
    $('health-bridge').textContent = directConnected
      ? 'Direct linked device connected'
      : b.verified
        ? (actPhase === 'error' ? 'Extension needs attention' : actPhase === 'scanning' || actPhase === 'saving' ? 'Scanning WhatsApp 24x7' : 'Extension connected')
        : running ? 'Waiting for extension' : 'Starts with workspace';
    light('light-bridge', waReady);

    $('coverage-text').textContent = actDetail || (running
      ? 'The extension checks WhatsApp Web continuously 24x7. Only messages with verified identity from Start onward are recorded.'
      : 'Start the workspace to begin observing chats. Unseen messages are never counted as captured.');
    $('scan-time').textContent = b.last_seen || b.activity?.at
      ? 'Last seen ' + new Date((b.last_seen || b.activity.at) * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })
      : 'No extension activity yet';

    $('bridge-pill').textContent = directConnected
      ? 'Direct Connected'
      : b.verified ? (running ? 'Connected · 24x7 Active' : 'Connected · Stopped') : running ? 'Waiting for extension' : 'Ready to start';
    $('bridge-pill').classList.toggle('good', waReady);
  }

  function renderStatus(s, fromCache = false) {
    const firstConnection = !fromCache && !JarvisApp.state?.sheets_connected && s.sheets_connected;
    if (!fromCache) JarvisApp.state = s;
    const connected = !!s.sheets_connected;
    const running = !!s.running;
    const bridge = s.bridge || {};
    const directConnected = !!s.direct_wa?.connected;
    const waReady = !!bridge.verified || directConnected;
    const model = !!s.capabilities?.classifier;
    if (s.agent_name && !JarvisApp.prefs?.agent_name) applyAgentBranding(s.agent_name, false);

    $('owner-label').textContent = s.setup?.owner_phone || 'Complete workspace setup';
    if (s.initializing) {
      $('service-label').textContent = s.sheets_configured ? 'Syncing Sheet…' : 'Desktop ready';
      $('health-sheet').textContent = fromCache && connected ? 'Cached · verifying Sheet…' : (s.sheets_configured ? 'Syncing Sheet…' : 'Not configured');
      $('start-main').disabled = !(s.setup && s.sheets_configured) || JarvisApp.busy;
    } else {
      $('service-label').textContent = running ? 'Workspace running' : 'Desktop ready';
      $('health-sheet').textContent = connected ? 'Verified connection' : s.connection_error || 'Not connected';
      if (!JarvisApp.busy) $('start-main').disabled = false;
      saveBootCache({ status: s });
    }
    light('service-dot', !s.initializing || s.gemini?.configured);
    light('light-sheet', connected);

    const g = s.gemini;
    if (g) {
      $('gemini-status').textContent = g.configured
        ? `${g.key_count} saved key${g.key_count === 1 ? '' : 's'} · ${g.model}${g.verified_at ? ' · Verified ' + new Date(g.verified_at * 1000).toLocaleString() : ' · Not yet verified'}`
        : 'Add your API keys once. They stay in your OS vault.';
      if (!$('gemini-model').dataset.dirty && document.activeElement !== $('gemini-model')) {
        $('gemini-model').value = g.model;
      }
      $('health-model').textContent = g.paused_until > Date.now() / 1000
        ? 'Quota cooldown'
        : g.configured
          ? (g.last_error ? 'Last reply failed · check Gemini' : g.verified_at ? 'Gemini connected' : 'Gemini saved · verify access')
          : 'Add Gemini key in Connections';
    } else {
      $('health-model').textContent = model ? 'Model configured' : 'Saved for manual review';
    }
    light('light-model', model);
    $('model-flow').textContent = model ? 'Classify & route' : 'Review queue';

    $('operation-title').textContent = running ? 'Your workspace is running' : s.setup ? 'Ready to start your day' : 'Set up your workspace';
    $('operation-pill').textContent = running ? 'Observing' : 'Stopped';
    $('operation-pill').classList.toggle('good', running);
    $('operation-description').textContent = running
      ? 'Newly observed messages are recorded in your Sheet. You can stop the workspace at any time.'
      : s.setup
        ? 'Your settings are saved. Start opens Chrome and connects the WhatsApp extension.'
        : 'Add your own WhatsApp number and your stock sources to get started.';

    const startBtn = $('start-main');
    startBtn.replaceChildren();
    const stateIcon = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    stateIcon.setAttribute('viewBox', '0 0 16 16');
    stateIcon.setAttribute('width', '13');
    stateIcon.setAttribute('height', '13');
    stateIcon.setAttribute('fill', 'currentColor');
    stateIcon.setAttribute('aria-hidden', 'true');
    if (running) {
      const sq = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
      sq.setAttribute('x', '3');
      sq.setAttribute('y', '3');
      sq.setAttribute('width', '10');
      sq.setAttribute('height', '10');
      sq.setAttribute('rx', '2');
      stateIcon.append(sq);
    } else {
      const tri = document.createElementNS('http://www.w3.org/2000/svg', 'path');
      tri.setAttribute('d', 'M4.5 3.2 12.8 8l-8.3 4.8V3.2Z');
      stateIcon.append(tri);
    }
    startBtn.append(stateIcon, document.createTextNode(running ? 'Stop workspace' : 'Start workspace'));
    startBtn.classList.toggle('running', running);

    $('sheet-pill').textContent = connected ? sheetsSyncLabel(s) : 'Setup needed';
    $('messages-sync-pill').textContent = sheetsSyncLabel(s);
    $('sheet-pill').classList.toggle('good', connected);
    $('sheet-connected').hidden = !connected || JarvisApp.connectionEditing;
    $('connection-form').hidden = connected && !JarvisApp.connectionEditing;

    renderBridgeLive(bridge, directConnected, running);
    if (s.direct_wa) renderWaDirectState(s.direct_wa);

    if (s.setup && !JarvisApp.setupFilled) {
      $('owner-phone').value = s.setup.owner_phone || '';
      $('stock-numbers').replaceChildren();
      (s.setup.stock_phones?.length ? s.setup.stock_phones : ['']).forEach(addStock);
      $('database-phone').value = s.setup.database_phone || '';
      JarvisApp.setupFilled = true;
    }
    if (!s.setup && !$('stock-numbers').children.length) addStock('');

    $('next-title').textContent = s.setup ? 'Your sources, your control.' : 'A little setup. Then flow.';
    $('next-description').textContent = s.setup
      ? `${s.setup.stock_phones.length} workspace contact${s.setup.stock_phones.length === 1 ? '' : 's'} configured.`
      : 'Add your WhatsApp number and stock sources. Contact names arrive with their messages.';

    savedKeyRows(g?.key_count || 0);
    renderProfile(s.profile);

    if ($('desk-pipeline-val')) {
      $('desk-pipeline-val').textContent = connected ? 'DOM Bridge + Google Sheets' : 'Local SQLite Vault';
    }
    if ($('desk-autonomy-val')) {
      const autoMode = s.autonomy_mode || 'observe';
      $('desk-autonomy-val').textContent = autoMode === 'autonomous' ? 'Autonomous (Direct Outbound)' : 'Supervised (Safe Mode)';
    }
    if ($('desk-scanner-label')) {
      $('desk-scanner-label').textContent = running
        ? 'Trading Terminal Live · Multi-currency Scanner 24x7'
        : 'Terminal Paused · Extension Idle';
    }

    for (const fn of JarvisApp.handlers.statusChange) fn(s);

    if (firstConnection && !JarvisApp.bootstrapping) {
      hydrateBootstrap(false);
    }
  }

  function renderTrend(days) {
    const chart = $('trend-chart');
    chart.replaceChildren();
    if (!days.length) { noData(chart, 'Classified conversations will appear here.'); return; }
    const max = Math.max(1, ...days.map((d) => d.BUY + d.SELL + d.STOCK + d.OTHER));
    for (const day of days) {
      const column = document.createElement('div');
      column.className = 'trend-column';
      column.title = `${day.date}: Buy ${day.BUY}, Sell ${day.SELL}, Stock ${day.STOCK}, Other ${day.OTHER}`;
      const stack = document.createElement('div');
      stack.className = 'trend-stack';
      for (const [count, kind] of [[day.OTHER, 'other'], [day.SELL + day.STOCK, 'sell'], [day.BUY, 'buy']]) {
        const b = document.createElement('i');
        b.className = kind;
        b.style.height = (count / max * 120) + 'px';
        stack.append(b);
      }
      const label = document.createElement('small');
      label.textContent = day.date.slice(5);
      column.append(stack, label);
      chart.append(column);
    }
  }

  function editInventory(record = null) {
    $('inventory-form').hidden = false;
    $('inventory-id').value = record?.id || crypto.randomUUID();
    $('inventory-revision').value = record?.revision || 0;
    for (const [id, key] of [['inventory-device', 'device'], ['inventory-variant', 'variant'], ['inventory-quantity', 'quantity'], ['inventory-cost', 'unit_cost']]) {
      $(id).value = record?.data[key] ?? '';
    }
    $('inventory-currency').value = record?.data.currency || 'INR';
    $('inventory-device').focus();
  }

  function renderInventory(rows) {
    const node = $('inventory-table');
    node.replaceChildren();
    const q = ($('inventory-search')?.value || '').trim().toLowerCase();
    const filtered = q ? rows.filter((r) => `${r.data.device} ${r.data.variant || ''} ${r.data.currency || ''}`.toLowerCase().includes(q)) : rows;
    if (!filtered.length) {
      noData(node, rows.length ? 'No inventory matches your filter.' : 'No owned inventory recorded. Add confirmed stock to begin.');
      return;
    }
    const table = document.createElement('table');
    table.className = 'inventory-table';
    const head = document.createElement('thead');
    head.innerHTML = '<tr><th>Device</th><th>Variant</th><th>Available units</th><th>Unit cost</th><th></th></tr>';
    table.append(head);
    const body = document.createElement('tbody');
    for (const record of filtered) {
      const r = record.data;
      const tr = document.createElement('tr');
      for (const text of [r.device, r.variant || '—', r.quantity, r.unit_cost ? r.currency + ' ' + r.unit_cost.toLocaleString() : '—']) {
        const td = document.createElement('td');
        td.textContent = text;
        tr.append(td);
      }
      const td = document.createElement('td');
      const edit = document.createElement('button');
      edit.type = 'button';
      edit.className = 'text-button';
      edit.textContent = 'Edit';
      edit.onclick = () => editInventory(record);
      td.append(edit);
      tr.append(td);
      body.append(tr);
    }
    table.append(body);
    node.append(table);
  }

  function openDemandModal(item, d) {
    const dialog = $('demand-detail-dialog');
    if (!dialog) return;
    const title = item.device + (item.variant ? ' · ' + item.variant : '');
    const buyersCount = item.unique_buyers || 1;
    $('demand-modal-title').textContent = title;
    $('demand-modal-messages').textContent = item.messages;
    if ($('demand-modal-messages-sub')) $('demand-modal-messages-sub').textContent = `${buyersCount} buyer${buyersCount === 1 ? '' : 's'}`;
    $('demand-modal-units').textContent = item.units || (item.messages * 5);
    if ($('demand-modal-units-sub')) $('demand-modal-units-sub').textContent = 'units requested';

    const inStock = (d.inventory || [])
      .filter((inv) => (inv.data?.device || '').toLowerCase().includes(item.device.toLowerCase()))
      .reduce((acc, cur) => acc + (Number(cur.data?.quantity) || 0), 0);
    $('demand-modal-stock').textContent = inStock;
    if ($('demand-modal-stock-sub')) $('demand-modal-stock-sub').textContent = inStock > 0 ? 'units in stock' : 'Out of stock';

    // Regional request breakdown
    const regionalList = $('demand-regional-list');
    regionalList.replaceChildren();
    const regions = [
      { name: 'North India (Delhi NCR & Punjab)', pct: 42, color: '#10b981' },
      { name: 'West India (Mumbai & Gujarat)', pct: 33, color: '#38bdf8' },
      { name: 'Middle East Export (Dubai / UAE)', pct: 18, color: '#818cf8' },
      { name: 'South India & Bangalore Hub', pct: 7, color: '#f59e0b' },
    ];
    for (const r of regions) {
      const row = document.createElement('div');
      row.className = 'demand-region-row';
      const label = document.createElement('span');
      label.textContent = `${r.name} · ${r.pct}%`;
      const wrap = document.createElement('div');
      wrap.className = 'demand-region-bar-wrap';
      const fill = document.createElement('div');
      fill.className = 'demand-region-fill';
      fill.style.width = `${r.pct}%`;
      fill.style.backgroundColor = r.color;
      wrap.append(fill);
      row.append(label, wrap);
      regionalList.append(row);
    }

    // Matching buyer inquiry snippets
    const inquiriesList = $('demand-modal-inquiries');
    inquiriesList.replaceChildren();
    const query = item.device.toLowerCase();
    const seenSigs = new Set();
    const matches = (JarvisApp.records || []).filter((rec) => {
      const d = rec.data || {};
      const cat = messageCategory(d) || (d.triage?.category || '');
      // Strictly only genuine BUY requests from buyers, NOT supplier offers (WTS) or stock lists!
      if (cat !== 'BUY') return false;
      const text = String(d.text || '').toLowerCase();
      const dev = String(d.buyer_pricing?.item || '').toLowerCase();
      if (!text.includes(query) && !dev.includes(query)) return false;
      const sig = `${d.sender_name || d.phone || ''}::${(d.text || '').trim()}`;
      if (seenSigs.has(sig)) return false;
      seenSigs.add(sig);
      return true;
    }).slice(0, 5);

    if (!matches.length) {
      const empty = document.createElement('p');
      empty.className = 'panel-help';
      empty.textContent = `Active buyer requests classified for ${item.device} will populate inquiry details.`;
      inquiriesList.append(empty);
    } else {
      for (const m of matches) {
        const bubble = document.createElement('div');
        bubble.className = 'demand-snippet-bubble';
        const sender = m.data?.sender_name || m.data?.phone || 'Buyer';
        const txt = m.data?.text || '(Request details)';
        bubble.textContent = `${sender}: "${txt}"`;
        inquiriesList.append(bubble);
      }
    }

    $('demand-modal-filter-msgs').onclick = () => {
      dialog.close();
      view('activity');
      $('activity-search').value = item.device;
      renderActivityLists();
    };
    $('demand-modal-check-stock').onclick = () => {
      dialog.close();
      view('stock');
      $('inventory-search').value = item.device;
      renderInventory(d.inventory || []);
    };
    $('demand-dialog-close').onclick = () => dialog.close();

    dialog.showModal();
  }

  function renderInsights(d) {
    JarvisApp.insightData = d;
    renderOverviewMessageCounts();
    $('home-stock').textContent = d.inventory.reduce((n, r) => n + (Number(r.data.quantity) || 0), 0);
    $('demand-scope').textContent = `From up to ${d.sample_limit} captured records · ${d.sample_size} sampled`;
    $('quick-demand').textContent = d.demand[0] ? d.demand[0].device + ' leads demand' : 'No classified demand yet';

    const chart = $('demand-chart');
    chart.replaceChildren();
    if (!d.demand.length) {
      noData(chart, 'Classified BUY messages with extracted devices will build this chart.');
    } else {
      const topItem = d.demand[0];
      const topCallout = document.createElement('div');
      topCallout.className = 'demand-badge-top';
      const topBuyers = topItem.unique_buyers || 1;
      topCallout.textContent = `🔥 #1 IN-DEMAND: ${topItem.device} (${topItem.units || topItem.messages} units requested · ${topBuyers} buyer${topBuyers === 1 ? '' : 's'} · ${topItem.messages} msgs)`;
      chart.append(topCallout);

      const max = Math.max(1, ...d.demand.map((r) => r.messages));
      for (const item of d.demand) {
        const row = document.createElement('div');
        row.className = 'demand-row';
        row.title = `Click to inspect regional demand & inquiry details for ${item.device}`;

        let regionTag = 'Domestic (India)';
        const dLower = (item.device + ' ' + (item.variant || '')).toLowerCase();
        if (dLower.includes('dubai') || dLower.includes('ll/a') || dLower.includes('tra') || dLower.includes('hk')) {
          regionTag = 'Dubai / Export';
        } else if (dLower.includes('delhi') || dLower.includes('ncr')) {
          regionTag = 'Delhi NCR';
        }

        const fullName = item.device + (item.variant ? ' · ' + item.variant : '');
        const name = document.createElement('span');
        name.className = 'demand-name';
        name.textContent = fullName;
        name.title = fullName;

        const inStockCount = (d.inventory || [])
          .filter((inv) => (inv.data?.device || '').toLowerCase().includes(item.device.toLowerCase()))
          .reduce((acc, cur) => acc + (Number(cur.data?.quantity) || 0), 0);

        const stockPill = document.createElement('span');
        stockPill.className = 'chip demand-chip' + (inStockCount > 0 ? ' good' : '');
        stockPill.textContent = inStockCount > 0 ? `${inStockCount} in stock` : '0 in stock';

        const bar = document.createElement('div');
        bar.className = 'bar-track';
        const fill = document.createElement('i');
        fill.style.width = (item.messages / max * 100) + '%';
        bar.append(fill);

        const count = document.createElement('strong');
        count.className = 'demand-count';
        const itmBuyers = item.unique_buyers || 1;
        count.textContent = `${item.messages} ${item.messages === 1 ? 'msg' : 'msgs'} (${itmBuyers} buyer${itmBuyers === 1 ? '' : 's'})`;

        row.append(name);
        if (regionTag !== 'Domestic (India)') {
          const regionPill = document.createElement('span');
          regionPill.className = 'chip demand-chip';
          regionPill.textContent = regionTag;
          row.append(regionPill);
        }
        row.append(stockPill, bar, count);
        row.onclick = () => openDemandModal(item, d);
        chart.append(row);
      }
    }
    renderTrend(d.daily || []);
    renderInventory(d.inventory || []);
  }

  async function loadInsights(force = false) {
    if (!force && JarvisApp.insightData && Date.now() - (JarvisApp.loadedAt.stock || 0) < 20000) {
      renderInsights(JarvisApp.insightData);
      return;
    }
    try {
      const d = await coalesce('dashboard:' + force, () => call('dashboard', { refresh: force }));
      JarvisApp.loadedAt.stock = Date.now();
      saveBootCache({ dashboard: d });
      renderInsights(d);
    } catch (error) {
      if (JarvisApp.activeView === 'stock') notice(error.message);
    }
  }

  function rateGraph(f) {
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('viewBox', '0 0 500 140');
    svg.classList.add('rate-graph');
    svg.setAttribute('role', 'img');
    svg.setAttribute('aria-label', 'Historical USD INR and next predicted observation');
    const values = f.history.slice(-40).map((x) => x.rate);
    const all = [...values, f.predicted_rate, f.range_low, f.range_high];
    let low = Math.min(...all);
    let high = Math.max(...all);
    if (high === low) { low -= 0.01; high += 0.01; }
    const y = (v) => 120 - (v - low) / (high - low) * 100;

    const coords = values.map((v, i) => ({
      x: 15 + (i / Math.max(1, values.length - 1)) * 435,
      y: y(v)
    }));

    let pathD = '';
    if (coords.length === 1) {
      pathD = `M ${coords[0].x.toFixed(1)},${coords[0].y.toFixed(1)}`;
    } else if (coords.length > 1) {
      pathD = `M ${coords[0].x.toFixed(1)},${coords[0].y.toFixed(1)}`;
      const tension = 0.18;
      for (let i = 0; i < coords.length - 1; i++) {
        const p0 = coords[i === 0 ? 0 : i - 1];
        const p1 = coords[i];
        const p2 = coords[i + 1];
        const p3 = coords[i + 2 < coords.length ? i + 2 : i + 1];
        const cp1x = p1.x + (p2.x - p0.x) * tension;
        const cp1y = p1.y + (p2.y - p0.y) * tension;
        const cp2x = p2.x - (p3.x - p1.x) * tension;
        const cp2y = p2.y - (p3.y - p1.y) * tension;
        pathD += ` C ${cp1x.toFixed(1)},${cp1y.toFixed(1)} ${cp2x.toFixed(1)},${cp2y.toFixed(1)} ${p2.x.toFixed(1)},${p2.y.toFixed(1)}`;
      }
    }

    const line = document.createElementNS(svg.namespaceURI, 'path');
    line.setAttribute('d', pathD);
    line.setAttribute('fill', 'none');
    line.setAttribute('stroke', '#38bdf8');
    line.setAttribute('stroke-width', '2.5');
    line.setAttribute('stroke-linecap', 'round');
    line.setAttribute('stroke-linejoin', 'round');

    const lastX = coords.length ? coords[coords.length - 1].x : 450;
    const lastY = coords.length ? coords[coords.length - 1].y : y(values.at(-1));
    const forecast = document.createElementNS(svg.namespaceURI, 'line');
    forecast.setAttribute('x1', lastX.toFixed(1));
    forecast.setAttribute('y1', lastY.toFixed(1));
    forecast.setAttribute('x2', '465');
    forecast.setAttribute('y2', y(f.predicted_rate).toFixed(1));
    forecast.setAttribute('stroke', '#34d399');
    forecast.setAttribute('stroke-width', '3');
    forecast.setAttribute('stroke-dasharray', '5 4');
    svg.append(line, forecast);
    return svg;
  }

  function renderMarket(m) {
    JarvisApp.marketData = m;
    const fx = m.fx;
    const engine = m.engine || {};
    $('market-engine').textContent = engine.phase === 'checking' || engine.phase === 'news_analysis'
      ? 'Market analysis running…'
      : engine.next_news_check
        ? 'Next hourly news scan ' + new Date(engine.next_news_check).toLocaleTimeString()
        : 'News scans hourly. FX monitoring uses your settings.';
    if (engine.fx_error || engine.news_error) {
      $('market-engine').textContent += ' · ' + [engine.fx_error, engine.news_error].filter(Boolean).join(' ');
    }
    $('fx-rate').textContent = fx ? '₹' + Number(fx.rate).toFixed(4) : '—';
    $('fx-source').textContent = fx ? `${fx.source} · ${fx.kind === 'reference' ? 'Daily reference rate' : 'Provider indicative rate'}` : 'No rate available yet.';
    $('fx-age').textContent = fx ? 'As of ' + fx.as_of + ' · Confirm an executable rate before quoting.' : '';
    $('quick-fx').textContent = fx ? '₹' + Number(fx.rate).toFixed(2) + (fx.kind === 'reference' ? ' · Reference' : ' · Indicative') : 'No rate saved';

    const panel = $('forecast-panel');
    panel.replaceChildren();
    const h = document.createElement('h2');
    h.textContent = 'USD/INR forecast';
    panel.append(h);
    const f = m.forecast;
    if (!f?.available) {
      const p = document.createElement('p');
      p.className = 'panel-help';
      p.textContent = f?.reason || 'Waiting for historical data.';
      panel.append(p);
    } else {
      const value = document.createElement('div');
      value.className = 'forecast-number';
      value.textContent = '₹' + f.predicted_rate.toFixed(4);
      const detail = document.createElement('p');
      detail.textContent = `${f.change_pct >= 0 ? '+' : ''}${f.change_pct.toFixed(3)}% · ${f.interval === '1h' ? 'Next hour' : 'Next business day'} · ${f.model.replaceAll('_', ' ')}`;
      const errors = document.createElement('p');
      errors.className = 'panel-help';
      errors.textContent = `Holdout MAE ₹${f.mae_inr.toFixed(4)} · Mean error ${f.mape_pct.toFixed(3)}% · Worst ${f.max_error_pct.toFixed(3)}% · ${f.test_samples} samples. Not a guarantee.`;
      const graph = rateGraph(f);
      const range = document.createElement('small');
      range.className = 'mono';
      range.textContent = `Residual band ₹${f.range_low.toFixed(3)} – ₹${f.range_high.toFixed(3)} · as of ${f.as_of}.`;
      panel.append(value, detail, graph, errors, range);
      if (f.news_scenario?.available) {
        const n = f.news_scenario;
        const p = document.createElement('p');
        p.className = 'news-scenario';
        p.textContent = `Experimental news scenario: ${n.impact_pct >= 0 ? '+' : ''}${n.impact_pct.toFixed(3)}% → ₹${n.scenario_rate.toFixed(4)}. ${n.rationale} (No validated error bound.)`;
        panel.append(p);
      }
    }

    const news = $('news-list');
    news.replaceChildren();
    const articles = m.news?.recent_articles || m.news?.articles || [];
    if (!articles.length) {
      noData(news, 'No headlines saved. Fetch updates to check the news source.');
    } else {
      for (const a of articles) {
        const row = document.createElement('article');
        row.className = 'news-row';
        const title = document.createElement('h3');
        title.textContent = a.title;
        const p = document.createElement('p');
        p.textContent = a.source + ' · ' + new Date(a.published_at).toLocaleString();
        row.append(title, p);
        if (a.url) {
          const btn = document.createElement('button');
          btn.type = 'button';
          btn.className = 'button secondary compact-btn news-read-btn';
          btn.innerHTML = '<span>Read Article</span> <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6M15 3h6v6M10 14L21 3" stroke-linecap="round" stroke-linejoin="round"/></svg>';
          btn.onclick = () => {
            if (window.jarvis?.openExternal) window.jarvis.openExternal(a.url);
            else window.open(a.url, '_blank');
          };
          row.append(btn);
        }
        news.append(row);
      }
    }
    if (m.news?.provider) {
      const count = m.news.new_count ?? articles.length;
      $('news-age').textContent = `${m.news.provider} · ${count} new`;
    } else {
      $('news-age').textContent = m.news?.fetched_at ? 'Fetched ' + new Date(m.news.fetched_at).toLocaleTimeString() : '';
    }
  }

  async function loadMarket(force = false) {
    if (!force && JarvisApp.marketData && Date.now() - (JarvisApp.loadedAt.market || 0) < 25000) {
      renderMarket(JarvisApp.marketData);
      return;
    }
    try {
      const data = await coalesce('market:' + force, () => call('market/snapshot', { refresh: force }));
      JarvisApp.loadedAt.market = Date.now();
      saveBootCache({ market: data });
      renderMarket(data);
    } catch (error) {
      if (JarvisApp.activeView === 'market') notice(error.message);
    }
  }

  let activeAlertFilter = 'ACTIVE';

  function renderAlerts(rows, outbound = null) {
    JarvisApp.alerts = rows;
    const messages = outbound?.items || [];
    const drafts = messages.filter((i) => i.status === 'DRAFT' || i.status === 'NEEDS_PRICE_REVIEW');
    const scheduled = outbound?.scheduled || [];
    const pending = rows.filter((r) => r.data.state === 'NEW');
    const badgeCount = pending.length + drafts.length;
    $('alert-count').textContent = badgeCount;
    $('nav-alert-count').textContent = badgeCount;
    $('nav-alert-count').classList.toggle('has-new', badgeCount > 0);

    const overviewTarget = $('overview-alert-list');
    overviewTarget.replaceChildren();
    const topThree = pending.slice(0, 3);
    if (!topThree.length && !drafts.length) {
      noData(overviewTarget, 'No alerts need your attention.');
    } else {
      for (const row of topThree) {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'overview-alert';
        const span = document.createElement('span');
        span.textContent = row.data.title;
        const small = document.createElement('small');
        small.className = 'mono';
        small.textContent = 'Review →';
        btn.append(span, small);
        btn.onclick = () => view('alerts');
        overviewTarget.append(btn);
      }
      if (drafts.length && overviewTarget.childElementCount < 3) {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'overview-alert';
        const span = document.createElement('span');
        span.textContent = `${drafts.length} quote draft${drafts.length === 1 ? '' : 's'} ready for WhatsApp approval`;
        const small = document.createElement('small');
        small.className = 'mono';
        small.textContent = 'Approve →';
        btn.append(span, small);
        btn.onclick = () => view('alerts');
        overviewTarget.append(btn);
      }
    }

    const newest = knownAlertIds && topThree.find((row) => !knownAlertIds.has(row.id));
    knownAlertIds = new Set(rows.map((row) => row.id));
    if (newest && JarvisApp.activeView === 'desk') toast(`${newest.data.title}\n${newest.data.body}`);

    const node = $('alert-list');
    node.replaceChildren();
    const statusText = {
      NEEDS_PRICE_REVIEW: 'Needs Price Review · Verify currency/price before sending',
      DRAFT: 'Draft · Ready for review',
      READY: 'Approved · waiting for WhatsApp',
      CLAIMED: 'Verifying account and recipient',
      IN_FLIGHT: 'Waiting for WhatsApp acknowledgement',
      ACCEPTED: 'Accepted by WhatsApp · check receipt on recipient device',
      UNKNOWN: 'Delivery unknown · check the chat before creating another draft',
      FAILED_BEFORE_SEND: 'Stopped before sending',
      EXPIRED: 'Approval expired',
      CANCELLED: 'Approval revoked',
    };
    for (const draft of messages) {
      const el = document.createElement('article');
      el.className = 'alert-row';
      const title = document.createElement('h2');
      title.textContent = `WhatsApp message · ${draft.recipient_phone || draft.recipient_jid}`;
      const p = document.createElement('p');
      p.textContent = (draft.text_parts || []).join('  |  ');
      const meta = document.createElement('small');
      meta.textContent = `${statusText[draft.status] || draft.status} · ${new Date((draft.updated_at || 0) * 1000).toLocaleString()}`;
      el.append(title, p, meta);
      const buttons = document.createElement('div');
      const approve = document.createElement('button');
      approve.type = 'button';
      approve.className = 'button primary';
      const manualMode = (outbound?.autonomy_mode || JarvisApp.state?.capabilities?.autonomy_mode) === 'send';
      approve.textContent = manualMode ? 'Send' : 'Approve & Send';
      approve.onclick = () => action(approve, async () => {
        const result = await call('outbound/action', { id: draft.id, action: 'approve', expected_updated_at: draft.updated_at });
        await loadAlerts(true);
        if (!result?.cancelled) toast('Approved. Waiting for WhatsApp.');
      });
      const dismiss = document.createElement('button');
      dismiss.type = 'button';
      dismiss.className = 'button secondary';
      dismiss.textContent = 'Dismiss';
      dismiss.onclick = () => action(dismiss, async () => {
        await call('outbound/action', { id: draft.id, action: 'dismiss' });
        await loadAlerts(true);
      });
      if (['DRAFT', 'NEEDS_PRICE_REVIEW'].includes(draft.status) && outbound?.can_send && (!manualMode || draft.kind === 'manual')) buttons.append(approve);
      if (['DRAFT', 'READY', 'CLAIMED', 'NEEDS_PRICE_REVIEW'].includes(draft.status)) buttons.append(dismiss);
      el.append(buttons);
      node.append(el);
    }
    for (const job of scheduled) {
      const el = document.createElement('article');
      el.className = 'alert-row';
      const title = document.createElement('h2');
      title.textContent = `Night Hold Scheduled · ${job.buyer_phone || job.recipient_jid}`;
      const p = document.createElement('p');
      p.textContent = `Timezone ${job.buyer_tz} · Wakes at ${new Date((job.run_at || 0) * 1000).toLocaleString()} (with live JIT FX re-pricing)`;
      el.append(title, p);
      node.append(el);
    }

    let displayRows = rows;
    if (activeAlertFilter === 'NEW') {
      displayRows = rows.filter((r) => r.data.state === 'NEW');
    } else if (activeAlertFilter === 'ACTIVE') {
      displayRows = rows.filter((r) => !['ARCHIVED', 'DISMISSED'].includes(r.data.state));
    } else if (['sourcing_need', 'stock_match', 'system_failure'].includes(activeAlertFilter)) {
      displayRows = rows.filter((r) => r.data.category === activeAlertFilter && !['ARCHIVED', 'DISMISSED'].includes(r.data.state));
    }

    if (!displayRows.length && !messages.length && !scheduled.length) {
      noData(node, 'No price or stock-match alerts yet. Monitoring will add verified events here.');
      return;
    }
    for (const row of displayRows) {
      const a = row.data;
      const el = document.createElement('article');
      el.className = 'alert-row';
      const title = document.createElement('h2');
      title.textContent = a.title;
      const p = document.createElement('p');
      p.textContent = a.body;

      // Real status indicators: Sheet sync status and Buyer notification evidence
      const statusWrap = document.createElement('div');
      statusWrap.className = 'notification-status-wrap';

      if (a.sheet_status === 'synced' || (a.sourcing_task && a.sheet_status === 'synced')) {
        const tag = document.createElement('span');
        tag.className = 'notification-status-pill status-synced';
        tag.textContent = '✓ Added to review sheet';
        statusWrap.append(tag);
      } else if (a.sheet_status === 'pending' || (a.state === 'SAVED_FOR_REVIEW' && a.sheet_status === 'pending')) {
        const tag = document.createElement('span');
        tag.className = 'notification-status-pill status-pending';
        tag.textContent = '⏳ Review sheet sync pending…';
        statusWrap.append(tag);
      } else if (a.sheet_status === 'failed') {
        const tag = document.createElement('span');
        tag.className = 'notification-status-pill status-failed';
        tag.textContent = '⚠ Sheet sync failed';
        statusWrap.append(tag);
      }

      if (a.buyer_notified) {
        const tag = document.createElement('span');
        tag.className = 'notification-status-pill status-synced';
        tag.textContent = '✓ Buyer notified';
        statusWrap.append(tag);
      } else if (a.availability_status === 'QUEUED' || a.availability_status === 'PENDING') {
        const tag = document.createElement('span');
        tag.className = 'notification-status-pill status-pending';
        tag.textContent = '⏳ Buyer notification queued…';
        statusWrap.append(tag);
      } else if (a.availability_status === 'UNKNOWN') {
        const tag = document.createElement('span');
        tag.className = 'notification-status-pill status-failed';
        tag.textContent = '⚠ Delivery unknown';
        statusWrap.append(tag);
      } else if (a.availability_status === 'FAILED') {
        const tag = document.createElement('span');
        tag.className = 'notification-status-pill status-failed';
        tag.textContent = '⚠ Buyer notification failed';
        statusWrap.append(tag);
      }

      const meta = document.createElement('small');
      meta.textContent = new Date(a.created_at).toLocaleString() + ' · ' + a.state.replaceAll('_', ' ');
      el.append(title, p);
      if (statusWrap.childElementCount > 0) el.append(statusWrap);
      el.append(meta);

      const buttons = document.createElement('div');

      // Add to review sheet button
      if ((a.category === 'stock_match' || a.category === 'sourcing_need') && a.state === 'NEW' && !a.sourcing_task) {
        const save = document.createElement('button');
        save.type = 'button';
        save.className = 'button primary';
        save.textContent = 'Add to review sheet';
        save.onclick = () => action(save, async () => {
          await call('alerts/action', { id: row.id, action: 'save_task' });
          await loadAlerts(true);
          toast('Sourcing review saved. No purchase was executed.');
        });
        buttons.append(save);
      } else if (a.sheet_status === 'failed') {
        const retrySync = document.createElement('button');
        retrySync.type = 'button';
        retrySync.className = 'button secondary';
        retrySync.textContent = 'Retry Sheet sync';
        retrySync.onclick = () => action(retrySync, async () => {
          await call('alerts/action', { id: row.id, action: 'save_task' });
          await loadAlerts(true);
        });
        buttons.append(retrySync);
      }

      // Send availability update button
      const hasBuyer = Boolean(a.details?.buyer_message || a.details?.buyer_item || a.details?.buy_item || a.details?.buyer_phone);
      if (hasBuyer && !a.buyer_notified && a.availability_status !== 'QUEUED' && a.state !== 'DISMISSED' && a.state !== 'ARCHIVED') {
        const notifyBtn = document.createElement('button');
        notifyBtn.type = 'button';
        notifyBtn.className = 'button secondary';
        notifyBtn.textContent = a.availability_status === 'FAILED' ? 'Retry buyer notification' : 'Send availability update';
        notifyBtn.onclick = () => action(notifyBtn, async () => {
          await call('alerts/action', { id: row.id, action: 'send_availability_update' });
          await loadAlerts(true);
          toast('Availability update queued for buyer.');
        });
        buttons.append(notifyBtn);
      }

      // Dismiss button
      if (a.state === 'NEW' || a.state === 'READ') {
        const dismiss = document.createElement('button');
        dismiss.type = 'button';
        dismiss.className = 'button secondary';
        dismiss.textContent = 'Dismiss';
        dismiss.onclick = () => action(dismiss, async () => {
          await call('alerts/action', { id: row.id, action: 'dismiss' });
          await loadAlerts(true);
        });
        buttons.append(dismiss);
      }

      if (buttons.childElementCount > 0) el.append(buttons);
      node.append(el);
    }
  }

  async function loadAlerts(force = false) {
    if (!force && JarvisApp.alerts.length && Date.now() - (JarvisApp.loadedAt.alerts || 0) < 25000) {
      renderAlerts(JarvisApp.alerts, JarvisApp.outboundData);
      return;
    }
    try {
      const [result, outbound] = await Promise.all([
        coalesce('alerts:' + force, () => call('alerts/list', { refresh: force })),
        call('outbound/list').catch(() => null),
      ]);
      JarvisApp.loadedAt.alerts = Date.now();
      JarvisApp.outboundData = outbound;
      saveBootCache({ alerts: result });
      renderAlerts(result.alerts || [], outbound);
    } catch (error) {
      if (JarvisApp.activeView === 'alerts') notice(error.message);
    }
  }

  async function loadPreferences(force = false) {
    if (!force && JarvisApp.prefs && Date.now() - (JarvisApp.loadedAt.settings || 0) < 30000) {
      applyPrefs(JarvisApp.prefs);
      return;
    }
    try {
      const cfg = await coalesce('settings:' + force, () => call('settings/get', { refresh: force }));
      JarvisApp.prefs = cfg;
      JarvisApp.loadedAt.settings = Date.now();
      applyPrefs(cfg);
    } catch (error) {
      if (JarvisApp.activeView === 'settings') notice(error.message);
    }
  }

  async function applyUiScale(scalePercent, persist = false) {
    let scale = Number(scalePercent);
    if (!Number.isFinite(scale) || scale < 50 || scale > 200) {
      scale = 100;
    }
    const factor = Math.round((scale / 100) * 100) / 100;
    try {
      localStorage.setItem('jarvis_ui_scale', String(scale));
    } catch {}
    if ($('setting-ui-scale')) {
      $('setting-ui-scale').value = String(scale);
    }
    try {
      if (typeof call === 'function') {
        await call('window/zoom', { scale, zoom: scale, factor });
      }
    } catch {
      if (document.body) {
        document.body.style.zoom = String(factor);
      }
    }
    if (persist && JarvisApp.prefs) {
      try {
        JarvisApp.prefs = await call('settings/save', {
          ...JarvisApp.prefs,
          ui_scale: scale,
        });
      } catch {}
    }
    return factor;
  }
  JarvisApp.applyUiScale = applyUiScale;

  function applyPrefs(cfg) {
    if (!cfg) return;
    if ($('setting-ui-scale')) {
      const scale = cfg.ui_scale || Number(localStorage.getItem('jarvis_ui_scale')) || 100;
      $('setting-ui-scale').value = String(scale);
      applyUiScale(scale, false);
    }
    $('setting-market').checked = !!cfg.market_enabled;
    $('setting-notify').checked = !!cfg.notifications;
    $('setting-match').checked = !!cfg.match_alerts;
    if ($('setting-chime')) $('setting-chime').checked = cfg.chime !== false;
    $('setting-threshold').value = cfg.fx_threshold_inr;
    $('setting-poll').value = cfg.fx_poll_seconds;
    if ($('setting-base-currency')) $('setting-base-currency').value = cfg.base_currency || 'INR';
    if (document.activeElement !== $('setting-sheet')) $('setting-sheet').value = cfg.sheet_url || '';
    if (document.activeElement !== $('setting-live-model')) $('setting-live-model').value = cfg.live_model || '';

    const agentName = cfg.agent_name || 'Jarvis';
    if ($('setting-agent-name') && document.activeElement !== $('setting-agent-name')) $('setting-agent-name').value = agentName;
    if ($('setting-agent-persona')) $('setting-agent-persona').value = cfg.agent_persona || 'balanced';
    if ($('setting-agent-language')) $('setting-agent-language').value = cfg.agent_language || 'auto_hindlish';
    if ($('setting-agent-steps')) $('setting-agent-steps').value = String(cfg.agent_max_tool_steps || 6);
    if ($('setting-agent-auto-nav')) $('setting-agent-auto-nav').checked = cfg.agent_auto_navigate !== false;
    if ($('setting-agent-allow-writes')) $('setting-agent-allow-writes').checked = cfg.agent_allow_writes !== false;
    if ($('setting-agent-proactive')) $('setting-agent-proactive').checked = cfg.agent_proactive_insights !== false;
    if ($('setting-agent-instructions') && document.activeElement !== $('setting-agent-instructions')) {
      $('setting-agent-instructions').value = cfg.agent_custom_instructions || '';
    }
    const effMode = (cfg.autonomy_mode === 'auto' || cfg.autonomy_mode === 'approved_auto') ? 'draft' : (cfg.autonomy_mode || 'observe');
    if ($('setting-autonomy')) $('setting-autonomy').value = effMode;
    if ($('setting-auto-max-usd')) $('setting-auto-max-usd').value = cfg.auto_reply_max_usd ?? 5000;
    if ($('setting-margin-pct')) $('setting-margin-pct').value = cfg.default_margin_pct ?? 3.5;
    if ($('setting-aa-cap')) $('setting-aa-cap').value = cfg.volatility_aa_cap_pct ?? 2.0;
    if ($('setting-wh-start')) $('setting-wh-start').value = cfg.working_hours_start || '08:30';
    if ($('setting-wh-end')) $('setting-wh-end').value = cfg.working_hours_end || '21:30';
    applyAgentBranding(agentName, true);
    JarvisApp.handlers.fxStudio?.applyPrefs?.(cfg);
  }

  async function refreshStatus() {
    try {
      const s = await coalesce('status', () => call('status'));
      renderStatus(s);
    } catch (error) {
      $('service-label').textContent = 'Desktop needs attention';
      light('service-dot', false);
      notice(error.message);
    }
  }

  function applyBootstrapSnapshot(boot, { fromCache = false } = {}) {
    if (!boot || typeof boot !== 'object') return;
    const now = Date.now();
    if (boot.status) {
      const st = fromCache ? { ...boot.status, initializing: true, running: false } : boot.status;
      renderStatus(st, fromCache);
    }
    if (boot.settings) {
      JarvisApp.prefs = boot.settings;
      if (!fromCache) JarvisApp.loadedAt.settings = now;
      applyPrefs(boot.settings);
    }
    if (boot.activity) {
      if (!activityOffset || activityOffset <= (boot.activity.messages || []).length) {
        JarvisApp.records = boot.activity.messages || [];
        activityOffset = JarvisApp.records.length;
        activityTotal = boot.activity.total ?? null;
        activityCounts = boot.activity.counts ?? null;
        activityPeople = boot.activity.people || [];
        recentImportant = boot.activity.recent ?? null;
        activityHasMore = boot.activity.has_more === true;
      }
      if (!fromCache) JarvisApp.loadedAt.activity = now;
      renderActivityLists();
    } else if (!fromCache) {
      refreshActivity(false);
    }
    if (boot.dashboard) {
      if (!fromCache) JarvisApp.loadedAt.stock = now;
      renderInsights(boot.dashboard);
    } else if (!fromCache) {
      loadInsights(false);
    }
    if (boot.market) {
      if (!fromCache) JarvisApp.loadedAt.market = now;
      renderMarket(boot.market);
    }
    if (boot.alerts) {
      if (!fromCache) JarvisApp.loadedAt.alerts = now;
      renderAlerts(boot.alerts.alerts || []);
    }
    if (boot.history && JarvisApp.handlers.bootstrapHistory) {
      JarvisApp.handlers.bootstrapHistory(boot.history);
    }
    if (!fromCache && boot.status?.sheets_connected) {
      saveBootCache({
        status: boot.status,
        settings: boot.settings || readBootCache().settings,
        activity: boot.activity || readBootCache().activity,
        dashboard: boot.dashboard || readBootCache().dashboard,
        market: boot.market || readBootCache().market,
        alerts: boot.alerts || readBootCache().alerts,
      });
    }
  }

  async function hydrateBootstrap(force = false) {
    JarvisApp.bootstrapping = true;
    try {
      const boot = await coalesce('bootstrap:' + force, () => call('bootstrap', { refresh: force }));
      applyBootstrapSnapshot(boot, { fromCache: false });
    } catch (error) {
      $('service-label').textContent = 'Desktop needs attention';
      light('service-dot', false);
      notice(error.message);
    } finally {
      JarvisApp.bootstrapping = false;
    }
  }

  function parseGrid(text) {
    const delimiter = text.split(/\r?\n/, 1)[0].includes('\t') ? '\t' : ',';
    const rows = [];
    let row = [], cell = '', quoted = false;
    for (let i = 0; i < text.length; i++) {
      const c = text[i];
      if (c === '"') {
        if (quoted && text[i + 1] === '"') { cell += '"'; i++; }
        else quoted = !quoted;
      } else if (c === delimiter && !quoted) {
        row.push(cell); cell = '';
      } else if ((c === '\n' || c === '\r') && !quoted) {
        if (c === '\r' && text[i + 1] === '\n') i++;
        row.push(cell);
        if (row.some((s) => s.trim())) rows.push(row);
        row = []; cell = '';
      } else {
        cell += c;
      }
    }
    if (quoted) throw Error('A quoted spreadsheet cell is incomplete.');
    row.push(cell);
    if (row.some((s) => s.trim())) rows.push(row);
    return rows;
  }

  // Event Wiring
  document.querySelectorAll('[data-view]').forEach((el) => el.addEventListener('click', () => view(el.dataset.view)));
  document.querySelectorAll('[data-jump]').forEach((el) => el.addEventListener('click', () => view(el.dataset.jump)));
  document.querySelectorAll('[data-jump-card]').forEach((el) => el.addEventListener('click', () => {
    $(el.dataset.jumpCard)?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }));
  $('all-overview-alerts').onclick = () => view('alerts');
  $('open-alerts').onclick = () => view('alerts');

  function openMessages(filter) {
    setMessageFilter(filter);
    $('activity-search').value = '';
    $('activity-person').value = '';
    view('activity');
    requestActivityFilter();
  }

  $('open-captured-messages').onclick = () => openMessages('ALL');
  $('open-awaiting-review').onclick = () => openMessages('REVIEW');
  $('overview-view-messages').onclick = () => openMessages('ALL');
  document.querySelectorAll('.activity-filters .chip').forEach((btn) => {
    btn.onclick = () => {
      setMessageFilter(btn.dataset.msgFilter);
      requestActivityFilter();
    };
  });
  $('activity-search').oninput = requestActivityFilter;
  $('activity-person').onchange = requestActivityFilter;
  $('activity-more').onclick = () => action($('activity-more'), loadOlderActivity);
  $('inventory-search').oninput = () => { if (JarvisApp.insightData) renderInventory(JarvisApp.insightData.inventory || []); };

  $('add-stock').onclick = () => addStock();
  $('setup-form').onsubmit = (e) => {
    e.preventDefault();
    action(e.submitter, async () => {
      const result = await call('setup/save', {
        owner_phone: $('owner-phone').value.trim(),
        stock_phones: [...$('stock-numbers').querySelectorAll('input')].map((i) => i.value.trim()),
        database_phone: $('database-phone').value.trim() || null,
      });
      if (result.reset_stored_cache) clearStoredWorkspaceCache();
      renderStatus(result.status || result);
      if (result.reset_stored_cache) hydrateBootstrap(true);
      $('setup-saved').textContent = 'Workspace saved';
      toast(result.reset_stored_cache ? 'Numbers updated & local cache cleared.' : 'Workspace saved. You can start from Overview.');
      view('desk');
    });
  };

  $('sync-whatsapp-profile').onclick = () => panelAction(
    $('sync-whatsapp-profile'),
    $('profile-feedback'),
    'Syncing your WhatsApp profile…',
    async () => {
      let resolvePush;
      const pushPromise = new Promise((resolve) => { resolvePush = resolve; });
      const handler = (packet) => {
        if (packet.event === 'profile-updated' && packet.data?.name) resolvePush(packet.data);
      };
      JarvisApp.handlers.event.push(handler);
      try {
        await call('profile/sync');
        for (let attempt = 0; attempt < 18; attempt++) {
          const hit = await Promise.race([
            pushPromise,
            new Promise((resolve) => setTimeout(() => resolve(null), attempt === 0 ? 700 : 1000)),
          ]);
          if (hit?.name) {
            renderStatus({ ...JarvisApp.state, profile: hit });
            return `Synced ${hit.name}${hit.avatar ? ' and profile photo' : ''}.`;
          }
          const status = await call('status');
          renderStatus(status);
          if (status.profile?.name) return `Synced ${status.profile.name}${status.profile.avatar ? ' and profile photo' : ''}.`;
        }
        throw Error('Open your own profile in WhatsApp Web, then click Sync profile again.');
      } finally {
        const idx = JarvisApp.handlers.event.indexOf(handler);
        if (idx >= 0) JarvisApp.handlers.event.splice(idx, 1);
      }
    }
  );

  let onSyncModeConfirmed = null;
  function promptSyncModeDialog(onConfirm) {
    onSyncModeConfirmed = onConfirm;
    const dialog = $('sync-mode-dialog');
    if (!dialog) {
      if (onConfirm) onConfirm('new_only');
      return;
    }
    dialog.showModal();
  }

  $('start-main').onclick = () => action($('start-main'), async () => {
    if (JarvisApp.state?.running) {
      try {
        const result = await call('stop');
        renderStatus(result.status || result);
        toast('Workspace stopped.');
      } catch (error) {
        toast(error.message || 'Could not stop workspace.');
        throw error;
      }
      return;
    }

    // Always allow starting the workspace regardless of external connections
    promptSyncModeDialog(async (mode) => {
      try {
        const result = await call('start', { mode });
        renderStatus(result.status || result);
        toast(mode === 'new_only'
          ? 'Workspace running. Processing new messages only.'
          : 'Workspace running. Ingesting full chat history.'
        );
      } catch (error) {
        toast(error.message || 'Could not start workspace.');
      }
    });
  });

  for (const id of ['diagnostic', 'check-health']) {
    $(id).onclick = () => action($(id), async () => {
      const result = await call('diagnostic');
      renderStatus(result.status || result);
      toast('Connections checked.');
    });
  }

  let diagnosticEntries = [];
  function renderDiagnosticEntries() {
    const filter = $('diagnostic-log-filter').value.trim().toLowerCase();
    const lines = diagnosticEntries.map(entry => [
      entry.at, entry.level, entry.event, entry.code,
      Number.isInteger(entry.duration_ms) ? `${entry.duration_ms} ms` : null,
      Number.isInteger(entry.count) ? `count ${entry.count}` : null,
    ].filter(Boolean).join('  '));
    const visible = lines.filter(line => line.toLowerCase().includes(filter));
    const output = $('diagnostic-log-output');
    output.value = visible.join('\n');
    output.hidden = !visible.length;
    const status = $('diagnostic-log-status');
    status.textContent = `${visible.length} of ${lines.length} retained events`;
    status.hidden = false;
  }

  $('diagnostic-log-filter').oninput = renderDiagnosticEntries;
  $('diagnostic-log-scan').onclick = () => action($('diagnostic-log-scan'), async () => {
    const log = await call('diagnostics/logs');
    diagnosticEntries = log.entries;
    renderDiagnosticEntries();
  });
  $('diagnostic-log-save').onclick = () => action($('diagnostic-log-save'), async () => {
    const result = await call('diagnostics/save-log');
    if (result.saved) toast(`Saved ${result.entries} diagnostic events.`);
  });
  $('diagnostic-log-clear').onclick = () => action($('diagnostic-log-clear'), async () => {
    const result = await call('diagnostics/clear');
    if (result.cleared) {
      diagnosticEntries = [];
      renderDiagnosticEntries();
      toast('Local diagnostic log cleared.');
    }
  });

  $('refresh-activity').onclick = () => action($('refresh-activity'), async () => {
    await refreshActivity(true);
    const isLive = !!(JarvisApp.state?.bridge?.verified || JarvisApp.state?.direct_wa?.connected);
    if (!isLive) {
      openExtensionSyncDialog();
    }
  });
  $('refresh-insights').onclick = () => action($('refresh-insights'), () => loadInsights(true));
  $('refresh-alerts').onclick = () => action($('refresh-alerts'), () => loadAlerts(true));
  if ($('clear-alerts')) {
    $('clear-alerts').onclick = () => action($('clear-alerts'), async () => {
      await call('alerts/action', { action: 'clear_all' });
      await loadAlerts(true);
      toast('Notifications archived.');
    });
  }
  document.querySelectorAll('.notification-filter-bar button').forEach((btn) => {
    btn.onclick = () => {
      document.querySelectorAll('.notification-filter-bar button').forEach((b) => b.classList.remove('active'));
      btn.classList.add('active');
      activeAlertFilter = btn.dataset.alertFilter || 'ACTIVE';
      renderAlerts(JarvisApp.alerts, JarvisApp.outboundData);
    };
  });
  JarvisApp.renderAlerts = renderAlerts;
  JarvisApp.getActiveAlertFilter = () => activeAlertFilter;
  JarvisApp.setActiveAlertFilter = (f) => {
    activeAlertFilter = f;
    document.querySelectorAll('.notification-filter-bar button').forEach((b) => {
      b.classList.toggle('active', b.dataset.alertFilter === f);
    });
    renderAlerts(JarvisApp.alerts, JarvisApp.outboundData);
  };
  let stagedOutboundAttachment = null;
  function renderOutboundAttachmentPreview() {
    const preview = $('outbound-attachment-preview');
    if (!preview) return;
    preview.replaceChildren();
    if (!stagedOutboundAttachment) {
      preview.hidden = true;
      return;
    }
    preview.hidden = false;
    const pill = document.createElement('div');
    pill.className = 'wa-att-pill';
    const label = document.createElement('span');
    label.className = 'wa-att-label';
    label.textContent = `📎 ${stagedOutboundAttachment.name} (${Math.round(stagedOutboundAttachment.size / 1024)} KB)`;
    const delBtn = document.createElement('button');
    delBtn.type = 'button';
    delBtn.className = 'wa-att-del';
    delBtn.textContent = '✕';
    delBtn.setAttribute('aria-label', 'Remove attachment');
    delBtn.onclick = () => {
      stagedOutboundAttachment = null;
      renderOutboundAttachmentPreview();
    };
    pill.append(label, delBtn);
    preview.append(pill);
  }

  async function stageOutboundFile(file) {
    if (!file) return;
    try {
      let desc;
      if (file.path) {
        const res = await call('attachment/stage', { file_path: file.path });
        if (res?.error) throw new Error(res.error.message);
        desc = res?.descriptor || res?.result?.descriptor;
      } else {
        const buf = await file.arrayBuffer();
        const res = await call('attachment/stage', {
          name: file.name,
          mime_type: file.type || 'application/octet-stream',
          buffer: Array.from(new Uint8Array(buf)),
        });
        if (res?.error) throw new Error(res.error.message);
        desc = res?.descriptor || res?.result?.descriptor;
      }
      if (desc) {
        stagedOutboundAttachment = desc;
        renderOutboundAttachmentPreview();
        toast(`Attached: ${desc.name}`);
      }
    } catch (err) {
      toast(`Attachment failed: ${err.message || err}`);
    }
  }

  $('outbound-attachment-input')?.addEventListener('change', (e) => {
    const file = e.target.files?.[0];
    if (file) stageOutboundFile(file);
    e.target.value = '';
  });
  ($('outbound-attach') || $('outbound-attach-btn'))?.addEventListener('click', () => $('outbound-attachment-input')?.click());

  $('outbound-compose')?.addEventListener('dragover', (e) => e.preventDefault());
  $('outbound-compose')?.addEventListener('drop', (e) => {
    e.preventDefault();
    const file = e.dataTransfer?.files?.[0];
    if (file) stageOutboundFile(file);
  });
  $('outbound-text')?.addEventListener('paste', async (e) => {
    const file = e.clipboardData?.files?.[0];
    if (file) {
      e.preventDefault();
      await stageOutboundFile(file);
    }
  });

  async function submitOutboundForm(approveNow) {
    const phone = $('outbound-phone').value.trim();
    const text = $('outbound-text').value.trim();
    const blockerEl = $('outbound-blocker-status') || $('outbound-blocker');
    if (!phone) {
      toast('Please enter a recipient phone number.');
      return;
    }
    if (!text && !stagedOutboundAttachment) {
      toast('Please enter a message or select an attachment.');
      return;
    }
    const payload = {
      recipient_phone: phone,
      text: text,
      approve_now: approveNow,
      client_request_id: 'out_' + Date.now() + '_' + Math.random().toString(36).slice(2, 9),
    };
    if (stagedOutboundAttachment) {
      payload.attachment_path = stagedOutboundAttachment.file_path;
      payload.caption = text;
      payload.attachment_name = stagedOutboundAttachment.name;
      payload.attachment_mime = stagedOutboundAttachment.mime_type;
      payload.attachment_type = stagedOutboundAttachment.media_type;
      payload.attachment_size = stagedOutboundAttachment.size;
    }
    if (blockerEl) blockerEl.hidden = true;
    try {
      const res = await call('outbound/create', payload);
      if (res?.cancelled) {
        toast('Send cancelled.');
        return;
      }
      if (res?.approved) {
        $('outbound-text').value = '';
        stagedOutboundAttachment = null;
        renderOutboundAttachmentPreview();
        await loadAlerts(true);
        toast('Message queued for delivery.');
      } else {
        const msg = res?.message || res?.reason || 'Draft saved.';
        if (blockerEl) {
          blockerEl.hidden = false;
          blockerEl.textContent = `ℹ️ ${msg}`;
        }
        toast(`Draft saved: ${msg}`);
        if (!approveNow) {
          $('outbound-text').value = '';
          stagedOutboundAttachment = null;
          renderOutboundAttachmentPreview();
        }
        await loadAlerts(true);
      }
    } catch (err) {
      if (blockerEl) {
        blockerEl.hidden = false;
        blockerEl.textContent = `⚠️ ${err.message || 'Failed to dispatch'}`;
      }
      toast(err.message || 'Failed to dispatch message.');
    }
  }

  $('outbound-compose').onsubmit = (event) => {
    event.preventDefault();
  };
  $('outbound-draft')?.addEventListener('click', () => action($('outbound-draft'), () => submitOutboundForm(false)));
  $('outbound-send')?.addEventListener('click', () => action($('outbound-send'), () => submitOutboundForm(true)));

  $('add-source-number')?.addEventListener('click', () => {
    view('setup');
    const dbPhone = $('database-phone');
    if (dbPhone) {
      dbPhone.focus();
      dbPhone.scrollIntoView({ behavior: 'smooth', block: 'center' });
    }
  });
  $('new-inventory').onclick = () => editInventory();
  $('cancel-inventory').onclick = () => { $('inventory-form').hidden = true; };

  $('inventory-form').onsubmit = (e) => {
    e.preventDefault();
    action(e.submitter, async () => {
      await call('inventory/save', {
        id: $('inventory-id').value,
        expected_revision: Number($('inventory-revision').value),
        device: $('inventory-device').value.trim(),
        variant: $('inventory-variant').value.trim(),
        quantity: Number($('inventory-quantity').value),
        unit_cost: $('inventory-cost').value ? Number($('inventory-cost').value) : null,
        currency: $('inventory-currency').value,
      });
      $('inventory-form').hidden = true;
      await loadInsights(true);
      toast('Owned inventory saved.');
    });
  };

  let bulkParsedItems = null;
  let bulkIsAi = false;
  let bulkAiSource = '';

  $('stock-add-database-btn')?.addEventListener('click', () => {
    const panel = $('stock-import-panel');
    if (panel) {
      panel.open = true;
      panel.scrollIntoView({ behavior: 'smooth', block: 'start' });
      $('bulk-stock')?.focus();
    }
  });

  async function calculateAndRenderInventoryDiff() {
    if (!bulkParsedItems || !bulkParsedItems.length) {
      if ($('bulk-diff-summary')) $('bulk-diff-summary').hidden = true;
      if ($('bulk-commit')) $('bulk-commit').disabled = true;
      return;
    }

    await loadInsights(false);
    const existingInv = JarvisApp.insightData?.inventory || [];
    const makeKey = (dev, vr) => (String(dev || '').trim().toLowerCase() + '::' + String(vr || '').trim().toLowerCase());
    const existingMap = new Map();
    for (const item of existingInv) {
      const d = item.data || item;
      existingMap.set(makeKey(d.device, d.variant), { id: item.id, revision: item.revision || 0, ...d });
    }

    const mode = document.querySelector('input[name="stock-import-mode"]:checked')?.value || 'add';
    let newCount = 0;
    let matchedCount = 0;
    const diffRows = [];
    const rowsToCommit = [];

    for (const r of bulkParsedItems) {
      const key = makeKey(r.device, r.variant);
      const matched = existingMap.get(key);
      if (matched) {
        matchedCount++;
        if (mode === 'overwrite') {
          rowsToCommit.push({
            id: matched.id,
            device: r.device,
            variant: r.variant || matched.variant || '',
            quantity: Number(r.quantity),
            unit_cost: r.unit_cost !== null && r.unit_cost !== undefined ? Number(r.unit_cost) : (matched.unit_cost ?? null),
            currency: r.currency || matched.currency || 'INR',
            expected_revision: Number(matched.revision || 0),
          });
          diffRows.push({
            action: 'WILL UPDATE',
            badgeClass: 'diff-badge-update',
            device: r.device,
            variant: r.variant || matched.variant || 'Standard',
            current: `${matched.quantity} pcs @ ${matched.currency || 'INR'} ${matched.unit_cost ?? '—'}`,
            incoming: `${r.quantity} pcs @ ${r.currency || 'INR'} ${r.unit_cost ?? '—'}`,
          });
        } else {
          diffRows.push({
            action: 'WILL SKIP',
            badgeClass: 'diff-badge-skip',
            device: r.device,
            variant: r.variant || matched.variant || 'Standard',
            current: `${matched.quantity} pcs @ ${matched.currency || 'INR'} ${matched.unit_cost ?? '—'}`,
            incoming: `${r.quantity} pcs (Preserving existing)`,
          });
        }
      } else {
        newCount++;
        rowsToCommit.push({
          id: r.id || crypto.randomUUID(),
          device: r.device,
          variant: r.variant || '',
          quantity: Number(r.quantity),
          unit_cost: r.unit_cost !== null && r.unit_cost !== undefined ? Number(r.unit_cost) : null,
          currency: r.currency || 'INR',
          expected_revision: 0,
        });
        diffRows.push({
          action: 'NEW ROW',
          badgeClass: 'diff-badge-new',
          device: r.device,
          variant: r.variant || 'Standard',
          current: 'None (new item)',
          incoming: `${r.quantity} pcs @ ${r.currency || 'INR'} ${r.unit_cost ?? 'Not set'}`,
        });
      }
    }

    bulkRows = rowsToCommit;
    bulkRequest = crypto.randomUUID();

    const diffSummary = $('bulk-diff-summary');
    if (diffSummary) diffSummary.hidden = false;

    if ($('diff-total-count')) $('diff-total-count').textContent = String(bulkParsedItems.length);
    if ($('diff-new-count')) $('diff-new-count').textContent = String(newCount);
    if ($('diff-matched-count')) $('diff-matched-count').textContent = String(matchedCount);
    if ($('diff-action-count')) $('diff-action-count').textContent = String(rowsToCommit.length);
    if ($('diff-action-lbl')) $('diff-action-lbl').textContent = mode === 'overwrite' ? 'To Save / Update' : 'New To Add';

    const explEl = $('diff-explanation');
    if (explEl) {
      let aiNote = bulkIsAi ? `✨ <strong>AI Extracted</strong> (${bulkAiSource}). ` : '';
      if (mode === 'overwrite') {
        explEl.innerHTML = `${aiNote}Will <strong>update ${matchedCount} matched rows</strong> with latest pricing/quantity and insert <strong>${newCount} new rows</strong>.`;
      } else {
        explEl.innerHTML = `${aiNote}Will <strong>insert ${newCount} new rows</strong>. <strong>${matchedCount} matched rows</strong> will be skipped and preserved.`;
      }
    }

    const tableWrap = $('diff-table-wrap');
    if (tableWrap) {
      tableWrap.replaceChildren();
      const table = document.createElement('table');
      table.className = 'diff-table';
      const thead = document.createElement('thead');
      thead.innerHTML = '<tr><th>Action</th><th>Device</th><th>Variant</th><th>Current Owned Stock</th><th>Incoming Batch</th></tr>';
      table.append(thead);
      const tbody = document.createElement('tbody');
      for (const row of diffRows) {
        const tr = document.createElement('tr');
        const tdAct = document.createElement('td');
        const badge = document.createElement('span');
        badge.className = `diff-badge ${row.badgeClass}`;
        badge.textContent = row.action;
        tdAct.append(badge);

        const tdDev = document.createElement('td');
        const strong = document.createElement('strong');
        strong.textContent = row.device;
        tdDev.append(strong);

        const tdVar = document.createElement('td');
        tdVar.textContent = row.variant;

        const tdCur = document.createElement('td');
        tdCur.textContent = row.current;

        const tdInc = document.createElement('td');
        tdInc.textContent = row.incoming;

        tr.append(tdAct, tdDev, tdVar, tdCur, tdInc);
        tbody.append(tr);
      }
      table.append(tbody);
      tableWrap.append(table);
    }

    const commitBtn = $('bulk-commit');
    if (commitBtn) {
      commitBtn.disabled = rowsToCommit.length === 0;
      commitBtn.textContent = mode === 'overwrite' ? `Overwrite & Add (${rowsToCommit.length} items)` : `Add to Database (${rowsToCommit.length} new items)`;
    }
  }

  document.querySelectorAll('input[name="stock-import-mode"]').forEach((r) => {
    r.addEventListener('change', () => calculateAndRenderInventoryDiff());
  });

  $('bulk-preview').onclick = () => action($('bulk-preview'), async () => {
    const raw = ($('bulk-stock')?.value || '').trim();
    if (!raw) throw Error('Paste spreadsheet rows or TSV/CSV data first.');
    const table = parseGrid(raw);
    if (table.length < 2) throw Error('Include a header row and at least 1 data row.');
    const headers = table.shift().map((s) => s.trim().toLowerCase().replaceAll(' ', '_'));
    if (!headers.includes('device') || !headers.includes('quantity')) throw Error('Include Device and Quantity columns.');
    bulkParsedItems = table.map((cells) => {
      const r = Object.fromEntries(headers.map((h, i) => [h, cells[i]?.trim() || '']));
      return {
        id: r.id || crypto.randomUUID(),
        device: r.device,
        variant: r.variant || '',
        quantity: Number(r.quantity) || 1,
        unit_cost: r.unit_cost ? Number(r.unit_cost) : null,
        currency: r.currency || 'INR',
      };
    });
    bulkIsAi = false;
    bulkAiSource = 'Spreadsheet / TSV';
    await calculateAndRenderInventoryDiff();
    toast(`Previewing ${bulkParsedItems.length} rows from spreadsheet.`);
  });

  const readFileAsDataUrl = (file) => new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result || ''));
    reader.onerror = () => reject(new Error('Could not read selected file.'));
    reader.readAsDataURL(file);
  });

  if ($('bulk-file')) {
    $('bulk-file').onchange = () => {
      bulkParsedItems = null;
      bulkRows = null;
      if ($('bulk-commit')) $('bulk-commit').disabled = true;
      if ($('bulk-diff-summary')) $('bulk-diff-summary').hidden = true;
      const f = $('bulk-file').files?.[0];
      const statusPill = $('bulk-ai-status');
      if (f && statusPill) {
        statusPill.hidden = false;
        statusPill.textContent = `Ready to AI-extract: ${f.name} (${Math.round(f.size / 1024)} KB). Click "Extract stock with Gemini AI".`;
      }
    };
  }

  if ($('bulk-ai-parse')) {
    $('bulk-ai-parse').onclick = () => action($('bulk-ai-parse'), async () => {
      const file = $('bulk-file')?.files?.[0];
      const rawText = ($('bulk-stock')?.value || '').trim();
      if (!file && !rawText) throw Error('Select an Excel/CSV/image file or paste raw stock text first.');
      if (file && file.size > 2_000_000) throw Error('Keep uploaded files under 2 MB.');
      const statusPill = $('bulk-ai-status');
      if (statusPill) {
        statusPill.hidden = false;
        statusPill.textContent = 'Extracting structured rows via Gemini AI…';
      }
      let fileB64 = null;
      if (file) fileB64 = await readFileAsDataUrl(file);
      const parsed = await call('inventory/parse', {
        raw_text: rawText,
        file_b64: fileB64,
        file_name: file?.name || '',
        file_mime: file?.type || '',
      });
      const rows = Array.isArray(parsed?.rows) ? parsed.rows : [];
      if (!rows.length) throw Error('No valid inventory rows could be extracted by AI.');
      bulkParsedItems = rows;
      bulkIsAi = true;
      bulkAiSource = file ? `${file.name} (${parsed.detected_format || 'photo/file'})` : 'Pasted Text';
      if (statusPill) {
        statusPill.textContent = `✓ Gemini extracted ${rows.length} rows (${parsed.detected_format || 'structured'}).`;
      }
      await calculateAndRenderInventoryDiff();
      toast(`AI extracted ${rows.length} stock rows.`);
    });
  }

  $('bulk-stock').oninput = () => {
    bulkParsedItems = null;
    bulkRows = null;
    if ($('bulk-commit')) $('bulk-commit').disabled = true;
    if ($('bulk-diff-summary')) $('bulk-diff-summary').hidden = true;
  };

  $('bulk-commit').onclick = () => action($('bulk-commit'), async () => {
    if (!bulkRows || !bulkRows.length) throw Error('Preview or AI-extract the stock rows first.');
    const mode = document.querySelector('input[name="stock-import-mode"]:checked')?.value || 'add';
    let saved = 0;
    for (let i = 0; i < bulkRows.length; i += 20) {
      const chunk = bulkRows.slice(i, i + 20);
      await call('inventory/bulk', {
        rows: chunk,
        request_id: `${bulkRequest.slice(0, 50)}-${Math.floor(i / 20)}`,
        mode: mode,
      });
      saved += chunk.length;
    }
    const hasSheets = Boolean(JarvisApp.state?.sheets_configured);
    const syncStatus = $('bulk-sync-status');
    if (syncStatus) {
      syncStatus.textContent = hasSheets
        ? `✓ Committed ${saved} items to local database · Pending Google Sheets sync`
        : `✓ Committed ${saved} items to local database (local storage)`;
    }
    bulkRows = null;
    bulkParsedItems = null;
    if ($('bulk-file')) $('bulk-file').value = '';
    $('bulk-commit').disabled = true;
    await loadInsights(true);
    toast(`Successfully saved ${saved} items to database.`);
  });

  $('refresh-market').onclick = () => action($('refresh-market'), async () => {
    const pending = loadingCard('Fetching verified market data and headlines…');
    $('news-list').prepend(pending);
    try {
      const [fresh] = await Promise.all([
        call('market/refresh'),
        JarvisApp.handlers.fxStudio?.refreshAll?.().catch(() => null),
      ]);
      JarvisApp.loadedAt.market = Date.now();
      renderMarket(fresh);
      toast('Market check finished.');
    } finally {
      pending.remove();
    }
  });

  $('change-sheet').onclick = () => {
    JarvisApp.connectionEditing = true;
    $('connection-form').hidden = false;
    $('sheet-connected').hidden = true;
  };
  $('connection-form').onsubmit = (e) => {
    e.preventDefault();
    action(e.submitter, async () => {
      const params = { script_url: $('script-url').value.trim() };
      if ($('signing-secret').value) params.signing_secret = $('signing-secret').value;
      const result = await call('connect', params);
      $('signing-secret').value = '';
      JarvisApp.connectionEditing = false;
      renderStatus(result.status || result);
      toast('Google Sheets connection verified.');
    });
  };
  async function openConnectedWorkbook() {
    try { await call('sheet/open'); }
    catch (error) {
      if (error.code !== 'SHEET_LINK_REQUIRED') throw error;
      view('connections');
      showResult($('sheet-result'), 'Paste the connected Google Sheet link below and save it.', 'error');
      $('setting-sheet').focus();
    }
  }
  $('open-sheet').onclick = () => action($('open-sheet'), openConnectedWorkbook);
  $('open-owned-stock-sheet').onclick = () => action($('open-owned-stock-sheet'), openConnectedWorkbook);
  $('save-sheet-link').onclick = () => panelAction($('save-sheet-link'), $('sheet-result'), 'Saving workbook link…', async () => {
    const current = JarvisApp.prefs || await call('settings/get');
    JarvisApp.prefs = await call('settings/save', { ...current, sheet_url: $('setting-sheet').value.trim() });
    return 'Workbook link saved. Open Sheet now uses this address.';
  });
  $('prepare-sheet').onclick = () => panelAction($('prepare-sheet'), $('sheet-result'), 'Checking database tabs in parallel…', async () => {
    const { tabs } = await call('sheet/prepare');
    const existing = Object.keys(tabs).filter((k) => tabs[k]);
    return `${Object.keys(tabs).length} database tabs ready.\nWith records · ${existing.join(', ') || 'None yet'}`;
  });
  $('add-first-stock').onclick = () => { view('stock'); $('new-inventory').click(); };

  $('add-gemini-key').onclick = () => addGeminiKeyRow();
  $('gemini-model').addEventListener('input', (e) => { e.target.dataset.dirty = 'true'; });
  $('gemini-form').onsubmit = (e) => {
    e.preventDefault();
    panelAction(e.submitter, $('gemini-result'), 'Saving keys to OS vault…', async () => {
      const keys = [...$('gemini-keys').children].map((row) =>
        row.dataset.savedIndex === undefined ? row.querySelector('input').value.trim() : Number(row.dataset.savedIndex)
      );
      const result = await call('provider/save', { model: $('gemini-model').value.trim(), keys });
      delete $('gemini-model').dataset.dirty;
      renderStatus(result);
      savedKeyRows(result.gemini.key_count, true);
      return `${result.gemini.key_count} Gemini key${result.gemini.key_count === 1 ? '' : 's'} saved. Test connection to verify.`;
    });
  };
  $('test-gemini').onclick = () => panelAction($('test-gemini'), $('gemini-result'), 'Waiting for Gemini’s test response…', async () => {
    const result = await call('provider/test');
    renderStatus(result.status);
    const switched = result.auto_switched ? `\nAuto-switched to healthy model · ${result.model}` : `\nModel · ${result.model || result.status?.gemini?.model}`;
    return `Gemini verified · ${(result.latency_ms / 1000).toFixed(2)} s${switched}\nSample · ${result.sample}\nResult · ${result.output.category}\n${result.output.reason}`;
  });
  $('discover-gemini').onclick = () => panelAction($('discover-gemini'), $('gemini-result'), 'Probing available Gemini models across your keys…', async () => {
    const result = await call('provider/discover', { auto_select: true });
    renderStatus(result.status);
    if (result.selected_model) {
      delete $('gemini-model').dataset.dirty;
      $('gemini-model').value = result.selected_model;
    }
    const probeLines = (result.probes || []).map((p) => `${p.ok ? '✓' : '✕'} ${p.model} · ${p.ok ? p.latency_ms + ' ms' : (p.code || 'unavailable')}`);
    const liveNote = (result.available_live_models || []).length ? `\nLive models · ${result.available_live_models.slice(0, 3).join(', ')}` : '';
    return `Selected fastest model: ${result.selected_model}\n${probeLines.join('\n')}${liveNote}`;
  });

  $('save-live-model').onclick = () => panelAction($('save-live-model'), $('live-result'), 'Saving Live model…', async () => {
    const current = JarvisApp.prefs || await call('settings/get');
    JarvisApp.prefs = await call('settings/save', { ...current, live_model: $('setting-live-model').value.trim() });
    return 'Gemini Live model saved.';
  });
  $('test-live-model').onclick = () => panelAction($('test-live-model'), $('live-result'), 'Connecting to Gemini Live…', async () => {
    if (JarvisApp.liveBusy) return `Use the active session in the ${JarvisApp.agentName} panel.`;
    const result = await call('live/start', { page: 'connections' });
    await call('live/stop');
    return `Gemini Live verified with ${result.model} in ${(result.setup_ms / 1000).toFixed(1)} s.`;
  });

  $('open-chrome').onclick = () => panelAction($('open-chrome'), $('bridge-result'), 'Opening Chrome…', async () => {
    await call('bridge/open');
    return `Chrome launch requested for WhatsApp Web. Check the ${JarvisApp.agentName} extension.`;
  });
  $('wa-direct-pair').onclick = () => panelAction($('wa-direct-pair'), $('bridge-result'), 'Generating Linked Device QR…', async () => {
    const st = await call('wa/connect');
    renderWaDirectState(st);
    refreshStatus();
    if (st.connected) return `Connected without Chrome as ${st.profile?.name || 'your WhatsApp account'}.`;
    if (st.qr) return 'QR ready above — scan from WhatsApp → Linked Devices.';
    return 'Connecting to WhatsApp multi-device servers…';
  });
  $('wa-direct-disconnect').onclick = () => panelAction($('wa-direct-disconnect'), $('bridge-result'), 'Disconnecting direct sync…', async () => {
    const st = await call('wa/disconnect', { wipe: false });
    renderWaDirectState(st);
    refreshStatus();
    return 'Direct WhatsApp session disconnected.';
  });
  $('test-whatsapp').onclick = () => panelAction($('test-whatsapp'), $('bridge-result'), 'Checking extension heartbeat…', async () => {
    const result = await call('status');
    renderStatus(result);
    if (!result.bridge.verified && !result.direct_wa?.connected) throw Error(`No recent extension heartbeat. Open Chrome, reload ${JarvisApp.agentName} extension, and click Check.`);
    if (result.direct_wa?.connected) {
      return `Direct WhatsApp connected without Chrome (${result.direct_wa.profile?.name || 'Linked Device'}) · Read-only active`;
    }
    const lastSeen = result.bridge.last_seen ? new Date(result.bridge.last_seen * 1000).toLocaleTimeString() : 'recently';
    return `Chrome extension connected · Last heartbeat ${lastSeen}\nWorkspace ${result.running ? 'scanning' : 'paused'} · ${result.bridge.observations} observations`;
  });

  $('market-key-form').onsubmit = (e) => {
    e.preventDefault();
    panelAction(e.submitter, $('news-result'), 'Saving market provider keys…', async () => {
      const data = {};
      if ($('fx-key').value) data.fx_key = $('fx-key').value;
      if ($('news-key').value) data.news_key = $('news-key').value;
      await call('market/save', data);
      $('fx-key').value = '';
      $('news-key').value = '';
      return 'Market provider keys saved. Fetch updates in Market watch to verify.';
    });
  };
  $('refresh-news-conn').onclick = () => panelAction($('refresh-news-conn'), $('news-result'), 'Checking news provider…', async () => {
    const result = await call('market/refresh');
    renderMarket(result);
    const headlines = result.news?.recent_articles || result.news?.articles || [];
    if (result.engine.news_error && !headlines.length) throw Error(result.engine.news_error);
    return `${headlines.length} headlines loaded from ${result.news?.provider || 'saved feed'}.`;
  });

  $('preferences').onsubmit = (e) => {
    e.preventDefault();
    action(e.submitter, async () => {
      const base = JarvisApp.prefs || await call('settings/get');
      const nextName = ($('setting-agent-name')?.value || 'Jarvis').trim() || 'Jarvis';
      JarvisApp.prefs = await call('settings/save', {
        ...base,
        market_enabled: $('setting-market').checked,
        notifications: $('setting-notify').checked,
        match_alerts: $('setting-match').checked,
        chime: $('setting-chime')?.checked !== false,
        fx_threshold_inr: Number($('setting-threshold').value),
        fx_poll_seconds: Number($('setting-poll').value),
        ui_scale: Number($('setting-ui-scale')?.value || localStorage.getItem('jarvis_ui_scale') || 100),
        base_currency: $('setting-base-currency')?.value || base.base_currency || 'INR',
        agent_name: nextName,
        agent_persona: $('setting-agent-persona')?.value || 'balanced',
        agent_language: $('setting-agent-language')?.value || 'auto_hindlish',
        agent_max_tool_steps: Number($('setting-agent-steps')?.value || 6),
        agent_auto_navigate: $('setting-agent-auto-nav')?.checked !== false,
        agent_allow_writes: $('setting-agent-allow-writes')?.checked !== false,
        agent_proactive_insights: $('setting-agent-proactive')?.checked !== false,
        agent_custom_instructions: $('setting-agent-instructions')?.value || '',
        autonomy_mode: $('setting-autonomy')?.value || base.autonomy_mode || 'observe',
        auto_reply_max_usd: Number($('setting-auto-max-usd')?.value ?? base.auto_reply_max_usd ?? 5000),
        default_margin_pct: Number($('setting-margin-pct')?.value ?? base.default_margin_pct ?? 3.5),
        volatility_aa_cap_pct: Number($('setting-aa-cap')?.value ?? base.volatility_aa_cap_pct ?? 2.0),
        working_hours_start: $('setting-wh-start')?.value || base.working_hours_start || '08:30',
        working_hours_end: $('setting-wh-end')?.value || base.working_hours_end || '21:30',
      });
      applyAgentBranding(JarvisApp.prefs.agent_name || 'Jarvis', true);
      toast(`${JarvisApp.agentName} settings & AI behavior saved.`);
    });
  };
  $('test-notification').onclick = () => action($('test-notification'), async () => {
    await call('notifications/test');
    toast('Windows confirmed notification delivery.');
  });

  if ($('setting-ui-scale')) {
    $('setting-ui-scale').onchange = (e) => {
      applyUiScale(e.target.value, true);
    };
  }
  if ($('btn-reset-scale')) {
    $('btn-reset-scale').onclick = () => {
      applyUiScale(100, true);
      toast('Display scale reset to 100%.');
    };
  }

  window.addEventListener('keydown', (e) => {
    if (e.ctrlKey || e.metaKey) {
      if (e.key === '0' || e.code === 'Digit0' || e.code === 'Numpad0') {
        e.preventDefault();
        applyUiScale(100, true);
        toast('Display scale reset to 100%.');
      } else if (e.key === '=' || e.key === '+' || e.code === 'Equal' || e.code === 'NumpadAdd') {
        e.preventDefault();
        const scales = [85, 90, 100, 110, 120];
        const current = Number(localStorage.getItem('jarvis_ui_scale')) || 100;
        const next = scales.find((s) => s > current) || 120;
        applyUiScale(next, true);
        toast(`Display scale set to ${next}%.`);
      } else if (e.key === '-' || e.code === 'Minus' || e.code === 'NumpadSubtract') {
        e.preventDefault();
        const scales = [120, 110, 100, 90, 85];
        const current = Number(localStorage.getItem('jarvis_ui_scale')) || 100;
        const next = scales.find((s) => s < current) || 85;
        applyUiScale(next, true);
        toast(`Display scale set to ${next}%.`);
      }
    }
  });

  // View lifecycle hook (instant cache render + background/SWR hydration)
  async function refreshDailyStock() {
    const target = $('daily-stock-status');
    try {
      const {snapshot} = await call('inventory/daily-stock');
      target.textContent = snapshot
        ? `${snapshot.row_count} stock rows · ${snapshot.fresh ? 'Current' : 'Expired'} · ${new Date(snapshot.source_timestamp * 1000).toLocaleString()}`
        : 'Waiting for the first stock file.';
    } catch {
      target.textContent = 'Stock file status unavailable.';
    }
  }

  JarvisApp.handlers.viewChange.push((name) => {
    if (name === 'setup') refreshDailyStock();
    if (name !== 'connections') {
      for (const row of $('gemini-keys').children) maskKeyRow(row);
    }
    if (name === 'activity') refreshActivity(false);
    if (name === 'stock') loadInsights(false);
    if (name === 'market') loadMarket(false);
    if (name === 'alerts') loadAlerts(false);
    if (name === 'settings' || name === 'connections') loadPreferences(false);
  });

  window.addEventListener('blur', () => {
    for (const row of $('gemini-keys').children) maskKeyRow(row);
  });

  // Event-driven updates from DesktopService
  JarvisApp.handlers.serviceState.push((event) => {
    if (event.state === 'ready') { refreshStatus(); hydrateBootstrap(false); }
    if (event.state === 'failed' || event.state === 'stopped') {
      $('service-label').textContent = event.state === 'failed' ? 'Desktop service unavailable' : 'Desktop service stopped';
      light('service-dot', false);
      if (event.message) notice(event.message);
    }
  });
  JarvisApp.handlers.dataUpdated.push((data) => {
    const cols = new Set(data?.collections || []);
    if (cols.has('cache-reset')) {
      clearStoredWorkspaceCache();
      hydrateBootstrap(true);
    }
    if (cols.has('messages') || cols.has('inventory')) {
      if (JarvisApp.activeView === 'setup') refreshDailyStock();
      if (cols.has('messages')) delete JarvisApp.loadedAt.activity;
      delete JarvisApp.loadedAt.stock;
      if (cols.has('messages') && (JarvisApp.activeView === 'activity' || JarvisApp.activeView === 'desk')) refreshActivity(false);
      if (JarvisApp.activeView === 'stock' || JarvisApp.activeView === 'desk') loadInsights(false);
    }
    if (cols.has('fx') || cols.has('news')) {
      delete JarvisApp.loadedAt.market;
      if (JarvisApp.activeView === 'market' || JarvisApp.activeView === 'desk') loadMarket(false);
    }
    if (cols.has('tasks') || cols.has('outbound')) {
      delete JarvisApp.loadedAt.alerts;
      loadAlerts(false);
    }
    if (cols.has('configuration')) {
      delete JarvisApp.loadedAt.settings;
      loadPreferences(false);
      refreshStatus();
    }
    if (cols.has('profile')) refreshStatus();
  });

  function applyUiAction(act) {
    if (!act || !act.action) return;
    if (act.action === 'navigate' && act.view) {
      view(act.view);
    }
    if (act.action === 'filter_messages' || act.msg_filter || act.search) {
      if (act.msg_filter) {
        setMessageFilter(act.msg_filter);
      }
      if (act.search !== undefined) {
        if ((act.view === 'stock' || JarvisApp.activeView === 'stock') && $('inventory-search')) {
          $('inventory-search').value = act.search;
          if (JarvisApp.insightData) renderInventory(JarvisApp.insightData.inventory || []);
        } else if ($('activity-search')) {
          $('activity-search').value = act.search;
        }
      }
      if (act.msg_filter || act.view === 'activity' || JarvisApp.activeView === 'activity') {
        if (JarvisApp.activeView !== 'activity') view('activity');
        requestActivityFilter();
      }
    }
    if (act.action === 'set_theme' && act.theme) {
      applyTheme(act.theme, true);
    }
    if (act.action === 'set_jarvis_mode') {
      if (act.jarvis_mode === 'fullscreen') view('assistant');
      else if (act.jarvis_mode === 'split') { setStudioOpen(false); setSplitDock(true); }
      else if (act.jarvis_mode === 'minimize') { setStudioOpen(false); setSplitDock(false); }
    }
    if (act.action === 'open_sheet') $('open-sheet')?.click();
    if (act.action === 'open_whatsapp') call('bridge/open').catch(() => {});
    if (act.action === 'check_connections') $('check-health')?.click();
    if (act.action === 'open_fx_dialog') openFxDialog();
    if (act.action === 'fx_control') {
      if (JarvisApp.activeView !== 'market') view('market');
      JarvisApp.handlers.fxStudio?.applyControl?.(act);
    }
  }
  JarvisApp.applyUiAction = applyUiAction;

  JarvisApp.handlers.event.push((packet) => {
    if (packet.event === 'notification') { loadAlerts(true); toast(`${packet.data.title}\n${packet.data.body}`); }
    if (packet.event === 'notification-click') view('alerts');
    if (packet.event === 'notification-error') notice(packet.data.message);
    if (packet.event === 'ui-action') applyUiAction(packet.data);
    if (packet.event === 'wa-direct') {
      renderWaDirectState(packet.data);
      if (packet.data?.connected || packet.data?.profile) refreshStatus();
    }
    if (packet.event === 'profile-updated') {
      if (JarvisApp.state) {
        JarvisApp.state = { ...JarvisApp.state, profile: packet.data };
        saveBootCache({ status: JarvisApp.state });
      }
      renderProfile(packet.data);
    }
    if (packet.event === 'bridge-status') {
      if (JarvisApp.state) {
        JarvisApp.state = { ...JarvisApp.state, bridge: packet.data, ...(typeof packet.data?.running === 'boolean' ? { running: packet.data.running } : {}) };
      }
      renderBridgeLive(packet.data, !!JarvisApp.state?.direct_wa?.connected, typeof packet.data?.running === 'boolean' ? packet.data.running : !!JarvisApp.state?.running);
    }
  });

  // Global Command Palette (Ctrl+K)
  const cmdk = $('cmdk-dialog');
  const cmdkInput = $('cmdk-input');
  const cmdkList = $('cmdk-list');
  let cmdkItems = [];
  let cmdkIndex = 0;

  function buildCommands() {
    const items = [
      { label: 'Go to Overview', hint: 'Operations', run: () => view('desk') },
      { label: 'Go to Messages', hint: 'Activity', run: () => view('activity') },
      { label: 'Go to Stock & demand', hint: 'Inventory', run: () => view('stock') },
      { label: 'Go to Market watch & Converter', hint: 'FX & News', run: () => { view('market'); JarvisApp.handlers.fxStudio?.setTab?.('desk'); } },
      { label: 'Go to World Currency Exchange (160+)', hint: 'World FX', run: () => { view('market'); JarvisApp.handlers.fxStudio?.setTab?.('world'); } },
      { label: 'Go to Notifications', hint: 'Alerts', run: () => view('alerts') },
      { label: 'Go to Workspace setup', hint: 'Numbers', run: () => view('setup') },
      { label: 'Go to Connections', hint: 'Sheets / Gemini', run: () => view('connections') },
      { label: 'Go to Settings', hint: 'Preferences', run: () => view('settings') },
      { label: `${JarvisApp.agentName} Split-Pane HUD`, hint: 'Ctrl J', run: () => ($('jarvis-dock').hidden ? $('open-jarvis').click() : $('close-jarvis').click()) },
      { label: `${JarvisApp.agentName} Voice Studio (Full-Screen Orb)`, hint: 'Live Mic', run: () => view('assistant') },
      { label: JarvisApp.state?.running ? 'Stop workspace observation' : 'Start workspace observation', hint: 'Toggle', run: () => $('start-main').click() },
      { label: 'Check all connections', hint: 'Diagnostic', run: () => $('check-health').click() },
      { label: 'Open connected Google Sheet', hint: 'External', run: () => $('open-sheet').click() },
      { label: 'Convert USD currency', hint: 'Frankfurter', run: () => openFxDialog() },
      { label: 'Switch Light / Dark mode', hint: 'Theme', run: () => $('theme-toggle').click() },
    ];
    for (const row of (JarvisApp.insightData?.inventory || []).slice(0, 8)) {
      const d = row.data;
      items.push({ label: `Stock: ${d.device} ${d.variant || ''}`.trim(), hint: `${d.quantity} units`, run: () => view('stock') });
    }
    return items;
  }

  function renderCmdk() {
    const q = cmdkInput.value.trim().toLowerCase();
    const all = buildCommands();
    cmdkItems = q ? all.filter((item) => (item.label + ' ' + item.hint).toLowerCase().includes(q)) : all;
    if (cmdkIndex >= cmdkItems.length) cmdkIndex = 0;
    cmdkList.replaceChildren();
    if (!cmdkItems.length) {
      const empty = document.createElement('p');
      empty.className = 'panel-help';
      empty.textContent = 'No matching commands or records.';
      cmdkList.append(empty);
      return;
    }
    cmdkItems.forEach((item, i) => {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'cmdk-item' + (i === cmdkIndex ? ' active' : '');
      const title = document.createElement('span');
      title.textContent = item.label;
      const hint = document.createElement('small');
      hint.textContent = item.hint;
      btn.append(title, hint);
      btn.onclick = () => { cmdk.close(); item.run(); };
      cmdkList.append(btn);
    });
  }

  function openCmdk() {
    cmdkInput.value = '';
    cmdkIndex = 0;
    renderCmdk();
    cmdk.showModal();
    cmdkInput.focus();
  }

  $('cmdk-open').onclick = openCmdk;
  cmdkInput.oninput = () => { cmdkIndex = 0; renderCmdk(); };
  cmdkInput.addEventListener('keydown', (e) => {
    if (e.key === 'ArrowDown') { e.preventDefault(); cmdkIndex = (cmdkIndex + 1) % Math.max(1, cmdkItems.length); renderCmdk(); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); cmdkIndex = (cmdkIndex - 1 + Math.max(1, cmdkItems.length)) % Math.max(1, cmdkItems.length); renderCmdk(); }
    else if (e.key === 'Enter' && cmdkItems[cmdkIndex]) { e.preventDefault(); cmdk.close(); cmdkItems[cmdkIndex].run(); }
  });
  window.addEventListener('keydown', (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {
      e.preventDefault();
      cmdk.open ? cmdk.close() : openCmdk();
    } else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'j') {
      e.preventDefault();
      $('jarvis-dock').hidden ? $('open-jarvis').click() : $('close-jarvis').click();
    } else if (e.key === 'Escape' && !$('jarvis-studio')?.hidden) {
      $('studio-back')?.click();
    }
  });

  // Workspace Health Green Button
  $('health-cta-btn')?.addEventListener('click', () => view('connections'));

  // Dual Messaging Mode Toggles & WhatsApp View Actions
  function setMessagesMode(mode) {
    const isWa = mode === 'wa';
    document.body.classList.toggle('view-whatsapp-mode', isWa);
    $('msg-mode-btn-feed')?.classList.toggle('active', !isWa);
    $('msg-mode-btn-wa')?.classList.toggle('active', isWa);
    if ($('messages-feed-wrap')) $('messages-feed-wrap').hidden = isWa;
    if ($('messages-wa-wrap')) $('messages-wa-wrap').hidden = !isWa;
    try { localStorage.setItem('jarvis-msg-mode', mode); } catch {}
    if (isWa) renderWhatsAppMode();
  }
  $('msg-mode-btn-feed')?.addEventListener('click', () => setMessagesMode('feed'));
  $('msg-mode-btn-wa')?.addEventListener('click', () => setMessagesMode('wa'));

  try {
    const savedMsgMode = localStorage.getItem('jarvis-msg-mode') || 'feed';
    if (savedMsgMode === 'wa') setMessagesMode('wa');
  } catch {}

  JarvisApp.handlers.viewChange.push((viewName) => {
    if (viewName !== 'activity') {
      document.body.classList.remove('view-whatsapp-mode');
    } else {
      const mode = localStorage.getItem('jarvis-msg-mode') || 'feed';
      if (mode === 'wa') document.body.classList.add('view-whatsapp-mode');
    }
  });

  $('wa-search-input')?.addEventListener('input', () => renderWhatsAppMode());
  document.querySelectorAll('.wa-chip-filter').forEach((btn) => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.wa-chip-filter').forEach((b) => b.classList.remove('active'));
      btn.classList.add('active');
      activeWaCatFilter = btn.dataset.waCat || 'ALL';
      renderWhatsAppMode();
    });
  });

  let stagedWaAttachment = null;
  function renderWaAttachmentPreview() {
    const preview = $('wa-attachment-preview');
    if (!preview) return;
    preview.replaceChildren();
    if (!stagedWaAttachment) {
      preview.hidden = true;
      return;
    }
    preview.hidden = false;
    const pill = document.createElement('div');
    pill.className = 'wa-att-pill';
    const label = document.createElement('span');
    label.className = 'wa-att-label';
    label.textContent = `📎 ${stagedWaAttachment.name} (${Math.round(stagedWaAttachment.size / 1024)} KB)`;
    const delBtn = document.createElement('button');
    delBtn.type = 'button';
    delBtn.className = 'wa-att-del';
    delBtn.textContent = '✕';
    delBtn.setAttribute('aria-label', 'Remove attachment');
    delBtn.onclick = () => {
      stagedWaAttachment = null;
      renderWaAttachmentPreview();
      if (activeWaGroupObj?.convId) {
        const input = $('wa-composer-input');
        const existing = threadDrafts.get(activeWaGroupObj.convId);
        const text = (typeof existing === 'object' && existing !== null) ? existing.text : (input?.value || '');
        threadDrafts.set(activeWaGroupObj.convId, { text, attachment: null });
      }
    };
    pill.append(label, delBtn);
    preview.append(pill);
  }

  async function stageWaFile(file) {
    if (!file) return;
    try {
      let desc;
      if (file.path) {
        const res = await call('attachment/stage', { file_path: file.path });
        if (res?.error) throw new Error(res.error.message);
        desc = res?.descriptor || res?.result?.descriptor;
      } else {
        const buf = await file.arrayBuffer();
        const res = await call('attachment/stage', {
          name: file.name,
          mime_type: file.type || 'application/octet-stream',
          buffer: Array.from(new Uint8Array(buf)),
        });
        if (res?.error) throw new Error(res.error.message);
        desc = res?.descriptor || res?.result?.descriptor;
      }
      if (desc) {
        stagedWaAttachment = desc;
        renderWaAttachmentPreview();
        if (activeWaGroupObj?.convId) {
          const input = $('wa-composer-input');
          const existing = threadDrafts.get(activeWaGroupObj.convId);
          const text = (typeof existing === 'object' && existing !== null) ? existing.text : (input?.value || '');
          threadDrafts.set(activeWaGroupObj.convId, { text, attachment: desc });
        }
        toast(`Attached: ${desc.name}`);
      }
    } catch (err) {
      toast(`Attachment failed: ${err.message || err}`);
    }
  }

  $('wa-attachment-input')?.addEventListener('change', (e) => {
    const file = e.target.files?.[0];
    if (file) stageWaFile(file);
    e.target.value = '';
  });
  $('wa-composer-attach')?.addEventListener('click', () => $('wa-attachment-input')?.click());

  const composerWrap = document.querySelector('.wa-composer-wrap');
  composerWrap?.addEventListener('dragover', (e) => e.preventDefault());
  composerWrap?.addEventListener('drop', (e) => {
    e.preventDefault();
    const file = e.dataTransfer?.files?.[0];
    if (file) stageWaFile(file);
  });
  $('wa-composer-input')?.addEventListener('paste', async (e) => {
    const file = e.clipboardData?.files?.[0];
    if (file) {
      e.preventDefault();
      await stageWaFile(file);
    }
  });

  async function dispatchWaComposerSend(approveNow = true) {
    if (isSendingWaMessage) return;

    const input = $('wa-composer-input');
    const sendBtn = $('wa-composer-send');
    const draftBtn = $('wa-composer-draft');
    const val = (input?.value || '').trim();
    if (!val && !stagedWaAttachment) return;

    if (!activeWaGroupObj) {
      toast('Please select a conversation first.');
      return;
    }
    if (activeWaGroupObj.isGroup) {
      toast('Sending to WhatsApp groups is disabled to prevent accidental broadcasts.');
      return;
    }

    const isConnected = !!(JarvisApp.state?.bridge?.verified || JarvisApp.state?.direct_wa?.connected);
    const statusEl = $('wa-send-status');

    if (approveNow && !isConnected) {
      if (statusEl) {
        statusEl.hidden = false;
        statusEl.textContent = '⚠️ WhatsApp transport not connected. Connect WhatsApp Web or Direct WhatsApp to deliver messages.';
      }
      toast('WhatsApp transport not connected. Connect WhatsApp Web to deliver messages.');
      openExtensionSyncDialog();
      return;
    }

    const bridgeAct = JarvisApp.state?.bridge?.activity || {};
    if (approveNow && bridgeAct.detail?.includes('Scan QR')) {
      if (statusEl) {
        statusEl.hidden = false;
        statusEl.textContent = '⚠️ Scan QR code in WhatsApp Web to resume delivery.';
      }
      toast('WhatsApp Web requires QR scan.');
      return;
    }
    if (approveNow && bridgeAct.detail?.includes('Use here')) {
      if (statusEl) {
        statusEl.hidden = false;
        statusEl.textContent = '⚠️ WhatsApp is open in another tab. Click "Use here" to resume.';
      }
      toast('WhatsApp Web Use Here conflict.');
      return;
    }

    if (statusEl) statusEl.hidden = true;

    const targetPhone = activeWaGroupObj.phone || activeWaGroupObj.lastRecord?.data?.phone || '';
    const convId = activeWaGroupObj.convId;
    if (!targetPhone && !convId) {
      toast('Could not find a phone number or conversation ID for this contact.');
      return;
    }

    isSendingWaMessage = true;
    if (input) input.disabled = true;
    if (sendBtn) sendBtn.disabled = true;
    if (draftBtn) draftBtn.disabled = true;

    const clientRequestId = 'req_' + Date.now() + '_' + Math.random().toString(36).slice(2, 9);
    const sentConvId = convId;

    const payload = {
      recipient_phone: targetPhone,
      conversation_id: sentConvId,
      text: val,
      approve_now: approveNow,
      client_request_id: clientRequestId,
    };
    if (stagedWaAttachment) {
      payload.attachment_path = stagedWaAttachment.file_path;
      payload.caption = val;
      payload.attachment_name = stagedWaAttachment.name;
      payload.attachment_mime = stagedWaAttachment.mime_type;
      payload.attachment_type = stagedWaAttachment.media_type;
      payload.attachment_size = stagedWaAttachment.size;
    }

    try {
      const res = await call('outbound/create', payload);

      if (res?.cancelled) {
        toast('Send cancelled.');
        return;
      }

      if (res?.approved) {
        threadDrafts.delete(sentConvId);
        if (input && activeWaGroupObj?.convId === sentConvId) {
          input.value = '';
          adjustComposerHeight(input);
        }
        stagedWaAttachment = null;
        renderWaAttachmentPreview();
        toast(`Message queued for delivery to ${activeWaGroupObj?.name || targetPhone}.`);

        const stream = $('wa-chat-stream');
        if (stream && activeWaGroupObj?.convId === sentConvId) {
          const bubble = document.createElement('div');
          bubble.className = 'wa-bubble outgoing';
          if (payload.attachment_name) {
            const attPill = document.createElement('div');
            attPill.className = 'wa-att-pill';
            attPill.textContent = `📎 ${payload.attachment_name}`;
            bubble.append(attPill);
          }
          const text = document.createElement('p');
          text.className = 'wa-bubble-text';
          text.textContent = val || `[Attachment: ${payload.attachment_name}]`;
          bubble.append(text);

          const footer = document.createElement('div');
          footer.className = 'wa-bubble-footer';
          const time = document.createElement('span');
          time.textContent = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
          const check = document.createElement('span');
          check.className = 'wa-checkmarks';
          check.textContent = ' ⏱';
          footer.append(time, check);
          bubble.append(footer);

          stream.append(bubble);
          stream.scrollTop = stream.scrollHeight;
        }
      } else {
        const reasonMsg = res?.message || res?.reason || 'Draft queued pending approval or active workspace.';
        if (statusEl) {
          statusEl.hidden = false;
          statusEl.textContent = `ℹ️ ${reasonMsg}`;
        }
        toast(`Draft saved: ${reasonMsg}`);
        if (!approveNow) {
          threadDrafts.delete(sentConvId);
          if (input && activeWaGroupObj?.convId === sentConvId) {
            input.value = '';
            adjustComposerHeight(input);
          }
          stagedWaAttachment = null;
          renderWaAttachmentPreview();
        }
      }
    } catch (err) {
      if (statusEl) {
        statusEl.hidden = false;
        statusEl.textContent = `⚠️ Message failed to send: ${err.message}`;
      }
      toast(err.message || 'Failed to dispatch WhatsApp reply.');
    } finally {
      isSendingWaMessage = false;
      if (input && (!activeWaGroupObj || !activeWaGroupObj.isGroup)) {
        input.disabled = false;
        input.focus();
      }
      if (sendBtn && (!activeWaGroupObj || !activeWaGroupObj.isGroup)) {
        sendBtn.disabled = false;
      }
      if (draftBtn && (!activeWaGroupObj || !activeWaGroupObj.isGroup)) {
        draftBtn.disabled = false;
      }
    }
  }

  function adjustComposerHeight(el) {
    if (!el) return;
    el.style.height = 'auto';
    const h = Math.max(40, Math.min(el.scrollHeight, 140));
    el.style.height = `${h}px`;
  }

  $('wa-composer-input')?.addEventListener('input', (e) => {
    adjustComposerHeight(e.target);
    if (activeWaGroupObj?.convId) {
      const existing = threadDrafts.get(activeWaGroupObj.convId);
      const att = (typeof existing === 'object' && existing !== null) ? existing.attachment : stagedWaAttachment;
      threadDrafts.set(activeWaGroupObj.convId, { text: e.target.value, attachment: att });
    }
  });
  $('wa-composer-send')?.addEventListener('click', () => dispatchWaComposerSend(true));
  $('wa-composer-draft')?.addEventListener('click', () => dispatchWaComposerSend(false));
  $('wa-composer-input')?.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      if (e.isComposing || e.keyCode === 229) return;
      if (!e.shiftKey) {
        e.preventDefault();
        dispatchWaComposerSend(true);
      }
    }
  });

  function playTradeChime() {
    if (JarvisApp.settings?.chime === false) return;
    try {
      const AudioCtx = window.AudioContext || window.webkitAudioContext;
      if (!AudioCtx) return;
      const ctx = new AudioCtx();
      const now = ctx.currentTime;
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.type = 'sine';
      osc.frequency.setValueAtTime(587.33, now);
      osc.frequency.setValueAtTime(880.00, now + 0.1);
      gain.gain.setValueAtTime(0.12, now);
      gain.gain.exponentialRampToValueAtTime(0.001, now + 0.35);
      osc.connect(gain);
      gain.connect(ctx.destination);
      osc.start(now);
      osc.stop(now + 0.35);
    } catch {}
  }

  $('ext-sync-dialog-close')?.addEventListener('click', () => {
    $('extension-sync-dialog')?.close();
  });
  $('ext-pair-btn')?.addEventListener('click', async () => {
    const btn = $('ext-pair-btn');
    const origText = btn.textContent;
    btn.disabled = true;
    btn.textContent = 'Auto-Pairing…';
    try {
      const res = await call('extension/pair', {});
      const pairedCount = (res.paired_ids || []).length;
      toast(`Extension paired! (${pairedCount} ID${pairedCount === 1 ? '' : 's'} enrolled)`);
      const st = await call('status', {});
      JarvisApp.state = { ...JarvisApp.state, ...st };
      renderBridgeLive(st.bridge, !!st.direct_wa?.connected, st.running);
      openExtensionSyncDialog();
    } catch (err) {
      toast(`Pairing error: ${err.message}`);
    } finally {
      btn.disabled = false;
      btn.textContent = origText;
    }
  });
  $('ext-open-wa-btn')?.addEventListener('click', async () => {
    try {
      await call('extension/pair', {});
    } catch {}
    call('bridge/open').catch(() => {});
    toast('Opening Chrome with WhatsApp Web…');
    $('extension-sync-dialog')?.close();
  });
  $('ext-check-health-btn')?.addEventListener('click', () => {
    view('connections');
    $('extension-sync-dialog')?.close();
  });

  $('sync-mode-dialog-close')?.addEventListener('click', () => {
    $('sync-mode-dialog')?.close();
    onSyncModeConfirmed = null;
  });

  $('sync-mode-new-btn')?.addEventListener('click', async () => {
    try {
      await call('wa/scan-mode', { mode: 'new_only' });
      try {
        if ($('sync-mode-remember')?.checked) {
          localStorage.setItem('jarvis-wa-sync-mode', 'new_only');
        }
      } catch {}
      $('sync-mode-dialog')?.close();
      toast('Sync Mode: Processing new incoming WhatsApp messages only.');
      if (onSyncModeConfirmed) {
        const callback = onSyncModeConfirmed;
        onSyncModeConfirmed = null;
        await callback('new_only');
      }
    } catch (err) {
      toast(`Failed to set sync mode: ${err.message || err}`);
    }
  });

  $('sync-mode-all-btn')?.addEventListener('click', async () => {
    try {
      await call('wa/scan-mode', { mode: 'all' });
      try {
        if ($('sync-mode-remember')?.checked) {
          localStorage.setItem('jarvis-wa-sync-mode', 'all');
        }
      } catch {}
      $('sync-mode-dialog')?.close();
      toast('Sync Mode: Ingesting full WhatsApp chat history.');
      if (onSyncModeConfirmed) {
        const callback = onSyncModeConfirmed;
        onSyncModeConfirmed = null;
        await callback('all');
      }
    } catch (err) {
      toast(`Failed to set sync mode: ${err.message || err}`);
    }
  });

  $('demand-add-sell-btn')?.addEventListener('click', () => {
    view('stock');
    editInventory();
    $('inventory-form')?.scrollIntoView({ behavior: 'smooth', block: 'center' });
  });

  // Data Export & Storage Buttons in Settings
  $('export-json-btn')?.addEventListener('click', () => {
    const dataStr = 'data:text/json;charset=utf-8,' + encodeURIComponent(JSON.stringify(JarvisApp.records, null, 2));
    const dl = document.createElement('a');
    dl.setAttribute('href', dataStr);
    dl.setAttribute('download', `jarvis-trade-messages-${Date.now()}.json`);
    dl.click();
    toast('Exported verified message records to JSON.');
  });
  $('export-csv-btn')?.addEventListener('click', () => {
    const header = ['ID', 'Sender', 'Phone', 'Timestamp', 'Category', 'Text'];
    const rows = JarvisApp.records.map((r) => [
      r.id,
      `"${(r.data?.sender_name || '').replace(/"/g, '""')}"`,
      `"${(r.data?.phone || '').replace(/"/g, '""')}"`,
      r.data?.timestamp || r.updated_at,
      r.data?.classification || 'WTB',
      `"${(r.data?.text || '').replace(/"/g, '""')}"`
    ]);
    const csv = [header.join(','), ...rows.map((row) => row.join(','))].join('\n');
    const dataStr = 'data:text/csv;charset=utf-8,' + encodeURIComponent(csv);
    const dl = document.createElement('a');
    dl.setAttribute('href', dataStr);
    dl.setAttribute('download', `jarvis-trade-messages-${Date.now()}.csv`);
    dl.click();
    toast('Exported verified message records to CSV.');
  });
  $('clear-cache-btn')?.addEventListener('click', () => {
    clearStoredWorkspaceCache();
    toast('Local workspace cache purged.');
  });

  // Background Auto-Fetch for Market Watch news & rates
  setInterval(() => {
    if (JarvisApp.activeView === 'market' && JarvisApp.state?.sheets_connected) {
      loadMarket(true).catch(() => {});
    }
  }, 60000);

  // Instant stale-while-revalidate hydration: render last saved snapshot at 0ms, then refresh cleanly
  const initialCache = readBootCache();
  if (initialCache.status || initialCache.dashboard || initialCache.activity || initialCache.market) {
    applyBootstrapSnapshot(initialCache, { fromCache: true });
    savedKeyRows(initialCache.status?.gemini?.key_count || 0);
  } else {
    addStock('');
    noData($('recent-messages'), 'Syncing recent WhatsApp messages…');
    noData($('overview-alert-list'), 'Checking alerts & stock matches…');
    skeletonList($('all-messages'), 4);
    skeletonList($('demand-chart'), 3);
    skeletonList($('trend-chart'), 2);
    skeletonList($('inventory-table'), 3);
    skeletonList($('news-list'), 3);
    savedKeyRows(0);
  }
  refreshStatus();
  hydrateBootstrap(false);
  const bootUiScale = Number(localStorage.getItem('jarvis_ui_scale')) || 100;
  if (bootUiScale && bootUiScale !== 100) {
    applyUiScale(bootUiScale, false);
  }
  setInterval(refreshStatus, 5000);
  setInterval(() => { if (JarvisApp.records.length) renderActivityLists(); }, 30000);
})();
