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
const debounce = (fn, ms = 260) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };

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

async function api(path, options) {
  const opts = Object.assign({ headers: {} }, options || {});
  if (opts.body && !(opts.body instanceof FormData)) {
    opts.method = opts.method || 'POST';
    opts.headers['Content-Type'] = 'application/json';
    if (typeof opts.body !== 'string') opts.body = JSON.stringify(opts.body);
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
  if (!res.ok) throw new Error(data.error || `Ошибка ${res.status}`);
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
function toast(title, sub, kind = 'ok') {
  const box = $('toasts');
  const el = document.createElement('div');
  el.className = 'toast ' + kind;
  el.innerHTML = `<span class="ic">${ICONS[kind] || ICONS.info}</span><span><b>${esc(title)}</b>${sub ? `<small>${esc(sub)}</small>` : ''}</span>`;
  box.appendChild(el);
  setTimeout(() => { el.classList.add('out'); setTimeout(() => el.remove(), 260); }, kind === 'bad' ? 5200 : 3200);
}
const fail = (e) => toast('Не получилось', e && e.message ? e.message : String(e), 'bad');

/* =========================================================== диалоги */
function openModal(id) { const d = $(id); if (d && !d.open) d.showModal(); }
function closeModal(id) { const d = $(id); if (d && d.open) d.close(); }
document.addEventListener('click', (e) => {
  const btn = e.target.closest('[data-close]');
  if (btn) { e.preventDefault(); closeModal(btn.dataset.close); }
});

/** Подтверждение опасного действия. */
function confirmDanger(text) { return window.confirm(text); }

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
    dateText, dateTimeText, agoText, todayISO, initials, debounce,
    toast, fail, openModal, closeModal, confirmDanger, CUR, store, catName,
    setChannelBar,
  },
  modules: {},
  bus: new EventTarget(),
};
window.PF = PF;
PF.emit = (name, detail) => PF.bus.dispatchEvent(new CustomEvent(name, { detail }));
PF.on = (name, fn) => PF.bus.addEventListener(name, (e) => fn(e.detail));

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
    v.hidden = !active;
  });
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
  store.set('pf_last_view', name);
  if (STOCK_IDS.has(name)) store.set('pf_last_stock', name);
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
function applyDensity() {
  const on = store.get('pf_density', '') === 'shop';
  document.documentElement.dataset.density = on ? 'shop' : 'desk';
  const btn = $('density_btn');
  if (btn) {
    btn.classList.toggle('on', on);
    btn.title = on ? 'Обычный вид (сейчас режим цеха)' : 'Режим цеха: крупные кнопки, меньше воздуха';
  }
}
applyDensity();
const densBtn = $('density_btn');
if (densBtn) densBtn.addEventListener('click', () => {
  const next = store.get('pf_density', '') === 'shop' ? '' : 'shop';
  store.set('pf_density', next);
  applyDensity();
  toast(next === 'shop' ? 'Режим цеха' : 'Обычный вид', next === 'shop' ? 'Крупные кнопки у станка' : 'Больше данных на экране');
});

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
      icon: { order: '▦', customer: '◎', spool: '◍', printer: '◉' }[r.type] || '•',
      title: r.title, sub: r.subtitle,
      run: () => {
        if (r.type === 'order') { PF.go('orders'); PF.modules.ops && PF.modules.ops.openOrder(r.id); }
        else if (r.type === 'customer') PF.go('customers');
        else if (r.type === 'spool') { PF.go('inventory'); PF.modules.money && PF.modules.money.openSpool(r.id); }
        else if (r.type === 'printer') { PF.state.activePrinter = r.id; PF.go('printers'); }
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
  { id: 'products', label: 'Товары' },
  { id: 'batches', label: 'Партии' },
  { id: 'documents', label: 'Документы' },
  { id: 'warehouses', label: 'Склады' },
  { id: 'shelf', label: 'Стеллаж' },
  { id: 'inventory', label: 'Пластик' },
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
    `<button type="button" data-view="${t.id}" class="${t.id === name ? 'on' : ''}">${t.label}</button>`).join('');
  bar.addEventListener('click', (e) => {
    const btn = e.target.closest('button[data-view]');
    if (btn) PF.go(btn.dataset.view);
  });
  const head = view.querySelector('.view-head');
  if (head && head.nextSibling) view.insertBefore(bar, head.nextSibling);
  else view.prepend(bar);
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
    const more = g.querySelector('details.nav-more');
    if (more && s && hit) more.open = true;
  });
  if (empty) empty.hidden = !s || shown > 0;
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
  PF.state.settings = data.settings || {};
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
    get('/api/nomenclature', { kind: 'product' }),
  ]);
  PF.state.orders = orders.orders || [];
  PF.state.customers = customers.customers || [];
  PF.state.spools = spools.spools || [];
  PF.state.catalog = catalog.catalog || [];
  PF.state.jobs = jobs || { queue: [], history: [] };
  PF.state.nomenclature = nomenclature.items || [];
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
})();
