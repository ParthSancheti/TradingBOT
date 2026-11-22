'use strict';

const $ = (id) => document.getElementById(id);

const JarvisApp = {
  agentName: 'Jarvis',
  state: null,
  records: [],
  insightData: null,
  marketData: null,
  alerts: [],
  prefs: null,
  busy: false,
  activeView: 'desk',
  setupFilled: false,
  connectionEditing: false,
  loadedAt: Object.create(null),
  inflight: Object.create(null),
  handlers: {
    viewChange: [],
    statusChange: [],
    dataUpdated: [],
    event: [],
    serviceState: [],
    brandingChange: [],
  },
  labels: {
    desk: 'Overview',
    activity: 'Messages',
    stock: 'Stock & demand',
    market: 'Market watch',
    alerts: 'Notifications',
    assistant: 'Ask Jarvis',
    setup: 'Workspace setup',
    connections: 'Connections',
    settings: 'Settings',
  },
};

function applyAgentBranding(rawName, syncWindow = true) {
  const clean = String(rawName || 'Jarvis').replace(/[<>{}\r\n\t]+/g, ' ').trim().slice(0, 40) || 'Jarvis';
  JarvisApp.agentName = clean;
  JarvisApp.labels.assistant = `Ask ${clean}`;
  document.title = `${clean} Trade`;
  const winTitle = document.querySelector('.win-title');
  if (winTitle) winTitle.textContent = `${clean} Trade`;
  if ($('brand-app-name')) $('brand-app-name').textContent = clean;
  if ($('nav-assistant-label')) $('nav-assistant-label').textContent = `Ask ${clean}`;
  if ($('dock-brand-title')) $('dock-brand-title').textContent = `${clean} Core`;
  if ($('core-brand-label')) $('core-brand-label').textContent = `${clean} Core`;
  if ($('studio-brand-title')) $('studio-brand-title').textContent = `${clean} voice`;
  if ($('dock-question')) $('dock-question').placeholder = `Ask or command ${clean}`;
  if ($('studio-question')) $('studio-question').placeholder = `Type a question or command for ${clean}`;
  if (JarvisApp.activeView === 'assistant') $('page-name').textContent = `Ask ${clean}`;
  const openBtnSpan = $('open-jarvis')?.querySelector('span');
  if (openBtnSpan && !$('open-jarvis')?.dataset.live?.includes('true')) {
    openBtnSpan.textContent = clean;
  }
  if (syncWindow && window.jarvis) {
    call('window/title', { agent_name: clean }).catch(() => {});
  }
  for (const fn of JarvisApp.handlers.brandingChange) {
    try { fn(clean); } catch {}
  }
}

function notice(message = '') {
  const el = $('notice');
  if (!el) return;
  el.textContent = message;
  el.hidden = !message;
}

let toastTimer = null;
function toast(message) {
  const el = $('toast');
  if (!el) return;
  clearTimeout(toastTimer);
  el.textContent = message;
  el.hidden = false;
  toastTimer = setTimeout(() => { el.hidden = true; }, 5200);
}

async function call(method, params = {}) {
  if (!window.jarvis) throw Error('Open Jarvis from the desktop launcher.');
  return await window.jarvis.call(method, params);
}

function coalesce(key, fn) {
  if (JarvisApp.inflight[key]) return JarvisApp.inflight[key];
  const promise = Promise.resolve().then(fn).finally(() => {
    delete JarvisApp.inflight[key];
  });
  JarvisApp.inflight[key] = promise;
  return promise;
}

async function action(button, fn) {
  if (button && button.getAttribute('aria-busy') === 'true') return;
  JarvisApp.busy = true;
  if (button) {
    button.disabled = true;
    button.setAttribute('aria-busy', 'true');
  }
  notice();
  try {
    return await fn();
  } catch (error) {
    notice(error.message || 'The action could not complete. Check your connections and try again.');
  } finally {
    if (button) {
      button.removeAttribute('aria-busy');
      button.disabled = false;
    }
    JarvisApp.busy = !!document.querySelector('button[aria-busy="true"]');
  }
}

function loadingCard(message) {
  const card = document.createElement('div');
  card.className = 'jarvis-loading';
  card.setAttribute('role', 'status');
  const badge = document.createElement('span');
  badge.className = 'jarvis-pulse';
  badge.setAttribute('aria-hidden', 'true');
  badge.textContent = 'J';
  const box = document.createElement('div');
  const title = document.createElement('strong');
  title.textContent = message;
  box.append(title, document.createElement('i'), document.createElement('i'));
  card.append(badge, box);
  return card;
}

