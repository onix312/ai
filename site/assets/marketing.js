/* PrintFlow 8.5 — генераторы контента (идеи 20–22, 33, 101–115).
   Все тексты считаются из фактов цеха коннектором; здесь только показ,
   копирование и печать A4-листов. Без сборщика и внешних зависимостей. */
(() => {
'use strict';
const { $, esc, num, nfmt, money, toast, fail } = PF.ui;
const { get, post } = PF.api;

let videoTimer = 0;
let videoFrames = [];
let videoIndex = 0;

function copyText(text, label) {
  const done = () => toast('Скопировано', label || '');
  const bad = () => fail(new Error('Не удалось скопировать — выделите текст вручную'));
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(done, bad);
  } else {
    const ta = document.createElement('textarea');
    ta.value = text;
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand('copy'); done(); } catch (e) { bad(); }
    ta.remove();
  }
}

function printWindow(html, title) {
  const w = window.open('', '_blank', 'width=900,height=1100');
  if (!w) { fail(new Error('Браузер заблокировал всплывающее окно — разрешите его')); return; }
  w.document.write(html);
  w.document.close();
  w.focus();
  setTimeout(() => { try { w.print(); } catch (e) { /* пользователь нажмёт сам */ } }, 500);
}

/* ============================================================ дневник цеха */
async function loadWeek() {
  const el = $('mk_week');
  try {
    const d = await get('/api/content/week', { days: 7 });
    el.textContent = d.text;
    $('mk_week_sub').textContent = `${nfmt(d.numbers.jobs_done)} заданий · ${money(d.numbers.income)}`;
    el.dataset.text = d.text;
    $('mk_week_copy').hidden = !d.text;
  } catch (e) { el.textContent = 'Не загрузилось: ' + e.message; }
}
$('mk_week_copy').addEventListener('click', () => copyText($('mk_week').dataset.text || '', 'Дневник цеха'));

/* ============================================================ соцпакет */
async function loadSocial() {
  const el = $('mk_social');
  try {
    const d = await get('/api/content/social', { days: 30 });
    el.textContent = `ШАПКА: ${d.header}\n\nКВАДРАТ:\n${d.square}\n\nСТОРИС:\n${d.story}`;
    el.dataset.text = el.textContent;
    $('mk_social_sub').textContent = d.period;
  } catch (e) { el.textContent = 'Не загрузилось: ' + e.message; }
}
$('mk_social_copy').addEventListener('click', () => copyText($('mk_social').dataset.text || '', 'Соцпакет'));

/* ============================================================ авито */
let avitoCard = null;
async function loadAvitoItems() {
  const sel = $('mk_avito_item');
  try {
    const d = await get('/api/shelf');
    sel.innerHTML = (d.items || [])
      .filter((i) => num(i.qty) > 0 || num(i.sold_7) > 0)
      .map((i) => `<option value="${esc(i.id)}">${esc(i.name)} · ${money(i.price)} · ${nfmt(i.qty)} шт</option>`)
      .join('') || '<option value="">Стеллаж пуст — добавьте позицию</option>';
  } catch (e) { sel.innerHTML = '<option value="">Ошибки загрузки</option>'; }
}
async function genAvito() {
  const id = $('mk_avito_item').value;
  const el = $('mk_avito');
  if (!id) { el.textContent = 'Сначала добавьте позиции на стеллаже.'; return; }
  try {
    avitoCard = await get('/api/content/avito', { item_id: id });
    el.textContent = `ЗАГОЛОВОК: ${avitoCard.title}\n\nОПИСАНИЕ:\n${avitoCard.description}\n\nКЛЮЧЕВЫЕ СЛОВА: ${avitoCard.keywords}\n\nЦена: ${money(avitoCard.price)}`;
    $('mk_avito_copy').hidden = false;
  } catch (e) { el.textContent = 'Не загрузилось: ' + e.message; avitoCard = null; }
}
$('mk_avito_gen').addEventListener('click', genAvito);
$('mk_avito_copy').addEventListener('click', () => {
  if (!avitoCard) return;
  copyText(`${avitoCard.title}\n\n${avitoCard.description}\n\n${avitoCard.keywords}`, 'Карточка Авито');
});

