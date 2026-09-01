/* PrintFlow 2.0 — ядро фронтенда.
   Источник правды — локальный коннектор (SQLite). Здесь: HTTP-клиент,
   общее состояние, роутер разделов, тосты, диалоги, графики и форматирование.
   Без сборщика и внешних зависимостей. */
(() => {
'use strict';

/* ============================================================ утилиты */
const $ = (id) => document.getElementById(id);
const $$ = (sel, root) => [...(root || document).querySelectorAll(sel)];
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const num = (v, d = 0) => { const n = parseFloat(String(v ?? '').replace(',', '.')); return Number.isFinite(n) ? n : d; };
const clamp = (v, a, b) => Math.max(a, Math.min(b, v));

const CUR = () => (PF.state.settings.currency || '₽');
const money = (v, frac = 0) => (Math.round(num(v) * 10 ** frac) / 10 ** frac)
  .toLocaleString('ru-RU', { minimumFractionDigits: frac, maximumFractionDigits: frac }) + ' ' + CUR();
const nfmt = (v, frac = 0) => (Math.round(num(v) * 10 ** frac) / 10 ** frac)
  .toLocaleString('ru-RU', { minimumFractionDigits: frac, maximumFractionDigits: frac });
const pct = (v) => nfmt(v, 1) + '%';

function hoursText(h) {
  h = num(h);
  if (!h) return '—';
  const total = Math.round(h * 60);
  const hh = Math.floor(total / 60), mm = total % 60;
  return hh ? `${hh} ч ${mm ? mm + ' мин' : ''}`.trim() : `${mm} мин`;
}
function minutesText(m) { return hoursText(num(m) / 60); }
function dateText(iso) {
  if (!iso) return '—';
  const d = new Date(iso.length <= 10 ? iso + 'T00:00:00' : iso);
  return isNaN(d) ? String(iso) : d.toLocaleDateString('ru-RU', { day: '2-digit', month: 'short' });
}
function dateTimeText(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  return isNaN(d) ? String(iso) : d.toLocaleString('ru-RU',
    { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' });
}
function agoText(iso) {
  if (!iso) return 'нет данных';
  const sec = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (!Number.isFinite(sec)) return '—';
  if (sec < 10) return 'только что';
  if (sec < 60) return `${Math.round(sec)} сек. назад`;
  if (sec < 3600) return `${Math.round(sec / 60)} мин. назад`;
  if (sec < 86400) return `${Math.round(sec / 3600)} ч назад`;
  return dateText(iso);
}
const todayISO = () => {
  const d = new Date(), pad = (v) => String(v).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
};
function initials(name) {
  const parts = String(name || '').trim().split(/\s+/).filter(Boolean);
  if (!parts.length) return '—';
  return (parts[0][0] + (parts[1] ? parts[1][0] : '')).toUpperCase();
}
const AVATAR_EMOJI = ['🐼','🦊','🌵','🐸','🦉','🐙','🦄','🐨','🐯','🦁','🐮','🐷','🐵','🐔','🐧','🐦','🦆','🦅','🦋','🐝','🐞','🌸','🌺','🌻'];
function avatarEmoji(name, seed) {
  // Детерминированный эмодзи-аватар: если имени нет — берём из seed (chat_id/id)
  const parts = String(name || '').trim().split(/\s+/).filter(Boolean);
  if (parts.length) return ''; // есть имя — используем initials
  const idx = Math.abs(parseInt(seed || '0', 10) || hashStr(String(seed || 'anon'))) % AVATAR_EMOJI.length;
  return AVATAR_EMOJI[idx];
}
function hashStr(s) { let h = 0; for (let i = 0; i < s.length; i++) { h = ((h << 5) - h) + s.charCodeAt(i); h |= 0; } return h; }
const debounce = (fn, ms = 260) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };

/* ================================================== Н3: плавный «докрут» числа
   KPI обновляются по живым данным; резкая смена цифры читается как скачок.
   Крутим только строки вида «число + подпись без цифр» («12 400 ₽», «96 %»);
   составные («3 ч 20 мин») и отключенная анимация — значение ставится сразу. */
const MOTION_OFF = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)');
const NUM_WITH_TAIL = /^([\d\u00a0\u202f\s.,\-]+)(\D*)$/;
function countUp(el, prevText, nextText, duration = 480) {
  if (!el) return;
  const a = NUM_WITH_TAIL.exec(String(prevText || '').trim());
  const b = NUM_WITH_TAIL.exec(String(nextText || '').trim());
  if (!a || !b) return;
  const parse = (t) => parseFloat(t.replace(/[\u00a0\u202f\s]/g, '').replace(',', '.'));
  const from = parse(a[1]), to = parse(b[1]);
  if (!Number.isFinite(from) || !Number.isFinite(to) || from === to) return;
  if ((MOTION_OFF && MOTION_OFF.matches) || document.hidden) return;
  const t0 = performance.now();
  const fmt = (v) => (Math.round(v * 100) / 100)
    .toLocaleString('ru-RU', { maximumFractionDigits: 2 }) + b[2];
  const step = (t) => {
    const k = Math.min(1, (t - t0) / duration);
    const eased = 1 - Math.pow(1 - k, 3);           // easeOutCubic: старт живой, финиш спокойный
    el.textContent = fmt(from + (to - from) * eased);
    if (k < 1) requestAnimationFrame(step);
    else el.textContent = nextText;                 // финал — ровно серверное значение
  };
  requestAnimationFrame(step);
};

/** Безопасный доступ к localStorage: файл:// и приватный режим его блокируют. */
const store = {
  get(key, fallback = null) {
    try { const v = localStorage.getItem(key); return v === null ? fallback : v; }
    catch (e) { return fallback; }
  },
  set(key, value) {
    try { localStorage.setItem(key, value); return true; } catch (e) { return false; }
  },
};

/* ============================================ 13.1: визуальные помощники
   Микро-взаимодействия панели: «подскок» счётчиков, кнопка-галочка,
   staggered-появление карточек, конфетти финиша, тонкая полоса загрузки. */
function bump(el) {
  if (!el) return;
  el.classList.remove('bump');
  void el.offsetWidth;                       // перезапуск CSS-анимации
  el.classList.add('bump');
}
function flashOk(btn) {
  if (!btn) return;
  const old = btn.dataset.flashOld || btn.innerHTML;
  btn.dataset.flashOld = old;
  btn.innerHTML = '<span aria-hidden="true">✓</span>';
  btn.classList.add('flash-ok');
  clearTimeout(btn._flashT);
  btn._flashT = setTimeout(() => { btn.innerHTML = old; btn.classList.remove('flash-ok'); }, 800);
}
function stagger(host, limit = 14) {
  if (!host || !host.children) return;
  for (let i = 0; i < host.children.length && i < limit; i++) {
    host.children[i].style.setProperty('--i', String(i));
  }
}
function confetti(origin) {
  if (MOTION_OFF && MOTION_OFF.matches) return;
  const host = document.body;
  const rect = origin && origin.getBoundingClientRect
    ? origin.getBoundingClientRect() : { left: innerWidth / 2, top: 90 };
  const colors = ['#4f46e5', '#7c3aed', '#10b981', '#f59e0b', '#ef4444', '#0ea5e9'];
  for (let i = 0; i < 14; i++) {
    const s = document.createElement('i');
    s.className = 'confetti-bit';
    s.style.left = (rect.left + (Math.random() * 44 - 22)) + 'px';
    s.style.top = (rect.top + Math.random() * 12) + 'px';
    s.style.background = colors[i % colors.length];
    s.style.setProperty('--dx', (Math.random() * 180 - 90) + 'px');
    s.style.setProperty('--dy', (70 + Math.random() * 150) + 'px');
    s.style.setProperty('--rot', (Math.random() * 560 - 280) + 'deg');
    host.appendChild(s);
    setTimeout(() => s.remove(), 1500);
  }
}

/* Тонкая полоса загрузки под шапкой: появляется на время живых запросов. */
let loadCount = 0;
const fetchOrig = window.fetch;
window.fetch = function fetchWithBar(...args) {
  loadCount += 1;
  const bar = $('top_load');
  if (bar) { bar.hidden = false; }
  const done = () => {
    loadCount = Math.max(0, loadCount - 1);
    const b = $('top_load');
    if (b && !loadCount) { b.classList.add('done'); setTimeout(() => { b.classList.remove('done'); b.hidden = true; }, 320); }
  };
  try {
    return fetchOrig.apply(this, args).then((r) => { done(); return r; }, (e) => { done(); throw e; });
  } catch (e) { done(); throw e; }
};

/* 13.1 (28): чип-детект номеров заказов. В поле поиска «№ 1001» →
   рядом появляется кнопка «Заказ №1001 — открыть»; один клик вместо
   ручного перехода на вкладку заказов. */
function wireNumberChip(input, opts) {
  if (!input || input.dataset.chip) return;
  input.dataset.chip = '1';
  const wrap = document.createElement('div');
  wrap.className = 'num-chip-wrap';
  input.parentNode.insertBefore(wrap, input.nextSibling);
  const chip = document.createElement('button');
  chip.type = 'button';
  chip.className = 'num-chip';
  chip.hidden = true;
  chip.innerHTML = '<span aria-hidden="true">▦</span><span></span>';
  wrap.appendChild(chip);
  const label = chip.querySelector('span:last-child');
  const check = debounce(() => {
    const m = /(?:№\s*)?(\d{3,5})/.exec(String(input.value || '').trim());
    if (!m) { chip.hidden = true; return; }
    const number = m[1];
    const order = (PF.state.orders || []).find((o) => String(o.number) === number);
    if (!order) { chip.hidden = true; return; }
    label.textContent = `Заказ №${number} — открыть`;
    chip.hidden = false;
    chip.onclick = () => {
      input.value = '';
      chip.hidden = true;
      PF.go('orders');
      if (PF.modules.ops && PF.modules.ops.openOrder) PF.modules.ops.openOrder(order.id);
    };
  }, 220);
  input.addEventListener('input', check);
  return chip;
}

/* Кнопка «наверх»: появляется после 600px прокрутки любой вкладки. */
const upBtn = $('up_btn');
if (upBtn) {
  window.addEventListener('scroll', () => {
    const y = window.scrollY || document.documentElement.scrollTop || 0;
    upBtn.hidden = y < 600;
  }, { passive: true });
  upBtn.addEventListener('click', () => window.scrollTo({ top: 0, behavior: MOTION_OFF && MOTION_OFF.matches ? 'auto' : 'smooth' }));
}

/* ============================================================== HTTP */
let offline = false;
function setOffline(flag, reason) {
  offline = flag;
  const bar = $('offline-bar');
  if (bar) bar.classList.toggle('show', flag);
  const dot = $('conn_dot');
  if (dot) dot.className = 'dot' + (flag ? ' bad' : ' ok');
  const title = $('conn_title');
  if (title) title.textContent = flag ? 'Коннектор недоступен' : 'Коннектор работает';
  const sub = $('conn_sub');
  if (sub) sub.textContent = flag ? (reason || 'Данные не сохраняются') : `Локально · v${PF.state.version || '2.0'}`;
}

function setChannelBar(channels) {
  const bar = $('channel-bar');
  const text = $('channel_bar_text');
  if (!bar || !text) return;
  const ch = channels || {};
  const mqtt = ch.mqtt || { ok: true };
  const ftps = ch.ftps || { ok: true };
  const disk = ch.disk || { ok: true };
  const bad = [];
  if (mqtt.ok === false) bad.push('MQTT');
  if (ftps.ok === false) bad.push('FTPS / SD');
  if (disk.ok === false) bad.push(disk.error || 'диск');
  if (!bad.length) {
    bar.classList.remove('show');
    text.textContent = 'MQTT · FTPS · диск — ок';
    return;
  }
  bar.classList.add('show');
  text.textContent = 'Нет связи: ' + bad.join(' · ');
}

/* Н2: сквозной id запроса. Один на попытку — при retry вызывающий код
   передаёт тот же ключ, поэтому в логе цепочка собирается целиком. */
function requestId() {
  if (window.crypto && crypto.randomUUID) return crypto.randomUUID().slice(0, 16);
  return 'r' + Date.now().toString(36) + Math.random().toString(36).slice(2, 8);
}
/* Ключ идемпотентности для записи: одинаковое действие, отправленное дважды
   (двойной клик, повтор после таймаута), сервер выполнит один раз. */
function idempotencyKey(path, body) {
  const raw = path + '\u0000' + (typeof body === 'string' ? body : JSON.stringify(body || {}));
  let hash = 0;
  for (let i = 0; i < raw.length; i += 1) hash = ((hash << 5) - hash + raw.charCodeAt(i)) | 0;
  return 'pf-' + (hash >>> 0).toString(36);
}

async function api(path, options) {
  const opts = Object.assign({ headers: {} }, options || {});
  const isWrite = !!(opts.body || (opts.method || '').toUpperCase() === 'POST');
  if (opts.body && !(opts.body instanceof FormData)) {
    opts.method = opts.method || 'POST';
    opts.headers['Content-Type'] = 'application/json';
    if (typeof opts.body !== 'string') opts.body = JSON.stringify(opts.body);
  }
  opts.headers['X-Request-Id'] = opts.requestId || requestId();
  if (isWrite && !opts.headers['Idempotency-Key']) {
    opts.headers['Idempotency-Key'] = opts.idempotencyKey
      || idempotencyKey(path.split('?')[0], opts.body);
  }
  let res;
  try {
    res = await fetch(path, opts);
  } catch (e) {
    setOffline(true);
    throw new Error('Нет связи с коннектором PrintFlow');
  }
  const text = await res.text();
  let data = {};
  if (text) { try { data = JSON.parse(text); } catch (e) { data = { error: text.slice(0, 300) }; } }
  // Н2: id запроса из заголовка — по нему сбой находится в connector.log.
  const rid = res.headers && res.headers.get ? (res.headers.get('X-Request-Id') || '') : '';
  if (rid && data && typeof data === 'object') data.request_id = data.request_id || rid;
  if (!res.ok) {
    const message = data.error || `Ошибка ${res.status}`;
    throw new Error(rid ? `${message} · ${rid}` : message);
  }
  // Н2: повтор уже выполнен — говорим об этом, а не показываем тишину.
  if (data && data.replayed && !opts.silentReplay) {
    toast('Уже обработано', 'Повторный запрос не создал вторую запись', 'info');
  }
  setOffline(false);
  return data;
}
const get = (path, query) => {
  const qs = query ? '?' + new URLSearchParams(
    Object.entries(query).filter(([, v]) => v !== '' && v != null)).toString() : '';
  return api(path + qs);
};
const post = (path, body) => api(path, { body: body || {} });

/* ============================================================= тосты */
const ICONS = { ok: '✓', bad: '✕', warn: '⚠', info: 'ℹ' };
function toast(title, sub, kind = 'ok', action = null) {
  const box = $('toasts');
  // В96: повторяющиеся события группируются в один тост со счётчиком,
  // вместо каскада одинаковых плашек.
  const twin = [...box.children].find((t) =>
    t.dataset.group === title && !t.classList.contains('out'));
  if (twin && !action) {
    const counter = twin.querySelector('.toast-count');
    const seen = (parseInt(twin.dataset.seen || '1', 10) || 1) + 1;
    twin.dataset.seen = String(seen);
    if (counter) counter.textContent = String(seen);
    else {
      const badge = document.createElement('span');
      badge.className = 'toast-count';
      badge.textContent = String(seen);
      twin.querySelector('b').appendChild(badge);
    }
    bump(twin);
    clearTimeout(twin._timer);
    twin._timer = setTimeout(() => twin.classList.add('out'), 3200);
    return twin;
  }
  const el = document.createElement('div');
  el.className = 'toast ' + kind;
  el.dataset.group = title;
  el.dataset.seen = '1';
  const dur = kind === 'bad' ? 5200 : 3200;
  const act = (action && action.label)
    ? `<button class="toast-action" type="button">${esc(action.label)}</button>` : '';
  el.innerHTML = `<span class="ic">${ICONS[kind] || ICONS.info}</span><span><b>${esc(title)}</b>${sub ? `<small>${esc(sub)}</small>` : ''}</span>${act}<button class="toast-close" aria-label="Закрыть">×</button><div class="toast-progress" style="animation:toastProgress ${dur}ms linear forwards"></div>`;
  box.appendChild(el);
  const close = () => { el.classList.add('out'); setTimeout(() => el.remove(), 260); };
  el.querySelector('.toast-close').onclick = close;
  const actionBtn = el.querySelector('.toast-action');
  if (actionBtn) {
    actionBtn.onclick = () => {
      try { if (action && action.run) action.run(); } catch (e) { fail(e); }
      close();
    };
  }
  setTimeout(close, dur);
}
const fail = (e) => toast('Не получилось', e && e.message ? e.message : String(e), 'bad');

/* ================================================== 15.1 (В-серия): общие хелперы */

/* В34: детерминированный цвет аватара по имени — тот же алгоритм, что
   в канбане заказов; один цвет = один человек во всех списках. */
function avColor(name) {
  const s = String(name || '');
  let h = 0;
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) % 360;
  return `hsl(${h} 52% 46%)`;
}

/* В95: кнопка «занята», пока идёт запрос. Спиннер внутри кнопки вместо
   тишины; повторный клик невозможен. */
async function withBusy(btn, fn) {
  if (!btn) return fn();
  if (btn.classList.contains('busy')) return undefined;
  btn.classList.add('busy');
  try {
    return await fn();
  } finally {
    btn.classList.remove('busy');
  }
}

/* В92: галочка успеха поверх действия — выдача, оплата, сохранение
   читаются как событие, а не как молчаливая перерисовка. */
function successFx() {
  if (MOTION_OFF && MOTION_OFF.matches) return;
  document.querySelectorAll('.pf-success').forEach((el) => el.remove());
  const fx = document.createElement('div');
  fx.className = 'pf-success';
  fx.innerHTML = '<i><svg viewBox="0 0 24 24" aria-hidden="true">'
    + '<path d="M5 13l4.4 4.4L19 8"/></svg></i>';
  document.body.appendChild(fx);
  setTimeout(() => fx.remove(), 900);
}

/* В96: тост с кнопкой «Вернуть» — живёт дольше обычного. */
function toastUndo(title, sub, run) {
  toast(title, sub, 'warn', { label: 'Вернуть', run });
}

/* В89: стопка скелетонов для ещё не заполненных списков. */
function skeletonStack(rows = 4, widths = [86, 64, 78, 52]) {
  return '<div class="skel-stack">' + Array.from({ length: rows }, (_, i) =>
    `<div class="skel-row"><i class="skel" style="width:${widths[i % widths.length]}%"></i></div>`).join('')
    + '</div>';
}

/* =========================================================== диалоги */
function openModal(id) { const d = $(id); if (d && !d.open) d.showModal(); }
function closeModal(id) { const d = $(id); if (d && d.open) d.close(); }
document.addEventListener('click', (e) => {
  const btn = e.target.closest('[data-close]');
  if (btn) { e.preventDefault(); closeModal(btn.dataset.close); }
  const emptyBtn = e.target.closest('[data-empty-click]');
  if (emptyBtn && emptyBtn.dataset.emptyClick) {
    e.preventDefault();
    e.stopPropagation();
    const target = $(emptyBtn.dataset.emptyClick);
    if (target) target.click();
  }
});

/** Подтверждение опасного действия. */
function confirmDanger(text) { return window.confirm(text); }

/** Пустой экран с одной кнопкой (П23). cta: {label, click: id кнопки в шапке}. */
function emptyHtml(icon, title, text, cta) {
  const btn = (cta && cta.label)
    ? `<button class="btn sm primary" type="button" data-empty-click="${esc(cta.click || '')}">${esc(cta.label)}</button>`
    : '';
  return `<div class="empty"><span class="big">${icon || '·'}</span>`
    + `<b>${esc(title || '')}</b>`
    + `<span>${esc(text || '')}</span>${btn}</div>`;
}

/* ====================================================== О2: полноэкранный
   просмотрщик (12.2). Один оверлей на весь сайт: снимки заказов, кадры
   камеры, превью материалов. Без внешних библиотек; закрытие — клик по
   фону, крестик или Esc. */
function lightbox(src, caption) {
  let host = $('pf-lightbox');
  if (!host) {
    host = document.createElement('div');
    host.id = 'pf-lightbox';
    host.className = 'pf-lb';
    host.innerHTML = '<button class="icon-btn pf-lb-x" type="button" aria-label="Закрыть">×</button>'
      + '<figure><img alt=""><figcaption></figcaption></figure>';
    document.body.appendChild(host);
    host.addEventListener('click', (e) => { if (!e.target.closest('img')) lightboxClose(); });
  }
  const img = host.querySelector('img');
  img.onload = () => img.classList.add('ready');
  img.classList.remove('ready');
  img.src = src;
  host.querySelector('figcaption').textContent = caption || '';
  host.classList.add('on');
  document.addEventListener('keydown', lightboxEsc);
}
function lightboxClose() {
  const host = $('pf-lightbox');
  if (!host) return;
  host.classList.remove('on');
  const img = host.querySelector('img');
  if (img) img.removeAttribute('src');
  document.removeEventListener('keydown', lightboxEsc);
}
function lightboxEsc(e) { if (e.key === 'Escape') lightboxClose(); }

/** Модалка вместо window.prompt (П22).
    ask({title, sub, fields, ok}) → Promise<object|string|null>.
    Один field без name — строка, как prompt; несколько — объект по name.
    Отмена / Esc / крестик → null. */
let askResolve = null;
function finishAsk(ok) {
  const resolve = askResolve;
  askResolve = null;
  const dlg = $('ask_modal');
  const box = $('ask_fields');
  let result = null;
  if (ok && box) {
    const inputs = $$('input, select, textarea', box);
    const out = {};
    inputs.forEach((el) => { out[el.name || 'value'] = el.value; });
    const keys = Object.keys(out);
    result = keys.length === 1 ? out[keys[0]] : out;
  }
  if (dlg && dlg.open) dlg.close();
  if (resolve) resolve(ok ? result : null);
}
function ask(opts) {
  if (typeof opts === 'string') opts = { title: opts };
  opts = opts || {};
  if (askResolve) {
    const prev = askResolve;
    askResolve = null;
    prev(null);
  }
  const fields = opts.fields && opts.fields.length ? opts.fields : [{
    name: 'value',
    label: opts.label || '',
    type: opts.type || 'text',
    value: opts.value != null ? opts.value : '',
    placeholder: opts.placeholder || '',
    hint: opts.hint || '',
    min: opts.min, max: opts.max, step: opts.step,
    options: opts.options,
    required: opts.required !== false,
  }];
  const eye = $('ask_eyebrow');
  const title = $('ask_title');
  const sub = $('ask_sub');
  const okBtn = $('ask_ok');
  const cancelBtn = $('ask_cancel');
  const box = $('ask_fields');
  if (!box || !title) return Promise.resolve(null);
  if (eye) eye.textContent = opts.eyebrow || 'Ввод';
  title.textContent = opts.title || 'Введите значение';
  if (sub) {
    sub.textContent = opts.sub || '';
    sub.hidden = !opts.sub;
  }
  if (okBtn) okBtn.textContent = opts.ok || 'OK';
  if (cancelBtn) cancelBtn.textContent = opts.cancel || 'Отмена';
  box.innerHTML = fields.map((f, i) => {
    const name = f.name || (fields.length === 1 ? 'value' : ('f' + i));
    const id = 'ask_f_' + name;
    const req = f.required === false ? '' : ' required';
    const hint = f.hint ? `<small>${esc(f.hint)}</small>` : '';
    const label = f.label || '';
    if (f.type === 'select' && f.options) {
      const optsHtml = f.options.map((o) => {
        const val = (o && typeof o === 'object') ? o.value : o;
        const lab = (o && typeof o === 'object') ? (o.label || o.value) : o;
        const sel = String(val) === String(f.value ?? '') ? ' selected' : '';
        return `<option value="${esc(val)}"${sel}>${esc(lab)}</option>`;
      }).join('');
      return `<label class="field"><span>${esc(label)}</span><select id="${esc(id)}" name="${esc(name)}"${req}>${optsHtml}</select>${hint}</label>`;
    }
    if (f.type === 'textarea') {
      return `<label class="field"><span>${esc(label)}</span><textarea id="${esc(id)}" name="${esc(name)}" rows="${f.rows || 4}" placeholder="${esc(f.placeholder || '')}"${req}>${esc(f.value ?? '')}</textarea>${hint}</label>`;
    }
    const type = f.type || 'text';
    const extra = [
      f.min != null ? ` min="${esc(f.min)}"` : '',
      f.max != null ? ` max="${esc(f.max)}"` : '',
      f.step != null ? ` step="${esc(f.step)}"` : '',
      f.placeholder ? ` placeholder="${esc(f.placeholder)}"` : '',
    ].join('');
    return `<label class="field"><span>${esc(label)}</span><input id="${esc(id)}" name="${esc(name)}" type="${esc(type)}" value="${esc(f.value ?? '')}" autocomplete="off"${extra}${req}>${hint}</label>`;
  }).join('');
  return new Promise((resolve) => {
    askResolve = resolve;
    openModal('ask_modal');
    setTimeout(() => {
      const first = box.querySelector('input, select, textarea');
      if (first) { first.focus(); if (first.select) first.select(); }
    }, 40);
  });
}
const askForm = $('ask_form');
if (askForm) askForm.addEventListener('submit', (e) => { e.preventDefault(); finishAsk(true); });
const askCancel = $('ask_cancel');
if (askCancel) askCancel.addEventListener('click', (e) => { e.preventDefault(); finishAsk(false); });
const askDlg = $('ask_modal');
if (askDlg) askDlg.addEventListener('close', () => { if (askResolve) finishAsk(false); });

/* Запасные названия статей: показываем человеческий текст, даже пока
   справочник расходов ещё не загрузился с коннектора. */
const CAT_FALLBACK = {
  order: 'Заказ', filament: 'Пластик', energy: 'Электричество',
  packaging: 'Упаковка', delivery: 'Доставка', fee: 'Комиссии площадок',
  equipment: 'Оборудование и запчасти', rent: 'Аренда',
  subscription: 'Подписки и сервисы', ads: 'Реклама и продвижение',
  tax: 'Налоги', insurance: 'Страховые взносы', withdrawal: 'Вывод себе',
  other: 'Прочее', sale: 'Продажа', service: 'Услуга',
};

/** Человеческое имя статьи расходов или дохода по её коду. */
function catName(id) {
  const code = String(id || '').trim();
  if (!code) return 'Без статьи';
  const found = (PF.state.expenseCategories || []).find((c) => c.id === code);
  return (found && found.name) || CAT_FALLBACK[code] || code;
}

/* ====================================================== шаблонизатор
   Идея 55/З15: разметка собирается тегом PF.html, который экранирует ВСЁ
   подставляемое по умолчанию. Сырой HTML — только явной пометкой raw().
   Это убирает целый класс опечаток «забыл esc()» и делает рендер читаемым.

     PF.html`<b>${name}</b>`            → экранированное имя
     PF.html`<div>${PF.raw(icon)}</div>`→ осознанно без экранирования
     PF.html`<ul>${rows.map(rowHtml)}</ul>` → массивы склеиваются

   Результат — строка; вставка выполняется PF.render(host, markup). */
function safeHtml(value) {
  if (value === null || value === undefined || value === false) return '';
  if (value && value.__raw === true) return value.raw;
  if (Array.isArray(value)) return value.map(safeHtml).join('');
  return esc(value);
}
function html(strings, ...values) {
  let out = '';
  for (let i = 0; i < strings.length; i++) {
    out += strings[i];
    if (i < values.length) out += safeHtml(values[i]);
  }
  return out;
}
const raw = (value) => ({ __raw: true, raw: String(value == null ? '' : value) });
function render(host, markup) {
  if (!host) return null;
  host.innerHTML = String(markup == null ? '' : markup);
  return host;
}

/* ======================================================= единый формат
   Идея 66: одно место, где числа превращаются в человеческий текст.
   Раньше копии format-функций жили в ops10.js (свой `esc10`, свой
   `fmtAt`), в tv.html и m.html — и расходились. Теперь панель, ТВ и
   мобильная берут формат отсюда: PF.fmt.grams(1234) → «1,23 кг». */
const fmt = {
  money, nfmt, pct,
  hours: hoursText,
  minutes: minutesText,
  date: dateText,
  dateTime: dateTimeText,
  ago: agoText,
  /** Вес пластика: граммы до килограмма, килограммы с двумя знаками. */
  grams(value) {
    const g = num(value);
    if (!g) return '—';
    if (g < 1000) return nfmt(Math.round(g)) + ' г';
    return nfmt(g / 1000, 2) + ' кг';
  },
  /** Количество штук: целое, с неразрывным пробелом в тысячах. */
  qty(value) {
    const n = num(value);
    if (!Number.isFinite(n)) return '—';
    return nfmt(Math.round(n * 100) / 100, n % 1 ? 1 : 0);
  },
  /** Короткая дата-время для списков: «31.08 14:20». */
  stamp(iso) {
    if (!iso) return '—';
    return dateTimeText(iso);
  },
};

/* ========================================================= состояние */
const PF = {
  state: {
    version: '', settings: {}, printers: [], statuses: [], niches: [], customers: [],
    orders: [], spools: [], catalog: [], nomenclature: [], warehouses: [],
    jobs: { queue: [], history: [] }, finance: null, live: null, activePrinter: '',
    events: [], financeDays: 30, dashDays: 7,
  },
  api: { get, post, api },
  ui: {
    $, $$, esc, num, clamp, money, nfmt, pct, hoursText, minutesText,
    dateText, dateTimeText, agoText, todayISO, initials, avatarEmoji, debounce,
    toast, fail, openModal, closeModal, confirmDanger, ask, emptyHtml, CUR, store, catName,
    setChannelBar, countUp, lightbox, lightboxClose,
    bump, flashOk, stagger, confetti, wireNumberChip,
    // 14.0 (55/66): шаблонизатор с автоэкранированием и единый форматтер
    html, raw, render, fmt,
    // 15.1 (В-серия): аватары, занятая кнопка, успех, undo, скелетоны
    avColor, withBusy, successFx, toastUndo, skeletonStack,
  },
  modules: {},
  bus: new EventTarget(),
};
window.PF = PF;
PF.emit = (name, detail) => PF.bus.dispatchEvent(new CustomEvent(name, { detail }));
PF.on = (name, fn) => PF.bus.addEventListener(name, (e) => fn(e.detail));

/* Подписка «когда панель готова». Модули, которые подгружаются лениво
   (идея 47), подключаются уже ПОСЛЕ события 'ready' — обычный PF.on('ready')
   у них не сработает никогда. PF.onReady вызывает fn сразу, если панель
   уже поднята, и честно ждёт события, если ещё нет. */
PF.ready = false;
PF.onReady = (fn) => {
  if (PF.ready) { fn(); return; }
  PF.on('ready', fn);
};

/* ============================================ ленивая загрузка модулей
   Идея 47: тяжёлые разделы (контент-студия, клиент-бот, центр смены)
   грузятся при первом входе, а не вместе со стартом панели. Скрипт
   регистрирует инициализацию через PF.module(name, init); загрузчик
   поднимает файл с тем же пином версии, что и остальные ассеты. */
const ASSET_VERSION = (() => {
  const tag = document.querySelector('script[src*="assets/"][src*="?v="]');
  const match = tag && /[?&]v=([^&]+)/.exec(tag.getAttribute('src') || '');
  return match ? match[1] : '';
})();
const LAZY_MODULES = {
  marketing: ['marketing.js'],
  clientbot: ['clientbot.js'],
  ops10: ['ops10.js'],
};
const lazyLoaded = new Set();
const lazyPending = new Map();
PF.module = (name, init) => {
  lazyLoaded.add(name);
  // Ошибку инициализации не прячем: иначе раздел выглядит живым, но не
  // работает (ровно так в панель уехали Б1/Б2). Пусть падает громко.
  if (typeof init === 'function') init();
};
PF.loadModule = (name) => {
  if (lazyLoaded.has(name)) return Promise.resolve(true);
  if (lazyPending.has(name)) return lazyPending.get(name);
  const files = LAZY_MODULES[name];
  if (!files) return Promise.resolve(false);
  const job = Promise.all(files.map((file) => new Promise((resolve, reject) => {
    const src = 'assets/' + file + (ASSET_VERSION ? '?v=' + ASSET_VERSION : '');
    if (document.querySelector(`script[data-lazy="${file}"]`)) { resolve(); return; }
    const tag = document.createElement('script');
    tag.src = src;
    tag.dataset.lazy = file;
    tag.onload = () => resolve();
    tag.onerror = () => reject(new Error('Не удалось загрузить ' + file));
    document.head.appendChild(tag);
  }))).then(() => {
    // Модуль может зарегистрироваться позже (скрипт исполняется синхронно,
    // но страховка дешёвая): если регистрации нет — считаем загруженным.
    lazyLoaded.add(name);
    return true;
  }).catch((e) => { console.error(e); return false; });
  lazyPending.set(name, job);
  return job;
};
PF.isLazyLoaded = (name) => lazyLoaded.has(name);

/* Видимость раздела: рендерить скрытые вкладки незачем (идея 45). */
PF.viewOn = (name) => {
  const view = document.getElementById('view-' + name);
  return !!view && view.classList.contains('on');
};
PF.whenView = (names, fn) => (...args) => {
  const list = Array.isArray(names) ? names : [names];
  return list.some((name) => PF.viewOn(name)) ? fn(...args) : undefined;
};

/* Единственный способ менять настройки в состоянии (Б3/14).

   Раньше модули писали `PF.state.settings = res.settings` напрямую: если
   сервер отвечал без поля settings (ошибка, частичный ответ, старый кэш),
   состояние становилось undefined и падала вся панель — каждый вызов
   money()/CUR() читает settings. Теперь ответ сливается с текущим
   состоянием, а пустой ответ не уничтожает то, что уже есть. */
PF.setSettings = (patch) => {
  const next = (patch && typeof patch === 'object' && !Array.isArray(patch)) ? patch : null;
  PF.state.settings = { ...(PF.state.settings || {}), ...(next || {}) };
  return PF.state.settings;
};

PF.status = (id) => PF.state.statuses.find((s) => s.id === id) || { id, name: id || '—', color: '#64748b' };
PF.niche = (id) => PF.state.niches.find((n) => n.id === id) || null;
PF.printer = (id) => PF.state.printers.find((p) => p.id === id) || null;
PF.finalStatusIds = () => PF.state.statuses.filter((s) => Number(s.is_final)).map((s) => s.id);
PF.isFinal = (order) => PF.finalStatusIds().includes(order.status);

/* Налог по выбранному режиму — те же правила, что в accounting.py.
   payer: 'person' (физлицо) или 'company' (юрлицо/ИП). */
PF.taxRate = (payer) => {
  const s = PF.state.settings || {};
  const n = (v, d) => { const x = Number(v); return Number.isFinite(x) ? x : (d || 0); };
  switch (s.tax_mode) {
    case 'npd': return payer === 'company' ? n(s.npd_rate_company, 6) : n(s.npd_rate_person, 4);
    case 'usn6': return n(s.usn_income_rate, 6);
    case 'usn15': return n(s.usn_profit_rate, 15);
    case 'manual': return n(s.tax_rate, 0);
    default: return 0; // «без налога» и патент с оборота не считаются
  }
};

/** Налог с конкретной сделки: на УСН 15 — с прибыли, иначе с оборота. */
PF.taxOf = (price, profitBase, payer) => {
  const s = PF.state.settings || {};
  const n = (v, d) => { const x = Number(v); return Number.isFinite(x) ? x : (d || 0); };
  if (s.tax_mode === 'usn15') return Math.max(0, n(profitBase)) * n(s.usn_profit_rate, 15) / 100;
  return n(price) * PF.taxRate(payer) / 100;
};

/** Подпись налоговой строки в расчётах: «после налога 6%» или «после налога». */
PF.taxLabel = () => {
  const s = PF.state.settings || {};
  const modes = { npd: 'НПД', usn6: 'УСН 6%', usn15: 'УСН 15%', patent: 'патент', manual: 'налог' };
  if (!s.tax_mode || s.tax_mode === 'none') return '';
  return modes[s.tax_mode] || 'налог';
};
PF.livePrinter = (id) => {
  const live = PF.state.live;
  if (!live) return null;
  const list = live.printers || [];
  return list.find((p) => p.id === (id || PF.state.activePrinter)) || live.active || list[0] || null;
};

/* ============================================================ графики */
/** Линейно-столбчатый график на инлайновом SVG. series: [{day, ...}] */
function drawChart(host, tipEl, series, keys, opts) {
  opts = opts || {};
  host.innerHTML = '';
  if (!series || !series.length) {
    host.innerHTML = '<div class="empty compact"><span>Данных за период пока нет.</span></div>';
    return;
  }
  const W = Math.max(320, host.clientWidth || 640), H = opts.height || 210;
  const pad = { l: 46, r: 12, t: 12, b: 24 };
  const iw = W - pad.l - pad.r, ih = H - pad.t - pad.b;
  const values = series.flatMap((row) => keys.map((k) => num(row[k.key])));
  const max = Math.max(1, ...values);
  const step = iw / Math.max(1, series.length);
  const x = (i) => pad.l + step * i + step / 2;
  const y = (v) => pad.t + ih - (num(v) / max) * ih;

  const ticks = 4;
  let grid = '';
  for (let i = 0; i <= ticks; i++) {
    const gy = pad.t + (ih / ticks) * i;
    const val = max - (max / ticks) * i;
    grid += `<line x1="${pad.l}" y1="${gy}" x2="${W - pad.r}" y2="${gy}" stroke="var(--line-soft)" stroke-width="1"/>`
      + `<text x="${pad.l - 8}" y="${gy + 4}" text-anchor="end" font-size="10" fill="var(--muted)">${opts.fmtAxis ? opts.fmtAxis(val) : nfmt(val)}</text>`;
  }

  let body = '';
  keys.forEach((k) => {
    if (k.type === 'bar') {
      const bw = Math.max(2, Math.min(22, step * 0.52));
      series.forEach((row, i) => {
        const v = num(row[k.key]);
        const h = Math.max(v > 0 ? 2 : 0, pad.t + ih - y(v));
        body += `<rect x="${x(i) - bw / 2}" y="${y(v)}" width="${bw}" height="${h}" rx="3" fill="${k.color}" opacity="${k.opacity || .85}"/>`;
      });
    } else {
      const pts = series.map((row, i) => `${x(i)},${y(row[k.key])}`).join(' ');
      if (k.area) {
        body += `<polygon points="${pad.l},${pad.t + ih} ${pts} ${W - pad.r},${pad.t + ih}" fill="${k.color}" opacity=".1"/>`;
      }
      body += `<polyline points="${pts}" fill="none" stroke="${k.color}" stroke-width="2.4" stroke-linejoin="round" stroke-linecap="round"/>`;
      series.forEach((row, i) => {
        body += `<circle cx="${x(i)}" cy="${y(row[k.key])}" r="2.6" fill="var(--panel)" stroke="${k.color}" stroke-width="2"/>`;
      });
    }
  });

  let labels = '';
  const every = Math.ceil(series.length / 8);
  series.forEach((row, i) => {
    if (i % every) return;
    labels += `<text x="${x(i)}" y="${H - 6}" text-anchor="middle" font-size="10" fill="var(--muted)">${esc(dateText(row.day))}</text>`;
  });

  let hits = '';
  series.forEach((row, i) => {
    hits += `<rect x="${pad.l + step * i}" y="${pad.t}" width="${step}" height="${ih}" fill="transparent" data-i="${i}"/>`;
  });

  host.innerHTML = `<svg class="chart" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" height="${H}">${grid}${body}${labels}${hits}</svg>`;

  if (!tipEl) return;
  const svg = host.querySelector('svg');
  svg.addEventListener('mousemove', (e) => {
    const cell = e.target.closest('[data-i]');
    if (!cell) { tipEl.classList.remove('show'); return; }
    const row = series[+cell.dataset.i];
    const rect = host.getBoundingClientRect();
    tipEl.innerHTML = `<b>${esc(dateText(row.day))}</b><br>` + keys.map((k) =>
      `${esc(k.label)}: ${k.fmt ? k.fmt(row[k.key]) : nfmt(row[k.key])}`).join('<br>');
    tipEl.style.left = (e.clientX - rect.left) + 'px';
    tipEl.style.top = (e.clientY - rect.top) + 'px';
    tipEl.classList.add('show');
  });
  svg.addEventListener('mouseleave', () => tipEl.classList.remove('show'));
}
function legend(host, keys) {
  host.innerHTML = keys.map((k) => `<span><i style="background:${k.color}"></i>${esc(k.label)}</span>`).join('');
}
PF.ui.drawChart = drawChart;
PF.ui.legend = legend;

/* =========================================================== роутер */
const VIEWS = {
  dashboard: { title: 'Обзор', sub: 'Производство, деньги и принтеры в одном месте' },
  printers: { title: 'Принтеры', sub: 'Живое состояние парка Bambu Lab' },
  queue: { title: 'Очередь печати', sub: 'Задания парка и журнал печати' },
  orders: { title: 'Заказы', sub: 'Канбан, сроки и экономика заказов' },
  customers: { title: 'Клиенты', sub: 'История покупок и сегменты' },
  products: { title: 'Товары', sub: 'Номенклатура, остатки, цены и экономика' },
  batches: { title: 'Партии печати', sub: 'Производство на склад и автоприход' },
  documents: { title: 'Документы', sub: 'Приход, продажа, перемещение, инвентаризация' },
  warehouses: { title: 'Склады', sub: 'Остатки по местам хранения и оборотка' },
  shelf: { title: 'Стеллаж', sub: 'Готовая продукция на полке магазина' },
  finance: { title: 'Финансы', sub: 'Автоматический учёт доходов и расходов' },
  inventory: { title: 'Склад пластика', sub: 'Остатки катушек и база изделий' },
  niches: { title: 'Ниши', sub: 'Проверка гипотез по фактическим заказам' },
  calc: { title: 'Калькулятор', sub: 'Себестоимость, цена и прибыль за час' },
  marketing: { title: 'Контент', sub: 'Посты, карточки, отчёты и таблички — генераторы 8.5' },
  clientbot: { title: 'Клиент-бот', sub: 'Telegram-бот для покупателей: витрина, заказы, статусы' },
  library: { title: 'Библиотека', sub: 'Инструкции, скрипты и материалы' },
  settings: { title: 'Настройки', sub: 'Тарифы, автоматизация и данные' },
};
/* привычные синонимы разделов, чтобы ссылки вида #spools не бросали на обзор */
const VIEW_ALIASES = {
  spools: 'inventory', filament: 'inventory', stock: 'inventory',
  shelf2: 'shelf', store: 'shelf', polka: 'shelf',
  nomenclature: 'products', goods: 'products', catalog: 'products', items: 'products',
  tovary: 'products', product: 'products',
  batch: 'batches', partii: 'batches', production: 'batches',
  docs2: 'documents', document: 'documents', documenty: 'documents',
  warehouse: 'warehouses', sklad: 'warehouses', wh: 'warehouses',
  money: 'finance', finances: 'finance', accounting: 'finance',
  home: 'dashboard', main: 'dashboard', overview: 'dashboard',
  jobs: 'queue', print: 'queue', clients: 'customers',
  calculator: 'calc', docs: 'library', settings2: 'settings',
  client: 'clientbot', clientsbot: 'clientbot', buyer: 'clientbot',
  покупатель: 'clientbot',
};
let currentView = '';

/** Сбросить именно прокрутку контента. В разных браузерах прокручиваемым
    корнем бывает html либо body; дополнительные контейнеры обнуляем на случай
    запуска в установленном приложении или WebView. */
function resetViewScroll() {
  window.scrollTo(0, 0);
  const roots = [document.scrollingElement, document.documentElement, document.body,
    $('main'), $('views')];
  roots.forEach((el) => { if (el) { el.scrollTop = 0; el.scrollLeft = 0; } });
}

function showView(name, sub) {
  if (!VIEWS[name] && VIEW_ALIASES[name]) name = VIEW_ALIASES[name];
  if (!VIEWS[name]) name = 'dashboard';
  currentView = name;
  $$('.view').forEach((v) => {
    const active = v.id === 'view-' + name;
    v.classList.toggle('on', active);
    v.classList.toggle('enter', active);     // 13.1: лёгкий fade-вход вкладки
    v.hidden = !active;
  });
  // 13.1 (42): утреннее приветствие на Обзоре до 11:00 — тёплый старт дня
  if (name === 'dashboard') {
    const h1 = document.querySelector('#view-dashboard .view-head h1');
    if (h1) {
      const hour = new Date().getHours();
      h1.textContent = (hour >= 5 && hour < 11) ? 'Доброе утро, цех!' : 'Производство сегодня';
    }
  }
  $$('.nav-link').forEach((a) => {
    const group = (a.dataset.views || '').split(/\s+/).filter(Boolean);
    const on = a.dataset.view === name || group.includes(name);
    a.classList.toggle('on', on);
    if (on) {
      const more = a.closest('details.nav-more');
      if (more) more.open = true;
    }
  });
  syncStockTabs(name);
  $('top_title').textContent = VIEWS[name].title;
  $('top_sub').textContent = VIEWS[name].sub;
  document.title = `${VIEWS[name].title} · NOZZA`;
  resetViewScroll();
  $('side').classList.remove('show');
  const scrim = $('scrim'); if (scrim) scrim.remove();
  PF.emit('view', { view: name, sub });
  // Идея 47: раздел может жить в отдельном файле, который грузится при
  // первом входе. После загрузки повторяем событие — модуль отрисуется.
  if (LAZY_MODULES[name] && !lazyLoaded.has(name)) {
    PF.loadModule(name).then((ok) => {
      if (ok && currentView === name) PF.emit('view', { view: name, sub, lazy: true });
    });
  }
  store.set('pf_last_view', name);
  if (STOCK_IDS.has(name)) store.set('pf_last_stock', name);
  // В89: пока панель ещё не получила первые данные, список раздела не
  // «мигает» пустотой — показывает скелетон будущих строк.
  if (!PF.ready) {
    const skelHosts = { queue: 'queue_list', products: 'prod_grid',
      customers: 'customers_tbody', finance: 'fin_tx' };
    const skelId = skelHosts[name];
    const skelHost = skelId ? $(skelId) : null;
    if (skelHost && !skelHost.childElementCount) skelHost.innerHTML = skeletonStack(5);
  }
  // После перерисовки и обработки hash браузером повторно фиксируем начало.
  requestAnimationFrame(resetViewScroll);
  setTimeout(resetViewScroll, 0);
}
function routeFromHash() {
  const raw = (location.hash || '').slice(1);
  if (!raw) {
    const last = store.get('pf_last_view', 'dashboard') || 'dashboard';
    showView(last);
    return;
  }
  const [name, sub] = raw.split('/');
  showView(name, sub);
}
/* ================================================== глубокие ссылки (57)
   `#orders/123` открывает карточку заказа, `#products/abc` — товар,
   `#customers/xyz` — клиента. Без этого ссылки из Telegram, QR-кодов и
   уведомлений могли вести только «в раздел», и оператор искал нужную
   карточку руками. Модуль регистрирует открыватель через PF.deepLink,
   а роутер вызывает его после показа раздела. */
PF.deepLinks = {};
PF.deepLink = (view, opener) => { PF.deepLinks[view] = opener; };
PF.on('view', (detail) => {
  if (!detail || !detail.sub) return;
  const opener = PF.deepLinks[detail.view];
  if (!opener) return;
  // Карточка может требовать данных, которые ещё не пришли: ждём 'data'.
  const run = () => { try { opener(detail.sub); } catch (e) { console.error(e); } };
  if ((PF.state.orders || []).length || (PF.state.nomenclature || []).length) run();
  else PF.on('data', function once() { run(); PF.bus.removeEventListener('data', once); });
});

PF.go = (view, sub) => {
  const hash = '#' + view + (sub ? '/' + sub : '');
  if (location.hash === hash) showView(view, sub);
  else location.hash = hash;
};
if ('scrollRestoration' in history) history.scrollRestoration = 'manual';
window.addEventListener('hashchange', routeFromHash);

/* ============================================================== тема */
function applyTheme() {
  const pref = PF.state.settings.theme || 'system';
  const dark = pref === 'dark' || (pref === 'system' &&
    window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches);
  document.documentElement.dataset.theme = dark ? 'dark' : 'light';
  document.documentElement.dataset.accent = PF.state.settings.accent || 'indigo';
}
PF.applyTheme = applyTheme;
if (window.matchMedia) {
  window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {
    if ((PF.state.settings.theme || 'system') === 'system') applyTheme();
  });
}
/* Плотность интерфейса (Б8): обычный вид → компактно → режим цеха.
   Компактно — меньше воздуха по всей панели (tokens.css), режим цеха —
   крупные кнопки у станка. Выбор живёт в localStorage. */
