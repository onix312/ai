/* PrintFlow 9.2 — контент-студия.
   Генераторы не «додумывают» цифры: всё, что приходит с сервера, построено
   на локальных фактах цеха. Пользователь может отредактировать текст до
   публикации, но исходник всегда можно вернуть одной кнопкой. */
(() => {
'use strict';

const { $, esc, num, nfmt, money, toast, fail, store, todayISO, debounce } = PF.ui;
const { get } = PF.api;
const PERIODS = [7, 30, 90];
let contentDays = Number(store.get('pf.content.days', '7'));
if (!PERIODS.includes(contentDays)) contentDays = 7;
let currentPane = store.get('pf.content.pane', 'today') || 'today';
if (!['today', 'sales', 'proof', 'print'].includes(currentPane)) currentPane = 'today';
let loadId = 0;
let videoTimer = 0;
let videoFrames = [];
let videoIndex = 0;
let avitoCard = null;
let promoCards = [];

/* ============================================================ мелочи */
function textValue(id) {
  const el = $(id);
  if (!el) return '';
  return 'value' in el ? el.value : el.textContent;
}

function setText(id, text) {
  const el = $(id);
  if (el) el.textContent = text == null ? '' : String(text);
}

/** Не затираем правку, сделанную в студии. Последняя версия генератора всё
    равно запоминается: «↺» вернёт её, а не старый текст. */
function setGeneratedEditor(id, text, force) {
  const el = $(id);
  if (!el) return;
  const value = String(text || '');
  el.dataset.generated = value;
  if (!force && el.dataset.dirty === '1') return;
  if ('value' in el) el.value = value;
  else el.textContent = value;
  el.dataset.dirty = '0';
}

function setEditorError(id, error) {
  const el = $(id);
  if (!el || el.dataset.dirty === '1') return;
  const value = `Не загрузилось: ${error && error.message ? error.message : String(error)}`;
  if ('value' in el) el.value = value;
  else el.textContent = value;
}

function resetEditor(id, label) {
  const el = $(id);
  if (!el || !el.dataset.generated) return;
  if ('value' in el) el.value = el.dataset.generated;
  else el.textContent = el.dataset.generated;
  el.dataset.dirty = '0';
  toast('Вернули текст генератора', label || 'можно снова редактировать');
}

async function copyText(text, label) {
  const value = String(text || '').trim();
  if (!value) return toast('Пока нечего копировать', 'Сначала дождитесь генерации', 'warn');
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(value);
    } else {
      const area = document.createElement('textarea');
      area.value = value;
      area.setAttribute('readonly', '');
      area.style.cssText = 'position:fixed;opacity:0;pointer-events:none';
      document.body.appendChild(area);
      area.select();
      const ok = document.execCommand('copy');
      area.remove();
      if (!ok) throw new Error('Браузер не дал доступ к буферу');
    }
    toast('Скопировано', label || 'текст можно вставить в канал');
  } catch (error) {
    fail(new Error('Не удалось скопировать — выделите текст вручную'));
  }
}

