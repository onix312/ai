/* NOZZA — общий движок генераторов печатных материалов.
   Подключается на странице генератора после generator.css и qr.js.

   Что делает:
   • Общая панель настроек: город, телефон, Telegram, ссылка QR и вариант знака.
     Значения живут в localStorage (nozza:contact / nozza:logo) и общие для
     всех генераторов — заполнили один раз, подтянулось везде.
   • Поля страницы с data-page="ключ" — состояние конкретного генератора
     (nozza:page:<путь>): любой набранный текст сохраняется и возвращается
     после перезагрузки страницы.
   • Любой ввод мгновенно перерисовывает макет — колбэк draw.
   • Помощники: NZ.esc, NZ.qr, NZ.markSrc, NZ.copy, NZ.download.

   Разметка панели на странице генератора:

     <div class="panel noprint">
       <div class="panel-row">
         <h1><img data-nz-mark src="../assets/brand/nozza-mark-1.svg" alt="">Название</h1>
         <div class="panel-row" data-nz-common="qr"></div>   <- общие поля
         <label>Своё поле <input data-page="mykey"></label>  <- поле страницы
       </div>
     </div>
     ...
     <script src="../assets/qr.js"></script>
     <script src="nz.js"></script>
     <script>NZ.mount(draw);</script>
*/
(() => {
'use strict';

const LS_CONTACT = 'nozza:contact';
const LS_LOGO = 'nozza:logo';

const DEFAULTS = {
  city: 'Симферополь',
  phone: '',
  tg: '@nozza_shop',
  site: 'https://nozza.ru',
  qrbase: 'https://nozza.ru/p/'
};

/* ---------------------------------------------------------------- хранилище */

function readLS(key) {
  try { return JSON.parse(localStorage.getItem(key) || 'null'); } catch (e) { return null; }
}
function writeLS(key, value) {
  try { localStorage.setItem(key, JSON.stringify(value)); } catch (e) { /* приватный режим */ }
}

function contact() { return Object.assign({}, DEFAULTS, readLS(LS_CONTACT) || {}); }
function contactSave(patch) { writeLS(LS_CONTACT, Object.assign(contact(), patch)); }

function logo() {
  let v = '1';
  try { v = localStorage.getItem(LS_LOGO) || '1'; } catch (e) { /* noop */ }
  return /^[1-4]$/.test(v) ? v : '1';
}
function logoSave(id) { try { localStorage.setItem(LS_LOGO, String(id)); } catch (e) { /* noop */ } }
function markSrc(white) { return `../assets/brand/nozza-mark-${logo()}${white ? '-white' : ''}.svg`; }

function pageKey() { return 'nozza:page:' + location.pathname; }
function pageState(defaults) {
  return Object.assign({}, defaults || {}, readLS(pageKey()) || {});
}
function pageStateSave(patch) { writeLS(pageKey(), Object.assign(readLS(pageKey()) || {}, patch)); }

/* ---------------------------------------------------------------- помощники */

const esc = (s) => String(s == null ? '' : s)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');

function qr(link, opts) {
  if (!link || !window.QR) return '';
  try { return QR.svg(link, Object.assign({ ecl: 'M', margin: 1 }, opts || {})); }
  catch (e) { return ''; }
}

function copy(text, btn, okLabel) {
  const done = () => { if (!btn) return; const t = btn.textContent; btn.textContent = okLabel || 'Скопировано ✓'; setTimeout(() => { btn.textContent = t; }, 1600); };
  const fallback = () => {
    const ta = document.createElement('textarea');
    ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0';
    document.body.appendChild(ta); ta.select();
    try { document.execCommand('copy'); done(); } catch (e) { /* noop */ }
    document.body.removeChild(ta);
  };
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(done).catch(fallback);
  } else fallback();
}

function download(name, text, mime) {
  const blob = new Blob([text], { type: mime || 'text/plain;charset=utf-8' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob); a.download = name; a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 1200);
}

function uid(prefix) { return (prefix || 'id') + '-' + Math.random().toString(36).slice(2, 8); }

/* Шаблон — переносит правки конкретного генератора между браузерами.
   Контакты включаем намеренно: на новом компьютере макет сразу остаётся
   пригодным к печати. Импорт ограничен тем же путём страницы. */
function exportTemplate() {
  return {
    format: 'nozza-generator-template', version: 1, path: location.pathname,
    savedAt: new Date().toISOString(), contact: contact(), logo: logo(),
    page: readLS(pageKey()) || {}
  };
}
function importTemplate(data) {
  if (!data || data.format !== 'nozza-generator-template' || data.version !== 1) {
    throw new Error('Это не шаблон генератора NOZZA.');
  }
  if (data.path !== location.pathname) {
    throw new Error('Шаблон создан для другого генератора.');
  }
  if (!data.page || typeof data.page !== 'object') throw new Error('В шаблоне нет данных макета.');
  if (data.contact && typeof data.contact === 'object') contactSave(data.contact);
  if (/^[1-4]$/.test(String(data.logo))) logoSave(data.logo);
  writeLS(pageKey(), data.page);
}

function addTemplateTools() {
  const panel = document.querySelector('.panel');
  if (!panel || panel.querySelector('[data-nz-template-tools]')) return;
  const tools = document.createElement('div');
  tools.className = 'panel-row nz-template-tools';
  tools.setAttribute('data-nz-template-tools', '');
  tools.innerHTML = '<button class="btn" type="button" data-nz-export title="Сохранить текущие поля и контакты в файл">⇩ Сохранить шаблон</button>' +
    '<label class="btn nz-import" title="Загрузить ранее сохранённый шаблон">⇧ Загрузить<input type="file" accept="application/json,.json" data-nz-import></label>' +
    '<button class="btn" type="button" data-nz-clear title="Удалить правки только этого генератора">↺ Очистить макет</button>';
  panel.appendChild(tools);
  tools.querySelector('[data-nz-export]').addEventListener('click', () => {
    const date = new Date().toISOString().slice(0, 10);
    download('nozza-шаблон-' + date + '.json', JSON.stringify(exportTemplate(), null, 2), 'application/json;charset=utf-8');
  });
  tools.querySelector('[data-nz-import]').addEventListener('change', (ev) => {
    const file = ev.target.files && ev.target.files[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = () => {
      try { importTemplate(JSON.parse(String(reader.result || ''))); location.reload(); }
      catch (e) { window.alert(e.message || 'Не удалось загрузить шаблон.'); }
    };
    reader.readAsText(file, 'utf-8');
  });
  tools.querySelector('[data-nz-clear]').addEventListener('click', () => {
    if (!window.confirm('Очистить все сохранённые правки этого макета? Общие контакты останутся.')) return;
    try { localStorage.removeItem(pageKey()); } catch (e) { /* noop */ }
    location.reload();
  });
}

/* ------------------------------------------------------------------- панель */

let drawFn = null, timer = null;
function schedule() {
  clearTimeout(timer);
  timer = setTimeout(() => { if (drawFn) { try { drawFn(); } catch (e) { console.error(e); } } }, 90);
}

function commonFields(el) {
  const withQr = el.getAttribute('data-nz-common') === 'qr';
  el.innerHTML =
    '<label>Город <input type="text" data-nz="city" style="width:110px"></label>' +
    '<label>Телефон <input type="text" data-nz="phone" placeholder="+7 978 000-00-00" style="width:150px"></label>' +
    '<label>Telegram <input type="text" data-nz="tg" style="width:110px"></label>' +
    (withQr ? '<label>Ссылка QR <input type="text" data-nz="qrbase" style="width:200px"></label>' : '') +
    '<label>Знак <select data-nz-logo><option value="1">1 — Слои</option><option value="2">2 — Сопло</option><option value="3">3 — Нить</option><option value="4">4 — Штамп</option></select></label>';
}

function refreshMarks() {
  document.querySelectorAll('img[data-nz-mark]').forEach((im) => {
    im.src = markSrc(im.getAttribute('data-nz-mark') === 'white');
  });
}

function mount(draw) {
  drawFn = draw;

  document.querySelectorAll('[data-nz-common]').forEach(commonFields);
  addTemplateTools();

  const c = contact();
  document.querySelectorAll('[data-nz]').forEach((inp) => {
    const key = inp.getAttribute('data-nz');
    if (c[key] != null && c[key] !== '') inp.value = c[key];
    else if (inp.value === '' && c[key] != null) inp.value = c[key];
    const handler = () => { const patch = {}; patch[key] = inp.value; contactSave(patch); schedule(); };
    inp.addEventListener('input', handler);
    inp.addEventListener('change', handler);
  });

  document.querySelectorAll('[data-nz-logo]').forEach((sel) => {
    sel.value = logo();
    sel.addEventListener('change', () => { logoSave(sel.value); refreshMarks(); schedule(); });
  });

  document.querySelectorAll('[data-nz-print]').forEach((b) => b.addEventListener('click', () => window.print()));

  const st = readLS(pageKey()) || {};
  document.querySelectorAll('[data-page]').forEach((inp) => {
    const key = inp.getAttribute('data-page');
    if (st[key] !== undefined) {
      if (inp.type === 'checkbox') inp.checked = !!st[key];
      else inp.value = st[key];
    }
    const handler = () => {
      const patch = {};
      patch[key] = (inp.type === 'checkbox') ? inp.checked : inp.value;
      pageStateSave(patch); schedule();
    };
    inp.addEventListener('input', handler);
    inp.addEventListener('change', handler);
  });

  refreshMarks();
  if (draw) { try { draw(); } catch (e) { console.error(e); } }
}

window.NZ = { esc, contact, contactSave, logo, logoSave, markSrc, qr, copy, download, uid, pageState, pageStateSave, mount, refreshMarks, schedule };
})();