const DENSITY_ORDER = ['desk', 'compact', 'shop'];
const DENSITY_LABEL = {
  desk: ['Обычный вид', 'Больше данных на экране'],
  compact: ['Компактно', 'Меньше воздуха, больше строк'],
  shop: ['Режим цеха', 'Крупные кнопки у станка'],
};
function applyDensity() {
  const cur = store.get('pf_density', '');
  const on = DENSITY_ORDER.includes(cur) ? cur : 'desk';
  document.documentElement.dataset.density = on;
  const btn = $('density_btn');
  if (btn) {
    btn.classList.toggle('on', on !== 'desk');
    btn.title = DENSITY_LABEL[on][0] + ' — клик переключит';
  }
}
applyDensity();
const densBtn = $('density_btn');
if (densBtn) densBtn.addEventListener('click', () => {
  const cur = store.get('pf_density', '');
  const idx = Math.max(0, DENSITY_ORDER.indexOf(DENSITY_ORDER.includes(cur) ? cur : 'desk'));
  const next = DENSITY_ORDER[(idx + 1) % DENSITY_ORDER.length];
  store.set('pf_density', next === 'desk' ? '' : next);
  applyDensity();
  const [name, sub] = DENSITY_LABEL[next];
  toast(name, sub);
});

/* ============================ 13.1 (З2-10/11/12): доступность интерфейса
   «Меньше анимаций» (перекрывает prefers-reduced-motion), «Высокий
   контраст» и «Режим дальтоников» — атрибуты на <html>, стили в theme.css. */