function downloadText(text, name) {
  const value = String(text || '').trim();
  if (!value) return toast('Пока нечего скачивать', 'Сначала дождитесь генерации', 'warn');
  const blob = new Blob([value], { type: 'text/plain;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = name;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 0);
  toast('Файл готов', name);
}

function printWindow(html, title) {
  const popup = window.open('', '_blank', 'width=900,height=1100');
  if (!popup) {
    fail(new Error('Браузер заблокировал окно печати — разрешите всплывающие окна для PrintFlow'));
    return;
  }
  const source = /<html[\s>]/i.test(html)
    ? html
    : `<!doctype html><html lang="ru"><head><meta charset="utf-8"><title>${esc(title || 'PrintFlow')}</title></head><body>${html}</body></html>`;
  popup.document.write(source);
  popup.document.close();
  popup.focus();
  setTimeout(() => { try { popup.print(); } catch (e) { /* пользователь нажмёт Печать сам */ } }, 450);
}

function ruPeriod(days) {
  return days === 1 ? '1 день' : `${days} ${days < 5 ? 'дня' : 'дней'}`;
}

function getResult(result) {
  return result && result.status === 'fulfilled' ? result.value : null;
}

function setLoading(isLoading) {
  const view = $('view-marketing');
  if (view) view.setAttribute('aria-busy', isLoading ? 'true' : 'false');
  const btn = $('mk_refresh');
  if (btn) {
    btn.disabled = isLoading;
    btn.textContent = isLoading ? 'Обновляем…' : '↻ Обновить';
  }
}

/* ====================================================== tab interface */
function activatePane(name, persist = true) {
  if (!['today', 'sales', 'proof', 'print'].includes(name)) name = 'today';
  currentPane = name;
  document.querySelectorAll('[data-mk-pane]').forEach((button) => {
    const on = button.dataset.mkPane === name;
    button.classList.toggle('on', on);
    button.setAttribute('aria-selected', on ? 'true' : 'false');
  });
  document.querySelectorAll('.mk-pane').forEach((pane) => {
    const on = pane.id === `mkpane-${name}`;
    pane.classList.toggle('on', on);
    pane.hidden = !on;
  });
  if (persist) store.set('pf.content.pane', name);
}

function syncPeriod() {
  document.querySelectorAll('#mk_period [data-days]').forEach((button) => {
    button.classList.toggle('on', Number(button.dataset.days) === contentDays);
  });
}

/* ======================================================= rendering */
function renderWeek(data) {
  if (!data) return;
  const numbers = data.numbers || {};
  setGeneratedEditor('mk_week', data.text || 'Пока нет фактов для истории цеха.');
  setText('mk_week_sub', `${nfmt(numbers.jobs_done)} заданий · ${money(numbers.income)} · ${ruPeriod(data.period_days || contentDays)}`);
  setText('mk_week_meta', `Факты за ${ruPeriod(data.period_days || contentDays)} · отредактируйте тон перед публикацией`);
  // 13.1 (61): превью в Telegram обновляется вместе с генератором
  renderTgPreview(textValue('mk_week'));
  renderBoard();
}

function renderSocial(data) {
  if (!data) return;
  const text = `ШАПКА\n${data.header || '—'}\n\nКВАДРАТ\n${data.square || '—'}\n\nСТОРИС\n${data.story || '—'}`;
  setGeneratedEditor('mk_social', text);
  setText('mk_social_sub', data.period || `за ${ruPeriod(contentDays)}`);
}

function renderShelfHeader(data) {
  if (!data) return;
  setGeneratedEditor('mk_shelfhead', data.text || 'Эта неделя: полка готовится к встрече.');
  const top = (data.top || []).map((item) => item.name).filter(Boolean);
  setText('mk_shelfhead_sub', top.length ? top.join(', ') : `продажи за ${ruPeriod(data.days || contentDays)}`);
}

function renderReport(data) {
  if (!data) return;
  const top = (data.top || []).slice(0, 3).map((item) => `${item.product} ×${nfmt(item.qty)}`).join(', ');
  const text = [
    `Выручка: ${money(data.income)} · Прибыль: ${money(data.profit)} (${nfmt(data.margin)}%)`,
    `Печать: ${nfmt(data.print_hours)} ч · ${nfmt(data.grams)} г · завершено ${nfmt(data.jobs_done)} заданий`,
    `Брак: ${nfmt(data.failure_rate)}% · новых клиентов: ${nfmt(data.customers_new)} · прибыль/ч: ${money(data.profit_per_print_hour)}`,
    top ? `Топ изделий: ${top}` : 'Топ изделий появится после первых оплаченных заказов.',
  ].join('\n');
  setGeneratedEditor('mk_report', text, true);
  setText('mk_report_sub', `за ${ruPeriod(data.period_days || contentDays)} · цифры нельзя отредактировать в PDF`);
}

function renderHoliday(data) {
  const host = $('mk_holiday');
  if (!host) return;
  const nearest = data && data.nearest ? data.nearest : null;
  const all = (data && data.all) || [];
  host.innerHTML = all.slice(0, 5).map((holiday) => {
    const isNearest = nearest && holiday.date === nearest.date && holiday.name === nearest.name;
    return `<span class="mk-holiday-item${isNearest ? ' nearest' : ''}">${esc(holiday.name)} <strong>через ${nfmt(holiday.days_left)} дн.</strong></span>`;
  }).join('') || '<span class="muted">Календарных поводов пока нет.</span>';
}

function renderSeason(data) {
  const host = $('mk_season');
  if (!host) return;
  const months = (data && data.months) || [];
  if (!months.length) { host.innerHTML = ''; return; }
  const max = Math.max(1, ...months.map((month) => num(month.orders)));
  host.innerHTML = `<div class="muted" style="font-size:11.3px">Заказы по месяцам${data.peak ? ` · пик: ${esc(data.peak.name)}` : ''}</div>`
    + months.map((month) => {
      const width = Math.round(num(month.orders) / max * 100);
      return `<div class="mk-season-row"><span class="n">${esc(month.name)}</span><span class="bar"><i style="width:${width}%"></i></span><b>${nfmt(month.orders)}</b></div>`;
    }).join('');
}

function renderPromo(data) {
  const host = $('mk_promo');
  if (!host) return;
  promoCards = (data && data.cards) || [];
  if (!promoCards.length) {
    host.innerHTML = '<div class="empty compact"><span>Сейчас нет активного повода. Сфокусируйтесь на истории цеха или карточках товаров.</span></div>';
    return;
  }
  host.innerHTML = promoCards.map((card, index) => `<article class="mk-promo-item">
    <b>${esc(card.title || card.name || 'Ближайший повод')}</b>
    <span class="mk-promo-date">${esc(card.date || '')} · через ${nfmt(card.days_left)} дн.</span>
    <p>${esc(card.text || card.hint || '')}</p>
    <button type="button" data-mk-promo-copy="${index}">Копировать</button>
  </article>`).join('');
}

function renderVideo(data) {
  const empty = $('mk_video_empty');
  const box = $('mk_video');
  const play = $('mk_video_play');
  const stop = $('mk_video_stop');
  if (!empty || !box || !play || !stop) return;
  videoFrames = (data && data.frames) || [];
  videoIndex = 0;
  if (!videoFrames.length) {
    empty.hidden = false;
    box.hidden = true;
    play.hidden = true;
    stop.hidden = true;
    empty.innerHTML = '<span>Кадров за выбранный период нет. Включите кейфреймы в «Настройки → Производство → Умный цех» — и здесь появится монтаж.</span>';
    return;
  }
  empty.hidden = true;
  box.hidden = false;
  play.hidden = false;
  stop.hidden = true;
  showFrame();
}

function showFrame() {
  const frame = videoFrames[videoIndex];
  if (!frame) return;
  const image = $('mk_video_img');
  if (image) image.src = frame.url;
  const date = String(frame.file || '').replace(/^\d{4}(\d{2})(\d{2})-(\d{2})(\d{2})(\d{2})\.jpg$/, '$2.$3 $4:$5:$6');
  setText('mk_video_cap', `${frame.job || 'Печать'} · кадр ${videoIndex + 1}/${videoFrames.length}${date ? ` · ${date}` : ''}`);
}

function stopVideo() {
  if (videoTimer) window.clearInterval(videoTimer);
  videoTimer = 0;
  const play = $('mk_video_play');
  const stop = $('mk_video_stop');
  if (play) play.hidden = !videoFrames.length;
  if (stop) stop.hidden = true;
}

function startVideo() {
  if (!videoFrames.length) return;
  stopVideo();
  const play = $('mk_video_play');
  const stop = $('mk_video_stop');
  if (play) play.hidden = true;
  if (stop) stop.hidden = false;
  videoTimer = window.setInterval(() => {
    videoIndex = (videoIndex + 1) % videoFrames.length;
    showFrame();
  }, 1500);
}

function renderPrintMap(data) {
  const host = $('mk_map');
  if (!host) return;
  const cells = (data && data.cells) || [];
  if (!cells.length) {
    host.innerHTML = '<span class="muted">Пока нет завершённых печатей — карта начнёт заполняться после первого задания.</span>';
    return;
  }
  const grid = Array.from({ length: 7 }, () => Array.from({ length: 53 }, () => 0));
  cells.forEach((cell) => {
    if (cell.week >= 0 && cell.week < 53 && cell.weekday >= 0 && cell.weekday < 7) grid[cell.weekday][cell.week] = cell.count;
  });
  let html = '<div class="mk-grid53" title="Строки — дни недели, столбцы — недели">';
  for (let week = 0; week < 53; week += 1) {
    for (let day = 0; day < 7; day += 1) {
      const value = grid[day][week] || 0;
      const level = value ? Math.min(4, 1 + Math.ceil(value / Math.max(1, data.max_day) * 3)) : 0;
      html += `<i class="l${level}" title="Неделя ${week + 1}: ${nfmt(value)} печ."></i>`;
    }
  }
  html += `</div><div class="muted" style="margin-top:7px;font-size:11.2px">${nfmt(data.total)} завершённых печатей · ${esc(data.year)}</div>`;
  host.innerHTML = html;
}

function renderActionPlan(week, holiday, shelf) {
  const host = $('mk_action_plan');
  if (!host) return;
  const numbers = (week && week.numbers) || {};
  const nearest = holiday && holiday.nearest;
  const shelfTop = (shelf && shelf.top && shelf.top[0]) || null;
  const tasks = [
    numbers.jobs_done
      ? { text: 'Скопировать «Историю цеха» и добавить один живой кадр процесса.', tag: 'пост' }
      : { text: 'Покажите старт: снимите один кадр детали на столе и расскажите, для чего она.', tag: 'первый пост' },
    nearest
      ? { text: `Подготовить оффер к «${nearest.name}» — до даты ${nfmt(nearest.days_left)} дн.`, tag: 'сезон' }
      : { text: 'Выберите одну позицию полки и обновите её фотографию для карточки.', tag: 'витрина' },
    shelfTop
      ? { text: `Проверить карточку «${shelfTop.name}»: у лидера должна быть свежая цена и фото.`, tag: 'товар' }
      : { text: 'Добавить на полку одну позицию, чтобы собрать карточку для Авито.', tag: 'товар' },
    { text: 'Распечатать стикеры или визитки для следующей выдачи заказа.', tag: 'в печать' },
  ];
  const key = `pf.content.plan.${todayISO()}`;
  let completed = {};
  try { completed = JSON.parse(store.get(key, '{}') || '{}'); } catch (e) { completed = {}; }
  host.innerHTML = tasks.map((task, index) => `<label class="mk-plan-item">
    <input type="checkbox" data-mk-plan="${index}"${completed[index] ? ' checked' : ''}>
    <span>${esc(task.text)}</span><small>${esc(task.tag)}</small>
  </label>`).join('');
}

function renderHero(week, report, holiday) {
  const numbers = (week && week.numbers) || (report || {});
  setText('mk_stat_jobs', nfmt(numbers.jobs_done));
  setText('mk_stat_jobs_note', `заданий за ${ruPeriod((week && week.period_days) || contentDays)}`);
  setText('mk_stat_hours', `${nfmt(numbers.print_hours)} ч`);
  setText('mk_stat_hours_note', 'фактической печати');
  setText('mk_stat_income', money(numbers.income));
  setText('mk_stat_income_note', 'доход за период');
  const nearest = holiday && holiday.nearest;
  setText('mk_stat_event', nearest ? nearest.name : '—');
  setText('mk_stat_event_note', nearest ? `через ${nfmt(nearest.days_left)} дн.` : 'нет ближайшей даты');
  const sentence = numbers.jobs_done
    ? `За ${ruPeriod(contentDays)} цех завершил ${nfmt(numbers.jobs_done)} заданий. Соберите из этого историю, карточку товара или доказательство для клиента.`
    : 'Данных пока мало — это нормально. Начните с карточки товара, одного процесса и повода из календаря.';
  setText('mk_hero_note', sentence);
}

/* ============================================================ loading */
async function loadAvitoItems() {
  const select = $('mk_avito_item');
  if (!select) return;
  const previous = select.value;
  try {
    const data = await get('/api/shelf');
    const items = (data.items || []).filter((item) => num(item.qty) > 0 || num(item.sold_7) > 0);
    select.innerHTML = items.length
      ? `<option value="">Выберите позицию с полки</option>${items.map((item) => `<option value="${esc(item.id)}">${esc(item.name)} · ${money(item.price)} · ${nfmt(item.qty)} шт.</option>`).join('')}`
      : '<option value="">Стеллаж пуст — добавьте позицию</option>';
    if (previous && items.some((item) => item.id === previous)) select.value = previous;
  } catch (error) {
    select.innerHTML = '<option value="">Не удалось загрузить стеллаж</option>';
  }
}

async function loadBusinessCustomers() {
  const button = $('mk_print_business');
  if (!button) return;
  let select = $('mk_business_cust');
  if (!select) {
    select = document.createElement('select');
    select.id = 'mk_business_cust';
    select.className = 'field';
    select.setAttribute('aria-label', 'Клиент для визитки');
    button.after(select);
  }
  const previous = select.value;
  const customers = PF.state.customers || [];
  select.innerHTML = '<option value="">Визитка цеха (без клиента)</option>'
    + customers.map((customer) => `<option value="${esc(customer.id)}">${esc(customer.name || customer.company || 'Клиент')}</option>`).join('');
  if (previous && customers.some((customer) => customer.id === previous)) select.value = previous;
}

async function refreshContent(options = {}) {
  const request = ++loadId;
  setLoading(true);
  const days = contentDays;
  const results = await Promise.allSettled([
    get('/api/content/week', { days }),
    get('/api/content/social', { days }),
    get('/api/content/shelf-header', { days: Math.min(days, 30) }),
    get('/api/content/report', { days }),
    get('/api/content/holiday'),
    get('/api/content/season'),
    get('/api/content/promo'),
    get('/api/content/week-video', { days: Math.min(days, 30) }),
    get('/api/content/print-map'),
    loadAvitoItems(),
  ]);
  if (request !== loadId) return;
  setLoading(false);

  const [weekR, socialR, shelfR, reportR, holidayR, seasonR, promoR, videoR, mapR] = results;
  const week = getResult(weekR);
  const social = getResult(socialR);
  const shelf = getResult(shelfR);
  const report = getResult(reportR);
  const holiday = getResult(holidayR);
  const season = getResult(seasonR);
  const promo = getResult(promoR);
  const video = getResult(videoR);
  const map = getResult(mapR);

  if (week) renderWeek(week); else setEditorError('mk_week', weekR.reason);
  if (social) renderSocial(social); else setEditorError('mk_social', socialR.reason);
  if (shelf) renderShelfHeader(shelf); else setEditorError('mk_shelfhead', shelfR.reason);
  if (report) renderReport(report); else setEditorError('mk_report', reportR.reason);
  if (holiday) renderHoliday(holiday); else setText('mk_holiday', 'Календарь недоступен.');
  if (season) renderSeason(season);
  if (promo) renderPromo(promo); else setText('mk_promo', 'Промо-пак недоступен.');
  if (video) renderVideo(video); else renderVideo({ frames: [] });
  if (map) renderPrintMap(map); else setText('mk_map', 'Карта недоступна.');
  renderActionPlan(week, holiday, shelf);
  renderHero(week, report, holiday);
  loadBusinessCustomers();

  if (!options.quiet) toast('Контент обновлён', `Факты за ${ruPeriod(days)}`);
}

/* =============================================================== Avito */
async function generateAvito() {
  const id = ($('mk_avito_item') || {}).value;
  if (!id) return toast('Выберите позицию', 'Нужен товар со стеллажа', 'warn');
  const button = $('mk_avito_gen');
  if (button) { button.disabled = true; button.textContent = 'Собираем…'; }
  try {
    avitoCard = await get('/api/content/avito', { item_id: id });
    const text = `ЗАГОЛОВОК\n${avitoCard.title}\n\nОПИСАНИЕ\n${avitoCard.description}\n\nКЛЮЧЕВЫЕ СЛОВА\n${avitoCard.keywords}\n\nЦЕНА\n${money(avitoCard.price)}`;
    setGeneratedEditor('mk_avito', text, true);
    setText('mk_avito_meta', `${avitoCard.name || 'Карточка'} · заголовок, описание, ключевые слова и цена`);
    ['mk_avito_copy', 'mk_avito_reset', 'mk_avito_download'].forEach((idName) => { const el = $(idName); if (el) el.hidden = false; });
    toast('Карточка собрана', 'Проверьте детали и вставьте в Авито');
  } catch (error) {
    setGeneratedEditor('mk_avito', `Не загрузилось: ${error.message}`, true);
    avitoCard = null;
    fail(error);
  } finally {
    if (button) { button.disabled = false; button.textContent = 'Собрать карточку'; }
  }
}

/* ============================================================= printing */
const LABELS = {
  danger: { title: 'ОСТОРОЖНО', text: 'Рядом работает 3D-принтер. Не трогайте станок во время печати.', color: '#ef4444' },
  wash: { title: 'Мойте руки', text: 'После работы с пластиком и присыпкой. Пластик не для еды.', color: '#0ea5e9' },
  delivery: { title: 'Печатаем за 1–3 дня', text: 'Заказали — напечатали — заберите. Статус заказа — по QR на упаковке.', color: '#22c55e' },
  gift: { title: 'Подарки к празднику', text: 'Адресники, таблички, QR-стойки с вашим текстом. Закажите заранее.', color: '#f97316' },
};

function labelCard(kind, note) {
  const label = LABELS[kind] || LABELS.danger;
  return `<div class="label-card">
    <div class="label-title" style="color:${label.color}">${esc(label.title)}</div>
    <div class="label-text">${esc(label.text)}</div>
    ${note ? `<div class="label-note">${esc(note)}</div>` : ''}
    <div class="label-foot">цех NOZZA · локальная 3D-печать</div>
  </div>`;
}

function labelSheet(kind, note, multiple = true) {
  const cards = multiple ? Array.from({ length: 4 }, () => labelCard(kind, note)).join('') : labelCard(kind, note);
  return `<!doctype html><html lang="ru"><head><meta charset="utf-8"><title>Табличка NOZZA</title>
<style>@page{size:A4;margin:10mm}*{box-sizing:border-box}body{margin:0;font-family:Arial,sans-serif;color:#1f2937}.grid{display:grid;grid-template-columns:1fr 1fr;gap:10mm}.label-card{height:60mm;padding:8mm 10mm;border:.6mm dashed #9ca3af;border-radius:3mm}.label-title{font-size:20pt;font-weight:800}.label-text{margin-top:6mm;font-size:11pt;line-height:1.35}.label-note{margin-top:4mm;color:#6b7280;font-size:10pt}.label-foot{margin-top:8mm;color:#9ca3af;font-size:9pt}</style></head><body><div class="grid">${cards}</div></body></html>`;
}

function allLabelsSheet() {
  return `<!doctype html><html lang="ru"><head><meta charset="utf-8"><title>Таблички NOZZA</title>
<style>@page{size:A4;margin:10mm}*{box-sizing:border-box}body{margin:0;font-family:Arial,sans-serif;color:#1f2937}.page{display:grid;grid-template-columns:1fr 1fr;gap:10mm;page-break-after:always}.label-card{height:60mm;padding:8mm 10mm;border:.6mm dashed #9ca3af;border-radius:3mm}.label-title{font-size:20pt;font-weight:800}.label-text{margin-top:6mm;font-size:11pt;line-height:1.35}.label-foot{margin-top:8mm;color:#9ca3af;font-size:9pt}</style></head><body>${Object.keys(LABELS).map((kind) => `<div class="page">${Array.from({ length: 4 }, () => labelCard(kind, '')).join('')}</div>`).join('')}</body></html>`;
}

/* ============================================================= bind */
/* ================================ 13.1 (60–62): доска контента недели
   План пн–вс с перетаскиванием карточек (как канбан заказов), кнопкой
   «положить сюда текущий текст» (подстановка фактов цеха) и счётчиком
   «N из 7 постов готово» в шапке. Хранится в localStorage этого браузера —
   это план, а не учёт. */
const WEEK_DAYS = ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс'];
const BOARD_KEY = 'pf.content.board';

function boardState() {
  try { return JSON.parse(localStorage.getItem(BOARD_KEY) || '{}'); } catch (e) { return {}; }
}
function saveBoardState(state) {
  try { localStorage.setItem(BOARD_KEY, JSON.stringify(state)); } catch (e) { /* ок */ }
}
function boardReadyCount() {
  const state = boardState();
  return WEEK_DAYS.filter((_, i) => state[i]).length;
}
function renderBoard() {
  const host = $('mk_board');
  if (!host) return;
  const state = boardState();
  host.innerHTML = WEEK_DAYS.map((day, i) => {
    const card = state[i];
    return `<div class="mk-board-col" data-day="${i}">`
      + `<div class="mk-board-day">${day}${card ? `<small>${esc(card.time || '')}</small>` : ''}</div>`
      + (card
        ? `<div class="mk-board-card" draggable="true" data-day="${i}" data-board-card="${i}" title="Перетащите в другой день · клик — превью в Telegram">`
          + `<span class="mk-board-text">${esc((card.title || card.text || '').slice(0, 130))}</span>`
          + `<button class="icon-btn sm" type="button" data-board-del="${i}" title="Убрать с доски">×</button></div>`
        : `<button class="mk-board-add" type="button" data-board-add="${i}" title="Положить сюда текущий текст (факты цеха)">+</button>`)
      + '</div>';
  }).join('');
  const counter = $('mk_board_count');
  if (counter) {
    const n = boardReadyCount();
    counter.textContent = `${n} из ${WEEK_DAYS.length} дней`;
    counter.className = 'chip outline' + (n === WEEK_DAYS.length ? ' ok' : '');
  }
}
function bindContentBoard() {
  const host = $('mk_board');
  if (!host) return;
  host.addEventListener('click', (e) => {
    const del = e.target.closest('[data-board-del]');
    if (del) {
      e.stopPropagation();
      const state = boardState();
      delete state[del.dataset.boardDel];
      saveBoardState(state);
      renderBoard();
      toast('Убрали с доски', WEEK_DAYS[Number(del.dataset.boardDel)]);
      return;
    }
    const add = e.target.closest('[data-board-add]');
    if (add) {
      const source = (textValue('mk_week') || '').trim() || (textValue('mk_social') || '').trim();
      if (!source) {
        toast('Сначала соберите пост', 'Нажмите «↻ Обновить» — текст появится в «Истории цеха»', 'warn');
        return;
      }
      const state = boardState();
      const title = source.split('\n')[0].slice(0, 60) || 'Пост';
      state[add.dataset.boardAdd] = { text: source, title, time: new Date().toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' }) };
      saveBoardState(state);
      renderBoard();
      toast('Пост на доске', `${WEEK_DAYS[Number(add.dataset.boardAdd)]} · можно перетащить в другой день`);
      return;
    }
    const card = e.target.closest('[data-board-card]');
    if (card) {
      const state = boardState();
      const item = state[card.dataset.boardCard];
      if (item) renderTgPreview(item.text || '', `${WEEK_DAYS[Number(card.dataset.boardCard)]} · ${item.title || ''}`);
    }
  });
  // Перетаскивание между днями — тот же жест, что в канбане заказов.
  let dragFrom = null;
  host.addEventListener('dragstart', (e) => {
    const card = e.target.closest('[data-board-card]');
    if (!card) return;
    dragFrom = card.dataset.boardCard;
    card.classList.add('dragging');
    if (e.dataTransfer) e.dataTransfer.effectAllowed = 'move';
  });
  host.addEventListener('dragend', (e) => {
    const card = e.target.closest('[data-board-card]');
    if (card) card.classList.remove('dragging');
    dragFrom = null;
  });
  host.addEventListener('dragover', (e) => {
    const col = e.target.closest('.mk-board-col');
    if (!col) return;
    e.preventDefault();
    if (e.dataTransfer) e.dataTransfer.dropEffect = 'move';
  });
  host.addEventListener('drop', (e) => {
    const col = e.target.closest('.mk-board-col');
    if (!col || dragFrom == null) return;
    e.preventDefault();
    const to = col.dataset.day;
    if (String(to) === String(dragFrom)) return;
    const state = boardState();
    const card = state[dragFrom];
    if (!card) return;
    delete state[dragFrom];
    state[to] = card;
    saveBoardState(state);
    renderBoard();
    toast('Перенесли пост', `${WEEK_DAYS[Number(dragFrom)]} → ${WEEK_DAYS[Number(to)]}`);
  });
  const reset = $('mk_board_reset');
  if (reset) {
    reset.addEventListener('click', () => {
      if (!window.confirm('Очистить доску контента на эту неделю?')) return;
      try { localStorage.removeItem(BOARD_KEY); } catch (err) { /* ок */ }
      renderBoard();
      toast('Доска очищена');
    });
  }
}
/* 13.1 (61): превью поста — мини-макет сообщения в Telegram. */
function renderTgPreview(text, title) {
  const host = $('mk_tg_preview');
  if (!host) return;
  const value = String(text || '').trim();
  const sub = $('mk_preview_sub');
  if (sub) sub.textContent = title
    ? `Превью: ${title}`
    : 'Живой макет «Истории цеха» — как увидит канал.';
  if (!value) {
    host.innerHTML = '<div class="empty compact"><span>Текст «Истории цеха» появится здесь по мере набора.</span></div>';
    return;
  }
  host.innerHTML = `<div class="tg-preview-card">`
    + `<div class="tg-preview-head"><span class="tg-preview-av" aria-hidden="true">🖨</span>`
    + `<span class="tg-preview-meta"><b>NOZZA · 3D-печать</b><small>${esc(title || 'Пост из фактов')} · только что</small></span></div>`
    + `<div class="tg-preview-body">${esc(value).replace(/\n{2,}/g, '<br><br>').replace(/\n/g, '<br>')}</div>`
    + `<div class="tg-preview-foot"><span>👁 0</span><span>💬 0</span><span>↗</span></div></div>`;
}

function bind() {
  document.querySelectorAll('.mk-editor').forEach((editor) => {
    if (!editor.readOnly) editor.addEventListener('input', () => { editor.dataset.dirty = '1'; });
  });

  // 13.1 (60–62): доска контента, превью в Telegram, счётчик недели
  bindContentBoard();
  const mkWeek = $('mk_week');
  if (mkWeek) {
    mkWeek.addEventListener('input', debounce(() => {
      renderTgPreview(mkWeek.value || '');
    }, 180));
  }

  $('mk_tabs').addEventListener('click', (event) => {
    const button = event.target.closest('[data-mk-pane]');
    if (button) activatePane(button.dataset.mkPane);
  });
  $('mk_period').addEventListener('click', (event) => {
    const button = event.target.closest('[data-days]');
    if (!button) return;
    const next = Number(button.dataset.days);
    if (!PERIODS.includes(next)) return;
    contentDays = next;
    store.set('pf.content.days', String(next));
    syncPeriod();
    refreshContent({ quiet: false }).catch(fail);
  });
  $('mk_refresh').addEventListener('click', () => refreshContent({ quiet: false }).catch(fail));

  $('mk_week_copy').addEventListener('click', () => copyText(textValue('mk_week'), 'история цеха'));
  $('mk_week_download').addEventListener('click', () => downloadText(textValue('mk_week'), `nozza-история-${todayISO()}.txt`));
  $('mk_week_reset').addEventListener('click', () => resetEditor('mk_week', 'история цеха'));
  $('mk_social_copy').addEventListener('click', () => copyText(textValue('mk_social'), 'соцсетевой пакет'));
  $('mk_social_download').addEventListener('click', () => downloadText(textValue('mk_social'), `nozza-соцпакет-${todayISO()}.txt`));
  $('mk_social_reset').addEventListener('click', () => resetEditor('mk_social', 'соцсетевой пакет'));
  $('mk_shelfhead_copy').addEventListener('click', () => copyText(textValue('mk_shelfhead'), 'шапка полки'));

  $('mk_avito_gen').addEventListener('click', generateAvito);
  $('mk_avito_copy').addEventListener('click', () => copyText(textValue('mk_avito'), 'карточка Авито'));
  $('mk_avito_download').addEventListener('click', () => downloadText(textValue('mk_avito'), `nozza-авито-${todayISO()}.txt`));
  $('mk_avito_reset').addEventListener('click', () => resetEditor('mk_avito', 'карточка Авито'));
  $('mk_promo').addEventListener('click', (event) => {
    const button = event.target.closest('[data-mk-promo-copy]');
    if (!button) return;
    const card = promoCards[Number(button.dataset.mkPromoCopy)];
    if (card) copyText(card.text || card.hint || '', card.title || card.name || 'промо-текст');
  });

  $('mk_report_open').addEventListener('click', async () => {
    try {
      const data = await get('/api/content/report/print', { days: contentDays });
      printWindow(data.html, 'Цеховой отчёт');
    } catch (error) { fail(error); }
  });
  $('mk_report_download').addEventListener('click', () => downloadText(textValue('mk_report'), `nozza-отчёт-${todayISO()}.txt`));
  $('mk_video_play').addEventListener('click', startVideo);
  $('mk_video_stop').addEventListener('click', stopVideo);

  $('mk_bc_gen').addEventListener('click', async () => {
    const out = $('mk_bc_out');
    const text = textValue('mk_bc_text').trim();
    if (!text) { out.textContent = 'Введите текст для штрихкода.'; return; }
    try {
      const data = await get('/api/labels/code128', { text });
      out.innerHTML = `${data.svg}<small class="muted">Code 128 ${esc(data.mode)} · ${nfmt(data.symbols)} символов · контрольная сумма проверена</small>`;
    } catch (error) {
      out.innerHTML = `<span style="color:var(--bad)">${esc(error.message)}</span>`;
    }
  });
  $('mk_bc_text').addEventListener('keydown', (event) => {
    if (event.key === 'Enter') { event.preventDefault(); $('mk_bc_gen').click(); }
  });

  $('mk_print_stickers').addEventListener('click', async () => {
    try {
      const data = await get('/api/content/stickers', { kind: 'all' });
      printWindow(data.html, 'Стикеры NOZZA');
    } catch (error) { fail(error); }
  });
  $('mk_print_business').addEventListener('click', async () => {
    try {
      const data = await get('/api/content/business-card', { customer_id: ($('mk_business_cust') || {}).value || '' });
      printWindow(data.html, 'Визитки NOZZA');
    } catch (error) { fail(error); }
  });
  $('mk_print_labels').addEventListener('click', () => printWindow(allLabelsSheet(), 'Таблички цеха'));
  $('mk_label_one').addEventListener('click', () => {
    printWindow(labelSheet(($('mk_labels_kind') || {}).value, textValue('mk_labels_note').trim()), 'Табличка цеха');
  });

  $('mk_action_plan').addEventListener('change', (event) => {
    const checkbox = event.target.closest('[data-mk-plan]');
    if (!checkbox) return;
    const key = `pf.content.plan.${todayISO()}`;
    let completed = {};
    try { completed = JSON.parse(store.get(key, '{}') || '{}'); } catch (e) { completed = {}; }
    completed[checkbox.dataset.mkPlan] = checkbox.checked;
    store.set(key, JSON.stringify(completed));
  });

  syncPeriod();
  activatePane(currentPane, false);
}

/* 14.0 (47): файл грузится лениво при первом входе в раздел, поэтому
   инициализация идёт через PF.module — событие 'ready' к этому моменту
   уже прошло, и обычный PF.on('ready') не сработал бы никогда. */
PF.module('marketing', () => {
  bind();
  loadBusinessCustomers();
  // При открытии страницы сразу по ссылке #marketing раздел уже видим —
  // запускаем студию сразу, не дожидаясь события маршрута.
  if (PF.viewOn('marketing')) refreshContent({ quiet: true }).catch(fail);
});
PF.on('data', () => {
  if (document.querySelector('#view-marketing.on')) loadBusinessCustomers();
});
PF.on('view', (detail) => {
  if (detail.view !== 'marketing') { stopVideo(); return; }
  activatePane(currentPane, false);
  refreshContent({ quiet: true }).catch(fail);
});
})();