/* ============================================================ праздники + сезон */
async function loadHoliday() {
  const el = $('mk_holiday');
  try {
    const d = await get('/api/content/holiday');
    const n = d.nearest || {};
    el.innerHTML = d.all.map((h) =>
      `<div class="mk-badge${h.id === 'nearest' ? '' : ''}">${esc(h.name)} — ${esc(h.date)} <b>через ${nfmt(h.days_left)} дн.</b></div>`
    ).join('');
  } catch (e) { el.textContent = '—'; }
  try {
    const s = await get('/api/content/season');
    const max = Math.max(1, ...s.months.map((m) => num(m.orders)));
    $('mk_season').innerHTML =
      `<div class="muted" style="font-size:12px;margin-bottom:6px">Индекс спроса: заказы по месяцам ${s.peak ? `· пик — ${esc(s.peak.name)}` : ''}</div>`
      + s.months.map((m) =>
        `<div class="mk-season-row"><span class="n">${esc(m.name)}</span>`
        + `<span class="bar"><i style="width:${Math.round(num(m.orders) / max * 100)}%"></i></span>`
        + `<b>${nfmt(m.orders)}</b></div>`).join('');
  } catch (e) { $('mk_season').innerHTML = ''; }
}

/* ============================================================ отчёт + промо */
async function loadReport() {
  const el = $('mk_report');
  try {
    const d = await get('/api/content/report', { days: 30 });
    el.textContent = `Выручка: ${money(d.income)} · Прибыль: ${money(d.profit)} (${nfmt(d.margin)}%)\n`
      + `Печать: ${nfmt(d.print_hours)} ч, ${nfmt(d.grams)} г · Заданий: ${nfmt(d.jobs_done)} (брак ${nfmt(d.failure_rate)}%)\n`
      + `Новых клиентов: ${nfmt(d.customers_new)} · Прибыль/ч печати: ${money(d.profit_per_print_hour)}/ч`;
    $('mk_report_sub').textContent = `за ${d.period_days} дней`;
  } catch (e) { el.textContent = 'Не загрузилось: ' + e.message; }
}
$('mk_report_open').addEventListener('click', async () => {
  try {
    const d = await get('/api/content/report/print', { days: 30 });
    printWindow(d.html, 'Цеховой отчёт');
  } catch (e) { fail(e); }
});

async function loadPromo() {
  const el = $('mk_promo');
  try {
    const d = await get('/api/content/promo');
    if (!d.cards || !d.cards.length) { el.innerHTML = '<span class="muted">Сейчас активных праздников нет — карточки появятся по календарю.</span>'; return; }
    el.innerHTML = d.cards.map((c) =>
      `<div style="border:1px solid var(--line);border-left:3px solid #f97316;border-radius:10px;padding:10px 12px;margin-bottom:8px">`
      + `<b>${esc(c.name)}</b> · ${esc(c.date)} <span class="muted">через ${nfmt(c.days_left)} дн.</span><br>`
      + `<small style="color:var(--muted)">${esc(c.hint || '')}</small><br><br>${esc(c.text)}</div>`
    ).join('');
  } catch (e) { el.textContent = '—'; }
}