function showResult(panel, message, kind = 'success') {
  if (!panel) return;
  panel.hidden = false;
  panel.dataset.kind = kind;
  panel.textContent = message;
}

function panelAction(button, panel, waiting, work) {
  if (panel?.dataset.kind === 'loading') return;
  return action(button, async () => {
    if (panel) {
      panel.hidden = false;
      panel.dataset.kind = 'loading';
      panel.replaceChildren(loadingCard(waiting));
    }
    try {
      showResult(panel, await work(), 'success');
    } catch (error) {
      showResult(panel, error.message || 'Action failed.', 'error');
    }
  });
}

function light(id, good) {
  const el = $(id);
  if (el) el.classList.toggle('good', !!good);
}

function skeletonList(node, count = 3) {
  if (!node) return;
  node.replaceChildren();
  const wrap = document.createElement('div');
  wrap.className = 'skeleton-stack';
  wrap.setAttribute('aria-hidden', 'true');
  for (let i = 0; i < count; i++) {
    const card = document.createElement('div');
    card.className = 'skeleton-card';
    for (const cls of ['w-sm', 'w-lg', 'w-md']) {
      const line = document.createElement('i');
      line.className = `skeleton-line ${cls}`;
      card.append(line);
    }
    wrap.append(card);
  }
  node.append(wrap);
}

function noData(node, message) {
  if (!node) return;
  node.replaceChildren();
  const p = document.createElement('p');
  p.className = 'chart-empty';
  p.textContent = message;
  node.append(p);
}

function renderAssistantText(container, text) {
  function inline(parent, value) {
    const pattern = /(\*\*[^*\n]+\*\*|`[^`\n]+`|\*[^*\n]+\*)/g;
    let offset = 0;
    for (const match of value.matchAll(pattern)) {
      parent.append(value.slice(offset, match.index));
      const token = match[0];
      const bold = token.startsWith('**');
      const node = document.createElement(bold ? 'strong' : token.startsWith('`') ? 'code' : 'em');
      node.textContent = token.slice(bold ? 2 : 1, bold ? -2 : -1);
      parent.append(node);
      offset = match.index + token.length;
    }
    parent.append(value.slice(offset));
  }
  container.replaceChildren();
  container.classList.add('assistant-format');
  let list = null;
  let code = null;
  for (const line of String(text).split(/\r?\n/)) {
    if (line.startsWith('```')) {
      if (code) code = null;
      else { code = document.createElement('pre'); container.append(code); }
      list = null;
      continue;
    }
    if (code) { code.append(line + '\n'); continue; }
    const item = /^(?:\s*)([-*]|\d+[.)])\s+(.+)$/.exec(line);
    if (item) {
      const tag = /^\d/.test(item[1]) ? 'OL' : 'UL';
      if (list?.tagName !== tag) { list = document.createElement(tag); container.append(list); }
      const li = document.createElement('li');
      inline(li, item[2]);
      list.append(li);
      continue;
    }
    list = null;
    if (!line.trim()) continue;
    const heading = /^#{1,6}\s+(.+)$/.exec(line);
    const node = document.createElement(heading ? 'h4' : 'p');
    inline(node, heading ? heading[1] : line);
    container.append(node);
  }
}

function setSplitDock(open) {
  const dock = $('jarvis-dock');
  const shell = $('app-shell');
  if (!dock || !shell) return;
  dock.hidden = !open;
  shell.classList.toggle('jarvis-split', !!open);
}

function setStudioOpen(open) {
  const studio = $('jarvis-studio');
  if (!studio) return;
  studio.hidden = !open;
  document.body.classList.toggle('studio-open', !!open);
  if (window.jarvis) {
    const activeTheme = document.documentElement.dataset.theme === 'dark' ? 'dark' : 'light';
    call('window/theme', { theme: activeTheme }).catch(() => {});
  }
  document.querySelectorAll('.sidebar-nav .nav').forEach((el) => {
    if (el.dataset.view === 'assistant') {
      el.classList.toggle('selected', !!open);
    } else {
      el.classList.toggle('selected', !open && el.dataset.view === JarvisApp.activeView);
    }
  });
}

function view(name) {
  if (name === 'assistant') {
    setSplitDock(false);
    setStudioOpen(true);
    for (const fn of JarvisApp.handlers.viewChange) fn('assistant');
    return;
  }
  if (!document.getElementById(`view-${name}`)) return;
  setStudioOpen(false);
  JarvisApp.activeView = name;
  document.querySelectorAll('.view').forEach((el) => {
    el.hidden = el.id !== `view-${name}`;
  });
  document.querySelectorAll('.sidebar-nav .nav').forEach((el) => {
    el.classList.toggle('selected', el.dataset.view === name);
  });
  $('page-name').textContent = JarvisApp.labels[name] || name;
  for (const fn of JarvisApp.handlers.viewChange) fn(name);
}