function applyAccessibility() {
  const root = document.documentElement;
  root.dataset.motion = store.get('pf_motion_off', '') === '1' ? 'off' : '';
  root.dataset.contrast = store.get('pf_contrast', '') === '1' ? 'on' : '';
  root.dataset.colorblind = store.get('pf_colorblind', '') === '1' ? 'on' : '';
  const mo = $('set_motion_off');
  if (mo) mo.checked = root.dataset.motion === 'off';
  const ct = $('set_contrast');
  if (ct) ct.checked = root.dataset.contrast === 'on';
  const cb = $('set_colorblind');
  if (cb) cb.checked = root.dataset.colorblind === 'on';
}
applyAccessibility();
const bindAccessibility = () => {
  const wire = (id, key) => {
    const el = $(id);
    if (!el) return;
    el.addEventListener('change', () => {
      store.set(key, el.checked ? '1' : '');
      applyAccessibility();
      toast(el.checked ? 'Включено' : 'Выключено', el.closest('.set-row') ? el.closest('.set-row').querySelector('.sinfo b').textContent : '');
    });
  };
  wire('set_motion_off', 'pf_motion_off');
  wire('set_contrast', 'pf_contrast');
  wire('set_colorblind', 'pf_colorblind');
};
PF.on('ready', bindAccessibility);

