/* PrintFlow 2.0 — принтеры Bambu Lab: телеметрия, управление, AMS,
   камера, файлы SD (FTPS) и очередь печати парка. */
(() => {
'use strict';
const U = PF.ui, { $, $$, esc, num, clamp, money, nfmt, hoursText, minutesText,
  dateTimeText, agoText, toast, fail, openModal, closeModal, confirmDanger } = U;
const { get, post, api } = PF.api;

const STATE_LABEL = {
  OFFLINE: 'Не в сети', IDLE: 'Готов', RUNNING: 'Печать', PREPARE: 'Подготовка',
  PAUSE: 'Пауза', PAUSED: 'Пауза', FINISH: 'Завершено', SLICING: 'Обработка',
  FAILED: 'Ошибка', ERROR: 'Ошибка', UNKNOWN: 'Нет данных',
};
const STATE_KIND = {
  RUNNING: 'running', PREPARE: 'running', SLICING: 'running',
  FINISH: 'ok', IDLE: 'ok', PAUSE: 'warn', PAUSED: 'warn',
  FAILED: 'bad', ERROR: 'bad',
};
const SPEED_LABEL = { 1: 'Тихая', 2: 'Обычная', 3: 'Спорт', 4: 'Ludicrous' };
const DANGER = {
  stop: 'Остановить печать? Задание будет прервано, деталь придётся печатать заново.',
  bed_level: 'Запустить калибровку стола? Принтер начнёт движение и нагрев.',
  calibration: 'Запустить полную калибровку? Займёт несколько минут.',
  unload_filament: 'Выгрузить филамент? Сопло нагреется.',
  extrude: 'Подать филамент? Сопло должно быть нагрето до рабочей температуры.',
  home: 'Припарковать оси? Принтер начнёт движение.',
  nozzle_temp: 'Задать температуру сопла? Принтер начнёт нагрев.',
  bed_temp: 'Задать температуру стола? Принтер начнёт нагрев.',
};

let filesCache = [], pendingFile = null, editingPrinter = null;
let camStream = '', camSession = Date.now();     // ключ живого MJPEG-соединения
let shotsKey = '';                              // чтобы не перезапрашивать архив зря
let chartCache = { id: '', minutes: 0, at: 0, points: [] };
let wallTimer = 0, maintTask = null;

/* ============================================================ хелперы */
const active = () => PF.livePrinter();
function requireLive() {
  const p = active();
  if (!p) throw new Error('Принтер ещё не добавлен');
  if (!p.connection.connected) throw new Error('Принтер не подключён. Проверьте IP, серийный номер и Access Code.');
  return p;
}
function bar(el, percent) { if (el) el.style.width = clamp(num(percent), 0, 100) + '%'; }
function text(id, value) { const el = $(id); if (el) el.textContent = value; }

/* ============================================================ вкладки */
function renderTabs() {
  const live = PF.state.live;
  const list = (live && live.printers) || [];
  const host = $('pr_tabs');
  $('pr_empty').hidden = list.length > 0;
  $('pr_workspace').hidden = list.length === 0;
  if (!list.length) { host.innerHTML = ''; return; }
  if (!PF.state.activePrinter || !list.some((p) => p.id === PF.state.activePrinter)) {
    PF.state.activePrinter = (live.active && live.active.id) || list[0].id;
  }
  host.innerHTML = list.map((p) => {
    const st = p.printer.state, kind = STATE_KIND[st] || '';
    const dot = p.connection.connected ? (kind === 'running' ? 'busy' : 'ok') : 'bad';
    return `<button class="printer-tab${p.id === PF.state.activePrinter ? ' on' : ''}" type="button" data-printer="${esc(p.id)}">`
      + `<span class="dot ${dot}"></span><span><b>${esc(p.name)}</b>`
      + `<small>${esc(p.printer.state_label || STATE_LABEL[st] || st)}`
      + (kind === 'running' ? ` · ${Math.round(num(p.printer.progress))}%` : '') + '</small></span></button>';
  }).join('');
}

/* ======================================================== телеметрия */
function renderLive() {
  renderTabs();
  const p = active();
  const pill = $('live_pill');
  if (!p) { pill.hidden = true; return; }

  pill.hidden = false;
  const st = p.printer.state, kind = STATE_KIND[st] || '';
  $('live_dot').className = 'dot ' + (p.connection.connected ? (kind === 'running' ? 'busy' : 'ok') : 'bad');
  text('live_name', p.name);
  text('live_state', (p.printer.state_label || STATE_LABEL[st] || st)
    + (kind === 'running' ? ` · ${Math.round(num(p.printer.progress))}%` : ''));

  const dot = $('conn_dot');
  if (!$('offline-bar').classList.contains('show')) {
    dot.className = 'dot ' + (p.connection.connected ? 'ok' : 'warn');
    $('conn_title').textContent = p.connection.connected ? 'Принтер на связи' : 'Принтер не в сети';
    $('conn_sub').textContent = p.connection.last_error
      ? String(p.connection.last_error).slice(0, 46)
      : (p.connection.host || 'локальная сеть');
  }

  const badge = $('pr_state');
  badge.className = 'state-badge ' + kind;
  badge.textContent = p.printer.state_label || STATE_LABEL[st] || st;
  $('pr_job').classList.toggle('running', kind === 'running');
  text('pr_task', p.printer.task || 'Нет активной печати');

  const job = (PF.state.jobs.queue || []).find((j) => j.printer_id === p.id && j.state === 'running');
  const order = job && job.order;
  text('pr_order', order ? `Заказ №${order.number} · ${order.product}`
    : (p.connection.connected ? 'Не связано с заказом' : (p.connection.last_error || 'Нет подключения')));

  const progress = clamp(num(p.printer.progress), 0, 100);
  text('pr_progress', Math.round(progress) + '%');
  const ring = $('pr_ring');
  if (ring) ring.style.strokeDashoffset = String(283 - 283 * progress / 100);
  text('pr_layers', `${nfmt(p.printer.layer)} / ${nfmt(p.printer.total_layers)}`);
  text('pr_remaining', p.printer.remaining_min ? minutesText(p.printer.remaining_min) : '—');
  text('pr_eta', p.printer.eta ? new Date(p.printer.eta * 1000)
    .toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' }) : '—');
  text('pr_speed', SPEED_LABEL[p.printer.speed_level] || p.printer.speed_label || '—');
  text('pr_wifi', p.printer.wifi || '—');
  const sel = $('pr_speed_sel');
  if (sel && document.activeElement !== sel) sel.value = String(p.printer.speed_level || 2);

  const t = p.temperature;
  text('pr_nozzle', nfmt(t.nozzle, 1)); text('pr_nozzle_t', nfmt(t.nozzle_target));
  text('pr_bed', nfmt(t.bed, 1)); text('pr_bed_t', nfmt(t.bed_target));
  text('pr_chamber', t.chamber ? nfmt(t.chamber, 1) : '—');
  const nic = $('pr_nozzle_ic');
  if (nic) nic.classList.toggle('on', num(t.nozzle) > 50);

  text('pr_fan_part', Math.round(num(p.fans.part)) + '%'); bar($('pr_fan_part_bar'), p.fans.part);
  text('pr_fan_aux', Math.round(num(p.fans.aux)) + '%'); bar($('pr_fan_aux_bar'), p.fans.aux);
  text('pr_fan_cham', Math.round(num(p.fans.chamber)) + '%'); bar($('pr_fan_cham_bar'), p.fans.chamber);

  text('pr_firmware', p.printer.firmware ? 'Прошивка ' + p.printer.firmware : 'Прошивка —');
  text('pr_sub', p.connection.connected
    ? `${p.name} · ${p.connection.host} · обновлено ${agoText(p.connection.last_message)}`
    : 'Мониторинг и управление по локальной сети. Принтер сейчас недоступен.');

  renderAms(p);
  renderHealth(p);
  renderCamera(p);
  renderAlerts(p);
  renderMaintenance(p);
  renderJobCost(p);
  renderChart(p);

  const controls = $$('[data-cmd],[data-set],[data-jog]');
  controls.forEach((b) => { b.disabled = !p.connection.connected; });
}

function renderAms(p) {
  const ams = p.ams || { trays: [] };
  const trays = ams.trays || [];
  text('pr_ams_count', trays.length ? `${trays.length} слот(ов)` : 'нет данных');
  text('pr_ams_env', ams.temperature != null || ams.humidity != null
    ? `Температура ${ams.temperature ?? '—'} °C · влажность ${ams.humidity ?? '—'}`
    : 'Температура и влажность —');
  const pbtn = $('pr_ams_profiles');
  if (pbtn) pbtn.hidden = !trays.length;
  const host = $('pr_ams');
  if (!trays.length) {
    host.innerHTML = '<div class="empty compact"><span>AMS не обнаружен или ещё не прислал данные.</span></div>';
    return;
  }
  host.innerHTML = trays.map((t) => {
    const remain = t.remain == null || t.remain < 0 ? null : num(t.remain);
    return `<div class="ams-slot${t.active ? ' active' : ''}">`
      + `<div class="swatch" style="--filament:${esc(t.color || '#cbd5e1')}"></div>`
      + `<b>${esc(t.label || ('Слот ' + (num(t.slot) + 1)))}</b>`
      + `<small>${esc(t.type || 'Не задан')}${remain != null ? ' · ' + Math.round(remain) + '%' : ''}</small>`
      + (remain != null ? `<div class="bar thin${remain < 15 ? ' warn' : ''}"><i style="width:${clamp(remain, 0, 100)}%"></i></div>` : '')
      + '<div class="acts">'
      + `<button class="btn sm" type="button" data-ams-load="${esc(String(t.slot))}">Подать</button>`
      + `<button class="btn sm" type="button" data-ams-edit="${esc(String(t.unit))}:${esc(String(t.slot))}" data-type="${esc(t.type || '')}" data-color="${esc(t.color || '#cccccc')}">Тип</button>`
      + '</div></div>';
  }).join('');
}

const SEV_LABEL = { info: 'Заметка', warn: 'Внимание', error: 'Ошибка', fatal: 'Критично' };
const SEV_ICON = { info: 'ⓘ', warn: '⚠', error: '✕', fatal: '⛔' };

function renderHealth(p) {
  const problems = Array.isArray(p.printer.problems) ? p.printer.problems : [];
  const badge = $('pr_health');
  const worst = p.printer.severity || '';
  badge.className = 'chip ' + (worst === 'fatal' || worst === 'error' ? 'bad' : worst === 'warn' ? 'warn' : 'ok');
  badge.textContent = problems.length ? `${problems.length} ${plural(problems.length, 'проблема', 'проблемы', 'проблем')}` : 'Нет ошибок';
  $('pr_errors').innerHTML = problems.length
    ? problems.map((h) => `<div class="hms-item sev-${esc(h.severity || 'warn')}">`
      + `<b>${SEV_ICON[h.severity] || '⚠'} ${esc(h.title || 'Неизвестная ошибка')}</b>`
      + `<span>${esc(SEV_LABEL[h.severity] || '')} · код ${esc(h.code || '')}</span>`
      + (h.why ? `<div class="why">${esc(h.why)}</div>` : '')
      + (h.advice ? `<div class="fix">${esc(h.advice)}</div>` : '')
      + (h.url ? `<a href="${esc(h.url)}" target="_blank" rel="noopener">Официальное описание кода ↗</a>` : '')
      + '</div>').join('')
    : '<div class="empty compact"><span>Активных ошибок нет.</span></div>';
}

function plural(n, one, few, many) {
  const a = Math.abs(n) % 100, b = a % 10;
  if (a > 10 && a < 20) return many;
  if (b > 1 && b < 5) return few;
  return b === 1 ? one : many;
}

/* ------------------------------------------------- тревоги сторожа печати */
function renderAlerts(p) {
  const host = $('pr_alerts');
  if (!host) return;
  const alerts = ((p.guard || {}).alerts) || [];
  host.innerHTML = alerts.map((a) => `<div class="alert-item ${esc(a.severity || 'warn')}">`
    + `<span class="ic">${SEV_ICON[a.severity] || '⚠'}</span>`
    + `<span><b>${esc(a.title || 'Сторож печати')}</b>${esc(a.reason || '')}`
    + (a.advice ? `<small>${esc(a.advice)}</small>` : '')
    + (a.at ? `<small>${esc(dateTimeText(a.at))}${(a.actions || []).length ? ' · сделано: ' + esc(a.actions.join(', ')) : ''}</small>` : '')
    + '</span>'
    + `<span class="acts"><button class="btn sm ghost" type="button" data-alert-clear="${esc(p.id)}">Скрыть</button></span>`
    + '</div>').join('');
}

/* ------------------------------------------------- наработка и регламент ТО */
function renderMaintenance(p) {
  const m = p.maintenance || {};
  const tasks = m.tasks || [];
  text('pr_runtime', m.hours != null
    ? `Наработка ${nfmt(Math.round(m.hours))} ч · ${tasks.length} ${plural(tasks.length, 'работа', 'работы', 'работ')} в регламенте`
    : 'Наработка —');
  const badge = $('pr_maint_badge');
  if (badge) {
    badge.className = 'chip ' + (m.due ? 'bad' : m.soon ? 'warn' : 'ok');
    badge.textContent = m.due ? `${m.due} просрочено` : m.soon ? `${m.soon} скоро` : 'Всё по плану';
  }
  $('pr_maint').innerHTML = tasks.length ? tasks.map((t) => {
    const pct = clamp(num(t.percent), 0, 100);
    const cls = t.due ? ' due' : t.soon ? ' soon' : '';
    const left = t.left_hours == null ? 'по расписанию'
      : t.left_hours <= 0 ? 'пора выполнить'
      : `через ${nfmt(Math.round(t.left_hours))} ч`;
    return `<div class="maint-row${cls}"><span class="name"><b>${esc(t.task)}</b>`
      + `<small>${esc(left)}${t.last_at ? ' · последний раз ' + esc(dateTextSafe(t.last_at)) : ''}</small></span>`
      + `<span class="bar thin${t.due ? ' warn' : ''}"><i style="width:${pct}%"></i></span>`
      + `<button class="btn sm" type="button" data-maint="${esc(t.id)}">Сделано</button></div>`;
  }).join('') : '<div class="empty compact"><span>Регламент появится после подключения принтера.</span></div>';
}

function dateTextSafe(v) { try { return U.dateText(v); } catch (e) { return String(v || ''); } }

/* ------------------------------------------------ во что обходится печать */
function renderJobCost(p) {
  const j = p.job || {};
  if (!j.spent && !j.cost_total) {
    text('pr_spent', '—');
    text('pr_cost_total', '—');
    return;
  }
  text('pr_spent', money(j.spent));
  text('pr_cost_total', `${money(j.cost_total)} · ${nfmt(j.grams)} г`);
}

/* --------------------------------------------- графики истории показателей */
async function renderChart(p, force) {
  const host = $('pr_chart');
  if (!host) return;
  const minutes = +($('pr_chart_range') || {}).value || 180;
  const fresh = chartCache.id === p.id && chartCache.minutes === minutes && Date.now() - chartCache.at < 25000;
  if (!fresh || force) {
    try {
      const data = await get('/api/printer/telemetry', { printer_id: p.id, minutes });
      chartCache = { id: p.id, minutes, at: Date.now(), points: data.points || [] };
    } catch (e) { chartCache = { id: p.id, minutes, at: Date.now(), points: [] }; }
  }
  const pts = chartCache.points;
  if (!pts.length) {
    host.innerHTML = '<div class="empty compact"><span>Пока нет истории. Точки записываются раз в минуту во время работы принтера.</span></div>';
    $('pr_chart_legend').innerHTML = '';
    return;
  }
  const series = [
    { name: 'Сопло, °C', color: '#f97316', values: pts.map((x) => num(x.nozzle)) },
    { name: 'Стол, °C', color: '#3b82f6', values: pts.map((x) => num(x.bed)) },
    { name: 'Камера, °C', color: '#22c55e', values: pts.map((x) => num(x.chamber)) },
    { name: 'Обдув, %', color: '#a855f7', values: pts.map((x) => num(x.fan_part)) },
  ].filter((sr) => sr.values.some((v) => v > 0));
  host.innerHTML = sparkLines(series, pts);
  $('pr_chart_legend').innerHTML = series.map((sr) =>
    `<span class="lg"><i style="background:${sr.color}"></i>${esc(sr.name)}</span>`).join('');
  text('pr_chart_sub', `${pts.length} ${plural(pts.length, 'точка', 'точки', 'точек')} · шаг ~1 мин`);
}

function sparkLines(series, pts) {
  const W = 640, H = 190, pad = { l: 34, r: 10, t: 12, b: 20 };
  if (!series.length) return '<div class="empty compact"><span>Нет данных для графика.</span></div>';
  const all = series.flatMap((s) => s.values);
  const max = Math.max(10, Math.ceil(Math.max(...all) / 10) * 10), min = 0;
  const x = (i) => pad.l + (i / Math.max(1, pts.length - 1)) * (W - pad.l - pad.r);
  const y = (v) => H - pad.b - ((v - min) / (max - min)) * (H - pad.t - pad.b);
  const grid = [0, 0.25, 0.5, 0.75, 1].map((f) => {
    const val = Math.round(max * (1 - f)), yy = pad.t + f * (H - pad.t - pad.b);
    return `<line x1="${pad.l}" y1="${yy}" x2="${W - pad.r}" y2="${yy}" stroke="currentColor" stroke-opacity=".12"></line>`
      + `<text x="${pad.l - 6}" y="${yy + 3.5}" text-anchor="end" font-size="9.5" fill="currentColor" fill-opacity=".5">${val}</text>`;
  }).join('');
  const paths = series.map((s) => {
    const d = s.values.map((v, i) => `${i ? 'L' : 'M'}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join('');
    return `<path d="${d}" fill="none" stroke="${s.color}" stroke-width="1.8" stroke-linejoin="round" stroke-linecap="round"></path>`;
  }).join('');
  const first = pts[0], last = pts[pts.length - 1];
  const stamp = (v) => String(v || '').slice(11, 16);
  return `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="История температур">`
    + grid + paths
    + `<text x="${pad.l}" y="${H - 5}" font-size="9.5" fill="currentColor" fill-opacity=".5">${stamp(first.at)}</text>`
    + `<text x="${W - pad.r}" y="${H - 5}" text-anchor="end" font-size="9.5" fill="currentColor" fill-opacity=".5">${stamp(last.at)}</text>`
    + '</svg>';
}

function camUrl(p, live) {
  return live
    ? `/api/printer/camera.mjpeg?printer_id=${encodeURIComponent(p.id)}&t=${camSession}`
    : `/api/printer/camera.jpg?printer_id=${encodeURIComponent(p.id)}&t=${Date.now()}`;
}

/* Живой поток: один MJPEG-запрос вместо перекачки кадров по таймеру.
   Переподключаем только при смене принтера, ошибке или ручном обновлении. */
function renderCamera(p) {
  const cam = p.camera || {};
  const demo = !!cam.demo;
  const img = $('pr_cam');
  text('pr_cam_status', cam.available
    ? (demo ? 'Демо-режим: заготовленные кадры' : 'Прямой эфир')
    : (cam.error || 'Нет сигнала'));
  text('pr_cam_age', cam.available
    ? (demo ? 'Принтер не подключён' : (cam.age < 3 ? 'Кадр только что' : `Кадр ${Math.round(cam.age)} сек. назад`))
    : '—');
  $('pr_cam_demo').hidden = !demo;
  $('pr_cam_empty').hidden = !!cam.available;
  img.classList.toggle('on', !!cam.available);

  if (!cam.available) {
    if (camStream) { img.removeAttribute('src'); camStream = ''; }
    return;
  }
  const key = p.id + ':' + camSession;
  if (camStream !== key) {
    camStream = key;
    img.onerror = () => { camStream = ''; };       // сорвался поток — соберём заново
    img.src = camUrl(p, true);
    if ($('cam_modal').open) $('cam_full').src = camUrl(p, true);
  }
  renderShots(p);
}

function renderShots(p) {
  const host = $('pr_shots');
  if (!host) return;
  const count = num((p.camera || {}).shots);   // в снимке приходит только счётчик
  const key = p.id + ':' + count;
  if (shotsKey === key) return;                // список не менялся — не дёргаем сеть
  shotsKey = key;
  if (!count) { host.innerHTML = ''; drawShots(p, []); return; }
  get('/api/printer/shots', { printer_id: p.id })
    .then((r) => drawShots(p, r.shots || []))
    .catch(() => { shotsKey = ''; });
}

/** Лента кадров: свой таймлапс происходящего, самые свежие — первыми. */
function drawShots(p, shots) {
  const host = $('pr_shots');
  if (!host) return;
  const empty = $('pr_shots_empty');
  if (empty) empty.hidden = shots.length > 0;
  host.innerHTML = shots.slice(0, 12).map((sh) => {
    const when = shotTime(sh.at);
    const note = sh.note || 'Снимок';
    return `<img src="/api/printer/shot.jpg?printer_id=${encodeURIComponent(p.id)}&id=${encodeURIComponent(sh.id)}"`
      + ` alt="${esc(note)}" title="${esc(note + ' · ' + when)}"`
      + ` data-shot="${esc(sh.id)}" loading="lazy">`;
  }).join('');
}

/** Время снимка приходит в секундах Unix — переводим в привычные часы и минуты. */
function shotTime(at) {
  const ms = num(at) * 1000;
  if (!ms) return '';
  try {
    return new Date(ms).toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' });
  } catch (e) { return ''; }
}



/* ======================================================== AMS-профили */
let amsProfiles = [];
async function loadAmsProfiles() {
  try {
    const data = await get('/api/ams-profiles');
    amsProfiles = data.profiles || [];
  } catch (e) { amsProfiles = []; }
  return amsProfiles;
}
function renderAmsProfilesList() {
  const list = $('ap_list');
  if (!list) return;
  list.innerHTML = amsProfiles.length ? amsProfiles.map((p) => {
    let slots = [];
    try { slots = JSON.parse(p.slots || '[]'); } catch (e) { slots = []; }
    return `<div class="set-row"><div class="sinfo"><b>${esc(p.name)}</b>`
      + `<small>${slots.map((sl) => `${esc(sl.type || '—')} ${esc(sl.color || '')}`).join(' · ') || 'пусто'}</small></div>`
      + `<button class="btn sm" type="button" data-ap-apply="${esc(p.id)}">Применить</button>`
      + `<button class="icon-btn sm danger" type="button" data-ap-del="${esc(p.id)}">×</button></div>`;
  }).join('') : '<div class="empty compact"><span>Профилей пока нет. Настройте слоты AMS и нажмите «Захватить».</span></div>';
}
function captureAmsSlots() {
  const snap = PF.livePrinter();
  const trays = (snap && snap.ams && snap.ams.trays) || [];
  const host = $('ap_slots');
  if (!host) return;
  host.innerHTML = trays.map((t) => `<div class="ams-profile-slot">`
    + `<b>${esc(t.label)}</b>`
    + `<input data-ap-slot-tray value="${t.slot}" type="hidden">`
    + `<input data-ap-slot-type value="${esc(t.type || '')}" placeholder="Тип (PLA)">`
    + `<input data-ap-slot-color value="${esc((t.color || '').replace('#', ''))}" placeholder="Цвет FFFFFF">`
    + `</div>`).join('')
    || '<div class="empty compact"><span>Слоты AMS не пришли — принтер не на связи.</span></div>';
}
async function openAmsProfiles() {
  await loadAmsProfiles();
  renderAmsProfilesList();
  captureAmsSlots();
  $('ap_name').value = '';
  openModal('ams_profile_modal');
}
async function saveAmsProfile() {
  const name = $('ap_name').value.trim();
  if (!name) return fail(new Error('Укажите название профиля'));
  const slots = $$('#ap_slots [data-ap-slot-tray]').map((el) => ({
    tray: num(el.value), type: (el.parentElement.querySelector('[data-ap-slot-type]').value || '').trim().toUpperCase(),
    color: (el.parentElement.querySelector('[data-ap-slot-color]').value || '').trim().toUpperCase(),
  })).filter((x) => x.type);
  try {
    await post('/api/ams-profile/save', { name, slots });
    closeModal('ams_profile_modal');
    toast('Профиль сохранён', name);
  } catch (e) { fail(e); }
}
function bindAmsProfiles() {
  const btn = $('pr_ams_profiles');
  if (btn) btn.addEventListener('click', openAmsProfiles);
  const save = $('ap_save');
  if (save) save.addEventListener('click', saveAmsProfile);
  const cap = $('ap_capture');
  if (cap) cap.addEventListener('click', captureAmsSlots);
  const list = $('ap_list');
  if (list) list.addEventListener('click', async (e) => {
    const apply = e.target.closest('[data-ap-apply]');
    if (apply) {
      try {
        await post('/api/ams-profile/apply', { id: apply.dataset.apApply, printer_id: PF.state.activePrinter });
        toast('Профиль применён');
      } catch (err) { fail(err); }
      return;
    }
    const del = e.target.closest('[data-ap-del]');
    if (del) {
      await post('/api/ams-profile/delete', { id: del.dataset.apDel });
      loadAmsProfiles().then(renderAmsProfilesList);
    }
  });
}

/* ================================================== отложенные команды */
async function openSchedule() {
  try {
    const data = await get('/api/schedule');
    const rows = data.commands || [];
    const now = new Date();
    now.setMinutes(now.getMinutes() + 10);
    const pad = (v) => String(v).padStart(2, '0');
    $('sch_at').value = `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`
      + `T${pad(now.getHours())}:${pad(now.getMinutes())}`;
    $('sch_value').value = '';
    $('sch_note').value = '';
    $('sch_command').value = 'light';
    $('sch_list').innerHTML = rows.length ? rows.map((c) => `<div class="tx-row">`
      + `<span class="tx-ic ${c.done ? 'income' : 'outline'}">${c.done ? '✓' : '⏱'}</span>`
      + `<div class="tx-body"><b>${esc(c.command)}${c.note ? ' · ' + esc(c.note) : ''}</b>`
      + `<small>${esc(dateTimeText(c.at))}${c.done ? ' · ' + esc(c.result || 'выполнено') : ''}</small></div>`
      + (!c.done ? `<button class="icon-btn sm danger" type="button" data-sch-del="${esc(c.id)}">×</button>` : '')
      + `</div>`).join('') : '<div class="empty compact"><span>Отложенных команд нет.</span></div>';
    openModal('schedule_modal');
  } catch (e) { fail(e); }
}
async function saveSchedule() {
  const at = $('sch_at').value;
  if (!at) return fail(new Error('Укажите время выполнения'));
  const command = $('sch_command').value;
  const valueRaw = $('sch_value').value;
  const value = command === 'load_filament' ? num(valueRaw)
    : (command === 'nozzle_temp' || command === 'bed_temp') ? num(valueRaw) : undefined;
  try {
    await post('/api/schedule/command', {
      printer_id: PF.state.activePrinter, command, value, at: new Date(at).toISOString(),
      note: $('sch_note').value.trim(),
    });
    toast('Команда запланирована');
    openSchedule();
  } catch (e) { fail(e); }
}
function bindSchedule() {
  const btn = $('pr_schedule_btn');
  if (btn) btn.addEventListener('click', openSchedule);
  const save = $('sch_save');
  if (save) save.addEventListener('click', saveSchedule);
  const list = $('sch_list');
  if (list) list.addEventListener('click', async (e) => {
    const del = e.target.closest('[data-sch-del]');
    if (!del) return;
    await post('/api/schedule/delete', { id: del.dataset.schDel });
    openSchedule();
  });
}

/* ======================================================== режим «Стена»
   Полноэкранный вид парка для второго монитора или планшета в мастерской:
   крупный процент, кадр камеры и тревоги — читается с нескольких метров. */
function wallOpen() {
  const el = $('wall');
  if (!el) return;
  el.hidden = false;
  document.body.style.overflow = 'hidden';
  wallTick();
  clearInterval(wallTimer);
  wallTimer = setInterval(wallTick, 5000);
  if (el.requestFullscreen) el.requestFullscreen().catch(() => {});
}

function wallClose() {
  const el = $('wall');
  if (!el || el.hidden) return;
  el.hidden = true;
  document.body.style.overflow = '';
  clearInterval(wallTimer);
  wallTimer = 0;
  $('wall_grid').innerHTML = '';           // рвём MJPEG-соединения плиток
  if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
}

async function wallTick() {
  const el = $('wall');
  if (!el || el.hidden) return;
  const now = new Date();
  text('wall_clock', now.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' }));
  try {
    const data = await get('/api/wall');
    wallRender(data);
  } catch (e) {
    $('wall_grid').innerHTML = `<div class="wall-empty"><b>Нет связи с коннектором</b><span>${esc(e.message)}</span></div>`;
  }
}

function wallRender(data) {
  const tiles = data.tiles || [];
  const farm = data.farm || {};
  $('wall_stats').innerHTML = [
    ['Печатают', `${farm.printing ?? 0} / ${tiles.length}`],
    ['Загрузка парка', Math.round(num(farm.load)) + '%'],
    ['В очереди', farm.queued ?? 0],
  ].map(([k, v]) => `<div><span>${esc(k)}</span><b>${esc(String(v))}</b></div>`).join('');

  const grid = $('wall_grid');
  if (!tiles.length) {
    grid.innerHTML = '<div class="wall-empty"><b>Принтеры не добавлены</b><span>Добавьте принтер в разделе «Принтеры».</span></div>';
  } else {
    // Перерисовываем плитку целиком только при смене принтера — иначе поток камеры рвался бы каждые 5 секунд.
    const key = tiles.map((t) => t.id).join(',');
    if (grid.dataset.key !== key) {
      grid.dataset.key = key;
      grid.dataset.count = String(tiles.length);
      grid.innerHTML = tiles.map((t) => `<article class="wall-tile" data-tile="${esc(t.id)}">`
        + `<img class="shot" data-cam alt="">`
        + '<div class="veil">'
        + '<div><div class="top"><b data-name></b><span class="demo-tag" data-demo hidden>Демо</span>'
        + '<span class="st" data-st></span></div><div class="task" data-task></div></div>'
        + '<div><div class="pct" data-pct>—</div><div class="pbar"><i data-bar></i></div><div class="facts" data-facts></div></div>'
        + '</div><div class="alarm-line" data-alarm hidden></div></article>').join('');
    }
    tiles.forEach((t) => {
      const el = grid.querySelector(`[data-tile="${CSS.escape(t.id)}"]`);
      if (!el) return;
      const kind = STATE_KIND[t.state] || '';
      const alarm = t.alerts && t.alerts.length ? t.alerts[0] : null;
      el.classList.toggle('running', kind === 'running');
      el.classList.toggle('alarm', !!alarm || t.severity === 'error' || t.severity === 'fatal');
      el.querySelector('[data-name]').textContent = t.name;
      const st = el.querySelector('[data-st]');
      st.textContent = t.online ? (t.state_label || STATE_LABEL[t.state] || t.state) : 'Не в сети';
      st.className = 'st ' + (t.online ? kind : 'bad');
      const order = t.order && t.order.number ? `№${t.order.number} · ${t.order.product || ''}` : '';
      el.querySelector('[data-task]').textContent = order || t.task || 'Нет активной печати';
      const printing = kind === 'running' || t.state === 'PAUSE';
      el.querySelector('[data-pct]').textContent = printing ? Math.round(num(t.progress)) + '%' : '';
      el.querySelector('[data-bar]').style.width = clamp(num(t.progress), 0, 100) + '%';
      el.querySelector('[data-bar]').parentElement.hidden = !printing;
      const facts = [];
      if (t.remaining_min) facts.push(['Осталось', minutesText(t.remaining_min)]);
      if (t.eta) facts.push(['Финиш', String(t.eta).slice(11, 16)]);
      if (t.layer) facts.push(['Слой', `${t.layer}/${t.total_layers || '—'}`]);
      if (t.nozzle) facts.push(['Сопло', Math.round(num(t.nozzle)) + '°']);
      if (t.spent) facts.push(['Потрачено', money(t.spent)]);
      if (t.ams_low) facts.push(['AMS', t.ams_low + ' слот(а) мало пластика']);
      if (t.maintenance_due) facts.push(['ТО', t.maintenance_due + ' просрочено']);
      el.querySelector('[data-facts]').innerHTML = facts
        .map(([k, v]) => `<span>${esc(k)} <b>${esc(String(v))}</b></span>`).join('');
      const alarmEl = el.querySelector('[data-alarm]');
      alarmEl.hidden = !alarm;
      if (alarm) alarmEl.textContent = `${alarm.title}: ${alarm.reason || ''}`;
      el.querySelector('[data-demo]').hidden = !(t.camera && t.camera.demo);
      const img = el.querySelector('[data-cam]');
      const want = t.camera && t.camera.available
        ? `/api/printer/camera.mjpeg?printer_id=${encodeURIComponent(t.id)}&t=${camSession}` : '';
      if (want && img.dataset.src !== want) { img.dataset.src = want; img.src = want; }
      if (!want && img.dataset.src) { img.removeAttribute('src'); delete img.dataset.src; }
    });
  }

  const q = data.queue || [];
  text('wall_queue', q.length
    ? 'В очереди: ' + q.map((j) => j.order ? `№${j.order}` : (j.name || 'задание')).join(' · ')
    : 'Очередь пуста');
  text('wall_note', data.quiet
    ? 'Тихие часы: автозапуск отложен · Esc — выход'
    : 'Обновляется автоматически · Esc — выход');
}

/* ============================================================ команды */
async function command(name, value, opts) {
  opts = opts || {};
  try {
    const p = requireLive();
    const ask = opts.confirm || DANGER[name];
    if (ask && !confirmDanger(ask)) return;
    await post('/api/printer/command', { printer_id: p.id, command: name, value });
    toast('Команда отправлена', opts.label || name);
    setTimeout(PF.poll, 500);
  } catch (e) { fail(e); }
}

/* ======================================================== файлы SD */
function fileIcon(name) { return /\.3mf$/i.test(name) ? '◲' : '⎘'; }
function sizeText(bytes) {
  const b = num(bytes);
  if (!b) return '';
  if (b > 1048576) return (b / 1048576).toFixed(1) + ' МБ';
  if (b > 1024) return Math.round(b / 1024) + ' КБ';
  return b + ' Б';
}
async function loadFiles() {
  const p = active();
  const host = $('pr_files');
  if (!p) return;
  if (!p.connection.connected) {
    // Не дёргаем FTPS впустую: недоступный принтер отвечает долгим таймаутом.
    host.innerHTML = '<div class="empty compact"><span>Принтер не на связи — список файлов недоступен.</span></div>';
    return;
  }
  host.innerHTML = '<div class="skeleton" style="height:60px"></div>';
  try {
    const data = await get('/api/printer/files', { printer_id: p.id });
    filesCache = data.files || [];
    host.innerHTML = filesCache.length ? filesCache.map((f) => `<div class="file-row">`
      + `<span class="fic">${fileIcon(f.name)}</span>`
      + `<span class="fname" title="${esc(f.path || f.name)}">${esc(f.name)}</span>`
      + `<span class="fsize">${esc(sizeText(f.size))}</span>`
      + '<span class="acts">'
      + `<button class="btn sm primary" type="button" data-print-file="${esc(f.path || f.name)}">Печать</button>`
      + `<button class="icon-btn sm danger" type="button" data-del-file="${esc(f.path || f.name)}">×</button>`
      + '</span></div>').join('')
      : '<div class="empty compact"><span>На SD-карте нет 3MF и G-code файлов.</span></div>';
  } catch (e) {
    host.innerHTML = `<div class="notice bad"><span>✕</span><span>${esc(e.message)}</span></div>`;
  }
}

async function uploadFile(file) {
  const p = active();
  if (!p) return fail(new Error('Сначала добавьте принтер'));
  if (!/\.(3mf|gcode)$/i.test(file.name)) return fail(new Error('Поддерживаются только 3MF и G-code'));
  const form = new FormData();
  form.append('file', file);
  form.append('printer_id', p.id);
  toast('Загружаем на принтер', file.name, 'info');
  try {
    const res = await api('/api/printer/upload', { method: 'POST', body: form });
    const est = res && res.estimate ? res.estimate : {};
    const bits = [];
    if (num(est.minutes)) bits.push(minutesText(est.minutes));
    if (num(est.grams)) bits.push('~' + nfmt(est.grams) + ' г');
    if (est.material) bits.push(est.material);
    if (est.color) bits.push(est.color);
    toast('Файл загружен', bits.length ? 'Оценка из 3MF: ' + bits.join(' · ') : file.name);
    loadFiles();
  } catch (e) { fail(e); }
}

/* ================================================== запуск печати */
function fillPrintModal(path) {
  pendingFile = path;
  $('pj_file').value = path;
  $('print_modal_title').textContent = 'Печать: ' + path.split('/').pop();
  $('pj_printer').innerHTML = (PF.state.live && PF.state.live.printers || [])
    .map((p) => `<option value="${esc(p.id)}"${p.id === PF.state.activePrinter ? ' selected' : ''}>${esc(p.name)}</option>`).join('');
  const open = PF.state.orders.filter((o) => !PF.isFinal(o));
  $('pj_order').innerHTML = '<option value="">Без заказа</option>' + open
    .map((o) => `<option value="${esc(o.id)}">№${esc(o.number)} · ${esc(o.product)}</option>`).join('');
  $('pj_spool').innerHTML = '<option value="">Определить автоматически</option>' + PF.state.spools
    .map((s) => `<option value="${esc(s.id)}">${esc(s.material)} ${esc(s.color_name)} · ${Math.round(num(s.remaining_grams))} г</option>`).join('');
  const guess = open.find((o) => o.file && path.toLowerCase().includes(String(o.file).toLowerCase()));
  if (guess) $('pj_order').value = guess.id;
  openModal('print_modal');
}
function printPayload() {
  const mapping = $('pj_ams_mapping').value.split(',').map((x) => parseInt(x, 10)).filter((x) => Number.isFinite(x));
  return {
    printer_id: $('pj_printer').value,
    file: pendingFile,
    name: pendingFile ? pendingFile.split('/').pop() : '',
    plate: num($('pj_plate').value, 1) || 1,
    order_id: $('pj_order').value || '',
    spool_id: $('pj_spool').value || '',
    use_ams: $('pj_use_ams').checked,
    bed_level: $('pj_bed_level').checked,
    flow_cali: $('pj_flow_cali').checked,
    timelapse: $('pj_timelapse').checked,
    ams_mapping: mapping,
  };
}

/* ============================================================ очередь */
function jobStateChip(state) {
  const map = {
    queued: ['outline', 'В очереди'], starting: ['accent', 'Стартует'], running: ['accent', 'Печатает'],
    done: ['ok', 'Готово'], failed: ['bad', 'Брак'], cancelled: ['warn', 'Отменено'],
  };
  const [cls, label] = map[state] || ['outline', state];
  return `<span class="chip ${cls}">${esc(label)}</span>`;
}
function renderQueue() {
  const queue = PF.state.jobs.queue || [];
  const history = PF.state.jobs.history || [];
  const running = queue.filter((j) => j.state === 'running').length;
  text('queue_sub', `${queue.length} в работе и в очереди · ${running} печатается`);
  const tag = $('nav_queue_tag');
  tag.hidden = !queue.length;
  tag.textContent = String(queue.length);
  tag.className = 'tag' + (running ? ' live' : '');

  $('queue_list').innerHTML = queue.length ? queue.map((j, i) => {
    const printer = PF.state.printers.find((p) => p.id === j.printer_id);
    const order = j.order;
    return `<div class="queue-item${j.state === 'running' ? ' running' : ''}">`
      + `<span class="qnum">${i + 1}</span><div class="qbody"><b>${esc(j.name || j.file || 'Задание')}</b>`
      + `<small>${jobStateChip(j.state)} ${esc(printer ? printer.name : 'любой принтер')}`
      + (order ? ` · заказ №${esc(order.number)}` : '')
      + (num(j.est_minutes) ? ` · оценка ${minutesText(j.est_minutes)}${num(j.est_grams) ? ' · ~' + nfmt(j.est_grams) + ' г' : ''}` : '')
      + '</small>'
      + (j.state === 'running' ? `<div class="bar thin" style="margin-top:6px"><i style="width:${clamp(num(j.progress), 0, 100)}%"></i></div>` : '')
      + '</div><div class="acts">'
      + (j.state === 'queued' ? `<button class="btn sm primary" type="button" data-job-start="${esc(j.id)}">Печать</button>` : '')
      + `<button class="icon-btn sm danger" type="button" data-job-cancel="${esc(j.id)}" title="Отменить">×</button>`
      + '</div></div>';
  }).join('') : '<div class="empty"><span class="big">≡</span><b>Очередь пуста</b><span>Добавьте задание или запустите файл с принтера.</span></div>';

  const hq = ($('history_search') || {}).value || '';
  const hfiltered = hq ? history.filter((j) => String(j.name || j.file || '').toLowerCase().includes(hq.toLowerCase())) : history;
  $('queue_history').innerHTML = hfiltered.length ? hfiltered.slice(0, 24).map((j) => `<div class="tx-row">`
    + `<span class="tx-ic ${j.state === 'done' ? 'income' : 'expense'}">${j.state === 'done' ? '✓' : '✕'}</span>`
    + `<div class="tx-body"><b>${esc(j.name || j.file || 'Печать')}</b>`
    + `<small>${esc(dateTimeText(j.finished_at))} · ${minutesText(j.duration_min)} · ${nfmt(j.grams)} г`
    + (num(j.est_minutes) ? ' · оценка была ' + minutesText(j.est_minutes) : '') + '</small></div>'
    + `<span class="amt">${money(j.cost)}</span></div>`).join('')
    : '<div class="empty compact"><span>' + (hq ? 'Ничего не найдено.' : 'Завершённых печатей пока нет.') + '</span></div>';
}
$('history_search').addEventListener('input', U.debounce(renderQueue, 200));

/* ==================================================== профиль принтера */
function openPrinterModal(id) {
  editingPrinter = id || null;
  const p = id ? PF.printer(id) : null;
  $('printer_modal_title').textContent = p ? 'Настройка: ' + p.name : 'Новый принтер';
  $('pf_name').value = p ? p.name : 'Основной P1S';
  $('pf_model').value = (p && p.model) || 'P1S';
  $('pf_host').value = (p && p.host) || '';
  $('pf_serial').value = (p && p.serial) || '';
  $('pf_access_code').value = '';
  $('pf_access_code').placeholder = p && p.has_access_code
    ? 'Сохранён · оставьте пустым, чтобы не менять' : '8 символов с экрана принтера';
  $('pf_has_ams').value = p ? String(p.has_ams ? 1 : 0) : '1';
  $('pf_enabled').value = p ? String(p.enabled ? 1 : 0) : '1';
  $('pf_notes').value = (p && p.notes) || '';
  $('pf_guard').value = p ? String(p.guard_enabled == null || p.guard_enabled ? 1 : 0) : '1';
  $('pf_camera_demo').value = p && p.camera_demo ? '1' : '0';
  $('pf_nozzle_size').value = (p && p.nozzle_size) || '0.4';
  $('pf_discovered').innerHTML = '';
  $('pf_result').innerHTML = '';
  $('printer_delete').hidden = !id;
  openModal('printer_modal');
}
async function savePrinter() {
  const payload = {
    id: editingPrinter || '',
    name: $('pf_name').value.trim() || 'Принтер',
    model: $('pf_model').value,
    host: $('pf_host').value.trim(),
    serial: $('pf_serial').value.trim(),
    access_code: $('pf_access_code').value,
    has_ams: +$('pf_has_ams').value,
    enabled: +$('pf_enabled').value,
    notes: $('pf_notes').value.trim(),
    guard_enabled: +$('pf_guard').value,
    camera_demo: +$('pf_camera_demo').value,
    nozzle_size: $('pf_nozzle_size').value.trim() || '0.4',
  };
  if (!payload.host) return fail(new Error('Укажите IP-адрес принтера'));
  $('pf_result').innerHTML = '<div class="notice"><span>⏳</span><span>Сохраняем и подключаемся…</span></div>';
  try {
    const res = await post('/api/printer/save', payload);
    PF.state.activePrinter = res.printer.id;
    await PF.refreshBootstrapPrinters();
    $('pf_result').innerHTML = '<div class="notice ok"><span>✓</span><span>Сохранено. Подключение занимает 5–10 секунд.</span></div>';
    toast('Принтер сохранён', payload.name);
    setTimeout(() => closeModal('printer_modal'), 900);
    setTimeout(PF.poll, 1500);
  } catch (e) {
    $('pf_result').innerHTML = `<div class="notice bad"><span>✕</span><span>${esc(e.message)}</span></div>`;
  }
}
PF.refreshBootstrapPrinters = async () => {
  const data = await get('/api/printers');
  PF.state.printers = data.printers || [];
  PF.emit('printers', PF.state.printers);
};

async function discover() {
  const box = $('pf_discovered');
  box.innerHTML = '<div class="skeleton" style="height:44px"></div>';
  try {
    const data = await get('/api/printer/discover');
    const list = data.found || [];
    box.innerHTML = list.length ? list.map((p) => `<button type="button" data-found='${esc(JSON.stringify(p))}'>`
      + `<b>${esc(p.name || p.model || 'Bambu Lab')}</b><span>${esc(p.host)} · ${esc(p.serial || 'серийный номер не найден')}</span></button>`).join('')
      : '<div class="notice"><span>ℹ</span><span>Автоматически не найдено. Введите IP и серийный номер вручную (Settings → WLAN на принтере).</span></div>';
  } catch (e) {
    box.innerHTML = `<div class="notice bad"><span>✕</span><span>${esc(e.message)}</span></div>`;
  }
}

/* ====================================================== задание вручную */
function openJob() {
  $('jf_name').value = '';
  $('jf_file').value = '';
  $('jf_plate').value = '1';
  $('jf_priority').value = '0';
  $('jf_printer_id').innerHTML = '<option value="">Любой свободный</option>' + PF.state.printers
    .map((p) => `<option value="${esc(p.id)}">${esc(p.name)}</option>`).join('');
  $('jf_order_id').innerHTML = '<option value="">Без заказа</option>' + PF.state.orders
    .filter((o) => !PF.isFinal(o)).map((o) => `<option value="${esc(o.id)}">№${esc(o.number)} · ${esc(o.product)}</option>`).join('');
  openModal('job_modal');
}

/* ============================================================= события */
function bind() {
  $('pr_tabs').addEventListener('click', (e) => {
    const btn = e.target.closest('[data-printer]');
    if (!btn) return;
    PF.state.activePrinter = btn.dataset.printer;
    renderLive();
    loadFiles();
    loadEvents();
  });

  document.addEventListener('click', (e) => {
    const cmd = e.target.closest('[data-cmd]');
    if (cmd) {
      const value = cmd.dataset.value !== undefined ? num(cmd.dataset.value) : undefined;
      command(cmd.dataset.cmd, value, { label: cmd.textContent.trim() });
      return;
    }
    const jog = e.target.closest('[data-jog]');
    if (jog) {
      const [axis, dist] = jog.dataset.jog.split(':');
      command('move', { axis, distance: num(dist) }, { confirm: '', label: `${axis} ${dist} мм` });
      return;
    }
    const set = e.target.closest('[data-set]');
    if (set) {
      const map = { nozzle_temp: 'pr_set_nozzle', bed_temp: 'pr_set_bed', part_fan: 'pr_set_fan' };
      command(set.dataset.set, num($(map[set.dataset.set]).value), { label: set.dataset.set });
      return;
    }
    const load = e.target.closest('[data-ams-load]');
    if (load) { command('load_filament', num(load.dataset.amsLoad), { confirm: 'Подать филамент из этого слота? Сопло нагреется.' }); return; }
    const amsEdit = e.target.closest('[data-ams-edit]');
    if (amsEdit) {
      const [unit, tray] = amsEdit.dataset.amsEdit.split(':');
      const type = window.prompt('Тип пластика в слоте (PLA, PETG, ABS, TPU…):', amsEdit.dataset.type || 'PLA');
      if (!type) return;
      command('ams_filament', { ams_id: num(unit), tray_id: num(tray), type: type.toUpperCase(), color: amsEdit.dataset.color },
        { confirm: '', label: 'AMS ' + type });
      return;
    }
    const pf = e.target.closest('[data-print-file]');
    if (pf) { fillPrintModal(pf.dataset.printFile); return; }
    const df = e.target.closest('[data-del-file]');
    if (df) {
      if (!confirmDanger('Удалить файл с SD-карты принтера?')) return;
      post('/api/printer/file/delete', { printer_id: PF.state.activePrinter, path: df.dataset.delFile })
        .then(() => { toast('Файл удалён'); loadFiles(); }).catch(fail);
      return;
    }
    const js = e.target.closest('[data-job-start]');
    if (js) {
      post('/api/jobs/start', { id: js.dataset.jobStart, printer_id: PF.state.activePrinter })
        .then(() => { toast('Задание запущено'); PF.refreshCore(); }).catch(fail);
      return;
    }
    const jc = e.target.closest('[data-job-cancel]');
    if (jc) {
      if (!confirmDanger('Отменить задание? Если оно печатается — печать будет остановлена.')) return;
      post('/api/jobs/cancel', { id: jc.dataset.jobCancel })
        .then(() => { toast('Задание отменено'); PF.refreshCore(); }).catch(fail);
      return;
    }
    const found = e.target.closest('[data-found]');
    if (found) {
      const p = JSON.parse(found.dataset.found);
      $('pf_host').value = p.host || '';
      $('pf_serial').value = p.serial || '';
      if (p.name && !$('pf_name').value) $('pf_name').value = p.name;
      toast('Данные подставлены', 'Введите Access Code и сохраните');
    }
  });

  $('pr_speed_apply').addEventListener('click', () =>
    command('speed', num($('pr_speed_sel').value, 2), { label: 'скорость' }));
  $('pr_reconnect').addEventListener('click', async () => {
    try {
      await post('/api/printer/connect', { printer_id: PF.state.activePrinter });
      toast('Переподключаемся', 'Обычно занимает 5–10 секунд');
      setTimeout(PF.poll, 1500);
    } catch (e) { fail(e); }
  });
  $('pr_add').addEventListener('click', () => openPrinterModal());
  $('pr_edit').addEventListener('click', () => {
    if (!PF.state.activePrinter) return openPrinterModal();
    openPrinterModal(PF.state.activePrinter);
  });
  $('printer_save').addEventListener('click', savePrinter);
  $('printer_delete').addEventListener('click', async () => {
    if (!editingPrinter || !confirmDanger('Удалить профиль принтера? История печати сохранится.')) return;
    try {
      await post('/api/printer/delete', { id: editingPrinter });
      PF.state.activePrinter = '';
      await PF.refreshBootstrapPrinters();
      closeModal('printer_modal');
      toast('Принтер удалён');
      PF.poll();
    } catch (e) { fail(e); }
  });
  $('pf_discover').addEventListener('click', discover);
  $('pf_ftps_test').addEventListener('click', async () => {
    try {
      const res = await post('/api/printer/ftps-test', { printer_id: editingPrinter || PF.state.activePrinter });
      $('pf_result').innerHTML = `<div class="notice ${res.ok ? 'ok' : 'bad'}"><span>${res.ok ? '✓' : '✕'}</span><span>${esc(res.ok ? 'FTPS доступен, файлы читаются.' : (res.error || 'Не удалось подключиться'))}</span></div>`;
    } catch (e) { $('pf_result').innerHTML = `<div class="notice bad"><span>✕</span><span>${esc(e.message)}</span></div>`; }
  });

  $('pr_cam_reload').addEventListener('click', () => {
    camSession = Date.now(); camStream = '';        // новый ключ — поток пересоберётся
    PF.poll();
  });
  $('pr_cam_full').addEventListener('click', () => {
    const p = active();
    if (!p) return;
    $('cam_full').src = camUrl(p, true);
    openModal('cam_modal');
  });
  $('cam_modal').addEventListener('close', () => { $('cam_full').removeAttribute('src'); });
  $('pr_cam_shot').addEventListener('click', async () => {
    try {
      const p = active();
      if (!p) throw new Error('Принтер ещё не добавлен');
      await post('/api/printer/snapshot', { printer_id: p.id, note: 'Снимок вручную' });
      shotsKey = '';                       // лента изменилась — перечитать архив
      toast('Кадр сохранён', 'Появится в ленте под камерой');
      PF.poll();
    } catch (e) { fail(e); }
  });
  $('pr_shots').addEventListener('click', (e) => {
    const img = e.target.closest('[data-shot]');
    if (!img) return;
    $('cam_full').src = img.src;
    openModal('cam_modal');
  });
  $('pr_chart_range').addEventListener('change', () => {
    const p = active();
    if (p) renderChart(p, true);
  });
  $('pr_alerts').addEventListener('click', async (e) => {
    const btn = e.target.closest('[data-alert-clear]');
    if (!btn) return;
    try {
      await post('/api/printer/alerts/clear', { printer_id: btn.dataset.alertClear });
      PF.poll();
    } catch (err) { fail(err); }
  });
  $('pr_maint').addEventListener('click', (e) => {
    const btn = e.target.closest('[data-maint]');
    if (!btn) return;
    const p = active();
    const task = ((p && p.maintenance && p.maintenance.tasks) || []).find((t) => t.id === btn.dataset.maint);
    if (!task) return;
    maintTask = task;
    $('mf_title').textContent = task.task;
    $('mf_info').innerHTML = `<span>ⓘ</span><span>Интервал ${esc(String(task.every_hours || '—'))} ч.`
      + (task.last_at ? ` Последний раз: ${esc(dateTextSafe(task.last_at))}.` : ' Ещё не выполнялась.')
      + '</span>';
    $('mf_note').value = '';
    openModal('maint_modal');
  });
  $('mf_done').addEventListener('click', async () => {
    if (!maintTask) return;
    try {
      await post('/api/printer/maintenance/done', { id: maintTask.id, note: $('mf_note').value.trim() });
      closeModal('maint_modal');
      toast('Отмечено', maintTask.task);
      maintTask = null;
      PF.poll();
    } catch (e) { fail(e); }
  });

  $('pr_wall').addEventListener('click', wallOpen);
  $('wall_close').addEventListener('click', wallClose);
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !$('wall').hidden) { e.preventDefault(); wallClose(); }
  });

  $('pr_files_refresh').addEventListener('click', loadFiles);
  $('pr_upload_btn').addEventListener('click', () => $('pr_file_input').click());
  $('pr_file_input').addEventListener('change', (e) => {
    if (e.target.files[0]) uploadFile(e.target.files[0]);
    e.target.value = '';
  });
  const drop = $('pr_drop');
  ['dragenter', 'dragover'].forEach((ev) => drop.addEventListener(ev, (e) => {
    e.preventDefault(); drop.classList.add('hot');
  }));
  ['dragleave', 'drop'].forEach((ev) => drop.addEventListener(ev, (e) => {
    e.preventDefault(); drop.classList.remove('hot');
  }));
  drop.addEventListener('drop', (e) => {
    const file = e.dataTransfer && e.dataTransfer.files[0];
    if (file) uploadFile(file);
  });

  $('pj_start').addEventListener('click', async () => {
    if (!confirmDanger('Запустить печать? Принтер немедленно начнёт нагрев и движение.')) return;
    try {
      await post('/api/printer/print', printPayload());
      closeModal('print_modal');
      toast('Печать запущена', pendingFile);
      PF.refreshCore();
      setTimeout(PF.poll, 1200);
    } catch (e) { fail(e); }
  });
  $('pj_queue').addEventListener('click', async () => {
    try {
      await post('/api/jobs/enqueue', printPayload());
      closeModal('print_modal');
      toast('Добавлено в очередь', pendingFile);
      PF.refreshCore();
    } catch (e) { fail(e); }
  });

  $('queue_add').addEventListener('click', openJob);
  $('job_save').addEventListener('click', async () => {
    const file = $('jf_file').value.trim();
    if (!file) return fail(new Error('Укажите имя файла на принтере'));
    try {
      await post('/api/jobs/enqueue', {
        name: $('jf_name').value.trim() || file,
        file,
        printer_id: $('jf_printer_id').value,
        order_id: $('jf_order_id').value,
        plate: num($('jf_plate').value, 1) || 1,
        priority: num($('jf_priority').value),
        use_ams: $('jf_use_ams').checked,
        bed_level: $('jf_bed_level').checked,
        source: 'manual',
      });
      closeModal('job_modal');
      toast('Задание добавлено');
      PF.refreshCore();
    } catch (e) { fail(e); }
  });
  $('queue_auto').addEventListener('change', async (e) => {
    try {
      await post('/api/settings', { auto_queue: e.target.checked });
      PF.state.settings.auto_queue = e.target.checked;
      toast(e.target.checked ? 'Автозапуск включён' : 'Автозапуск выключен',
        e.target.checked ? 'Следующее задание стартует само' : 'Задания запускаются вручную');
    } catch (err) { fail(err); }
  });

  $('pr_events_refresh').addEventListener('click', loadEvents);

  // Диагностика связи (5.0): порты принтера и сравнение подсети.
  $('pr_net_btn').addEventListener('click', async () => {
    const p = PF.printer(PF.state.activePrinter);
    const host = (p && p.host) || '';
    const box = $('pr_net');
    if (!host) { box.innerHTML = '<div class="empty compact"><span>У принтера не задан IP-адрес.</span></div>'; return; }
    box.innerHTML = '<div class="skeleton" style="height:44px"></div>';
    try {
      const data = await get('/api/network/diagnose', { host });
      const dot = { ok: 'ok', warn: 'warn', bad: 'bad' }[data.level] || '';
      box.innerHTML = `<div class="notice ${dot}"><span>${dot === 'ok' ? '✓' : '⚠'}</span><span>${esc(data.text)}</span></div>`
        + (data.ports || []).map((p2) => `<div class="tx-row">`
          + `<span class="tx-ic ${p2.ok ? 'income' : 'expense'}">${p2.ok ? '✓' : '✕'}</span>`
          + `<div class="tx-body"><b>${esc(p2.name)} · порт ${p2.port}</b><small>${esc(p2.label)}</small></div>`
          + `<span class="amt">${p2.ok ? p2.ms + ' мс' : 'нет ответа'}</span></div>`).join('');
      if (data.local_ips && data.local_ips.length) {
        box.insertAdjacentHTML('beforeend', '<small class="muted">Этот компьютер: ' + data.local_ips.map(esc).join(', ') + '</small>');
      }
    } catch (e) { box.innerHTML = `<div class="notice bad"><span>✕</span><span>${esc(e.message)}</span></div>`; }
  });
}

async function loadEvents() {
  const host = $('pr_events');
  if (!host) return;
  try {
    const data = await get('/api/events', { limit: 30, printer_id: PF.state.activePrinter });
    const list = data.events || [];
    host.innerHTML = list.length ? list.map((e) => `<div class="event ${esc(e.kind)}"><span class="edot"></span>`
      + `<span class="etext"><b>${esc(e.title)}</b><small>${esc(e.detail || '')}</small></span>`
      + `<time>${esc(dateTimeText(e.at))}</time></div>`).join('')
      : '<div class="empty compact"><span>Событий пока нет.</span></div>';
  } catch (err) {
    host.innerHTML = '<div class="empty compact"><span>Журнал недоступен.</span></div>';
  }
}

/* =============================================================== старт */
PF.on('ready', () => { bindAmsProfiles(); bindSchedule();
  bind();
  renderTabs();
  $('queue_auto').checked = !!PF.state.settings.auto_queue;
  const tag = $('nav_printers_tag');
  const total = PF.state.printers.length;
  tag.hidden = !total;
  tag.textContent = String(total);
});
PF.on('live', () => { renderLive(); });
PF.on('data', () => { renderQueue(); });
PF.on('printers', () => { renderTabs(); });
PF.on('view', (d) => {
  if (d.view === 'printers') { loadFiles(); loadEvents(); }
});

PF.modules.printer = { command, openJob, loadFiles, renderLive, openPrinterModal, fillPrintModal };
})();
