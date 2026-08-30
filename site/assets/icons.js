/* PrintFlow 11.1 — единый набор иконок (идея Н1).

  Панель использует текстовые глифы (◈ ≡ ₽) и эмодзи только потому, что
  внешние библиотеки запрещены. Здесь — свой мини-набор inline-SVG:
  24×24, обводка currentColor, без шрифтов и сети. Иконки наследуют цвет
  и размер контекста, поэтому работают в светлой/тёмной теме и режиме цеха.

  Разметка не ломается без этого файла: в элементе остаётся прежний глиф,
  data-icon лишь заменяет его на SVG. Динамические модули могут звать
  PFIcons.svg('name') или PFIcons.apply(root) для своих кнопок.
*/
(() => {
'use strict';

/* Каждая иконка — внутренности <svg viewBox="0 0 24 24">.
   Стиль общий: stroke currentColor, но отдельные элементы могут
   переопределить fill/stroke локально (точки, заливки). */
const ICONS = {
  /* --- навигация --- */
  dashboard: '<rect x="3" y="3" width="8" height="8" rx="2"/><rect x="13" y="3" width="8" height="5" rx="2"/><rect x="13" y="10" width="8" height="11" rx="2"/><rect x="3" y="13" width="8" height="8" rx="2"/>',
  printer: '<path d="M6.5 9V3.5h11V9"/><rect x="3" y="9" width="18" height="8" rx="2"/><path d="M6.5 14h11v6.5h-11z"/>',
  queue: '<path d="M8.5 6h12M8.5 12h12M8.5 18h12"/><circle cx="3.7" cy="6" r="1.1" fill="currentColor" stroke="none"/><circle cx="3.7" cy="12" r="1.1" fill="currentColor" stroke="none"/><circle cx="3.7" cy="18" r="1.1" fill="currentColor" stroke="none"/>',
  kanban: '<rect x="3" y="3" width="5.5" height="18" rx="1.6"/><rect x="9.75" y="3" width="5.5" height="12" rx="1.6"/><rect x="16.5" y="3" width="4.5" height="8" rx="1.6"/>',
  box: '<path d="M21 8l-9-5-9 5v8l9 5 9-5z"/><path d="M3 8l9 5 9-5"/><path d="M12 13v8"/>',
  ruble: '<path d="M8.5 20.5V3.5h4.8a5 5 0 0 1 0 10H8.5"/><path d="M5.5 13.5h9"/>',
  calculator: '<rect x="5" y="3" width="14" height="18" rx="2"/><path d="M8.5 7h7"/><path d="M8.5 12h.01M12 12h.01M15.5 12h.01M8.5 16h.01M12 16h.01M15.5 16h.01"/>',
  users: '<circle cx="9" cy="8" r="3.2"/><path d="M3.6 19.5c.6-3.1 2.8-4.7 5.4-4.7s4.8 1.6 5.4 4.7"/><path d="M15.4 5.3a3.2 3.2 0 0 1 0 6"/><path d="M16.8 14.7c2.2.5 3.4 2.1 3.8 4.5"/>',
  target: '<circle cx="12" cy="12" r="8.5"/><circle cx="12" cy="12" r="4.4"/><circle cx="12" cy="12" r="1" fill="currentColor" stroke="none"/>',
  pen: '<path d="M4 20l4.6-1L20 7.6a2.1 2.1 0 0 0-3-3L5.6 16z"/><path d="M13.6 6.9l3 3"/>',
  hexagon: '<path d="M12 3l7.8 4.5v9L12 21l-7.8-4.5v-9z"/><path d="M12 8.2v4.4"/><circle cx="12" cy="15.6" r=".9" fill="currentColor" stroke="none"/>',
  send: '<path d="M21 3L10.5 13.5"/><path d="M21 3l-6.8 18-3.7-7.5L3 9.8z"/>',
  book: '<path d="M5 4.5A1.5 1.5 0 0 1 6.5 3H20v15H6.5A1.5 1.5 0 0 0 5 19.5z"/><path d="M5 19.5A1.5 1.5 0 0 1 6.5 18H20v3H6.5A1.5 1.5 0 0 1 5 19.5z"/>',
  sliders: '<path d="M4 7h9M17.5 7H20M4 17h4M12.5 17H20"/><circle cx="15" cy="7" r="2.1"/><circle cx="10" cy="17" r="2.1"/>',

  /* --- действия --- */
  menu: '<path d="M4 6h16M4 12h16M4 18h16"/>',
  search: '<circle cx="11" cy="11" r="6.5"/><path d="M20 20l-4.4-4.4"/>',
  theme: '<circle cx="12" cy="12" r="8.5"/><path d="M12 3.5a8.5 8.5 0 0 1 0 17z" fill="currentColor" stroke="none"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  layout: '<rect x="3" y="3" width="18" height="18" rx="2"/><path d="M3 9h18M9 9v12"/>',
  refresh: '<path d="M20 12a8 8 0 1 1-2.4-5.7"/><path d="M20 3v4.5h-4.5"/>',
  pause: '<path d="M9 5v14M15 5v14"/>',
  play: '<path d="M7.5 4.5l12 7.5-12 7.5z"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2.6V5M12 19v2.4M2.6 12H5M19 12h2.4M5.2 5.2l1.8 1.8M17 17l1.8 1.8M18.8 5.2L17 7M7 17l-1.8 1.8"/>',
  film: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M7.5 4v16M16.5 4v16M3 9h4.5M3 15h4.5M16.5 9H21M16.5 15H21"/>',
  bed: '<rect x="3" y="3" width="18" height="18" rx="2"/><path d="M7.5 7.5h9v9h-9z" stroke-dasharray="3 3"/>',
  stop: '<rect x="6" y="6" width="12" height="12" rx="1.6"/>',
  download: '<path d="M12 4v11M7 11l5 5 5-5"/><path d="M5 20h14"/>',
  upload: '<path d="M12 20V9M7 13l5-5 5 5"/><path d="M5 4h14"/>',
  close: '<path d="M6 6l12 12M18 6L6 18"/>',
  check: '<path d="M5 13l4.5 4.5L19 7"/>',

  /* --- библиотека и материалы --- */
  rocket: '<path d="M12 2.5c2.9 2 4.3 4.8 4.3 8.1L14.2 15H9.8L7.7 10.6c0-3.3 1.4-6.1 4.3-8.1z"/><circle cx="12" cy="9" r="1.7"/><path d="M9.8 15l-2.4 2.6.5 3.4 2.6-2.2M14.2 15l2.4 2.6-.5 3.4-2.6-2.2"/>',
  chart: '<path d="M4 20h16"/><rect x="5.5" y="12" width="3" height="6" rx="1"/><rect x="10.5" y="8" width="3" height="10" rx="1"/><rect x="15.5" y="4.5" width="3" height="13.5" rx="1"/>',
  shapes: '<circle cx="8" cy="8" r="4.5"/><rect x="11.5" y="11.5" width="9" height="9" rx="2"/>',
  wrench: '<path d="M20.7 6a5.3 5.3 0 0 1-7 6.6l-6.6 6.6a2.1 2.1 0 0 1-3-3l6.6-6.6A5.3 5.3 0 0 1 17.3 2L14 5.3l.6 3.2 3.2.6z"/>',
  briefcase: '<rect x="3" y="7" width="18" height="13" rx="2"/><path d="M9 7V5a2 2 0 0 1 2-2h2a2 2 0 0 1 2 2v2M3 12.5h18"/>',
  phone: '<rect x="7" y="2.5" width="10" height="19" rx="2.5"/><path d="M11 18.5h2"/>',
  clapper: '<rect x="3" y="10" width="18" height="10.5" rx="2"/><path d="M3.4 9.4L5.2 4l15.4 3.6-1.2 4.4"/><path d="M8.2 12.3l1-2.9M13.2 13.4l1-2.9"/>',
  bank: '<path d="M3 10l9-6.5L21 10"/><path d="M5.5 10v7M10 10v7M14 10v7M18.5 10v7"/><path d="M3.5 20.5h17"/>',
  scale: '<path d="M12 3.5v17M8 20.5h8"/><path d="M4.5 6.5h15"/><path d="M4.5 6.5L2 12a3 3 0 0 0 5 0zM19.5 6.5L22 12a3 3 0 0 1-5 0z"/>',
  page: '<path d="M6 2.5h8l4 4v15H6z"/><path d="M14 2.5v4h4"/>',
  mail: '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="M3 7.5l9 6 9-6"/>',
  moon: '<path d="M20 14.5A8.5 8.5 0 1 1 9.5 4a7 7 0 0 0 10.5 10.5z"/>',
  robot: '<rect x="5" y="8" width="14" height="11" rx="2.5"/><path d="M12 8V5.2"/><circle cx="12" cy="4" r="1.1" fill="currentColor" stroke="none"/><circle cx="9" cy="13" r="1.1" fill="currentColor" stroke="none"/><circle cx="15" cy="13" r="1.1" fill="currentColor" stroke="none"/><path d="M9.5 16.3h5"/>',
  antenna: '<path d="M12 20.5V13"/><path d="M8.5 20.5h7"/><circle cx="12" cy="11.5" r="1.4"/><path d="M8.8 8.9a4.6 4.6 0 0 1 6.4 0"/><path d="M6.3 6.4a8.1 8.1 0 0 1 11.4 0"/>',
  spool: '<circle cx="12" cy="12" r="8.5"/><circle cx="12" cy="12" r="4.6"/><circle cx="12" cy="12" r="1.2" fill="currentColor" stroke="none"/>',
  bag: '<path d="M6 8h12l1.1 12.5H4.9z"/><path d="M9 8V6a3 3 0 0 1 6 0v2"/>',
  sign: '<path d="M4 4.5h16V15H4z"/><path d="M9 15v5.5M6 20.5h6M15 15l3.5 5.5"/>',
  flask: '<path d="M10 2.5h4"/><path d="M10 2.5v6.2l-5.6 9a2 2 0 0 0 1.7 3h11.8a2 2 0 0 0 1.7-3L14 8.7V2.5"/><path d="M7.4 15.5h9.2"/>',
  help: '<circle cx="12" cy="12" r="8.5"/><path d="M9.6 9.3a2.5 2.5 0 1 1 3.4 2.4c-.7.3-1 .8-1 1.6v.3"/><circle cx="12" cy="16.8" r=".9" fill="currentColor" stroke="none"/>',
  camera: '<path d="M4 8h3.2l2-2.5h5.6l2 2.5H20A1.5 1.5 0 0 1 21.5 9.5V18a1.5 1.5 0 0 1-1.5 1.5H4A1.5 1.5 0 0 1 2.5 18V9.5A1.5 1.5 0 0 1 4 8z"/><circle cx="12" cy="13.5" r="3.6"/>',

  /* --- 12.2: принтеры, заказы и Telegram (О1) --- */
  telegram: '<circle cx="12" cy="12" r="9"/><path d="M16.9 8.2l-8.3 3.2c-.6.2-.6 1 0 1.2l2 .7 3.4-2.6c.2-.1.4.1.2.3l-2.6 2.4v2c.3 0 .6-.1.8-.3l.9-.9 2.1 1.6c.4.3.8.1.9-.4l1.6-6.4c.1-.5-.4-.9-1-.8z"/>',
  bolt: '<path d="M13 2.5L5.5 13.5H11l-1 8L18.5 10.5H13z"/>',
  cancel: '<circle cx="12" cy="12" r="8.6"/><path d="M7.8 16.2l8.4-8.4"/>',
  message: '<path d="M4.5 4.5h15A1.5 1.5 0 0 1 21 6v8.5a1.5 1.5 0 0 1-1.5 1.5H12l-5 4v-4H4.5A1.5 1.5 0 0 1 3 14.5V6a1.5 1.5 0 0 1 1.5-1.5z"/>',
  star: '<path d="M12 3.6l2.5 5.2 5.7.8-4.1 4 1 5.7-5.1-2.7-5.1 2.7 1-5.7-4.1-4 5.7-.8z"/>',
  drop: '<path d="M12 3.2c3.4 4 6 6.9 6 10a6 6 0 0 1-12 0c0-3.1 2.6-6 6-10z"/><path d="M9.3 14.4a2.9 2.9 0 0 0 2.2 2.4"/>',
  thermo: '<path d="M14 14.6V5.2a2 2 0 1 0-4 0v9.4a4.2 4.2 0 1 0 4 0z"/><circle cx="12" cy="17.6" r="1.5" fill="currentColor" stroke="none"/>',
  wind: '<path d="M3 8h9.5a2.6 2.6 0 1 0-2.6-2.7"/><path d="M3 12h13a2.8 2.8 0 1 1-2.8 2.9"/><path d="M3 16h6.2a2.2 2.2 0 1 1-2.2 2.2"/>',
  wifi: '<path d="M2.8 9a13 13 0 0 1 18.4 0"/><path d="M6 12.3a8.5 8.5 0 0 1 12 0"/><path d="M9.2 15.5a4 4 0 0 1 5.6 0"/><circle cx="12" cy="19" r="1.2" fill="currentColor" stroke="none"/>',
  shield: '<path d="M12 2.8l7 2.7v5.7c0 4.6-3 7.8-7 9.6-4-1.8-7-5-7-9.6V5.5z"/><path d="M8.8 11.8l2.2 2.2 4.2-4.6"/>',
  timer: '<circle cx="12" cy="13.5" r="7.5"/><path d="M12 9.6v4.1l2.6 2.2"/><path d="M9.5 2.5h5M12 2.5v3.5"/>',
  cube: '<path d="M12 2.8l8 4.4v9.6l-8 4.4-8-4.4V7.2z"/><path d="M4 7.2l8 4.4 8-4.4"/><path d="M12 11.6v9.6"/>',
  image: '<rect x="3" y="4.5" width="18" height="15" rx="2.2"/><circle cx="8.5" cy="10" r="1.7"/><path d="M4 17l4.8-4.6 3.6 3.4 3.3-3 4.3 4.2"/>',
  link: '<path d="M9.5 14.5l5-5"/><path d="M12.8 6.7l1.7-1.7a4.3 4.3 0 0 1 6 6L18.8 13"/><path d="M11.2 17.3l-1.7 1.7a4.3 4.3 0 0 1-6-6L5.2 11"/>',
};

/** SVG-строка иконки; неизвестное имя — пустая строка (глиф остаётся). */
function svg(name, size) {
  const body = Object.prototype.hasOwnProperty.call(ICONS, name) ? ICONS[name] : '';
  if (!body) return '';
  const s = Number(size) > 0 ? Number(size) : 0;
  const dims = s ? ` width="${s}" height="${s}"` : '';
  return `<svg viewBox="0 0 24 24"${dims} fill="none" stroke="currentColor" `
    + `stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" `
    + `aria-hidden="true" focusable="false">${body}</svg>`;
}

/** Заменить глифы на SVG в элементах с data-icon (повторно — идемпотентно). */
function apply(root) {
  const scope = root || document;
  scope.querySelectorAll('[data-icon]').forEach((el) => {
    const name = el.getAttribute('data-icon') || '';
    const body = svg(name, el.getAttribute('data-icon-size'));
    if (!body || el.dataset.iconDone === '1') return;
    el.dataset.iconDone = '1';
    el.innerHTML = body;
  });
}

window.PFIcons = {
  svg,
  apply,
  has: (name) => Object.prototype.hasOwnProperty.call(ICONS, name),
  names: () => Object.keys(ICONS),
};

/* Модулям удобно звать иконку из общего PF.ui. */
if (window.PF && window.PF.ui) window.PF.ui.icon = svg;

apply();
})();