/* ============================================================ видео недели */
function stopVideo() {
  if (videoTimer) { clearInterval(videoTimer); videoTimer = 0; }
  $('mk_video_play').hidden = true;
  $('mk_video_stop').hidden = true;
}
async function loadWeekVideo() {
  const empty = $('mk_video_empty'), box = $('mk_video');
  try {
    const d = await get('/api/content/week-video', { days: 7 });
    videoFrames = d.frames || [];
    if (!videoFrames.length) {
      empty.innerHTML = '<span>За неделю кадров нет. Включите кейфреймы в Настройках → «8.5 — умный цех» (мин. 0,5 мин) — и видео соберётся само.</span>';
      return;
    }
    empty.hidden = true;
    box.hidden = false;
    videoIndex = 0;
    showFrame();
    $('mk_video_play').hidden = false;
  } catch (e) { empty.innerHTML = `<span>${esc(e.message)}</span>`; }
}
function showFrame() {
  const f = videoFrames[videoIndex];
  if (!f) return;
  $('mk_video_img').src = f.url;
  const t = f.file.replace(/^\d{4}(\d{2})(\d{2})-(\d{2})(\d{2})(\d{2})\.jpg$/, '$2.$3 $4:$5:$6');
  $('mk_video_cap').textContent = `${f.job || 'Печать'} · кадр ${videoIndex + 1}/${videoFrames.length} · ${t}`;
}
$('mk_video_play').addEventListener('click', () => {
  if (videoTimer) return stopVideo();
  stopVideo();
  videoTimer = setInterval(() => {
    videoIndex = (videoIndex + 1) % videoFrames.length;
    showFrame();
  }, 1500);
  $('mk_video_play').hidden = true;
  $('mk_video_stop').hidden = false;
});
$('mk_video_stop').addEventListener('click', stopVideo);

/* ============================================================ карта печатей */
async function loadPrintMap() {
  const el = $('mk_map');
  try {
    const d = await get('/api/content/print-map');
    if (!d.cells || !d.cells.length) {
      el.innerHTML = '<span class="muted">Пока нет завершённых печатей — точки появятся после первого задания.</span>';
      return;
    }
    const grid = Array.from({ length: 7 }, () => Array.from({ length: 53 }, () => 0));
    d.cells.forEach((c) => { if (c.week >= 0 && c.week < 53) grid[c.weekday][c.week] = c.count; });
    let html = '<div class="mk-grid53" title="Строки — дни недели (пн…вс), столбцы — недели">';
    for (let w = 0; w < 53; w++) {
      for (let wd = 0; wd < 7; wd++) {
        const v = grid[wd][w] || 0;
        const lvl = v ? Math.min(4, 1 + Math.ceil(v / Math.max(1, d.max_day) * 3)) : 0;
        html += `<i class="l${lvl}" title="${w} нед., ${v} печ."></i>`;
      }
    }
    el.innerHTML = html + `</div><div class="muted" style="margin-top:6px;font-size:12px">${nfmt(d.total)} завершённых печатей · ${d.year}</div>`;
  } catch (e) { el.textContent = '—'; }
}

/* ============================================================ шапка полки */
async function loadShelfHead() {
  const el = $('mk_shelfhead');
  try {
    const d = await get('/api/content/shelf-header', { days: 7 });
    el.textContent = d.text;
    el.dataset.text = d.text;
    $('mk_shelfhead_sub').textContent = d.top && d.top.length ? d.top.map((t) => t.name).join(', ') : 'за 7 дней';
  } catch (e) { el.textContent = '—'; }
}
$('mk_shelfhead_copy').addEventListener('click', () => copyText($('mk_shelfhead').dataset.text || '', 'Шапка полки'));

/* ============================================================ штрихкод */
$('mk_bc_gen').addEventListener('click', async () => {
  const out = $('mk_bc_out');
  const text = ($('mk_bc_text').value || '').trim();
  if (!text) { out.innerHTML = '<span class="muted">Введите текст.</span>'; return; }
  try {
    const d = await get('/api/labels/code128', { text });
    out.innerHTML = `${d.svg}<div class="muted" style="font-size:11.5px;margin-top:6px">Code 128 ${d.mode} · ${d.symbols} символов · контроль ок ✓ — вставьте в лист ценников</div>`;
  } catch (e) { out.innerHTML = `<span style="color:#ef4444">${esc(e.message)}</span>`; }
});