function applyTheme(theme, syncWindow = true) {
  const mode = theme === 'dark' ? 'dark' : 'light';
  document.documentElement.dataset.theme = mode;
  try { localStorage.setItem('jarvis-theme', mode); } catch {}
  const label = $('theme-label');
  if (label) label.textContent = mode === 'light' ? 'Light' : 'Dark';
  if (syncWindow && window.jarvis) {
    call('window/theme', { theme: mode }).catch(() => {});
  }
}

function toggleThemeWithRadialAnimation(event) {
  const next = document.documentElement.dataset.theme === 'light' ? 'dark' : 'light';
  if (!document.startViewTransition) {
    applyTheme(next, true);
    return;
  }
  const rect = $('theme-toggle')?.getBoundingClientRect();
  const x = event?.clientX || (rect ? rect.left + rect.width / 2 : window.innerWidth - 60);
  const y = event?.clientY || (rect ? rect.top + rect.height / 2 : 30);
  const endRadius = Math.hypot(
    Math.max(x, window.innerWidth - x),
    Math.max(y, window.innerHeight - y)
  );
  const transition = document.startViewTransition(() => {
    applyTheme(next, true);
  });
  transition.ready.then(() => {
    document.documentElement.animate(
      {
        clipPath: [
          `circle(0px at ${x}px ${y}px)`,
          `circle(${endRadius}px at ${x}px ${y}px)`
        ],
      },
      {
        duration: 550,
        easing: 'cubic-bezier(0.22, 1, 0.36, 1)',
        pseudoElement: '::view-transition-new(root)',
      }
    );
  });
}

document.addEventListener('DOMContentLoaded', () => {
  let saved = 'light';
  try {
    if (!localStorage.getItem('jarvis-theme-v2')) {
      localStorage.setItem('jarvis-theme', 'light');
      localStorage.setItem('jarvis-theme-v2', '1');
    }
    saved = localStorage.getItem('jarvis-theme') || 'light';
  } catch {}
  applyTheme(saved, true);
  $('theme-toggle')?.addEventListener('click', (e) => toggleThemeWithRadialAnimation(e));

  // Sidebar profile card click navigates to workspace setup
  $('sidebar-account-btn')?.addEventListener('click', () => view('setup'));
  $('settings-open-setup-btn')?.addEventListener('click', () => view('setup'));

  // Apple-grade smooth scroll header elevation
  const content = document.querySelector('.content');
  const topbar = document.querySelector('.topbar');
  if (content && topbar) {
    content.addEventListener('scroll', () => {
      if (content.scrollTop > 8) {
        topbar.classList.add('scrolled');
      } else {
        topbar.classList.remove('scrolled');
      }
    }, { passive: true });
  }

  // Universal Apple-grade tactile click spring & micro-ripple
  document.addEventListener('pointerdown', (e) => {
    const target = e.target.closest('button, .button, .nav, .chip, .icon-button, .text-button, .wa-contact-card, .demand-row, .market-tab, .fx-top-card, .currency-pill, .mode-toggle-btn');
    if (!target) return;
    target.classList.add('active-press');

    const rect = target.getBoundingClientRect();
    const ripple = document.createElement('span');
    ripple.className = 'click-ripple-pulse';
    const size = Math.max(rect.width, rect.height, 40);
    ripple.style.width = ripple.style.height = `${size}px`;
    ripple.style.left = `${e.clientX - rect.left - size / 2}px`;
    ripple.style.top = `${e.clientY - rect.top - size / 2}px`;
    target.append(ripple);
    setTimeout(() => ripple.remove(), 420);

    const onUp = () => {
      target.classList.remove('active-press');
      window.removeEventListener('pointerup', onUp);
      window.removeEventListener('pointercancel', onUp);
    };
    window.addEventListener('pointerup', onUp);
    window.addEventListener('pointercancel', onUp);
  }, { passive: true });
});

window.jarvis?.onServiceState((event) => {
  for (const fn of JarvisApp.handlers.serviceState) fn(event);
});
window.jarvis?.onEvent((packet) => {
  if (packet.event === 'data-updated') {
    for (const fn of JarvisApp.handlers.dataUpdated) fn(packet.data);
  }
  for (const fn of JarvisApp.handlers.event) fn(packet);
});