$('theme_btn').addEventListener('click', async () => {
  const order = ['system', 'light', 'dark'];
  const next = order[(order.indexOf(PF.state.settings.theme || 'system') + 1) % order.length];
  PF.state.settings.theme = next;
  applyTheme();
  const names = { system: 'как в системе', light: 'светлая', dark: 'тёмная' };
  toast('Тема: ' + names[next]);
  try { await post('/api/settings', { theme: next }); } catch (e) { /* офлайн — не критично */ }
});

/* ================================================= командная палитра */
let paletteItems = [], paletteSel = 0;

function baseCommands() {
  const nav = Object.entries(VIEWS).map(([id, v]) => ({
    group: 'Разделы', icon: '→', title: v.title, sub: v.sub, run: () => PF.go(id),
  }));
  return nav.concat([
    { group: 'Действия', icon: '+', title: 'Новый заказ', sub: 'Создать карточку заказа', run: () => PF.modules.ops && PF.modules.ops.openOrder() },
    { group: 'Действия', icon: '✨', title: 'Преобразовать активную печать в заказ', sub: 'Создать заказ из текущей печати', run: () => PF.modules.printer && PF.modules.printer.convertActiveToOrder() },
    { group: 'Действия', icon: '+', title: 'Новая катушка', sub: 'Добавить пластик на склад', run: () => PF.modules.money && PF.modules.money.openSpool() },
    { group: 'Действия', icon: '+', title: 'Новая проводка', sub: 'Доход или расход вручную', run: () => PF.modules.money && PF.modules.money.openTx() },
    { group: 'Действия', icon: '+', title: 'Задание в очередь', sub: 'Печать файла с принтера', run: () => PF.modules.printer && PF.modules.printer.openJob() },
    { group: 'Принтер', icon: '❙❙', title: 'Пауза печати', sub: 'Активный принтер', run: () => PF.modules.printer && PF.modules.printer.command('pause') },
    { group: 'Принтер', icon: '▶', title: 'Продолжить печать', sub: 'Активный принтер', run: () => PF.modules.printer && PF.modules.printer.command('resume') },
    { group: 'Принтер', icon: '☀', title: 'Свет камеры', sub: 'Включить или выключить', run: () => PF.modules.printer && PF.modules.printer.command('light') },
    { group: 'Принтер', icon: '■', title: 'Остановить печать', sub: 'Требует подтверждения', run: () => PF.modules.printer && PF.modules.printer.command('stop') },
    { group: 'Система', icon: '↓', title: 'Скачать резервную копию', sub: 'JSON со всеми данными', run: () => PF.modules.settings && PF.modules.settings.downloadBackup() },
    { group: 'Система', icon: '◐', title: 'Переключить тему', sub: 'Светлая / тёмная', run: () => $('theme_btn').click() },
    { group: 'Система', icon: '▣', title: 'Режим цеха', sub: 'Крупные кнопки у станка', run: () => $('density_btn') && $('density_btn').click() },
  ]);
}

