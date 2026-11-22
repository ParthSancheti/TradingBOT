'use strict';

(() => {
  const DEFAULT_PINNED = [
    'USD', 'INR', 'AED', 'EUR', 'GBP', 'SAR', 'CNY', 'JPY',
    'SGD', 'HKD', 'CAD', 'AUD', 'CHF', 'QAR', 'KWD', 'KRW',
  ];
  const FALLBACK_CATALOG = [
    ['USD', 'US Dollar', '$', 'currency'],
    ['INR', 'Indian Rupee', '₹', 'currency'],
    ['AED', 'UAE Dirham', 'د.إ', 'currency'],
    ['EUR', 'Euro', '€', 'currency'],
    ['GBP', 'British Pound', '£', 'currency'],
    ['SAR', 'Saudi Riyal', '﷼', 'currency'],
    ['CNY', 'Chinese Yuan', '¥', 'currency'],
    ['JPY', 'Japanese Yen', '¥', 'currency'],
    ['SGD', 'Singapore Dollar', 'S$', 'currency'],
    ['HKD', 'Hong Kong Dollar', 'HK$', 'currency'],
    ['CAD', 'Canadian Dollar', 'C$', 'currency'],
    ['AUD', 'Australian Dollar', 'A$', 'currency'],
    ['CHF', 'Swiss Franc', 'CHF', 'currency'],
    ['QAR', 'Qatari Riyal', 'ر.ق', 'currency'],
    ['KWD', 'Kuwaiti Dinar', 'د.ك', 'currency'],
    ['KRW', 'South Korean Won', '₩', 'currency'],
    ['THB', 'Thai Baht', '฿', 'currency'],
    ['MYR', 'Malaysian Ringgit', 'RM', 'currency'],
    ['TRY', 'Turkish Lira', '₺', 'currency'],
    ['ZAR', 'South African Rand', 'R', 'currency'],
    ['BRL', 'Brazilian Real', 'R$', 'currency'],
    ['MXN', 'Mexican Peso', '$', 'currency'],
    ['XAU', 'Gold (Troy Oz)', 'XAU', 'metal'],
    ['XAG', 'Silver (Troy Oz)', 'XAG', 'metal'],
  ].map(([code, name, symbol, type]) => ({
    code,
    name,
    symbol,
    type,
    pinned_default: DEFAULT_PINNED.includes(code),
  }));

  const SERIES_COLORS = ['#0ea5e9', '#8b5cf6', '#10b981', '#f59e0b', '#ec4899', '#14b8a6'];

  function loadStoredPins() {
    try {
      const raw = JSON.parse(localStorage.getItem('jarvis-fx-pins') || 'null');
      if (Array.isArray(raw) && raw.length) {
        const clean = raw.map((c) => String(c).toUpperCase()).filter((c) => /^[A-Z]{3}$/.test(c));
        if (clean.length) return clean;
      }
    } catch {}
    return [...DEFAULT_PINNED];
  }

  function loadStoredCode(key, fallback) {
    try {
      const v = (localStorage.getItem(key) || '').toUpperCase();
      if (/^[A-Z]{3}$/.test(v)) return v;
    } catch {}
    return fallback;
  }

  const state = {
    tab: 'desk',
    from: loadStoredCode('jarvis-fx-base', 'USD'),
    to: loadStoredCode('jarvis-fx-quote', 'INR'),
    worldBase: loadStoredCode('jarvis-fx-base', 'USD'),
    days: 30,
    worldDays: 30,
    worldFilter: 'ALL',
    pins: loadStoredPins(),
    catalog: FALLBACK_CATALOG,
    worldData: null,
    worldCache: Object.create(null),
    historyData: null,
    historyFetchedAt: 0,
    hiddenCompare: new Set(),
    loadingWorld: false,
    loadingHistory: false,
    prefSaveTimer: null,
    exactTimer: null,
    exactSeq: 0,
  };

  const FX_WORLD_CACHE_KEY = 'jarvis-fx-world-v1';
  const FX_HIST_CACHE_KEY = 'jarvis-fx-hist-v1';

  function saveStoredWorldBoard(base, days, board) {
    try {
      const payload = JSON.stringify({ base, days, board, savedAt: Date.now() });
      if (payload.length < 220000) localStorage.setItem(FX_WORLD_CACHE_KEY, payload);
    } catch {}
  }

  function loadStoredWorldBoard(base, days) {
    try {
      const parsed = JSON.parse(localStorage.getItem(FX_WORLD_CACHE_KEY) || 'null');
      if (parsed && parsed.board && parsed.base === base && Number(parsed.days) === Number(days)) {
        return parsed.board;
      }
    } catch {}
    return null;
  }

  function pairHistoryKey(base, quote, days) {
    return `${base}:${quote}:${days}`;
  }

  function readStoredHistMap() {
    try {
      const parsed = JSON.parse(localStorage.getItem(FX_HIST_CACHE_KEY) || '{}');
      return parsed && typeof parsed === 'object' ? parsed : {};
    } catch {}
    return {};
  }

  function saveStoredHistory(base, quote, days, hist) {
    try {
      const map = readStoredHistMap();
      map[pairHistoryKey(base, quote, days)] = { hist, savedAt: Date.now() };
      const keys = Object.keys(map).sort((a, b) => (map[b]?.savedAt || 0) - (map[a]?.savedAt || 0));
      const trimmed = {};
      for (const k of keys.slice(0, 6)) trimmed[k] = map[k];
      localStorage.setItem(FX_HIST_CACHE_KEY, JSON.stringify(trimmed));
    } catch {}
  }

  function loadStoredHistory(base, quote, days) {
    const entry = readStoredHistMap()[pairHistoryKey(base, quote, days)];
    return entry?.hist || null;
  }

  function restoreStoredMarketFx() {
    const board = loadStoredWorldBoard(state.worldBase, state.worldDays);
    if (board) {
      state.worldCache[state.worldBase] = { board, days: state.worldDays, at: 0 };
      state.worldData = board;
      if (Array.isArray(board.currencies) && board.currencies.length) {
        state.catalog = board.currencies;
      }
      if ($('fx-board-age') && board.as_of) {
        $('fx-board-age').textContent = `Cached · As of ${board.as_of} · ${board.rates?.length || 0} currencies · Refreshing…`;
      }
    }
    const hist = loadStoredHistory(state.from, state.to, state.days);
    if (hist) {
      state.historyData = hist;
    }
  }

  function persistLocalPrefs() {
    try {
      localStorage.setItem('jarvis-fx-pins', JSON.stringify(state.pins));
      localStorage.setItem('jarvis-fx-base', state.from);
      localStorage.setItem('jarvis-fx-quote', state.to);
    } catch {}
    clearTimeout(state.prefSaveTimer);
    state.prefSaveTimer = setTimeout(async () => {
      if (!JarvisApp.state?.sheets_connected || !JarvisApp.prefs) return;
      try {
        JarvisApp.prefs = await call('settings/save', {
          ...JarvisApp.prefs,
          fx_base: state.from,
          fx_quote: state.to,
          fx_watchlist: state.pins.slice(0, 32),
        });
      } catch {}
    }, 900);
  }

  function applyPrefs(cfg) {
    if (!cfg) return;
    if (Array.isArray(cfg.fx_watchlist) && cfg.fx_watchlist.length) {
      const valid = cfg.fx_watchlist.filter((c) => /^[A-Z]{3}$/.test(c));
      if (valid.length) state.pins = valid;
    }
    if (cfg.fx_base && /^[A-Z]{3}$/.test(cfg.fx_base)) {
      state.from = cfg.fx_base;
      state.worldBase = cfg.fx_base;
    }
    if (cfg.fx_quote && /^[A-Z]{3}$/.test(cfg.fx_quote)) {
      state.to = cfg.fx_quote;
    }
    populateSelects();
    renderPinnedStrip();
    syncConverterControls();
  }

  function fmtRate(n) {
    if (typeof n !== 'number' || !Number.isFinite(n) || n <= 0) return '—';
    if (n >= 1000) return n.toLocaleString(undefined, { maximumFractionDigits: 2 });
    if (n >= 10) return n.toFixed(4);
    if (n >= 0.1) return n.toFixed(4);
    return n.toFixed(6);
  }

  function fmtMoney(n, code) {
    if (typeof n !== 'number' || !Number.isFinite(n)) return '—';
    const digits = (code === 'JPY' || code === 'KRW' || code === 'VND' || code === 'IDR')
      ? 0
      : (['XAU', 'XAG', 'XPT', 'XPD'].includes(code) ? 4 : (n < 1 && n > 0 ? 4 : 2));
    return n.toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits });
  }

  function ltrPrefix(info, code) {
    const s = info?.symbol || '';
    if (!s || s === code || /[\u0600-\u06FF\u0590-\u05FF]/.test(s)) return '';
    return s;
  }

  function fmtDelta(v) {
    if (typeof v !== 'number' || !Number.isFinite(v)) return '—';
    return (v >= 0 ? '+' : '') + v.toFixed(2) + '%';
  }

  function catalogMap() {
    const m = new Map();
    for (const c of state.catalog) m.set(c.code, c);
    return m;
  }

  function populateSelects() {
    const pinsSet = new Set(state.pins);
    const orderedPinned = state.pins
      .map((code) => catalogMap().get(code) || { code, name: code, symbol: code, type: 'currency' });
    const otherFiat = state.catalog
      .filter((c) => c.type === 'currency' && !pinsSet.has(c.code))
      .sort((a, b) => a.code.localeCompare(b.code));
    const metals = state.catalog
      .filter((c) => c.type === 'metal' && !pinsSet.has(c.code))
      .sort((a, b) => a.code.localeCompare(b.code));

    function fillSelect(sel, selectedCode) {
      if (!sel) return;
      sel.replaceChildren();
      const addGroup = (label, items) => {
        if (!items.length) return;
        const g = document.createElement('optgroup');
        g.label = label;
        for (const item of items) {
          const opt = document.createElement('option');
          opt.value = item.code;
          opt.textContent = `${item.code} — ${item.name}${item.symbol && item.symbol !== item.code ? ' (' + item.symbol + ')' : ''}`;
          g.append(opt);
        }
        sel.append(g);
      };
      addGroup('★ Default & Pinned Currencies', orderedPinned);
      addGroup('All World Currencies (A–Z)', otherFiat);
      addGroup('Precious Metals', metals);
      if ([...sel.options].some((o) => o.value === selectedCode)) {
        sel.value = selectedCode;
      }
    }

    fillSelect($('conv-from'), state.from);
    fillSelect($('conv-to'), state.to);
    fillSelect($('world-base'), state.worldBase);

    const picker = $('fx-pin-picker');
    if (picker) {
      picker.replaceChildren();
      const unpinned = state.catalog.filter((c) => !pinsSet.has(c.code));
      const list = unpinned.length ? unpinned : state.catalog;
      for (const item of list) {
        const opt = document.createElement('option');
        opt.value = item.code;
        opt.textContent = `${item.code} — ${item.name}`;
        picker.append(opt);
      }
    }

    const dl = $('world-currency-datalist');
    if (dl) {
      dl.replaceChildren();
      for (const item of [...orderedPinned, ...otherFiat, ...metals]) {
        const opt = document.createElement('option');
        opt.value = item.code;
        opt.label = `${item.code} · ${item.name}`;
        dl.append(opt);
      }
    }
  }

  function svgEl(tag, attrs = {}) {
    const el = document.createElementNS('http://www.w3.org/2000/svg', tag);
    for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, String(v));
    return el;
  }

  function buildSmoothSvgPath(points, tension = 0.2) {
    if (!points || !points.length) return '';
    if (points.length === 1) return `M ${points[0].x.toFixed(1)},${points[0].y.toFixed(1)}`;
    if (points.length === 2) {
      return `M ${points[0].x.toFixed(1)},${points[0].y.toFixed(1)} L ${points[1].x.toFixed(1)},${points[1].y.toFixed(1)}`;
    }
    let d = `M ${points[0].x.toFixed(1)},${points[0].y.toFixed(1)}`;
    for (let i = 0; i < points.length - 1; i++) {
      const p0 = points[i === 0 ? 0 : i - 1];
      const p1 = points[i];
      const p2 = points[i + 1];
      const p3 = points[i + 2 < points.length ? i + 2 : i + 1];

      const cp1x = p1.x + (p2.x - p0.x) * tension;
      const cp1y = p1.y + (p2.y - p0.y) * tension;
      const cp2x = p2.x - (p3.x - p1.x) * tension;
      const cp2y = p2.y - (p3.y - p1.y) * tension;

      d += ` C ${cp1x.toFixed(1)},${cp1y.toFixed(1)} ${cp2x.toFixed(1)},${cp2y.toFixed(1)} ${p2.x.toFixed(1)},${p2.y.toFixed(1)}`;
    }
    return d;
  }

  function buildSparkline(points, up, cls = 'spark-svg') {
    const svg = svgEl('svg', { viewBox: '0 0 100 28', class: cls, 'aria-hidden': 'true' });
    if (!points || points.length < 2) {
      svg.append(svgEl('line', { x1: 2, y1: 14, x2: 98, y2: 14, stroke: '#94a3b8', 'stroke-width': 1.4, 'stroke-dasharray': '3 3' }));
      return svg;
    }
    const vals = points.map((p) => p.rate);
    let lo = Math.min(...vals);
    let hi = Math.max(...vals);
    if (hi === lo) { lo -= 0.001; hi += 0.001; }
    const sparkCoords = vals.map((v, i) => ({
      x: (i / (vals.length - 1)) * 96 + 2,
      y: 24 - ((v - lo) / (hi - lo)) * 20,
    }));
    const stroke = up === null ? '#0ea5e9' : (up ? '#10b981' : '#f43f5e');
    svg.append(svgEl('path', {
      d: buildSmoothSvgPath(sparkCoords, 0.18),
      fill: 'none',
      stroke,
      'stroke-width': 2,
      'stroke-linecap': 'round',
      'stroke-linejoin': 'round',
    }));
    return svg;
  }

  function activeRateMap(baseCode = state.worldBase) {
    const board = state.worldCache[baseCode]?.board || (state.worldData?.base === baseCode ? state.worldData : null);
    if (!board?.rates) return null;
    const map = new Map();
    map.set(board.base, { code: board.base, rate: 1, as_of: board.as_of, name: catalogMap().get(board.base)?.name || board.base });
    for (const r of board.rates) map.set(r.code, r);
    return { base: board.base, as_of: board.as_of, map };
  }

  function crossRate(fromCode, toCode) {
    if (fromCode === toCode) return { rate: 1, as_of: state.worldData?.as_of || '' };
    const fromBoard = activeRateMap(fromCode);
    if (fromBoard && fromBoard.map.has(toCode)) {
      const row = fromBoard.map.get(toCode);
      return { rate: row.rate, as_of: row.as_of };
    }
    const cur = activeRateMap(state.worldBase) || activeRateMap('USD');
    if (cur && cur.map.has(fromCode) && cur.map.has(toCode)) {
      const rf = cur.map.get(fromCode).rate;
      const rt = cur.map.get(toCode).rate;
      if (rf > 0 && rt > 0) return { rate: rt / rf, as_of: cur.as_of };
    }
    return null;
  }

  function syncConverterControls() {
    if ($('conv-from') && [...$('conv-from').options].some((o) => o.value === state.from)) $('conv-from').value = state.from;
    if ($('conv-to') && [...$('conv-to').options].some((o) => o.value === state.to)) $('conv-to').value = state.to;
    document.querySelectorAll('.fx-preset-row .chip').forEach((btn) => {
      btn.classList.toggle('active', btn.dataset.pair === `${state.from}:${state.to}`);
    });
    const pinBtn = $('conv-pin-btn');
    if (pinBtn) {
      const isPinned = state.pins.includes(state.to);
      pinBtn.textContent = isPinned ? `Pinned (${state.to})` : `Pin ${state.to}`;
    }
    updateConversion();
  }

  function renderPinnedStrip() {
    const grid = $('fx-top-cards');
    if (!grid) return;
    const base = state.worldBase || 'USD';
    const label = $('pinned-base-label');
    if (label) label.textContent = base;
    grid.replaceChildren();

    const board = state.worldCache[base]?.board || state.worldData;
    const rateByCode = new Map();
    if (board?.rates && board.base === base) {
      for (const r of board.rates) rateByCode.set(r.code, r);
    }

    const targets = state.pins.filter((c) => c !== base).slice(0, 5);
    if (!targets.length) return;
    for (const code of targets) {
      const row = rateByCode.get(code);
      const info = catalogMap().get(code);
      const card = document.createElement('button');
      card.type = 'button';
      card.className = 'fx-top-card' + (state.from === base && state.to === code ? ' active' : '');
      card.onclick = () => {
        state.from = base;
        state.to = code;
        persistLocalPrefs();
        syncConverterControls();
        renderPinnedStrip();
        loadPairHistory();
      };

      const head = document.createElement('div');
      head.className = 'fx-top-card-head';
      const pair = document.createElement('span');
      pair.className = 'fx-pair-code';
      pair.textContent = `${base} → ${code}`;
      const delta = document.createElement('span');
      const dVal = row?.change_period_pct ?? row?.change_1d_pct ?? null;
      delta.className = 'delta-pill' + (dVal === null ? '' : (dVal >= 0 ? ' up' : ' down'));
      delta.textContent = dVal === null ? '—' : fmtDelta(dVal);
      head.append(pair, delta);

      const rateEl = document.createElement('strong');
      rateEl.className = 'fx-top-card-rate';
      const pfx = ltrPrefix(info, code);
      rateEl.textContent = row ? `${pfx}${fmtRate(row.rate)}` : '—';

      const spark = buildSparkline(row?.sparkline || [], dVal === null ? null : dVal >= 0, 'fx-top-spark');

      const foot = document.createElement('div');
      foot.className = 'fx-top-card-foot';
      const name = document.createElement('span');
      name.className = 'fx-top-name';
      name.textContent = info?.name || code;
      foot.append(name);

      card.append(head, rateEl, spark, foot);
      grid.append(card);
    }
  }

  function updateConversion(verifyRemote = false) {
    const amount = Number($('conv-amount')?.value || 0);
    const out = $('conv-output');
    const unitEl = $('conv-unit-rate');
    const srcEl = $('conv-source-badge');
    const basket = $('conv-basket');
    if (!out) return;

    document.querySelectorAll('.amt-chip').forEach((btn) => {
      btn.classList.toggle('active', Number(btn.dataset.amt) === amount);
    });

    if (!(amount > 0) || !Number.isFinite(amount)) {
      out.textContent = 'Enter a positive amount';
      if (unitEl) unitEl.textContent = '—';
      return;
    }

    const cross = crossRate(state.from, state.to);
    const toInfo = catalogMap().get(state.to);
    const pfx = ltrPrefix(toInfo, state.to);
    const sym = pfx ? `${pfx} ` : '';
    if (cross) {
      const converted = amount * cross.rate;
      out.textContent = `${fmtMoney(amount, state.from)} ${state.from} = ${sym}${fmtMoney(converted, state.to)} ${state.to}`;
      if (unitEl) unitEl.textContent = `1 ${state.from} = ${fmtRate(cross.rate)} ${state.to}  ·  1 ${state.to} = ${fmtRate(1 / cross.rate)} ${state.from}`;
      if (srcEl) srcEl.textContent = cross.as_of ? `Frankfurter reference · ${cross.as_of}` : 'Live reference calculation';
    } else if (!verifyRemote) {
      out.textContent = 'Loading rate…';
      verifyRemote = true;
    }

    if (basket) {
      basket.replaceChildren();
      const targets = state.pins.filter((c) => c !== state.from).slice(0, 8);
      for (const code of targets) {
        const cRate = crossRate(state.from, code);
        const item = document.createElement('button');
        item.type = 'button';
        item.className = 'conv-basket-item';
        item.onclick = () => {
          state.to = code;
          persistLocalPrefs();
          syncConverterControls();
          loadPairHistory();
        };
        const left = document.createElement('span');
        left.textContent = code;
        const right = document.createElement('strong');
        const info = catalogMap().get(code);
        const prefix = ltrPrefix(info, code);
        right.textContent = cRate ? `${prefix}${fmtMoney(amount * cRate.rate, code)}` : '—';
        item.append(left, right);
        basket.append(item);
      }
    }

    if (verifyRemote) {
      clearTimeout(state.exactTimer);
      const seq = ++state.exactSeq;
      state.exactTimer = setTimeout(async () => {
        try {
          const res = await call('fx/convert', { amount: String(amount), base: state.from, quote: state.to });
          if (seq !== state.exactSeq) return;
          const convNum = Number(res.converted);
          out.textContent = `${fmtMoney(amount, state.from)} ${res.base} = ${sym}${fmtMoney(convNum, res.quote)} ${res.quote}`;
          const r = Number(res.rate);
          if (unitEl && r > 0) {
            unitEl.textContent = `1 ${res.base} = ${fmtRate(r)} ${res.quote}  ·  1 ${res.quote} = ${fmtRate(1 / r)} ${res.base}`;
          }
          if (srcEl) srcEl.textContent = res.as_of ? `${res.source} reference · ${res.as_of}` : res.source;
        } catch {}
      }, 220);
    }
  }

  function renderPairHistoryChart(data) {
    const box = $('pair-chart-box');
    const stats = $('pair-chart-stats');
    const hover = $('pair-chart-hover');
    const title = $('chart-pair-title');
    if (title) title.textContent = `${state.from} → ${state.to} (${state.days}D)`;
    if (!box) return;
    box.replaceChildren();

    const pts = data?.points || [];
    if (!pts.length) {
      noData(box, 'Loading pair trend history…');
      if (stats) stats.replaceChildren();
      return;
    }

    const W = 560;
    const H = 220;
    const PAD = { t: 18, r: 16, b: 28, l: 54 };
    const iw = W - PAD.l - PAD.r;
    const ih = H - PAD.t - PAD.b;
    const allVals = pts.flatMap((p) => [p.rate, p.ma7]);
    let lo = Math.min(...allVals);
    let hi = Math.max(...allVals);
    if (hi === lo) { lo *= 0.995; hi *= 1.005; }
    const padSpan = (hi - lo) * 0.1;
    lo -= padSpan;
    hi += padSpan;

    const xFor = (i) => pts.length === 1 ? PAD.l + iw / 2 : PAD.l + (i / (pts.length - 1)) * iw;
    const yFor = (v) => PAD.t + ih - ((v - lo) / (hi - lo)) * ih;

    const gradId = `pairGrad-${data.base}-${data.quote}-${state.days}`;
    const svg = svgEl('svg', { viewBox: `0 0 ${W} ${H}`, class: 'pair-chart-svg', role: 'img', 'aria-label': `${data.base} to ${data.quote} rate trend` });
    const defs = svgEl('defs');
    const grad = svgEl('linearGradient', { id: gradId, x1: '0', y1: '0', x2: '0', y2: '1' });
    grad.append(svgEl('stop', { offset: '0%', 'stop-color': '#0ea5e9', 'stop-opacity': '0.32' }));
    grad.append(svgEl('stop', { offset: '100%', 'stop-color': '#0ea5e9', 'stop-opacity': '0.01' }));
    defs.append(grad);
    svg.append(defs);

    // Grid & Y labels
    for (let step = 0; step <= 3; step++) {
      const ratio = step / 3;
      const y = PAD.t + ratio * ih;
      const val = hi - ratio * (hi - lo);
      svg.append(svgEl('line', { x1: PAD.l, y1: y.toFixed(1), x2: W - PAD.r, y2: y.toFixed(1), stroke: 'rgba(148,163,184,0.22)', 'stroke-width': 1 }));
      const lbl = svgEl('text', { x: PAD.l - 8, y: (y + 3).toFixed(1), 'text-anchor': 'end', fill: '#64748b', 'font-size': 10, 'font-family': 'monospace' });
      lbl.textContent = fmtRate(val);
      svg.append(lbl);
    }

    const dataCoords = pts.map((p, i) => ({ x: xFor(i), y: yFor(p.rate) }));
    const maCoords = pts.map((p, i) => ({ x: xFor(i), y: yFor(p.ma7) }));

    const smoothLineD = buildSmoothSvgPath(dataCoords, 0.18);
    const smoothMaD = buildSmoothSvgPath(maCoords, 0.18);
    const areaD = `${smoothLineD} L ${xFor(pts.length - 1).toFixed(1)},${(PAD.t + ih).toFixed(1)} L ${xFor(0).toFixed(1)},${(PAD.t + ih).toFixed(1)} Z`;

    svg.append(svgEl('path', { d: areaD, fill: `url(#${gradId})`, class: 'chart-animated-area' }));
    svg.append(svgEl('path', { d: smoothMaD, fill: 'none', stroke: '#8b5cf6', 'stroke-width': 1.8, 'stroke-dasharray': '4 3', opacity: '0.85', class: 'chart-animated-line' }));
    svg.append(svgEl('path', { d: smoothLineD, fill: 'none', stroke: '#0ea5e9', 'stroke-width': 2.6, 'stroke-linecap': 'round', 'stroke-linejoin': 'round', class: 'chart-animated-line' }));

    // X axis labels
    const ticks = pts.length > 2 ? [0, Math.floor((pts.length - 1) / 2), pts.length - 1] : [0];
    for (const idx of ticks) {
      const t = svgEl('text', { x: xFor(idx).toFixed(1), y: H - 6, 'text-anchor': idx === 0 ? 'start' : (idx === pts.length - 1 ? 'end' : 'middle'), fill: '#64748b', 'font-size': 10, 'font-family': 'monospace' });
      t.textContent = pts[idx].date;
      svg.append(t);
    }

    // Hover dots
    pts.forEach((p, i) => {
      const dot = svgEl('circle', { cx: xFor(i).toFixed(1), cy: yFor(p.rate).toFixed(1), r: 6, fill: 'transparent', stroke: 'none' });
      dot.addEventListener('mouseenter', () => {
        if (hover) hover.textContent = `${p.date} · 1 ${data.base} = ${fmtRate(p.rate)} ${data.quote} (7D MA: ${fmtRate(p.ma7)})`;
      });
      svg.append(dot);
    });

    const lastPt = pts[pts.length - 1];
    svg.append(svgEl('circle', { cx: xFor(pts.length - 1).toFixed(1), cy: yFor(lastPt.rate).toFixed(1), r: 4, fill: '#0ea5e9', stroke: '#ffffff', 'stroke-width': 2 }));
    box.append(svg);

    if (hover) hover.textContent = `Latest (${data.as_of}): 1 ${data.base} = ${fmtRate(data.latest_rate)} ${data.quote} · Violet dashed line: 7D Moving Avg`;

    if (stats) {
      stats.replaceChildren();
      const cells = [
        ['Latest spot', fmtRate(data.latest_rate)],
        [`${data.days}D change`, fmtDelta(data.change_pct)],
        ['Period low / high', `${fmtRate(data.low)} – ${fmtRate(data.high)}`],
        ['Period average', fmtRate(data.mean)],
      ];
      for (const [k, v] of cells) {
        const cell = document.createElement('div');
        cell.className = 'pair-stat-cell';
        const s = document.createElement('small');
        s.textContent = k;
        const st = document.createElement('strong');
        st.textContent = v;
        cell.append(s, st);
        stats.append(cell);
      }
    }
  }

  function renderWorldCompareChart() {
    const box = $('world-compare-chart');
    const legend = $('world-compare-legend');
    const title = $('world-compare-title');
    if (title) title.textContent = `Relative Performance vs ${state.worldBase} (${state.worldDays}D Normalized %)`;
    if (!box || !legend) return;
    box.replaceChildren();
    legend.replaceChildren();

    const seriesList = state.worldData?.comparison || [];
    if (!seriesList.length) {
      noData(box, 'Select a base currency with historical data to compare multi-currency movement.');
      return;
    }

    const visible = seriesList.filter((s) => !state.hiddenCompare.has(s.code));
    seriesList.forEach((s, idx) => {
      const color = SERIES_COLORS[idx % SERIES_COLORS.length];
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'world-legend-item';
      btn.style.opacity = state.hiddenCompare.has(s.code) ? '0.45' : '1';
      const dot = document.createElement('span');
      dot.className = 'world-legend-dot';
      dot.style.background = color;
      const last = s.points[s.points.length - 1];
      btn.append(dot, document.createTextNode(`${s.code} ${last ? fmtDelta(last.pct) : ''}`));
      btn.onclick = () => {
        if (state.hiddenCompare.has(s.code)) state.hiddenCompare.delete(s.code);
        else state.hiddenCompare.add(s.code);
        renderWorldCompareChart();
      };
      legend.append(btn);
    });

    if (!visible.length) {
      noData(box, 'Toggle at least one currency above to inspect comparative trends.');
      return;
    }

    const W = 940;
    const H = 210;
    const PAD = { t: 16, r: 22, b: 26, l: 52 };
    const iw = W - PAD.l - PAD.r;
    const ih = H - PAD.t - PAD.b;
    const allPcts = visible.flatMap((s) => s.points.map((p) => p.pct));
    let lo = Math.min(0, ...allPcts);
    let hi = Math.max(0, ...allPcts);
    if (hi === lo) { lo = -1; hi = 1; }
    const pad = (hi - lo) * 0.12;
    lo -= pad;
    hi += pad;

    const svg = svgEl('svg', { viewBox: `0 0 ${W} ${H}`, class: 'pair-chart-svg', role: 'img', 'aria-label': 'Multi-currency normalized percentage comparison' });
    const yFor = (v) => PAD.t + ih - ((v - lo) / (hi - lo)) * ih;

    // 0% zero baseline & bounds
    for (const v of [lo, 0, hi]) {
      const y = yFor(v);
      svg.append(svgEl('line', {
        x1: PAD.l,
        y1: y.toFixed(1),
        x2: W - PAD.r,
        y2: y.toFixed(1),
        stroke: v === 0 ? 'rgba(148,163,184,0.5)' : 'rgba(148,163,184,0.2)',
        'stroke-dasharray': v === 0 ? '4 3' : 'none',
        'stroke-width': 1,
      }));
      const lbl = svgEl('text', { x: PAD.l - 8, y: (y + 3).toFixed(1), 'text-anchor': 'end', fill: '#64748b', 'font-size': 10, 'font-family': 'monospace' });
      lbl.textContent = (v >= 0 ? '+' : '') + v.toFixed(1) + '%';
      svg.append(lbl);
    }

    seriesList.forEach((s, idx) => {
      if (state.hiddenCompare.has(s.code) || s.points.length < 2) return;
      const color = SERIES_COLORS[idx % SERIES_COLORS.length];
      const seriesCoords = s.points.map((p, i) => ({
        x: PAD.l + (i / (s.points.length - 1)) * iw,
        y: yFor(p.pct),
      }));
      svg.append(svgEl('path', {
        d: buildSmoothSvgPath(seriesCoords, 0.18),
        fill: 'none',
        stroke: color,
        'stroke-width': 2.3,
        'stroke-linecap': 'round',
        'stroke-linejoin': 'round',
        class: 'chart-animated-line',
      }));
    });

    const samplePts = visible[0].points;
    if (samplePts.length >= 2) {
      for (const idx of [0, Math.floor((samplePts.length - 1) / 2), samplePts.length - 1]) {
        const x = PAD.l + (idx / (samplePts.length - 1)) * iw;
        const lbl = svgEl('text', { x: x.toFixed(1), y: H - 6, 'text-anchor': idx === 0 ? 'start' : (idx === samplePts.length - 1 ? 'end' : 'middle'), fill: '#64748b', 'font-size': 10, 'font-family': 'monospace' });
        lbl.textContent = samplePts[idx].date;
        svg.append(lbl);
      }
    }
    box.append(svg);
  }

  function renderWorldMatrix() {
    const wrap = $('world-matrix-wrap');
    if (!wrap) return;
    wrap.replaceChildren();
    const data = state.worldData;
    if (!data?.rates?.length) {
      noData(wrap, state.loadingWorld ? 'Loading 160+ world currencies…' : 'World exchange data is not loaded yet.');
      return;
    }

    const pinsSet = new Set(state.pins);
    const enriched = data.rates.map((r) => ({ ...r, pinned: pinsSet.has(r.code) }));
    const pinnedRank = new Map(state.pins.map((c, idx) => [c, idx]));
    enriched.sort((a, b) => {
      if (a.pinned !== b.pinned) return a.pinned ? -1 : 1;
      if (a.pinned) return (pinnedRank.get(a.code) ?? 999) - (pinnedRank.get(b.code) ?? 999);
      if (a.type !== b.type) return a.type === 'currency' ? -1 : 1;
      return a.code.localeCompare(b.code);
    });

    const countAll = $('world-count-all');
    const countPinned = $('world-count-pinned');
    if (countAll) countAll.textContent = enriched.length;
    if (countPinned) countPinned.textContent = enriched.filter((r) => r.pinned).length;

    const q = ($('world-search')?.value || '').trim().toLowerCase();
    const filtered = enriched.filter((r) => {
      if (state.worldFilter === 'PINNED' && !r.pinned) return false;
      if (state.worldFilter === 'GAINERS' && !(r.change_1d_pct > 0 || r.change_period_pct > 0)) return false;
      if (state.worldFilter === 'LOSERS' && !(r.change_1d_pct < 0 || r.change_period_pct < 0)) return false;
      if (state.worldFilter === 'METAL' && r.type !== 'metal') return false;
      if (!q) return true;
      return `${r.code} ${r.name} ${r.symbol}`.toLowerCase().includes(q);
    });

    const metaEl = $('world-matrix-meta');
    if (metaEl) metaEl.textContent = `Showing ${filtered.length} of ${enriched.length} vs ${data.base} · As of ${data.as_of}`;

    if (!filtered.length) {
      noData(wrap, 'No currencies match your filter or search query.');
      return;
    }

    const table = document.createElement('table');
    table.className = 'world-table';
    table.innerHTML = '<thead><tr><th>Pin</th><th>Currency</th><th>Rate vs base</th><th>Inverse</th><th>1D %</th><th>Period %</th><th>Range</th><th>Trend</th><th>Actions</th></tr></thead>';
    const tbody = document.createElement('tbody');

    for (const row of filtered) {
      const tr = document.createElement('tr');
      if (row.pinned) tr.className = 'pinned-row';

      // Pin button
      const tdPin = document.createElement('td');
      const star = document.createElement('button');
      star.type = 'button';
      star.className = 'pin-star-btn' + (row.pinned ? ' pinned' : '');
      const starIcon = svgEl('svg', {
        viewBox: '0 0 24 24',
        width: '14',
        height: '14',
        fill: row.pinned ? 'currentColor' : 'none',
        stroke: 'currentColor',
        'stroke-width': '1.8',
        'stroke-linejoin': 'round',
        'aria-hidden': 'true',
      });
      starIcon.append(svgEl('polygon', { points: '12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2' }));
      star.append(starIcon);
      star.title = row.pinned ? `Unpin ${row.code}` : `Pin ${row.code} to top`;
      star.setAttribute('aria-label', star.title);
      star.onclick = () => togglePin(row.code);
      tdPin.append(star);

      // Currency cell
      const tdCurr = document.createElement('td');
      const currCell = document.createElement('div');
      currCell.className = 'world-curr-cell';
      const badge = document.createElement('span');
      badge.className = 'world-curr-badge';
      badge.textContent = row.code;
      const currMeta = document.createElement('div');
      currMeta.className = 'world-curr-meta';
      const nameEl = document.createElement('strong');
      nameEl.textContent = row.name;
      const symEl = document.createElement('small');
      symEl.textContent = `${row.symbol}${row.type === 'metal' ? ' · Metal' : ''}`;
      currMeta.append(nameEl, symEl);
      currCell.append(badge, currMeta);
      tdCurr.append(currCell);

      // Spot rate
      const tdRate = document.createElement('td');
      tdRate.className = 'mono';
      tdRate.textContent = `1 ${data.base} = ${fmtRate(row.rate)}`;

      // Inverse
      const tdInv = document.createElement('td');
      tdInv.className = 'mono';
      tdInv.textContent = `${fmtRate(row.inverse_rate)} ${data.base}`;

      // 1D %
      const td1d = document.createElement('td');
      const d1 = document.createElement('span');
      d1.className = 'delta-pill' + (row.change_1d_pct == null ? '' : (row.change_1d_pct >= 0 ? ' up' : ' down'));
      d1.textContent = fmtDelta(row.change_1d_pct);
      td1d.append(d1);

      // Period %
      const tdPer = document.createElement('td');
      const dp = document.createElement('span');
      dp.className = 'delta-pill' + (row.change_period_pct == null ? '' : (row.change_period_pct >= 0 ? ' up' : ' down'));
      dp.textContent = fmtDelta(row.change_period_pct);
      tdPer.append(dp);

      // Range
      const tdRange = document.createElement('td');
      tdRange.className = 'mono';
      tdRange.textContent = (row.period_low && row.period_high) ? `${fmtRate(row.period_low)} – ${fmtRate(row.period_high)}` : '—';

      // Sparkline
      const tdSpark = document.createElement('td');
      tdSpark.append(buildSparkline(row.sparkline, row.change_period_pct == null ? null : row.change_period_pct >= 0));

      // Actions
      const tdAct = document.createElement('td');
      const actWrap = document.createElement('div');
      actWrap.className = 'world-actions';
      const convBtn = document.createElement('button');
      convBtn.type = 'button';
      convBtn.className = 'button secondary compact-btn';
      convBtn.textContent = 'Convert';
      convBtn.onclick = () => {
        state.from = data.base;
        state.to = row.code;
        persistLocalPrefs();
        setMarketTab('desk');
        syncConverterControls();
        loadPairHistory();
      };
      const chartBtn = document.createElement('button');
      chartBtn.type = 'button';
      chartBtn.className = 'text-button';
      chartBtn.textContent = 'Chart';
      chartBtn.onclick = () => {
        state.from = data.base;
        state.to = row.code;
        setMarketTab('desk');
        syncConverterControls();
        loadPairHistory();
      };
      actWrap.append(convBtn, chartBtn);
      tdAct.append(actWrap);

      tr.append(tdPin, tdCurr, tdRate, tdInv, td1d, tdPer, tdRange, tdSpark, tdAct);
      tbody.append(tr);
    }

    table.append(tbody);
    wrap.append(table);
  }

  function togglePin(code) {
    code = String(code || '').toUpperCase();
    if (!/^[A-Z]{3}$/.test(code)) return;
    if (state.pins.includes(code)) {
      if (state.pins.length > 1) state.pins = state.pins.filter((c) => c !== code);
    } else {
      state.pins = [code, ...state.pins].slice(0, 32);
    }
    persistLocalPrefs();
    populateSelects();
    renderPinnedStrip();
    syncConverterControls();
    renderWorldMatrix();
    loadWorldRates(true);
  }

  async function loadWorldRates(force = false, retryCount = 0) {
    const base = state.worldBase || 'USD';
    const days = state.worldDays || 30;
    const cached = state.worldCache[base];
    if (!force && cached && cached.days === days && Date.now() - cached.at < 60000) {
      state.worldData = cached.board;
      renderPinnedStrip();
      updateConversion();
      renderWorldCompareChart();
      renderWorldMatrix();
      return;
    }
    if (state.worldPromise && !force) return state.worldPromise;
    state.loadingWorld = true;
    const task = (async () => {
      try {
        const board = await call('fx/world', {
          base,
          symbols: state.pins.slice(0, 20),
          days,
          refresh: force,
        });
        state.worldCache[base] = { board, days, at: Date.now() };
        state.worldData = board;
        saveStoredWorldBoard(base, days, board);
        if (Array.isArray(board.currencies) && board.currencies.length) {
          state.catalog = board.currencies;
          populateSelects();
        }
        if ($('fx-board-age')) {
          $('fx-board-age').textContent = `Reference rates · As of ${board.as_of} · ${board.rates?.length || 0} currencies`;
        }
        renderPinnedStrip();
        syncConverterControls();
        renderWorldCompareChart();
        renderWorldMatrix();
      } catch (error) {
        if (retryCount < 1 && !state.worldData) {
          setTimeout(() => loadWorldRates(false, retryCount + 1), 1400);
          return;
        }
        if ($('fx-board-age')) {
          if (state.worldData?.as_of) {
            $('fx-board-age').textContent = `Cached · As of ${state.worldData.as_of} · Live refresh unavailable`;
          } else {
            $('fx-board-age').textContent = error.message || 'Rates unavailable';
          }
        }
        renderPinnedStrip();
        renderWorldMatrix();
      } finally {
        state.loadingWorld = false;
        state.worldPromise = null;
      }
    })();
    state.worldPromise = task;
    return task;
  }

  async function loadPairHistory(force = false) {
    const reqBase = state.from;
    const reqQuote = state.to;
    const reqDays = state.days;
    const cachedHist = !force ? loadStoredHistory(reqBase, reqQuote, reqDays) : null;
    if (cachedHist && (state.historyData?.base !== reqBase || state.historyData?.quote !== reqQuote || Number(state.historyData?.days || reqDays) !== Number(reqDays))) {
      state.historyData = cachedHist;
      renderPairHistoryChart(cachedHist);
    }
    state.loadingHistory = true;
    try {
      const hist = await call('fx/history', {
        base: reqBase,
        quote: reqQuote,
        days: reqDays,
        refresh: force,
      });
      const storedHist = { ...hist, days: reqDays };
      state.historyData = storedHist;
      state.historyFetchedAt = Date.now();
      saveStoredHistory(reqBase, reqQuote, reqDays, storedHist);
      renderPairHistoryChart(storedHist);
    } catch {
      const matchesCurrent = state.historyData && state.historyData.base === reqBase && state.historyData.quote === reqQuote && Number(state.historyData.days || reqDays) === Number(reqDays);
      if (!matchesCurrent) renderPairHistoryChart(cachedHist || null);
    } finally {
      state.loadingHistory = false;
    }
  }

  function setMarketTab(tab) {
    state.tab = tab === 'world' ? 'world' : 'desk';
    const isWorld = state.tab === 'world';
    const deskPanel = $('market-panel-desk');
    const worldPanel = $('market-panel-world');
    if (deskPanel) deskPanel.hidden = isWorld;
    if (worldPanel) worldPanel.hidden = !isWorld;
    document.querySelectorAll('.market-tab').forEach((btn) => {
      const active = btn.dataset.marketTab === state.tab;
      btn.classList.toggle('active', active);
      btn.setAttribute('aria-selected', String(active));
    });
    if (isWorld && !state.worldData) loadWorldRates(false);
    else if (isWorld) {
      renderWorldCompareChart();
      renderWorldMatrix();
    }
  }

  function wireControls() {
    document.querySelectorAll('.market-tab').forEach((btn) => {
      btn.onclick = () => setMarketTab(btn.dataset.marketTab);
    });
    $('conv-open-world')?.addEventListener('click', () => setMarketTab('world'));

    $('fx-pin-add')?.addEventListener('click', () => {
      const val = $('fx-pin-picker')?.value;
      if (val && !state.pins.includes(val)) {
        state.pins.push(val);
        persistLocalPrefs();
        populateSelects();
        loadWorldRates(true);
      }
    });

    document.querySelectorAll('.fx-preset-row .chip').forEach((btn) => {
      btn.onclick = () => {
        const [f, t] = (btn.dataset.pair || 'USD:INR').split(':');
        state.from = f;
        state.to = t;
        persistLocalPrefs();
        syncConverterControls();
        renderPinnedStrip();
        loadPairHistory();
      };
    });

    $('conv-amount')?.addEventListener('input', () => updateConversion());
    document.querySelectorAll('.amt-chip').forEach((btn) => {
      btn.onclick = () => {
        if ($('conv-amount')) $('conv-amount').value = btn.dataset.amt;
        updateConversion();
      };
    });
    $('conv-from')?.addEventListener('change', (e) => {
      state.from = e.target.value;
      persistLocalPrefs();
      syncConverterControls();
      loadPairHistory();
    });
    $('conv-to')?.addEventListener('change', (e) => {
      state.to = e.target.value;
      persistLocalPrefs();
      syncConverterControls();
      loadPairHistory();
    });
    $('conv-swap')?.addEventListener('click', () => {
      const prev = state.from;
      state.from = state.to;
      state.to = prev;
      persistLocalPrefs();
      syncConverterControls();
      loadPairHistory();
    });
    $('conv-pin-btn')?.addEventListener('click', () => togglePin(state.to));
    $('conv-copy-btn')?.addEventListener('click', async () => {
      const txt = $('conv-output')?.textContent;
      if (txt && navigator.clipboard) {
        try { await navigator.clipboard.writeText(txt); toast(`Copied ${txt}`); } catch {}
      }
    });

    document.querySelectorAll('#fx-range-pills .chip').forEach((btn) => {
      btn.onclick = () => {
        state.days = Number(btn.dataset.days) || 30;
        document.querySelectorAll('#fx-range-pills .chip').forEach((c) => c.classList.toggle('active', c === btn));
        loadPairHistory();
      };
    });

    $('world-base')?.addEventListener('change', (e) => {
      state.worldBase = e.target.value;
      document.querySelectorAll('[data-world-base]').forEach((c) => c.classList.toggle('active', c.dataset.worldBase === state.worldBase));
      loadWorldRates(false);
    });
    document.querySelectorAll('[data-world-base]').forEach((btn) => {
      btn.onclick = () => {
        state.worldBase = btn.dataset.worldBase;
        if ($('world-base')) $('world-base').value = state.worldBase;
        document.querySelectorAll('[data-world-base]').forEach((c) => c.classList.toggle('active', c === btn));
        loadWorldRates(false);
      };
    });
    document.querySelectorAll('#world-range-pills .chip').forEach((btn) => {
      btn.onclick = () => {
        state.worldDays = Number(btn.dataset.worldDays) || 30;
        document.querySelectorAll('#world-range-pills .chip').forEach((c) => c.classList.toggle('active', c === btn));
        loadWorldRates(true);
      };
    });
    document.querySelectorAll('[data-world-filter]').forEach((btn) => {
      btn.onclick = () => {
        state.worldFilter = btn.dataset.worldFilter;
        document.querySelectorAll('[data-world-filter]').forEach((c) => c.classList.toggle('active', c === btn));
        renderWorldMatrix();
      };
    });
    $('world-search')?.addEventListener('input', () => renderWorldMatrix());
    $('world-reset-pins')?.addEventListener('click', () => {
      state.pins = [...DEFAULT_PINNED];
      persistLocalPrefs();
      populateSelects();
      loadWorldRates(true);
    });
  }

  document.addEventListener('DOMContentLoaded', () => {
    restoreStoredMarketFx();
    populateSelects();
    wireControls();
    syncConverterControls();
    renderPinnedStrip();
    if (state.historyData) renderPairHistoryChart(state.historyData);
    if (state.worldData) {
      renderWorldCompareChart();
      renderWorldMatrix();
    }

    JarvisApp.handlers.viewChange.push((name) => {
      if (name === 'market') {
        const wCached = state.worldCache[state.worldBase || 'USD'];
        if (!wCached || Date.now() - wCached.at >= 60000) loadWorldRates(false);
        if (!state.historyFetchedAt || state.historyData?.base !== state.from || state.historyData?.quote !== state.to) {
          loadPairHistory(false);
        }
      }
    });

    function applyControl(act) {
      if (!act || typeof act !== 'object') return;
      if (act.market_tab === 'desk' || act.market_tab === 'world') setMarketTab(act.market_tab);
      let pairChanged = false;
      if (act.fx_base && /^[A-Z]{3}$/i.test(act.fx_base)) {
        state.from = String(act.fx_base).toUpperCase();
        state.worldBase = state.from;
        pairChanged = true;
      }
      if (act.fx_quote && /^[A-Z]{3}$/i.test(act.fx_quote)) {
        state.to = String(act.fx_quote).toUpperCase();
        pairChanged = true;
      }
      if (act.fx_amount !== undefined && $('conv-amount')) {
        const num = Number(act.fx_amount);
        if (Number.isFinite(num) && num > 0) $('conv-amount').value = String(num);
      }
      if (act.fx_days && [7, 30, 90, 180, 365].includes(Number(act.fx_days))) {
        state.days = Number(act.fx_days);
        document.querySelectorAll('#fx-range-pills .chip').forEach((c) => c.classList.toggle('active', Number(c.dataset.days) === state.days));
        pairChanged = true;
      }
      populateSelects();
      syncConverterControls();
      renderPinnedStrip();
      updateConversion(true);
      if (pairChanged) loadPairHistory(true);
    }

    JarvisApp.handlers.fxStudio = {
      setTab: setMarketTab,
      applyPrefs,
      applyControl,
      refreshAll: () => Promise.all([loadWorldRates(true), loadPairHistory(true)]),
    };

    // Background warmup of world board & default USD/INR chart (board includes full 160+ currency catalog)
    setTimeout(() => {
      const wCached = state.worldCache[state.worldBase || 'USD'];
      if ((!wCached || Date.now() - wCached.at >= 60000) && !state.loadingWorld) loadWorldRates(false);
      if (!state.historyFetchedAt && !state.loadingHistory) loadPairHistory(false);
    }, 850);
  });
})();
