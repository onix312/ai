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

let camTimer = 0, filesCache = [], pendingFile = null, editingPrinter = null;

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

function renderHealth(p) {
  const hms = Array.isArray(p.printer.hms) ? p.printer.hms : [];
  const err = p.printer.print_error && !['0', 0].includes(p.printer.print_error);
  const count = hms.length + (err ? 1 : 0);
  const badge = $('pr_health');
  badge.className = 'chip ' + (count ? 'bad' : 'ok');
  badge.textContent = count ? `${count} проблем(ы)` : 'Нет ошибок';
  $('pr_errors').innerHTML = count
    ? (err ? `<div class="hms-item"><b>Ошибка печати</b><span>код ${esc(String(p.printer.print_error))}</span></div>` : '')
      + hms.map((h) => `<div class="hms-item"><b>HMS ${esc(String(h.attr ?? ''))}</b><span>${esc(String(h.code ?? JSON.stringify(h)))}</span></div>`).join('')
    : '<div class="empty compact"><span>Активных ошибок нет.</span></div>';
}

function renderCamera(p) {
  const cam = p.camera || {};
  text('pr_cam_status', cam.available ? 'Поток активен' : (cam.error || 'Нет сигнала'));
  text('pr_cam_age', cam.available ? (cam.age < 3 ? 'Кадр только что' : `Кадр ${Math.round(cam.age)} сек. назад`) : '—');
  $('pr_cam_empty').hidden = !!cam.available;
  $('pr_cam').classList.toggle('on', !!cam.available);
  if (cam.available && Date.now() - camTimer > 1200) {
    camTimer = Date.now();
    const url = `/api/printer/camera.jpg?printer_id=${encodeURIComponent(p.id)}&t=${camTimer}`;
    $('pr_cam').src = url;
    if ($('cam_modal').open) $('cam_full').src = url;
  }
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
    await api('/api/printer/upload', { method: 'POST', body: form });
    toast('Файл загружен', file.name);
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
      + (order ? ` · заказ №${esc(order.number)}` : '') + '</small>'
      + (j.state === 'running' ? `<div class="bar thin" style="margin-top:6px"><i style="width:${clamp(num(j.progress), 0, 100)}%"></i></div>` : '')
      + '</div><div class="acts">'
      + (j.state === 'queued' ? `<button class="btn sm primary" type="button" data-job-start="${esc(j.id)}">Печать</button>` : '')
      + `<button class="icon-btn sm danger" type="button" data-job-cancel="${esc(j.id)}" title="Отменить">×</button>`
      + '</div></div>';
  }).join('') : '<div class="empty"><span class="big">≡</span><b>Очередь пуста</b><span>Добавьте задание или запустите файл с принтера.</span></div>';

  $('queue_history').innerHTML = history.length ? history.slice(0, 24).map((j) => `<div class="tx-row">`
    + `<span class="tx-ic ${j.state === 'done' ? 'income' : 'expense'}">${j.state === 'done' ? '✓' : '✕'}</span>`
    + `<div class="tx-body"><b>${esc(j.name || j.file || 'Печать')}</b>`
    + `<small>${esc(dateTimeText(j.finished_at))} · ${minutesText(j.duration_min)} · ${nfmt(j.grams)} г</small></div>`
    + `<span class="amt">${money(j.cost)}</span></div>`).join('')
    : '<div class="empty compact"><span>Завершённых печатей пока нет.</span></div>';
}

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

  $('pr_cam_reload').addEventListener('click', () => { camTimer = 0; PF.poll(); });
  $('pr_cam_full').addEventListener('click', () => {
    const p = active();
    if (!p) return;
    $('cam_full').src = `/api/printer/camera.jpg?printer_id=${encodeURIComponent(p.id)}&t=${Date.now()}`;
    openModal('cam_modal');
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
PF.on('ready', () => {
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