function renderPalette(items) {
  paletteItems = items;
  paletteSel = 0;
  const list = $('palette_list');
  if (!items.length) { list.innerHTML = '<div class="empty compact"><span>Ничего не найдено.</span></div>'; return; }
  let html = '', group = '';
  items.forEach((it, i) => {
    if (it.group !== group) { group = it.group; html += `<div class="palette-group">${esc(group)}</div>`; }
    html += `<button class="palette-item${i === 0 ? ' sel' : ''}" type="button" data-i="${i}">`
      + `<span class="ic">${esc(it.icon || '•')}</span><span><b>${esc(it.title)}</b>`
      + (it.sub ? `<small>${esc(it.sub)}</small>` : '') + '</span></button>';
  });
  list.innerHTML = html;
}
function moveSel(delta) {
  const btns = $$('.palette-item', $('palette_list'));
  if (!btns.length) return;
  paletteSel = clamp(paletteSel + delta, 0, btns.length - 1);
  btns.forEach((b, i) => b.classList.toggle('sel', i === paletteSel));
  btns[paletteSel].scrollIntoView({ block: 'nearest' });
}
function runSel() {
  const it = paletteItems[paletteSel];
  if (!it) return;
  closeModal('palette');
  setTimeout(() => { try { it.run(); } catch (e) { fail(e); } }, 40);
}
const searchRemote = debounce(async (q) => {
  if (!q || q.length < 2) return;
  try {
    const data = await get('/api/search', { q });
    const found = (data.results || []).map((r) => ({
      group: 'Найдено',
      icon: { order: '▦', customer: '◎', spool: '◍', printer: '◉', product: '▩', document: '📄' }[r.type] || '•',
      title: r.title, sub: r.subtitle,
      run: () => {
        if (r.type === 'order') { PF.go('orders'); PF.modules.ops && PF.modules.ops.openOrder(r.id); }
        else if (r.type === 'customer') PF.go('customers');
        else if (r.type === 'spool') { PF.go('inventory'); PF.modules.money && PF.modules.money.openSpool(r.id); }
        else if (r.type === 'printer') { PF.state.activePrinter = r.id; PF.go('printers'); }
        // 13.1 (12): товары и документы — единый поиск из палитры
        else if (r.type === 'product') { PF.go('products'); PF.modules.products && PF.modules.products.openNom(r.id); }
        else if (r.type === 'document') { PF.go('documents'); PF.modules.products && PF.modules.products.openDoc(r.id); }
      },
    }));
    const local = filterCommands($('palette_input').value);
    renderPalette(found.concat(local));
  } catch (e) { /* поиск не критичен */ }
}, 220);