/* ============================================================ печать A4 */
$('mk_print_stickers').addEventListener('click', async () => {
  try {
    const d = await get('/api/content/stickers', { kind: 'all' });
    printWindow(d.html, 'Стикеры NOZZA');
  } catch (e) { fail(e); }
});

async function loadBusinessCustomers() {
  const sel = document.createElement('select');
  sel.id = 'mk_business_cust';
  sel.className = 'field';
  sel.style.minWidth = '200px';
  try {
    sel.innerHTML = '<option value="">Визитка цеха (без клиента)</option>'
      + PF.state.customers.map((c) => `<option value="${esc(c.id)}">${esc(c.name || 'Клиент')}</option>`).join('');
  } catch (e) { /* state может быть пуст */ }
  const btn = $('mk_print_business');
  btn.after(sel);
  btn.addEventListener('click', async () => {
    try {
      const d = await get('/api/content/business-card', { customer_id: sel.value });
      printWindow(d.html, 'Визитки NOZZA');
    } catch (e) { fail(e); }
  });
}

const LABELS = {
  danger: { t: 'ОСТОРОЖНО', s: 'Рядом работает 3D-принтер. Не трогайте станок во время печати.', c: '#ef4444' },
  wash: { t: 'Мойте руки', s: 'После работы с пластиком и присыпкой. Пластик не для еды.', c: '#0ea5e9' },
  delivery: { t: 'Печатаем за 1–3 дня', s: 'Заказали — напечатали — заберите. Статус заказа — по QR на упаковке.', c: '#22c55e' },
  gift: { t: 'Подарки к празднику', s: 'Адресники, таблички, QR-стойки с вашим текстом. Закажите заранее.', c: '#f97316' },
};
function labelSheet(kind, note) {
  const L = LABELS[kind] || LABELS.danger;
  const card = `<div style="border:0.6mm dashed #9ca3af;border-radius:3mm;padding:8mm 10mm;width:80mm;height:60mm;box-sizing:border-box">
    <div style="font-size:20pt;font-weight:800;color:${L.c}">${esc(L.t)}</div>
    <div style="font-size:11pt;color:#374151;margin-top:6mm">${esc(L.s)}</div>
    ${note ? `<div style="font-size:10pt;color:#6b7280;margin-top:4mm">${esc(note)}</div>` : ''}
    <div style="margin-top:8mm;font-size:9pt;color:#9ca3af">цех NOZZA · локальная 3D-печать</div>
  </div>`;
  return `<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8"><title>Табличка</title>
  <style>@page{size:A4;margin:10mm}body{font-family:Arial,sans-serif;margin:0}
  .grid{display:grid;grid-template-columns:1fr 1fr;gap:10mm}</style></head>
  <body><div class="grid">${card}${card}${card}${card}</div></body></html>`;
}
$('mk_label_one').addEventListener('click', () => {
  printWindow(labelSheet($('mk_labels_kind').value, ($('mk_labels_note').value || '').trim()), 'Табличка цеха');
});
$('mk_print_labels').addEventListener('click', () => {
  const all = Object.keys(LABELS).map((k) => labelSheet(k, '')).join('');
  printWindow(`<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8"><title>Таблички цеха</title>
  <style>@page{size:A4;margin:10mm}body{font-family:Arial,sans-serif;margin:0}
  .grid{display:grid;grid-template-columns:1fr 1fr;gap:10mm}
  .page{display:grid;grid-template-columns:1fr 1fr;gap:10mm;page-break-after:always}</style></head>
  <body>${all}</body></html>`, 'Все таблички цеха');
});

/* ============================================================ старт */
PF.on('ready', () => { loadBusinessCustomers(); });
PF.on('view', (d) => {
  if (d.view !== 'marketing') return;
  stopVideo();
  loadWeek(); loadSocial(); loadAvitoItems(); loadHoliday();
  loadReport(); loadPromo(); loadWeekVideo(); loadPrintMap(); loadShelfHead();
});
})();
