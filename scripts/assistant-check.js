#!/usr/bin/env node
/* Headless-стенд страницы помощника (18.21).
 *
 * Зачем: site/assistant.html держит всю логику во встроенном скрипте, а
 * `node --check` видит только синтаксис. Обращение к функции, которой больше
 * нет (после переноса инструментов из старой страницы), или карточка действия,
 * которая не отправляет `confirmed`, видны лишь при выполнении. Браузера в
 * проекте нет, поэтому берём настоящий скрипт страницы, подставляем заглушку
 * DOM и сервер-заглушку и проверяем главный путь: фраза → мозг → ответ →
 * карточка действия → «Подтвердить» → маршрут каталога и журнал.
 *
 * Запуск: node scripts/assistant-check.js
 */
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ROOT = path.resolve(__dirname, '..');
const html = fs.readFileSync(path.join(ROOT, 'site', 'assistant.html'), 'utf8');
const scripts = [...html.matchAll(/<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/g)].map((m) => m[1]);
if (scripts.length !== 1 || scripts[0].indexOf('/api/assistant/chat') < 0) {
  console.error('НЕ НАЙДЕН встроенный скрипт помощника с /api/assistant/chat в site/assistant.html');
  process.exit(1);
}
const IDS = new Set([...html.matchAll(/\sid="([^"]+)"/g)].map((m) => m[1]));

let passed = 0;
let failed = 0;
function check(label, condition, detail) {
  if (condition) { passed += 1; console.log(`  ok  ${label}`); }
  else { failed += 1; console.log(`  FAIL ${label}${detail ? ' — ' + detail : ''}`); }
}

/* ------------------------------------------------------------ заглушка DOM */
function classList() {
  const set = new Set();
  return {
    add: (...n) => n.forEach((x) => set.add(x)), remove: (...n) => n.forEach((x) => set.delete(x)),
    toggle(name, on) { const want = on === undefined ? !set.has(name) : !!on; if (want) set.add(name); else set.delete(name); return want; },
    contains: (n) => set.has(n),
  };
}
const created = [];
function allHtml(el) {
  return el._html + el.children.map(allHtml).join('');
}
function mkEl(tag, id) {
  const el = {
    tagName: String(tag || 'div').toUpperCase(), id: id || '', children: [], handlers: {}, dataset: {}, style: {}, attrs: {},
    textContent: '', value: '', disabled: false, hidden: false, className: '', title: '', href: '', type: '',
    scrollTop: 0, scrollHeight: 100, files: [], classList: classList(), parent: null,
    _html: '',
    get innerHTML() { return this._html; },
    set innerHTML(v) { this._html = String(v); this.children = []; },
    get lastChild() { return this.children[this.children.length - 1] || { textContent: '' }; },
    get firstChild() { return this.children[0] || null; },
    get lastElementChild() { return this.children[this.children.length - 1] || null; },
    appendChild(child) { child.parent = this; this.children.push(child); return child; },
    insertAdjacentHTML(_where, markup) { this._html += String(markup); },
    remove() { if (this.parent) this.parent.children = this.parent.children.filter((c) => c !== this); },
    addEventListener(type, fn) { (this.handlers[type] = this.handlers[type] || []).push(fn); },
    removeEventListener(type, fn) { this.handlers[type] = (this.handlers[type] || []).filter((f) => f !== fn); },
    dispatch(type, event) { (this.handlers[type] || []).slice().forEach((fn) => fn(Object.assign({ preventDefault() {}, key: '' }, event || {}))); },
    click() { this.dispatch('click'); },
    focus() {}, setAttribute(n, v) { this.attrs[n] = v; }, getAttribute(n) { return this.attrs[n]; }, removeAttribute(n) { delete this.attrs[n]; },
    querySelector(sel) {
      // Кнопки карточек ищутся по разметке, которую вставил скрипт (в том числе
      // во вложенные узлы); остальные селекторы дают постоянный дочерний узел,
      // чтобы запись в `.bubble` не терялась между вызовами.
      if (/^\[data-/.test(sel)) {
        if (allHtml(this).indexOf(sel.replace(/[[\]]/g, '')) < 0) return null;
      }
      this._q = this._q || {};
      if (!this._q[sel]) {
        const child = mkEl('div');
        child.className = sel.replace(/^\./, '');
        this.appendChild(child);
        this._q[sel] = child;
      }
      return this._q[sel];
    },
    querySelectorAll(sel) {
      if (sel === '[data-run]') {
        const found = [...allHtml(this).matchAll(/data-run="(\w+)"/g)].map((m) => {
          const button = mkEl('button');
          button.dataset.run = m[1];
          return button;
        });
        this._runButtons = found;
        return found;
      }
      if (sel === '.acts') return [mkEl('div')];
      return [];
    },
  };
  created.push(el);
  return el;
}
const elements = {};
IDS.forEach((id) => { elements[id] = mkEl('div', id); });
const tabs = ['chat', 'shop', 'avito', 'tg', 'pc'].map((pane) => { const tab = mkEl('button'); tab.dataset.pane = pane; return tab; });
const panes = ['chat', 'shop', 'avito', 'tg', 'pc'].map((pane) => { const node = mkEl('section'); node.dataset.pane = pane; return node; });
const chips = ['Что сейчас печатается?', '2+2'].map((q) => { const chip = mkEl('button'); chip.dataset.q = q; return chip; });
const document = {
  hidden: false,
  documentElement: { dataset: {}, setAttribute() {} },
  getElementById: (id) => elements[id] || null,
  createElement: (tag) => mkEl(tag),
  querySelectorAll(sel) {
    if (sel.indexOf('.as-tab') >= 0) return tabs;
    if (sel === '.as-pane') return panes;
    if (sel.indexOf('.chip') >= 0) return chips;
    return [];
  },
  addEventListener() {}, removeEventListener() {},
};

/* ---------------------------------------------------------- сервер-заглушка */
const calls = [];
const BRAIN = {
  ok: true, kind: 'action', source: 'entity', reply: 'Поставить на паузу: Альфа. Подтвердите в карточке.',
  action: { id: 'printer_command', title: 'Команда станку', method: 'POST', path: '/api/printer/command', confirm: true, doc: '' },
  params: { printer_id: 'p1', command: 'pause' }, explain: 'Пауза на Альфе', warnings: [],
  steps: [{ kind: 'entity', title: 'Станок', detail: 'по имени «Альфа»' }],
  facts: [{ kind: 'станок', title: 'Альфа', text: 'печатает', ref: 'printer:p1' }],
  suggestions: ['А у второго?'], link: { title: 'Принтеры', href: '/#printers' }, ms: 12,
};
const ROUTES = {
  'GET /api/assistant/status': { available: true, model: 'qwen2.5:3b', reason: '', actions: [BRAIN.action], speech: { available: true } },
  'GET /api/assistant/agent': { available: true, window: 'Telegram', wake_word: false },
  'GET /api/assistant/skills': { available: true, count: 2, ready: 1, skills: [{ name: 'system.volume', title: 'Громкость', available: true, description: 'Звук' }] },
  'GET /api/assistant/journal': { ok: true, events: [{ title: 'Команда станку', at: '2026-09-24T10:00:00', data: { outcome: 'done' } }] },
  'GET /api/assistant/context': { ok: true, date: 'Сегодня четверг', farm: 'Печатают 1 из 2.', owner: 'Олег',
    printers: [{ id: 'p1', name: 'Альфа', state: 'RUNNING', progress: 40, task: 'Ваза' }], debts: { total: 2400, count: 1 } },
  'GET /api/assistant/memory': { ok: true, memories: [{ id: 1, text: 'Мария берёт только PETG', pinned: 0 }] },
  'GET /api/assistant/dialog': { ok: true, turns: [{ role: 'user', text: 'привет' }, { role: 'assistant', text: 'Привет, Олег!', meta: { source: 'farm' } }] },
  'POST /api/assistant/chat': BRAIN,
  'POST /api/printer/command': { ok: true, hint: 'Пауза отправлена' },
  'POST /api/assistant/journal': { ok: true },
  'POST /api/assistant/memory': { ok: true },
  'POST /api/assistant/dialog': { ok: true, cleared: 2 },
};
function fetchStub(url, opts) {
  const method = (opts && opts.method) || 'GET';
  const pathname = String(url).split('?')[0];
  let body = null;
  if (opts && typeof opts.body === 'string') { try { body = JSON.parse(opts.body); } catch (e) { body = opts.body; } }
  calls.push({ method, path: pathname, url: String(url), body });
  const payload = ROUTES[`${method} ${pathname}`];
  const text = JSON.stringify(payload || { ok: false, reason: 'нет заглушки ' + method + ' ' + pathname });
  return Promise.resolve({ ok: !!payload, status: payload ? 200 : 404, text: () => Promise.resolve(text) });
}

const errors = [];
const context = {
  document, window: null, console, JSON, Math, Date, Object, Array, String, Number, Promise, URLSearchParams, RegExp, Error,
  fetch: fetchStub, FormData: class { append() {} }, Blob: class {}, navigator: {},
  localStorage: { getItem: () => null, setItem() {} },
  setTimeout: (fn) => { fn(); return 0; }, clearTimeout() {}, setInterval: () => 0,
  matchMedia: () => ({ matches: true }),
  history: { replaceState() {} }, location: { hash: '' },
};
context.window = context;
context.window.matchMedia = context.matchMedia;
context.window.history = context.history;
context.window.location = context.location;
vm.createContext(context);

const flush = () => new Promise((resolve) => setImmediate(resolve));

(async () => {
  try {
    vm.runInContext(scripts[0], context, { filename: 'assistant.html' });
  } catch (error) {
    errors.push(error);
  }
  for (let i = 0; i < 6; i += 1) await flush();
  check('скрипт страницы выполняется без исключений', !errors.length, errors.map(String).join('; '));
  const first = calls.map((c) => `${c.method} ${c.path}`);
  ['GET /api/assistant/status', 'GET /api/assistant/context', 'GET /api/assistant/memory', 'GET /api/assistant/dialog']
    .forEach((call) => check(`при открытии идёт ${call}`, first.indexOf(call) >= 0, first.join(', ')));
  check('модель показана в шапке', elements.as_state.innerHTML.indexOf('qwen2.5:3b') >= 0, elements.as_state.innerHTML);
  check('микрофон включается, когда жив рантайм речи', elements.as_mic.disabled === false);
  check('сводка цеха отрисована', elements.ctx_body.innerHTML.indexOf('Альфа') >= 0, elements.ctx_body.innerHTML.slice(0, 120));
  check('история разговора восстановлена', elements.as_log.children.length >= 2, String(elements.as_log.children.length));
  check('память показана', elements.mem_list.children.length === 1, String(elements.mem_list.children.length));

  // Фраза → мозг.
  elements.as_text.value = 'поставь на паузу Альфу';
  elements.as_send.click();
  for (let i = 0; i < 6; i += 1) await flush();
  const chat = calls.find((c) => c.method === 'POST' && c.path === '/api/assistant/chat');
  check('фраза уходит в /api/assistant/chat', !!chat);
  check('в мозг уходят текст, сессия и источник', chat && chat.body.text === 'поставь на паузу Альфу'
    && chat.body.session === 'main' && chat.body.source === 'panel', chat && JSON.stringify(chat.body));
  check('интент-диспетчер страница больше не зовёт', !calls.some((c) => c.path === '/api/assistant/intent'));
  const answer = elements.as_log.children[elements.as_log.children.length - 1];
  const bubble = answer && answer.children.find((c) => c.className === 'bubble');
  const markup = bubble ? allHtml(bubble) : '';
  check('ответ мозга показан словами', markup.indexOf('Поставить на паузу: Альфа') >= 0, markup.slice(0, 160));
  check('след рассуждения «Как я понял»', markup.indexOf('Как я понял') >= 0 && markup.indexOf('по имени') >= 0);
  check('факты базы свёрнуты под ответом', markup.indexOf('Факты базы') >= 0);
  check('карточка действия с «Подтвердить»', markup.indexOf('data-run="confirm"') >= 0 && markup.indexOf('Подтвердить') >= 0);
  check('ссылка на раздел панели', bubble && bubble.children.some((c) => c.className === 'acts'));
  check('подсказки следующего шага', bubble && bubble.children.some((c) => c.className === 'chips'));

  // «Подтвердить» → маршрут каталога с confirmed и запись в журнал.
  const buttons = answer._runButtons || [];
  const confirm = buttons.find((b) => b.dataset.run === 'confirm');
  check('кнопка «Подтвердить» привязана', !!confirm && (confirm.handlers.click || []).length > 0);
  if (confirm) confirm.click();
  for (let i = 0; i < 6; i += 1) await flush();
  const command = calls.find((c) => c.method === 'POST' && c.path === '/api/printer/command');
  check('действие идёт в маршрут из каталога', !!command);
  check('confirmed=true ставит только кнопка человека', command && command.body.confirmed === true
    && command.body.printer_id === 'p1' && command.body.command === 'pause', command && JSON.stringify(command.body));
  const journal = calls.filter((c) => c.method === 'POST' && c.path === '/api/assistant/journal');
  check('после действия пишется журнал', journal.some((c) => c.body.action === 'printer_command' && c.body.outcome === 'done'));

  // Память и новый разговор.
  elements.mem_text.value = 'по пятницам не печатаем';
  elements.mem_add.click();
  for (let i = 0; i < 4; i += 1) await flush();
  check('запись памяти уходит op=remember', calls.some((c) => c.path === '/api/assistant/memory' && c.method === 'POST'
    && c.body.op === 'remember' && c.body.text === 'по пятницам не печатаем'));
  elements.chat_new.click();
  for (let i = 0; i < 4; i += 1) await flush();
  check('«Новый разговор» очищает сессию', calls.some((c) => c.path === '/api/assistant/dialog' && c.method === 'POST' && c.body.op === 'clear'));

  // Вкладки.
  tabs[4].click();
  check('вкладка «Компьютер» открывается', panes[4].classList.contains('on') && !panes[0].classList.contains('on'));

  console.log(`\nСтенд помощника: ${passed} ok, ${failed} fail`);
  process.exit(failed ? 1 : 0);
})();