function filterCommands(q) {
  const s = (q || '').trim().toLowerCase();
  const all = baseCommands();
  if (!s) return all;
  return all.filter((c) => (c.title + ' ' + (c.sub || '') + ' ' + c.group).toLowerCase().includes(s));
}
function openPalette() {
  renderPalette(baseCommands());
  openModal('palette');
  const input = $('palette_input');
  input.value = '';
  setTimeout(() => input.focus(), 30);
}
$('cmd_open').addEventListener('click', openPalette);
$('palette_input').addEventListener('input', (e) => {
  renderPalette(filterCommands(e.target.value));
  searchRemote(e.target.value.trim());
});
$('palette_list').addEventListener('click', (e) => {
  const btn = e.target.closest('.palette-item');
  if (!btn) return;
  paletteSel = +btn.dataset.i;
  runSel();
});
$('palette').addEventListener('keydown', (e) => {
  if (e.key === 'ArrowDown') { e.preventDefault(); moveSel(1); }
  else if (e.key === 'ArrowUp') { e.preventDefault(); moveSel(-1); }
  else if (e.key === 'Enter') { e.preventDefault(); runSel(); }
});
document.addEventListener('keydown', (e) => {
  const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement && document.activeElement.tagName);
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') { e.preventDefault(); openPalette(); return; }
  if (e.key === '/' && !typing) { e.preventDefault(); openPalette(); return; }
  if (e.key.toLowerCase() === 'n' && !typing && !e.ctrlKey && !e.metaKey && !e.altKey) {
    e.preventDefault(); PF.modules.ops && PF.modules.ops.openOrder();
  }
});

