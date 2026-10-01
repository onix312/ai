/* PrintFlow 19 — integrated Nozza rail.
   P1 is intentionally read/chat-first. It uses the existing assistant contract
   but never executes a returned action itself. P2 moves reasoning ownership to
   Luma's PrintFlow provider and domain action registry. */
(() => {
'use strict';

const SESSION = 'printflow-v19';
const U = PF.ui;
const { $, esc, num, store } = U;
const { get, post } = PF.api;

let currentView = 'dashboard';
let assistantStatus = null;
let lumaStatus = null;
let contextState = null;
let uiEntity = {};
let uiFilters = {};
let busy = false;
let initialized = false;

const VIEW_LABELS = {
  dashboard: 'Сегодня',
  orders: 'Заказы',
  customers: 'Клиенты',
  printers: 'Принтеры',
  queue: 'Очередь',
  products: 'Товары',
  batches: 'Партии',
  documents: 'Документы',
  warehouses: 'Склады',
  shelf: 'Стеллаж',
  inventory: 'Материалы',
  finance: 'Финансы',
  calc: 'Pricing Studio',
  niches: 'Рост',
  print: 'Печать',
  conveyor: 'FarmLoop',
  ops10: 'Центр смены',
  clientbot: 'Входящие',
  settings: 'Настройки',
};

const PROMPTS = {
  dashboard: ['Что требует внимания?', 'Что печатать дальше?', 'Как идут продажи?'],
  orders: ['Какие заказы просрочены?', 'Что можно выдать?', 'Где нужен ответ клиенту?'],
  customers: ['Кто должен деньги?', 'Кто давно не заказывал?', 'Есть ли дубли клиентов?'],
  printers: ['Что сейчас печатается?', 'Есть ли проблемы у принтеров?', 'Что запустить следующим?'],
  queue: ['Почему такой порядок?', 'Что можно запустить сейчас?', 'Есть ли задания без принтера?'],
  shelf: ['Что надо пополнить?', 'Что залежалось?', 'Что можно принести со склада?'],
  inventory: ['Какого пластика мало?', 'Хватит ли материала на очередь?', 'Что купить?'],
  finance: ['Какая прибыль сейчас?', 'Кто должен деньги?', 'Что изменилось по расходам?'],
  products: ['Что надо допечатать?', 'Где заморожены деньги?', 'Какие товары прибыльнее?'],
};

function setOpen(open, focus = false) {
  document.body.classList.toggle('nozza-open', !!open);
  const rail = $('nozza_rail');
  const toggle = $('nozza_toggle');
  if (rail) rail.setAttribute('aria-hidden', open ? 'false' : 'true');
  if (toggle) toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
  store.set('pf_v19_nozza_open', open ? '1' : '0');
  if (open && focus) setTimeout(() => $('nozza_input') && $('nozza_input').focus(), 80);
}

function openRail(focus = true) { setOpen(true, focus); }
function closeRail() { setOpen(false); }

function statusLine() {
  const dot = $('nozza_status_dot');
  const text = $('nozza_status');
  if (!text) return;

  const modelOk = !!(assistantStatus && assistantStatus.available);
  const lumaOk = !!(lumaStatus && lumaStatus.available);
  if (dot) dot.className = modelOk || lumaOk ? 'ok' : '';

  if (lumaOk && modelOk) text.textContent = 'Luma core online · локальная модель готова';
  else if (lumaOk) text.textContent = 'Luma core online';
  else if (modelOk) text.textContent = 'Локальная модель готова · Luma недоступна';
  else {
    const why = (assistantStatus && assistantStatus.reason)
      || (lumaStatus && lumaStatus.reason)
      || 'локальный AI недоступен';
    text.textContent = String(why).slice(0, 86);
  }
}

function renderContext() {
  const host = $('nozza_context');
  if (!host) return;
  const chips = [
    { label: VIEW_LABELS[currentView] || currentView, kind: 'accent' },
  ];
  const c = contextState || {};
  if (c.queue != null) chips.push({ label: `Очередь: ${num(c.queue)}` });
  if (c.debts && num(c.debts.count)) chips.push({
    label: `Долги: ${num(c.debts.count)}`,
    kind: 'warn',
  });
  const active = (c.printers || []).filter((p) =>
    /RUN|PRINT|PAUSE|PREPARE/i.test(String(p.state || '')));
  if (active.length) chips.push({ label: `Печатают: ${active.length}`, kind: 'ok' });
  host.innerHTML = chips.map((c) =>
    `<span class="chip ${esc(c.kind || 'outline')}">${esc(c.label)}</span>`).join('');
}

function promptSet() {
  const prompts = PROMPTS[currentView] || [
    'Что здесь важно?',
    'Что требует внимания?',
    'Что можно сделать дальше?',
  ];
  const host = $('nozza_prompts');
  if (!host) return;
  host.innerHTML = prompts.map((p) =>
    `<button type="button" data-nozza-prompt="${esc(p)}">${esc(p)}</button>`).join('');
}

function scrollBottom() {
  const host = $('nozza_messages');
  if (host) requestAnimationFrame(() => { host.scrollTop = host.scrollHeight; });
}

function welcomeHtml() {
  return `<div class="nozza-welcome" id="nozza_welcome">
    <b>Nozza видит контекст PrintFlow</b>
    <span>Спросите про заказы, принтеры, очередь, клиентов, деньги или стеллаж. На этом этапе действия только предлагаются — физические и финансовые команды не выполняются из rail автоматически.</span>
  </div>`;
}

function renderTurns(turns) {
  const host = $('nozza_messages');
  if (!host) return;
  host.innerHTML = welcomeHtml();
  (turns || []).forEach((turn) => appendTurn(turn.role, turn.text, turn.meta, false));
  scrollBottom();
}

function appendTurn(role, text, meta = null, doScroll = true) {
  const host = $('nozza_messages');
  if (!host || !text) return;
  const safeRole = role === 'user' ? 'user' : 'assistant';
  const row = document.createElement('div');
  row.className = 'nozza-turn ' + safeRole;
  row.innerHTML = `<div class="nozza-bubble">${esc(String(text))}</div>`;
  host.appendChild(row);

  if (safeRole === 'assistant' && meta && meta.action) {
    appendProposal(
      typeof meta.action === 'object' ? meta.action : { id: meta.action },
      meta.params || {},
      meta.explain || '',
      false
    );
  }
  if (doScroll) scrollBottom();
}

function appendProposal(action, params, explain, doScroll = true) {
  if (!action) return;
  const host = $('nozza_messages');
  if (!host) return;
  const card = document.createElement('div');
  card.className = 'nozza-proposal';
  const title = action.title || action.id || 'Действие PrintFlow';
  const detail = explain || action.doc || 'Nozza подготовила действие.';
  const paramText = Object.entries(params || {})
    .filter(([, v]) => v !== '' && v != null)
    .slice(0, 5)
    .map(([k, v]) => `${k}: ${typeof v === 'object' ? '…' : String(v)}`)
    .join(' · ');
  card.innerHTML =
    `<b>${esc(title)}</b><span>${esc(detail)}</span>`
    + (paramText ? `<small class="muted" style="display:block;margin-top:4px">${esc(paramText)}</small>` : '')
    + '<small class="muted" style="display:block;margin-top:6px">Выполнение будет подключено через v19 Action Registry с проверкой и подтверждением.</small>';
  host.appendChild(card);
  if (doScroll) scrollBottom();
}

function showThinking(show) {
  const host = $('nozza_messages');
  const orb = $('nozza_orb');
  if (!host) return;
  let el = $('nozza_thinking');
  if (show && !el) {
    el = document.createElement('div');
    el.id = 'nozza_thinking';
    el.className = 'nozza-thinking';
    el.setAttribute('aria-label', 'Nozza думает');
    el.innerHTML = '<i></i><i></i><i></i>';
    host.appendChild(el);
  } else if (!show && el) {
    el.remove();
  }
  if (orb) orb.classList.toggle('thinking', !!show);
  scrollBottom();
}

async function publishUiContext() {
  try {
    await post('/api/assistant/ui-context', {
      view: currentView,
      sub: '',
      entity: uiEntity,
      filters: uiFilters,
    });
  } catch (e) {
    /* context is helpful but never blocks the panel */
  }
}

function setScreenContext(entity = {}, filters = {}) {
  uiEntity = entity && typeof entity === 'object' ? { ...entity } : {};
  uiFilters = filters && typeof filters === 'object' ? { ...filters } : {};
  publishUiContext().catch(() => {});
  renderContext();
}

async function refreshState() {
  const tasks = await Promise.allSettled([
    get('/api/assistant/status'),
    get('/api/assistant/agent'),
    get('/api/assistant/context'),
  ]);
  if (tasks[0].status === 'fulfilled') assistantStatus = tasks[0].value;
  if (tasks[1].status === 'fulfilled') lumaStatus = tasks[1].value;
  if (tasks[2].status === 'fulfilled') contextState = tasks[2].value;
  statusLine();
  renderContext();
}

async function loadDialog() {
  if (lumaStatus && lumaStatus.available) {
    renderTurns([]);
    return;
  }
  try {
    const data = await get('/api/assistant/dialog', { session: SESSION, limit: 40 });
    renderTurns(data.turns || []);
  } catch (e) {
    renderTurns([]);
  }
}

function makeRequestId() {
  return 'pf19-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 8);
}

function appendPending(pending, skill = '') {
  if (!pending || !pending.id) return;
  const host = $('nozza_messages');
  if (!host) return;
  const card = document.createElement('div');
  card.className = 'nozza-proposal';
  card.dataset.pendingId = pending.id;
  const label = pending.text || skill || 'Действие Luma';
  card.innerHTML =
    `<b>Требуется подтверждение</b><span>${esc(label)}</span>`
    + '<div style="display:flex;gap:6px;margin-top:8px">'
    + `<button class="btn sm primary" type="button" data-nozza-confirm="${esc(pending.id)}">Подтвердить</button>`
    + `<button class="btn sm ghost" type="button" data-nozza-reject="${esc(pending.id)}">Отменить</button>`
    + '</div>';
  host.appendChild(card);
  scrollBottom();
}

async function confirmPending(id, confirmed) {
  const clean = String(id || '').trim();
  if (!clean) return;
  try {
    const result = await post('/api/assistant/luma/confirm', {
      id: clean,
      confirmed: !!confirmed,
    });
    const card = document.querySelector(`[data-pending-id="${CSS.escape(clean)}"]`);
    if (card) card.remove();
    const nested = result && result.result && typeof result.result === 'object'
      ? result.result : {};
    const detail = confirmed
      ? (result.done ? 'Действие выполнено.' : (result.reason || nested.reason || 'Действие не выполнено.'))
      : 'Действие отменено.';
    appendTurn('assistant', detail);
    refreshState().catch(() => {});
  } catch (e) {
    appendTurn('assistant', 'Подтверждение не прошло: ' + (e.message || e));
  }
}

async function lumaOrLegacy(clean) {
  if (lumaStatus && lumaStatus.available) {
    try {
      const response = await post('/api/assistant/luma', {
        text: clean,
        session: SESSION,
        source: 'printflow-v19-rail',
        request_id: makeRequestId(),
      });
      if (response && response.ok !== false) return { ...response, _brain: 'luma' };
    } catch (e) {
      /* legacy fallback below */
    }
  }
  const legacy = await post('/api/assistant/chat', {
    contract_version: 1,
    text: clean,
    session: SESSION,
    source: 'printflow-v19-rail',
    delegate: true,
    request_id: makeRequestId(),
  });
  return { ...legacy, _brain: 'legacy' };
}

async function send(text) {
  const clean = String(text || '').trim();
  if (!clean || busy) return;
  busy = true;
  const input = $('nozza_input');
  const sendBtn = $('nozza_send');
  if (input) input.value = '';
  if (sendBtn) sendBtn.disabled = true;
  appendTurn('user', clean);
  showThinking(true);

  try {
    await publishUiContext();
    const res = await lumaOrLegacy(clean);
    showThinking(false);
    appendTurn('assistant',
      res.reply || res.answer || res.reason || 'Нет ответа.',
      null);
    if (res.action) appendProposal(res.action, res.params || {}, res.explain || '');
    if (res.pending && res.pending.id) appendPending(res.pending, res.skill || '');
    if (Array.isArray(res.suggestions) && res.suggestions.length) {
      const host = $('nozza_prompts');
      if (host) {
        host.innerHTML = res.suggestions.slice(0, 4).map((p) =>
          `<button type="button" data-nozza-prompt="${esc(p)}">${esc(p)}</button>`).join('');
      }
    }
    refreshState().catch(() => {});
  } catch (e) {
    showThinking(false);
    appendTurn('assistant', 'Не удалось получить ответ: ' + (e.message || e));
  } finally {
    busy = false;
    if (sendBtn) sendBtn.disabled = false;
    if (input) input.focus();
  }
}

function bind() {
  const toggle = $('nozza_toggle');
  if (toggle) toggle.addEventListener('click', () => {
    const next = !document.body.classList.contains('nozza-open');
    setOpen(next, next);
  });
  const nav = $('nozza_nav_open');
  if (nav) nav.addEventListener('click', () => openRail());
  const close = $('nozza_close');
  if (close) close.addEventListener('click', closeRail);
  const mobile = $('mobile_nozza');
  if (mobile) mobile.addEventListener('click', () => openRail());

  const form = $('nozza_form');
  if (form) form.addEventListener('submit', (e) => {
    e.preventDefault();
    send($('nozza_input') ? $('nozza_input').value : '');
  });
  const input = $('nozza_input');
  if (input) input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      send(input.value);
    }
  });
  document.addEventListener('click', (e) => {
    const approve = e.target.closest('[data-nozza-confirm]');
    if (approve) {
      confirmPending(approve.dataset.nozzaConfirm, true);
      return;
    }
    const reject = e.target.closest('[data-nozza-reject]');
    if (reject) {
      confirmPending(reject.dataset.nozzaReject, false);
      return;
    }
    const btn = e.target.closest('[data-nozza-prompt]');
    if (!btn) return;
    const prompt = btn.dataset.nozzaPrompt || '';
    if (prompt) send(prompt);
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && document.body.classList.contains('nozza-open')) {
      const rail = $('nozza_rail');
      if (rail && rail.contains(document.activeElement)) {
        closeRail();
        if ($('nozza_toggle')) $('nozza_toggle').focus();
      }
    }
  });

  PF.on('view', (detail) => {
    currentView = (detail && detail.view) || currentView;
    uiEntity = {};
    uiFilters = {};
    renderContext();
    promptSet();
    publishUiContext().catch(() => {});
  });
}

async function init() {
  if (initialized) return;
  initialized = true;
  bind();
  currentView = (location.hash || '#dashboard').slice(1).split('/')[0] || 'dashboard';
  promptSet();
  renderContext();
  await publishUiContext();
  await refreshState();
  await loadDialog();
  const restore = store.get('pf_v19_nozza_open', '0') === '1';
  if (restore && window.innerWidth > 1100) setOpen(true, false);
}

PF.onReady(() => { init().catch(() => {}); });
PF.modules.nozzaRail = {
  open: openRail,
  close: closeRail,
  refresh: refreshState,
  send,
  setContext: setScreenContext,
};
})();