/* ====================================================== навигация UI */
document.addEventListener('click', (e) => {
  const ordLink = e.target.closest('[data-order-open]');
  if (ordLink) {
    e.preventDefault();
    const orderId = ordLink.dataset.orderOpen;
    if (orderId && PF.modules.ops && PF.modules.ops.openOrder) {
      PF.go('orders');
      PF.modules.ops.openOrder(orderId);
    }
    return;
  }
  const convBtn = e.target.closest('[data-convert-order]');
  if (convBtn) {
    e.preventDefault();
    if (PF.modules.printer && PF.modules.printer.convertActiveToOrder) {
      PF.modules.printer.convertActiveToOrder(convBtn.dataset.convertOrder || PF.state.activePrinter);
    }
    return;
  }
  const link = e.target.closest('a[data-view]');
  if (!link) return;
  e.preventDefault();
  PF.go(link.dataset.view, link.dataset.article);
});
$('burger').addEventListener('click', () => {
  const side = $('side');
  side.classList.toggle('show');
  if (side.classList.contains('show')) {
    const scrim = document.createElement('div');
    scrim.id = 'scrim';
    scrim.addEventListener('click', () => { side.classList.remove('show'); scrim.remove(); });
    document.body.appendChild(scrim);
  } else { const s = $('scrim'); if (s) s.remove(); }
});
const STOCK_TABS = [
  { id: 'products', label: 'Товары', icon: '📦' },
  { id: 'batches', label: 'Партии', icon: '🖨' },
  { id: 'documents', label: 'Документы', icon: '📋' },
  { id: 'warehouses', label: 'Склады', icon: '🏬' },
  { id: 'shelf', label: 'Стеллаж', icon: '🏷' },
  { id: 'inventory', label: 'Пластик', icon: '🧶' },
];
const STOCK_IDS = new Set(STOCK_TABS.map((t) => t.id));

function syncStockTabs(name) {
  $$('.stock-tabs').forEach((el) => el.remove());
  if (!STOCK_IDS.has(name)) return;
  const view = $('view-' + name);
  if (!view) return;
  const bar = document.createElement('div');
  bar.className = 'seg tabs stock-tabs';
  bar.innerHTML = STOCK_TABS.map((t) =>
    `<button type="button" data-view="${t.id}" class="${t.id === name ? 'on' : ''}"><span class="tab-ic">${t.icon}</span><span>${t.label}</span><span class="tab-hits" data-tab-badge="${t.id}" hidden></span></button>`).join('');
  bar.addEventListener('click', (e) => {
    const btn = e.target.closest('button[data-view]');
    if (btn) PF.go(btn.dataset.view);
  });
  const head = view.querySelector('.view-head');
  if (head && head.nextSibling) view.insertBefore(bar, head.nextSibling);
  else view.prepend(bar);
  if (window.PF && PF.updateStockTabBadges) PF.updateStockTabBadges();
}

function filterNav(q) {
  const s = String(q || '').trim().toLowerCase();
  const empty = $('nav_empty');
  let shown = 0;
  $$('#side .nav-group').forEach((g) => {
    let hit = 0;
    $$('.nav-link', g).forEach((a) => {
      const blob = (a.textContent + ' ' + (a.dataset.find || '') + ' ' + (a.dataset.view || '') + ' ' + (a.dataset.views || '')).toLowerCase();
      const ok = !s || blob.includes(s);
      a.hidden = !ok;
      if (ok) { hit++; shown++; }
    });
    g.hidden = !!s && !hit;
    $$('.nav-more-section', g).forEach((section) => {
      const sectionHit = $$('.nav-link', section).some((link) => !link.hidden);
      section.hidden = !!s && !sectionHit;
    });
    const more = g.querySelector('details.nav-more');
    if (more && s && hit) more.open = true;
  });
  if (empty) empty.hidden = !s || shown > 0;
}
const navMore = $('nav_more');
if (navMore) {
  const savedMoreState = store.get('pf_nav_more_open', null);
  if (savedMoreState !== null) navMore.open = savedMoreState === '1';
  navMore.addEventListener('toggle', () => store.set('pf_nav_more_open', navMore.open ? '1' : '0'));
}
const navFind = $('nav_find');
if (navFind) {
  navFind.addEventListener('input', () => filterNav(navFind.value));
  navFind.addEventListener('keydown', (e) => {
    if (e.key !== 'Enter') return;
    const first = $$('#side .nav-link').find((a) => !a.hidden);
    if (first) { e.preventDefault(); PF.go(first.dataset.view); navFind.value = ''; filterNav(''); }
  });
}

$('conn_chip').addEventListener('click', () => PF.go('settings'));
$('quick_order').addEventListener('click', () => PF.modules.ops && PF.modules.ops.openOrder());

/* ======================================================= загрузка данных */
async function bootstrap() {
  const data = await get('/api/bootstrap');
  PF.state.version = data.version;
  PF.setSettings(data.settings);
  PF.state.printers = data.printers || [];
  PF.state.statuses = data.statuses || [];
  PF.state.niches = data.niches || [];
  PF.state.summary = data.summary || {};
  PF.state.live = data.state || null;
  if (!PF.state.activePrinter && PF.state.printers.length) PF.state.activePrinter = PF.state.printers[0].id;
  applyTheme();
  setOffline(false);
  PF.emit('bootstrap', data);
}

async function refreshCore() {
  const [orders, customers, spools, catalog, jobs, nomenclature] = await Promise.all([
    get('/api/orders'), get('/api/customers'), get('/api/spools'),
    get('/api/catalog'), get('/api/jobs', { limit: 60 }),
    get('/api/nomenclature'),
  ]);
  PF.state.orders = orders.orders || [];
  PF.state.customers = customers.customers || [];
  PF.state.spools = spools.spools || [];
  PF.state.catalog = catalog.catalog || [];
  PF.state.jobs = jobs || { queue: [], history: [] };
  PF.state.nomenclature = nomenclature.items || [];
  PF.state.groups = nomenclature.groups || [];
  PF.state.warehouses = nomenclature.warehouses || [];
  PF.emit('data');
}
PF.refreshCore = refreshCore;

async function refreshLists() {
  const [statuses, niches] = await Promise.all([get('/api/statuses'), get('/api/niches')]);
  PF.state.statuses = statuses.statuses || [];
  PF.state.niches = niches.niches || [];
}
PF.refreshLists = refreshLists;

async function refreshFinance(days) {
  PF.state.financeDays = days || PF.state.financeDays;
  const data = await get('/api/finance', { days: PF.state.financeDays });
  PF.state.finance = data;
  PF.emit('finance', data);
  return data;
}
PF.refreshFinance = refreshFinance;

async function refreshEvents() {
  const data = await get('/api/events', { limit: 60 });
  PF.state.events = data.events || [];
  PF.emit('events', PF.state.events);
}
PF.refreshEvents = refreshEvents;

/** Server-Sent Events: сервер сам присылает изменения, а не мы его дёргаем.

    telemetry — новое состояние парка (только когда принтер что-то прислал);
    event     — новая запись журнала (печать, заказ, склад);
    resync    — вкладка отстала (спящий телефон), перечитываем всё.

    Поллинг остаётся страховкой: если SSE недоступен (прокси, старый браузер),
    интерфейс работает как раньше, просто с опросом каждые 2.5 секунды. */
let sseOk = false;
let coreTimer = 0;

/** Изменения журнала, после которых имеет смысл перечитать заказы и склад. */
const DATA_EVENTS = /^(complete|error|stop|start|order|payment|income|expense|stock|doc|shelf|batch|spool|defect|import|restore)/;

function applyLive(data) {
  if (!data) return;
  // Рассылка идёт всем вкладкам сразу, поэтому активный принтер выбираем
  // здесь: у каждой вкладки он может быть свой.
  if (PF.state.activePrinter && Array.isArray(data.printers)) {
    const mine = data.printers.find((p) => p.id === PF.state.activePrinter);
    if (mine) data.active = mine;
  }
  PF.state.live = data;
  // Профили принтеров приходят из /api/printers; здесь обновляем только
  // имена, чтобы переименование было видно сразу.
  (data.printers || []).forEach((p) => {
    const known = PF.printer(p.id);
    if (known) { known.name = p.name; known.model = p.model; }
  });
  setOffline(false);
  PF.emit('live', data);
}

/** Заказы и склад перечитываем пачкой: несколько событий подряд — один запрос. */
function scheduleCore() {
  if (coreTimer) return;
  coreTimer = setTimeout(() => {
    coreTimer = 0;
    if (!offline) refreshCore().catch(() => {});
  }, 1200);
}

function connectSSE() {
  if (!window.EventSource || typeof EventSource === 'undefined') return;
  let es;
  try { es = new EventSource('/api/stream'); } catch (e) { return; }

  es.addEventListener('telemetry', (msg) => {
    if (offline) return;
    try { applyLive(JSON.parse(msg.data)); } catch (e) { /* битый кадр — переживём */ }
  });

  es.addEventListener('event', (msg) => {
    if (offline) return;
    let row;
    try { row = JSON.parse(msg.data); } catch (e) { return; }
    PF.state.events = [row, ...(PF.state.events || [])].slice(0, 60);
    PF.emit('events', PF.state.events);
    // Браузерное уведомление — мгновенно, не дожидаясь опроса.
    PF.emit('notify', row);
    if (DATA_EVENTS.test(row.kind || '')) scheduleCore();
  });

  es.addEventListener('resync', () => {
    if (offline) return;
    poll();
    refreshEvents().catch(() => {});
    refreshCore().catch(() => {});
  });

  // Совместимость со старым сервером, который слал пустой «refresh».
  es.addEventListener('refresh', () => {
    if (offline) return;
    poll();
    refreshEvents().catch(() => {});
    refreshCore().catch(() => {});
  });

  es.onopen = () => { sseOk = true; resizePolling(); };
  es.onerror = () => { sseOk = false; resizePolling(); };
}

let pollTimer = 0;
function resizePolling() {
  // При живом SSE опрос нужен только как страховка от разрыва соединения.
  const interval = sseOk ? 20000 : 2500;
  if (pollTimer) { clearInterval(pollTimer); pollTimer = 0; }
  pollTimer = setInterval(poll, interval);
}

/** Живое состояние принтеров — запасной путь, когда SSE недоступен. */
async function poll() {
  try {
    const data = await get('/api/state', { printer_id: PF.state.activePrinter });
    applyLive(data);
  } catch (e) {
    setOffline(true, 'Запустите PrintFlow: python pf.py');
    PF.emit('live', null);
  }
}
PF.poll = poll;

/* ============================================================== старт */
async function start() {
  routeFromHash();
  try {
    await bootstrap();
  } catch (e) {
    setOffline(true, 'Запустите PrintFlow: python pf.py');
  }
  PF.ready = true;
  PF.emit('ready');
  try { await refreshCore(); } catch (e) { /* офлайн */ }
  try { await refreshFinance(PF.state.financeDays); } catch (e) { /* офлайн */ }
  try { await refreshEvents(); } catch (e) { /* офлайн */ }
  poll();
  resizePolling();
  connectSSE();
  // Периодическая сверка: при живом SSE она нужна редко — события приходят сами.
  setInterval(() => { if (!offline && !sseOk) refreshEvents().catch(() => {}); }, 20000);
  setInterval(() => { if (!offline) refreshCore().catch(() => {}); }, sseOk ? 180000 : 45000);
}
if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
else start();

/* 13.1 (43): экран «панель печатается» — прячем после готовности,
   страховка — 6 секунд (если коннектор молчит). */
function dismissSplash() {
  const splash = $('splash');
  if (!splash) return;
  splash.classList.add('done');
  setTimeout(() => splash.remove(), 520);
}
PF.on('ready', () => { setTimeout(dismissSplash, 260); });
setTimeout(dismissSplash, 6000);

/* 13.1 (28): чип-детект номеров — поиск заказов, документов и текст заявки. */
wireNumberChip($('orders_search'));
wireNumberChip($('doc_search'));
wireNumberChip($('intake_text'));
})();
