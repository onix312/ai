/* PrintFlow 2.0 — принтеры Bambu Lab: телеметрия, управление, AMS,
   камера, файлы SD (FTPS) и очередь печати парка. */
(() => {
'use strict';
const U = PF.ui, { $, $$, esc, num, clamp, money, nfmt, hoursText, minutesText,
  dateText, dateTimeText, agoText, toast, fail, openModal, closeModal, confirmDanger, ask } = U;
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
  pause: 'Поставить печать на паузу? Оператор должен контролировать состояние принтера.',
  resume: 'Продолжить печать? Принтер снова нагреется и продолжит движение.',
  load_filament: 'Подать филамент? Сопло нагреется и AMS сменит материал.',
  stop: 'Остановить печать? Задание будет прервано, деталь придётся печатать заново.',
  bed_level: 'Запустить калибровку стола? Принтер начнёт движение и нагрев.',
  calibration: 'Запустить полную калибровку? Займёт несколько минут.',
  unload_filament: 'Выгрузить филамент? Сопло нагреется.',
  extrude: 'Подать филамент? Сопло должно быть нагрето до рабочей температуры.',
  home: 'Припарковать оси? Принтер начнёт движение.',
  nozzle_temp: 'Задать температуру сопла? Принтер начнёт нагрев.',
  bed_temp: 'Задать температуру стола? Принтер начнёт нагрев.',
};

let filesCache = [], pendingFile = null, editingPrinter = null, queueLocalFile = null, filesPath = '/';
let camStream = '', camSession = Date.now();     // ключ живого MJPEG-соединения
let bedMapOn = {};        // 119: тумблер проекции плиты по printer_id
let bedMapKey = '';       // 119: последний нарисованный набор полигонов (не перерисовывать зря)
let bedMapBusy = false;
let bedMapCache = null;   // 119: последние полученные объекты (для клика-исключения)
let shotsKey = '';                              // чтобы не перезапрашивать архив зря
let chartCache = { id: '', minutes: 0, at: 0, points: [] };
let wallTimer = 0, maintTask = null;

/* ============================================================ хелперы */
const active = () => PF.livePrinter();
function requireLive() {
  const p = active();
  if (!p) throw new Error('Принтер ещё не добавлен');
  if (!p.connection.connected) throw new Error(p.connection.mode === 'cloud'
    ? 'Принтер не подключён. Проверьте вход в Bambu Cloud (Настройки → Bambu Cloud).'
    : 'Принтер не подключён. Проверьте IP, серийный номер и Access Code.');
  return p;
}
function bar(el, percent) { if (el) el.style.width = clamp(num(percent), 0, 100) + '%'; }
function text(id, value) { const el = $(id); if (el) el.textContent = value; }

/* ================================================ лента парка (12.2, ПР1)
   Каждый принтер — карточка-«пульт»: кольцо прогресса, состояние, что
   печатает и сколько осталось, катушки AMS, флажки тревог. Клик — выбрать
   принтер. Пока live-снимка нет, но принтеры заведены — показываем
   скелетоны, чтобы вкладка не выглядела пустой (ПР8). */
const PK_C = 2 * Math.PI * 15.5;   // длина окружности мини-кольца (r = 15.5)

function renderTabs() {
  const live = PF.state.live;
  const list = (live && live.printers) || [];
  const host = $('pr_park') || $('pr_tabs');
  if (!host) return;
  const configured = (PF.state.printers || []).length;
  $('pr_empty').hidden = list.length > 0 || configured > 0;
  $('pr_workspace').hidden = list.length === 0 && configured === 0;
  if (!list.length) {
    host.innerHTML = configured && !live
      ? Array.from({ length: configured }, () =>
        `<div class="pk-card skel" aria-busy="true"><span class="pk-ring"><i class="skel" style="width:34px;height:34px;border-radius:50%"></i></span>`
        + `<span class="pk-main"><i class="skel" style="width:56%;height:12px;display:block"></i>`
        + `<i class="skel" style="width:82%;height:10px;margin-top:6px;display:block"></i></span></div>`).join('')
      : '';
    return;
  }
  if (!PF.state.activePrinter || !list.some((p) => p.id === PF.state.activePrinter)) {
    PF.state.activePrinter = (live.active && live.active.id) || list[0].id;
  }
  host.innerHTML = list.map((p) => {
    const st = p.printer.state, kind = STATE_KIND[st] || '';
    const conn = p.connection.connected;
    const progress = clamp(num(p.printer.progress), 0, 100);
    const running = kind === 'running';
    const problems = (p.printer.problems || []).length;
    const alerts = (((p.guard || {}).alerts) || []).length;
    const maint = num((p.maintenance || {}).due);
    const trays = ((p.ams || {}).trays) || [];
    const lowFil = trays.some((t) => t.remain != null && t.remain >= 0 && num(t.remain) < 15);
    const sw = trays.slice(0, 4).map((t) => {
      // 13.1 (24): остаток на «трубке» AMS — затемнение снизу по проценту
      const has = t.present !== false && (t.present || t.generic || t.type || t.uuid);
      const remain = num(t.remain, -1);
      const pct = remain >= 0 ? clamp(remain, 0, 100) : null;
      return `<span class="pk-tube${has ? '' : ' ghost'}" title="${esc((t.label || 'Слот') + ' · ' + (has ? (t.type || 'AMS') : 'пусто') + (pct != null ? ` · остаток ${Math.round(pct)}%` : ''))}">`
        + `<i class="pk-tube-body" style="background:${esc(t.color || '#38445c')}">${pct != null ? `<b style="height:${Math.round(pct)}%"></b>` : ''}</i></span>`;
    }).join('');
    const sub = running
      ? `${esc(String(p.printer.task || 'Печать').slice(0, 34))} · осталось ${p.printer.remaining_min ? minutesText(p.printer.remaining_min) : '—'}`
      : conn ? esc(p.printer.state_label || STATE_LABEL[st] || st)
        : esc(String(p.connection.last_error || 'Нет связи').slice(0, 40));
    return `<button class="pk-card${p.id === PF.state.activePrinter ? ' on' : ''}${conn ? '' : ' off'}${running ? ' run' : ''}" type="button" data-printer="${esc(p.id)}"`
      + ` title="${esc(p.name)} · ${esc(p.printer.state_label || STATE_LABEL[st] || st)}">`
      + printerSilhouette(p)
      + `<span class="pk-ring"><svg viewBox="0 0 40 40" aria-hidden="true">`
      + `<circle class="tr" cx="20" cy="20" r="15.5"/>`
      + `<circle class="fl" cx="20" cy="20" r="15.5" stroke-dasharray="${PK_C.toFixed(1)}" stroke-dashoffset="${conn ? (PK_C * (1 - progress / 100)).toFixed(1) : PK_C.toFixed(1)}"/>`
      + `</svg><b>${conn ? (running ? Math.round(progress) + '%' : conn ? '✓' : '') : '◌'}</b></span>`
      + `<span class="pk-main"><b>${esc(p.name)}</b><small>${sub}</small>`
      + `<span class="pk-ams">${sw}</span></span>`
      + `<span class="pk-flags">`
      + (alerts ? `<i class="fl alarm" title="Тревога сторожа печати">!</i>` : '')
      + (problems ? `<i class="fl hms" title="HMS: ${problems} — откройте карточку">▲</i>` : '')
      + (maint ? `<i class="fl maint" title="Нужно обслуживание">⚙</i>` : '')
      + (lowFil ? `<i class="fl low" title="Мало пластика в AMS">◍</i>` : '')
      + `</span></button>`;
  }).join('');
}

/* ======================================================== телеметрия */
/* В11: живой силуэт принтера — машина вместо процентов. Сопло/стол
   подсвечиваются фактическим нагревом, дверца «дышит» во время печати. */
function printerSilhouette(p) {
  const t = p.temperature || {};
  const nozzleHot = num(t.nozzle) > 90;
  const bedHot = num(t.bed) > 35;
  const running = STATE_KIND[p.printer.state] === 'running';
  const cls = ['psil', nozzleHot ? 'hot-nozzle' : '', bedHot ? 'hot-bed' : '',
    running ? 'is-running' : ''].filter(Boolean).join(' ');
  const temps = `Сопло ${nfmt(t.nozzle, 0)}° · стол ${nfmt(t.bed, 0)}°`;
  return `<svg class="${cls}" width="34" height="30" viewBox="0 0 48 42" aria-hidden="true" role="img">`
    + `<title>${esc(temps)}</title>`
    + '<rect class="p-body" x="4" y="2" width="40" height="38" rx="5"/>'
    + '<rect class="p-door" x="9" y="12" width="30" height="22" rx="3"/>'
    + '<rect class="p-bed" x="12" y="28" width="24" height="3.4" rx="1.6"/>'
    + '<rect class="p-nozzle" x="21.4" y="14" width="5.2" height="9.4" rx="1.4"/>'
    + '<path class="p-body" d="M14 6.4h20"/>'
    + '</svg>';
}

/* В12: ETA-циферблат — «во сколько закончится» читается как положение
   стрелки на обычных часах, а не как ещё одно число. */
function renderEtaClock(etaUnix, late) {
  const host = $('pr_eta_clock');
  if (!host) return;
  if (!etaUnix) { host.innerHTML = ''; return; }
  const d = new Date(etaUnix * 1000);
  const minutes = d.getHours() * 60 + d.getMinutes();
  const deg = (minutes / 720) * 360;
  const ticks = [0, 90, 180, 270].map((a) =>
    `<line class="ec-tick" x1="12" y1="3.4" x2="12" y2="5.6" transform="rotate(${a} 12 12)"/>`).join('');
  host.innerHTML = '<svg width="22" height="22" viewBox="0 0 24 24" aria-hidden="true">'
    + `<circle class="ec-face" cx="12" cy="12" r="10"/>${ticks}`
    + `<line class="ec-hand" x1="12" y1="12.6" x2="12" y2="5.8" transform="rotate(${deg.toFixed(1)} 12 12)"/>`
    + '<circle class="ec-dot" cx="12" cy="12" r="1.6"/></svg>';
  host.classList.toggle('late', Boolean(late));
}

function renderLive() {
  renderTabs();
  const p = active();
  const pill = $('live_pill');
  if (!p) { pill.hidden = true; return; }
  renderBedStatus(p);

  pill.hidden = false;
  const st = p.printer.state, kind = STATE_KIND[st] || '';
  // 13.1 (26): контекстные команды — «Пауза» только при печати, «Продолжить»
  // только в паузе, «Стоп» приглушён вне работы.
  const cmdBtnEl = (cmd) => document.querySelector(`.cmd-btn[data-cmd="${cmd}"]`);
  const pauseBtnEl = cmdBtnEl('pause'), resumeBtnEl = cmdBtnEl('resume'), stopBtnEl = cmdBtnEl('stop');
  if (pauseBtnEl || resumeBtnEl || stopBtnEl) {
    const isRun = kind === 'running';
    const isPaused = st === 'PAUSE' || st === 'PAUSED';
    if (pauseBtnEl) pauseBtnEl.hidden = !isRun;
    if (resumeBtnEl) resumeBtnEl.hidden = !isPaused;
    if (stopBtnEl) stopBtnEl.hidden = !(isRun || isPaused);
  }
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
      : (p.connection.mode === 'cloud'
        ? `Bambu Cloud${p.connection.host ? ' · ' + p.connection.host : ''}`
        : (p.connection.host || 'локальная сеть'));
  }

  const badge = $('pr_state');
  badge.className = 'state-badge ' + kind;
  badge.textContent = p.printer.state_label || STATE_LABEL[st] || st;
  $('pr_job').classList.toggle('running', kind === 'running');
  text('pr_task', p.printer.task || 'Нет активной печати');

  const job = (PF.state.jobs.queue || []).find((j) => j.printer_id === p.id && j.state === 'running');
  const order = (job && job.order) || (p.job && p.job.order);
  const orderEl = $('pr_order');
  if (orderEl) {
    if (order && order.number) {
      orderEl.innerHTML = `<a href="#orders" class="order-link" data-order-open="${esc(order.id || '')}">Заказ №${esc(order.number)} · ${esc(order.product || '')}</a>`
        + ` <button class="btn sm ghost" type="button" data-link-order="${esc(p.id)}" title="Перепривязать эту печать к другому заказу" style="margin-left:8px;padding:2px 8px;font-size:11px">🔗 Другой заказ</button>`;
    } else if (p.connection.connected && (kind === 'running' || p.printer.state === 'PAUSE' || p.printer.task)) {
      orderEl.innerHTML = `Не связано с заказом`
        + ` <button class="btn sm primary" type="button" data-link-order="${esc(p.id)}" title="Привязать эту печать к уже существующему заказу" style="margin-left:8px;padding:2px 10px;font-size:12px"><span class="ic">🔗</span>Привязать к заказу</button>`
        + ` <button class="btn sm ghost" type="button" data-convert-order="${esc(p.id)}" title="Создать новый заказ из этой печати" style="margin-left:6px;padding:2px 10px;font-size:12px"><span class="ic">✨</span>Новый заказ</button>`;
    } else {
      orderEl.textContent = p.connection.connected ? 'Не связано с заказом' : (p.connection.last_error || 'Нет подключения');
    }
  }

  const progress = clamp(num(p.printer.progress), 0, 100);
  const progEl = $('pr_progress');
  const progPrev = progEl ? progEl.textContent : '';
  const progNext = Math.round(progress) + '%';
  text('pr_progress', progNext);
  U.countUp(progEl, progPrev, progNext);       // ПР2: плавный докрут процентов
  const ring = $('pr_ring');
  if (ring) ring.style.strokeDashoffset = String(283 - 283 * progress / 100);
  const layersFill = $('pr_layers_fill');       // ПР2: степпер слоёв под названием задания
  if (layersFill) {
    const total = num(p.printer.total_layers);
    layersFill.parentElement.classList.toggle('on', kind === 'running' && total > 0);
    layersFill.style.width = total ? `${clamp(progress, 0, 100)}%` : '0%';
  }
  text('pr_layers', `${nfmt(p.printer.layer)} / ${nfmt(p.printer.total_layers)}`);
  text('pr_remaining', p.printer.remaining_min ? minutesText(p.printer.remaining_min) : '—');
  const etaText = p.printer.eta ? new Date(p.printer.eta * 1000)
    .toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' }) : '—';
  const etaEl = $('pr_eta');
  if (etaEl) {
    const delayed = kind === 'running' && num(p.printer.remaining_min) > 0
      && num(p.printer.slowdown_min) > 0;   // если прошивка отдаёт поправку — подсветим
    etaEl.textContent = etaText;
    etaEl.classList.toggle('delayed', delayed);
    renderEtaClock(p.printer.eta, delayed);
  }
  text('pr_speed', SPEED_LABEL[p.printer.speed_level] || p.printer.speed_label || '—');
  text('pr_wifi', p.printer.wifi || '—');
  const sel = $('pr_speed_sel');
  if (sel && document.activeElement !== sel) sel.value = String(p.printer.speed_level || 2);

  const t = p.temperature;
  // 13.1 (25): «замороженная» телеметрия — при обрыве связи показываем
  // последние известные значения с пометкой «данные N мин назад», а не «—».
  const lastGood = (window.__pfLastGood = window.__pfLastGood || {});
  const frozen = !p.connection.connected;
  let tShow = t;
  let frozenMin = 0;
  if (frozen) {
    const lg = lastGood[p.id];
    if (lg) {
      tShow = lg.t;
      frozenMin = Math.max(1, Math.round((Date.now() - lg.at) / 60000));
    }
  } else if (Number.isFinite(num(t.nozzle))) {
    lastGood[p.id] = { at: Date.now(), t, fans: p.fans, progress: num(p.printer.progress), task: p.printer.task || '' };
  }
  const frozenBadge = $('pr_frozen');
  if (frozenBadge) {
    frozenBadge.hidden = !frozen || !lastGood[p.id];
    if (frozen && lastGood[p.id]) {
      frozenBadge.textContent = `данные ${frozenMin} мин назад — связь прервалась, показываем последнее известное`;
    }
  }
  text('pr_nozzle', nfmt(tShow.nozzle, 1)); text('pr_nozzle_t', nfmt(tShow.nozzle_target));
  text('pr_bed', nfmt(tShow.bed, 1)); text('pr_bed_t', nfmt(tShow.bed_target));
  text('pr_chamber', tShow.chamber ? nfmt(tShow.chamber, 1) : '—');
  renderTempGauge('nozzle', tShow.nozzle, tShow.nozzle_target);
  renderTempGauge('bed', tShow.bed, tShow.bed_target);
  const chamHint = $('pr_chamber_hint');
  if (chamHint) chamHint.textContent = num(tShow.chamber) > 35 ? 'прогрев корпуса' : '';
  const nic = $('pr_nozzle_ic');
  if (nic) nic.classList.toggle('on', num(tShow.nozzle) > 50);

  const fansShow = (frozen && lastGood[p.id]) ? lastGood[p.id].fans : p.fans;
  text('pr_fan_part', Math.round(num(fansShow.part)) + '%'); bar($('pr_fan_part_bar'), fansShow.part);
  text('pr_fan_aux', Math.round(num(fansShow.aux)) + '%'); bar($('pr_fan_aux_bar'), fansShow.aux);
  text('pr_fan_cham', Math.round(num(fansShow.chamber)) + '%'); bar($('pr_fan_cham_bar'), fansShow.chamber);

  text('pr_firmware', p.printer.firmware ? 'Прошивка ' + p.printer.firmware : 'Прошивка —');
  text('pr_sub', p.connection.connected
    ? `${p.name} · ${p.connection.host} · обновлено ${agoText(p.connection.last_message)}`
    : 'Мониторинг и управление по локальной сети. Принтер сейчас недоступен.');

  renderAms(p);
  if (!renderAms._sugLoaded || renderAms._sugPrinter !== p.id) {
    renderAms._sugLoaded = true;
    renderAms._sugPrinter = p.id;
    loadAmsSuggestion(p.id).then(() => { if (active() && active().id === p.id) renderAms(p); });
  }
  renderHealth(p);
  renderCamera(p);
  updateBedProjection(p);
  renderAlerts(p);
  renderMaintenance(p);
  renderJobCost(p);
  renderChart(p);

  const controls = $$('[data-cmd],[data-set],[data-jog]');
  controls.forEach((b) => { b.disabled = !p.connection.connected; });
}

/* --------------------------------------------- ПР3: температуры-приборы
   Полоска «сейчас/цель» + состояние строки (нагрев → пульс-штриховка,
   в норме → зелёная), а сверху — микро-спарклайн из кэша истории. */
const sparkCache = { id: '', nozzle: [], bed: [] };

function renderTempGauge(key, nowV, targetV) {
  const bar = $('pr_' + key + '_bar');
  const row = $('pr_row_' + key);
  if (!bar || !row) return;
  const nv = num(nowV), tv = num(targetV);
  const scale = tv > 0 ? tv : (key === 'nozzle' ? 260 : 110);
  bar.style.width = clamp(nv / scale * 100, 0, 100) + '%';
  const heating = tv > 0 && nv < tv - 8;
  const atTemp = tv > 0 && !heating && Math.abs(nv - tv) <= 8;
  row.classList.toggle('heating', heating);
  row.classList.toggle('at-temp', atTemp);
  bar.style.setProperty('--tgt', tv > 0 ? clamp(100, 0, 100) + '%' : '');
  const spark = $('pr_' + key + '_spark');
  if (spark) spark.innerHTML = sparkPath(sparkCache[key] || []);
}

/** Линия последних значений для спарклайна; < 4 точек — пустой svg. */
function sparkPath(values) {
  const vs = (values || []).slice(-44).map((v) => num(v));
  if (vs.length < 4) return '';
  const min = Math.min(...vs), max = Math.max(...vs);
  const span = Math.max(1e-6, max - min);
  const pts = vs.map((v, i) => {
    const x = (i / (vs.length - 1)) * 110;
    const y = 20 - ((v - min) / span) * 18 + 1;
    return `${i ? 'L' : 'M'}${x.toFixed(1)},${y.toFixed(1)}`;
  }).join('');
  return `<path d="${pts}"/>`;
}

function amsColorName(hex) {
  hex = String(hex || '').trim().replace('#', '');
  if (hex.length < 6) return '';
  const r = parseInt(hex.slice(0,2),16), g = parseInt(hex.slice(2,4),16), b = parseInt(hex.slice(4,6),16);
  const mx = Math.max(r,g,b), mn = Math.min(r,g,b);
  if (mx - mn < 30) {
    if (mx < 60) return 'Чёрный';
    if (mx > 200) return 'Белый';
    return 'Серый';
  }
  if (r >= g && r >= b) return g > 90 ? 'Оранжевый' : 'Красный';
  if (g >= r && g >= b) return 'Зелёный';
  return 'Синий';
}
function renderAms(p) {
  const ams = p.ams || { trays: [] };
  const trays = ams.trays || [];
  const occupied = trays.filter((t) => t.present !== false && (t.present || t.generic || t.type || t.uuid));
  text('pr_ams_count', trays.length
    ? `${occupied.length} из ${trays.length} занято`
    : 'нет данных');
  const hum = num(ams.humidity);
  const humZone = hum <= 20 ? { label: 'сухо', color: '#22c55e' }
    : hum <= 40 ? { label: 'норма', color: '#3b82f6' }
    : hum <= 60 ? { label: 'влажно', color: '#f59e0b' }
    : { label: 'крит', color: '#ef4444' };
  const humBar = hum > 0 ? `<div style="display:inline-block;width:60px;height:8px;background:#e5e7eb;border-radius:4px;overflow:hidden;margin:0 6px;vertical-align:middle"><div style="width:${Math.min(100, hum)}%;height:100%;background:${humZone.color}"></div></div>` : '';
  text('pr_ams_env', ams.temperature != null || ams.humidity != null
    ? `Температура ${ams.temperature ?? '—'} °C · влажность ${humBar}${ams.humidity ?? '—'}% (${humZone.label})`
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
    const human = amsColorName(t.color) || '';
    const present = t.present !== false && (t.present || t.generic || t.type || t.uuid);
    const generic = !!(t.generic || (present && !t.bambulab && !t.uuid));
    const empty = !present;
    const low = remain != null && remain < 15;
    const drying = (num(ams.temperature) > 45 && num(ams.humidity) > 0) ? ` · сушка ${Math.round(num(ams.temperature))}°C` : '';
    let spoolHint = '';
    try {
      const sp = (PF.state.spools || []).find((s) => String(s.ams_slot) === String(t.slot) && String(s.printer_id) === String(p.id));
      if (sp && sp.color_name) spoolHint = ' · ' + sp.color_name;
    } catch(e) {}
    const typeLabel = empty ? 'пусто' : (t.type || 'Тип не задан');
    return `<div class="ams-tube${t.active ? ' active' : ''}${empty ? ' empty' : ''}${generic ? ' generic' : ''}${low ? ' low' : ''}"`
      + ` title="${esc((t.label || ('Слот ' + (num(t.slot) + 1))) + ' · ' + typeLabel + (human && !empty ? ' · ' + human : '') + spoolHint + (remain != null ? ` · ${Math.round(remain)}%` : '') + drying)}">`
      + `<div class="tube-body"><i class="tube-fill" style="--filament:${esc(t.color || '#cbd5e1')};--lvl:${empty ? 0 : (remain == null ? 100 : clamp(remain, 3, 100))}%"></i>`
      + `<span class="tube-pct">${empty ? '' : (remain != null ? Math.round(remain) + '%' : '—')}</span>`
      + (t.active ? '<span class="tube-use" title="Сейчас печатает этим">▶</span>' : '')
      + '</div>'
      + `<div class="tube-meta"><b>${esc(t.label || ('Слот ' + (num(t.slot) + 1)))}</b>`
      + `<small>${esc(typeLabel)}${human && !empty ? ' · ' + esc(human) : ''}</small>`
      + `<small class="tube-tags">${generic ? 'сторонний' : (empty ? '' : 'RFID')}${spoolHint ? ' · ' + esc(spoolHint.slice(3)) : ''}</small></div>`
      + '<div class="acts">'
      + (empty ? '' : `<button class="btn sm" type="button" data-ams-load="${esc(String(t.slot))}">Подать</button>`)
      + `<button class="btn sm" type="button" data-ams-edit="${esc(String(t.unit))}:${esc(String(t.slot))}" data-type="${esc(t.type || '')}" data-color="${esc(t.color || '#cccccc')}" title="Изменить тип и цвет">Тип</button>`
      + '</div></div>';
  }).join('') + renderAmsSuggestion(p);
}

/* Память AMS (идея 41): «как в прошлый раз» */
let amsSuggestion = null;
async function loadAmsSuggestion(printerId) {
  try {
    const d = await get('/api/ams/suggestion');
    amsSuggestion = (d.suggestion || []).find((x) => x.printer_id === printerId) || null;
  } catch (e) { amsSuggestion = null; }
}
function renderAmsSuggestion(p) {
  const sug = amsSuggestion;
  if (!sug || !sug.slots || !sug.slots.length) return '';
  const chips = sug.slots.map((s) =>
    `<span class="chip outline" style="margin:2px 4px 2px 0">слот ${num(s.slot) + 1}: ${esc(s.material || '?')}${s.color ? ' · ' + esc(s.color) : ''}</span>`
  ).join('');
  return `<div class="notice" style="margin-top:10px"><span>⟲</span><span>`
    + `<b>Как в прошлый раз:</b> ${chips}<br>`
    + `<small class="muted">Последняя раскладка катушек этого принтера. Сравните со слотами выше — если не совпало, проверьте вручную.</small></span></div>`;
}

const SEV_LABEL = { info: 'Заметка', warn: 'Внимание', error: 'Ошибка', fatal: 'Критично' };
const SEV_ICON = { info: 'ⓘ', warn: '⚠', error: '✕', fatal: '⛔' };

let eventsCache = [];   // журнал принтера: им же подсвечиваем «последняя ошибка» (ПР6)

function renderHealth(p) {
  const problems = Array.isArray(p.printer.problems) ? p.printer.problems : [];
  const badge = $('pr_health');
  const badgeText = $('pr_health_text');
  const worst = p.printer.severity || '';
  const sev = worst === 'fatal' || worst === 'error' ? 'bad' : worst === 'warn' ? 'warn' : 'ok';
  badge.className = 'chip ' + sev;
  if (badgeText) {
    badgeText.textContent = problems.length
      ? `${problems.length} ${plural(problems.length, 'проблема', 'проблемы', 'проблем')}` : 'Нет ошибок';
  } else badge.textContent = problems.length
    ? `${problems.length} ${plural(problems.length, 'проблема', 'проблемы', 'проблем')}` : 'Нет ошибок';
  const card = $('pr_health_card');
  if (card) card.classList.toggle('has-alarm', sev !== 'ok');
  $('pr_errors').innerHTML = problems.length
    ? problems.map((h) => `<div class="hms-item sev-${esc(h.severity || 'warn')}">`
      + `<b>${SEV_ICON[h.severity] || '⚠'} ${esc(h.title || 'Неизвестная ошибка')}</b>`
      + `<span>${esc(SEV_LABEL[h.severity] || '')} · код ${esc(h.code || '')}</span>`
      + (h.why ? `<div class="why">${esc(h.why)}</div>` : '')
      + (h.advice ? `<div class="fix">${esc(h.advice)}</div>` : '')
      + (h.url ? `<a href="${esc(h.url)}" target="_blank" rel="noopener">Официальное описание кода ↗</a>` : '')
      + '</div>').join('')
    : lastErrorLine();
}

/** Спокойный вид карточки, когда ошибок нет: видно, что следим, и когда была беда. */
function lastErrorLine() {
  const bad = (eventsCache || []).find((e) => e.kind === 'error'
    || /ошибк|сбой|обрыв|fail/i.test(String(e.title || '')));
  const ok = p_connText();
  return `<div class="health-ok"><span class="shield" data-icon="shield">✓</span>`
    + `<span><b>Активных ошибок нет</b><small>${bad
      ? `последняя — ${esc(String(bad.title).slice(0, 42))} · ${agoText(bad.at)}`
      : 'с начала наблюдения всё чисто'}${ok ? ' · ' + esc(ok) : ''}</small></span></div>`;
}
function p_connText() {
  const p = active();
  if (!p) return '';
  if (!p.connection.connected) return '';
  return p.connection.last_message ? 'связь ' + agoText(p.connection.last_message) : '';
}

function plural(n, one, few, many) {
  const a = Math.abs(n) % 100, b = a % 10;
  if (a > 10 && a < 20) return many;
  if (b > 1 && b < 5) return few;
  return b === 1 ? one : many;
}

/* ------------------------------------------------- тревоги сторожа печати */
async function refreshEnough() {
  const host = $('pr_enough');
  const textEl = $('pr_enough_text');
  if (!host || !textEl) return;
  const p = active();
  if (!p) { host.hidden = true; return; }
  try {
    const info = await get('/api/workshop/enough', { printer_id: p.id });
    if (!info.job) { host.hidden = true; return; }
    host.hidden = false;
    host.className = 'notice' + (info.enough ? '' : ' warn');
    textEl.textContent = info.enough
      ? `На «${info.job.name || 'задание'}» хватит (${nfmt(info.have)} г)`
      : (info.message || 'Мало пластика на следующее задание');
  } catch (e) { host.hidden = true; }
}

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
  const profitEl = $('pr_job_profit');
  if (profitEl) {
    if (j.profit != null && num(j.price)) {
      profitEl.textContent = `${money(j.profit)} · ${nfmt(j.break_even_pct || 0)}% цены съедено`;
      profitEl.className = num(j.profit) >= 0 ? 'pos' : 'neg';
    } else {
      profitEl.textContent = '';
    }
  }
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
  // ПР3: те же точки питают микро-спарклайны в строках температур.
  sparkCache.id = p.id;
  sparkCache.nozzle = pts.map((x) => num(x.nozzle));
  sparkCache.bed = pts.map((x) => num(x.bed));
  sparkCache.chamber = pts.map((x) => num(x.chamber));
  sparkCache.fan_part = pts.map((x) => num(x.fan_part));
  sparkCache.fan_aux = pts.map((x) => num(x.fan_aux));
  ['nozzle', 'bed', 'chamber', 'fan_part', 'fan_aux'].forEach((k) => {
    const spark = $('pr_' + k + '_spark');
    if (spark) spark.innerHTML = sparkPath(sparkCache[k]);
  });
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
  const isCloudNoIP = p.connection && p.connection.mode === 'cloud' && !p.connection.host;
  let statusText = cam.available ? (demo ? 'Демо-режим: заготовленные кадры' : 'Прямой эфир') : (cam.error || 'Нет сигнала');
  if (isCloudNoIP && !cam.available) statusText = 'Камера — только по локальной сети (укажите IP)';
  text('pr_cam_status', statusText);
  text('pr_cam_age', cam.available
    ? (demo ? 'Принтер не подключён' : (cam.age < 3 ? 'Кадр только что' : `Кадр ${Math.round(cam.age)} сек. назад`))
    : (isCloudNoIP ? 'Добавьте IP в Настройках → Принтеры' : '—'));
  $('pr_cam_demo').hidden = !demo;
  const emptyEl = $('pr_cam_empty');
  if (isCloudNoIP && !cam.available) {
    emptyEl.innerHTML = '<span class="big">◉</span>Камера в облачном режиме недоступна<br><small>Принтер управляется через Bambu Cloud, а камера — только по локальной сети (порт 6000).<br>Решение: укажите IP принтера в Настройках → Принтеры → поле «IP-адрес» (посмотрите в Настройки → WLAN на экране принтера). После этого камера появится даже в облачном режиме. Или включите Демо-камеру для проверки интерфейса.</small><br><button class="btn sm" type="button" onclick="PF.modules.printer.openPrinterModal(PF.state.activePrinter)">Указать IP</button>';
    emptyEl.hidden = false;
  } else if (!cam.available && p.connection.mode === 'cloud' && p.connection.connected) {
    emptyEl.innerHTML = '<span class="big">◉</span>Облако подключено, камера ищет локальную сеть<br><small>порт 6000 · проверьте что ПК и принтер в одной Wi-Fi сети (192.168.x.x)</small>';
    emptyEl.hidden = false;
  } else {
    if (emptyEl.dataset.cloudHtml) emptyEl.innerHTML = '<span class="big">◉</span>Ожидаем видеопоток<br><small>порт 6000 · только локальная сеть</small>';
    emptyEl.hidden = !!cam.available;
  }
  img.classList.toggle('on', !!cam.available);
  // ПР5: LIVE мигает чаще, когда кадр совсем свежий, и тускнеет на паузе потока
  const liveChip = img.parentElement.querySelector('.cam-live');
  if (liveChip) {
    liveChip.classList.toggle('fresh', !!cam.available && !demo && num(cam.age) < 3);
    liveChip.classList.toggle('stale', !!cam.available && num(cam.age) >= 30);
  }
  // 15.2: FPS потока виден рядом с LIVE — «камера живая» становится цифрой
  const fpsChip = img.parentElement.querySelector('.cam-fps');
  if (fpsChip) {
    const fpsVal = num(cam.fps);
    fpsChip.hidden = !cam.available || demo || !fpsVal;
    fpsChip.textContent = fpsVal ? fpsVal.toFixed(1) + ' к/с' : '';
  }
  // 15.2: сторож потока. Сервер обрывает MJPEG по таймеру, а браузер при
  // обрыве multipart не всегда дёргает onerror — картинка «замерзает».
  // Возраст кадра приходит по SSE; вырос порог — пересобираем поток.
  if (cam.available && !demo && camStream && num(cam.age) > 15) {
    camSession = Date.now();
    camStream = '';
  }

  if (!cam.available) {
    if (camStream) { img.removeAttribute('src'); camStream = ''; }
    return;
  }
  const key = p.id + ':' + camSession;
  if (camStream !== key) {
    camStream = key;
    img.onerror = () => {                          // сорвался поток — соберём заново
      camStream = '';
      camSession = Date.now();
    };
    img.src = camUrl(p, true);
    if ($('cam_modal').open) $('cam_full').src = camUrl(p, true);
  }
  renderShots(p);
}


/* --------------------------- 119: проекция плиты на живое видео ---------------------------
   Эталон пустого стола калибруется четырьмя кликами (гомография на сервере),
   дальше /api/camera/projection отдаёт контуры объектов задания в долях кадра.
   Панель рисует их SVG-оверлеем поверх MJPEG; клик по контуру — исключить объект. */

function bedMapSvg() { return $('pr_cam_map'); }

function drawBedMap(objects) {
  const svg = bedMapSvg();
  if (!svg) return;
  bedMapCache = objects && objects.length ? objects : null;
  if (!objects || !objects.length) { if (svg.childNodes.length) svg.innerHTML = ''; bedMapKey = ''; return; }
  const sig = JSON.stringify(objects);
  if (sig === bedMapKey) return;              // те же объекты — не дёргаем DOM
  bedMapKey = sig;
  const parts = [];
  objects.forEach((o, i) => {
    if (!o.pts || o.pts.length !== 4) return;
    const pts = o.pts.map((pt) => `${(num(pt[0]) * 1000).toFixed(1)},${(num(pt[1]) * 1000).toFixed(1)}`).join(' ');
    parts.push(`<polygon points="${pts}" class="bedmap-obj" data-bedmap-i="${i}" style="pointer-events:auto"/>`);
    const cx = o.pts.reduce((acc, pt) => acc + num(pt[0]), 0) / 4 * 1000;
    const cy = o.pts.reduce((acc, pt) => acc + num(pt[1]), 0) / 4 * 1000;
    const label = esc(String(o.name || 'Объект')).slice(0, 22);
    parts.push(`<text x="${cx.toFixed(1)}" y="${cy.toFixed(1)}" class="bedmap-label" style="pointer-events:none">${i + 1}. ${label}</text>`);
  });
  svg.innerHTML = parts.join('');
}

async function updateBedProjection(p) {
  const svg = bedMapSvg();
  if (!svg) return;
  const on = !!bedMapOn[p.id];
  svg.classList.toggle('on', on);
  if (!on || !p || !p.camera || !p.camera.available) { if (!on) drawBedMap(null); return; }
  if (bedMapBusy) return;
  bedMapBusy = true;
  try {
    const data = await api(`/api/camera/projection?printer_id=${encodeURIComponent(p.id)}`);
    if (data && data.has_map) drawBedMap(data.objects || []);
    else drawBedMap(null);
  } catch (e) { drawBedMap(null); }
  finally { bedMapBusy = false; }
}

function bindBedMapClicks() {
  const svg = bedMapSvg();
  if (!svg) return;
  svg.addEventListener('click', (ev) => {
    const poly = ev.target.closest('[data-bedmap-i]');
    if (!poly) return;
    const idx = num(poly.getAttribute('data-bedmap-i'), -1);
    const p = active();
    if (!p || idx < 0 || !bedMapCache || !bedMapCache[idx]) return;
    const obj = bedMapCache[idx];
    if (!confirmDanger(`Исключить «${obj.name}» из печати? Объект будет пропущен командой skip_objects.`)) return;
    command('skip_objects', [obj.id], { confirm: '', label: 'исключить объект (проекция)' });
  });
}

/* --- модалка калибровки: 4 клика по углам стола на эталоне --- */
const bedmapPts = [];

function bedmapRender() {
  const svg = $('bedmap_svg');
  if (!svg) return;
  const parts = [];
  const labels = ['1 перед-лево', '2 перед-право', '3 зад-право', '4 зад-лево'];
  bedmapPts.forEach((pt, i) => {
    parts.push(`<circle cx="${(pt[0] * 100).toFixed(2)}" cy="${(pt[1] * 100).toFixed(2)}" r="0.9" class="bedmap-dot"/>`);
    parts.push(`<text x="${(pt[0] * 100 + 1.4).toFixed(2)}" y="${(pt[1] * 100 - 1.2).toFixed(2)}" class="bedmap-label" font-size="2.6">${labels[i] || ''}</text>`);
  });
  if (bedmapPts.length >= 2) {
    const ptsAttr = bedmapPts.map((pt) => `${(pt[0] * 100).toFixed(2)},${(pt[1] * 100).toFixed(2)}`).join(' ');
    parts.push(`<polyline points="${ptsAttr}" class="bedmap-polyline" fill="none"/>`);
    if (bedmapPts.length === 4) {
      parts.push(`<polygon points="${ptsAttr}" class="bedmap-fill"/>`);
    }
  }
  svg.innerHTML = parts.join('');
  $('bedmap_save').disabled = bedmapPts.length !== 4;
  $('bedmap_hint').textContent = bedmapPts.length >= 4
    ? 'Все четыре угла размечены — сохраняйте'
    : `Угол ${bedmapPts.length + 1} из 4: ${labels[bedmapPts.length] || ''}`;
}

function openBedMapModal() {
  bedmapPts.length = 0;
  bedmapRender();
  const img = $('bedmap_img');
  img.src = '/api/camera/bed-ref.jpg?ts=' + Date.now();
  img.onerror = () => {
    $('bedmap_hint').textContent = 'Эталона стола нет: снимите «Пустой стол» на вкладке принтера (кнопка рядом с камерой) и вернитесь';
  };
  openModal('bedmap_modal');
}

async function saveBedMap() {
  if (bedmapPts.length !== 4) return;
  try {
    await post('/api/camera/calibrate', { printer_id: PF.state.activePrinter, corners: bedmapPts.slice() });
    toast('Стол размечен', 'Проекция плиты готова — включите её кнопкой под камерой');
    closeModal('bedmap_modal');
    bedMapKey = '';
  } catch (e) { fail(e); }
}

async function resetBedMap() {
  try {
    await post('/api/camera/calibrate/reset', { printer_id: PF.state.activePrinter });
    toast('Калибровка сброшена', '', 'info');
    bedmapPts.length = 0;
    bedmapRender();
    bedMapKey = '';
  } catch (e) { fail(e); }
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
  // ПР5: кадры, добавленные сторожем (спагетти, «не двигается», перерасход),
  // подсвечиваются красной рамкой и значком — лента превращается в доказательную базу.
  const GUARD_NOTE = /спагетти|двигается|перерасход|сторож|не печата/i;
  host.innerHTML = shots.slice(0, 12).map((sh) => {
    const when = shotTime(sh.at);
    const note = sh.note || 'Снимок';
    const guard = GUARD_NOTE.test(note);
    return `<span class="shot-cell${guard ? ' guard' : ''}">`
      + `<img src="/api/printer/shot.jpg?printer_id=${encodeURIComponent(p.id)}&id=${encodeURIComponent(sh.id)}"`
      + ` alt="${esc(note)}" title="${esc(note + ' · ' + when + (guard ? ' · кадр сторожа — кликните для увеличения' : ''))}"`
      + ` data-shot="${esc(sh.id)}" loading="lazy">`
      + (guard ? '<b>⚠</b>' : '') + '</span>';
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
  const sync = $('pr_ams_sync');
  if (sync) sync.addEventListener('click', async () => {
    try {
      const res = await post('/api/printer/ams/sync', { printer_id: PF.state.activePrinter });
      const bits = [];
      if (res.created) bits.push(`новых катушек: ${res.created}`);
      if (res.updated) bits.push(`обновлено: ${res.updated}`);
      if (res.unbound) bits.push(`отвязано: ${res.unbound}`);
      toast('Данные AMS занесены в базу', bits.join(' · ') || 'Изменений нет');
      PF.refreshCore();
    } catch (e) { fail(e); }
  });
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
      if (!confirmDanger('Профиль отправит настройки материалов в AMS. Подтвердить физическое действие?')) return;
      try {
        await post('/api/ams-profile/apply', { id: apply.dataset.apApply, printer_id: PF.state.activePrinter, confirmed: true });
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
    await post('/api/printer/command', {
      printer_id: p.id, command: name, value, confirmed: Boolean(ask),
    });
    toast('Команда отправлена', opts.label || name);
    setTimeout(PF.poll, 500);
  } catch (e) { fail(e); }
}

/* 13.1 (29): текущий кадр камеры — в полноэкранный просмотрщик. */
async function openCameraSnapshot() {
  const p = active();
  if (!p) return fail(new Error('Принтер ещё не добавлен'));
  try {
    const src = '/api/printer/camera.jpg?printer_id=' + encodeURIComponent(p.id) + '&t=' + Date.now();
    const probe = await fetch(src, { method: 'HEAD' });
    if (!probe.ok) throw new Error(probe.status === 503 ? 'Кадр ещё не получен — проверьте камеру' : `Камера: ${probe.status}`);
    U.lightbox(src, `${p.name} · камера — сейчас`);
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
  if (p.connection.mode === 'cloud' && !p.connection.host) {
    // Облачный принтер без локального IP: сервер сам попробует найти IP
    // (SSDP) и Access Code (облачный список устройств) — тогда SD-карта
    // станет видимой по FTPS. Если не вышло — показываем облачную историю.
    host.innerHTML = '<div class="skeleton" aria-busy="true" style="height:60px"></div>';
    try {
      const sd = await get('/api/printer/files', { printer_id: p.id, path: filesPath || '/' });
      if (!sd.error) { renderFileList(sd); return; }
      const data = await get('/api/printer/cloud-files', { printer_id: p.id });
      const tasks = data.tasks || [];
      const hint = `<div class="notice"><span>ℹ</span><span>${esc(sd.error)}</span></div>`;
      host.innerHTML = hint + (tasks.length ? tasks.map((t) => `<div class="file-row">`
        + '<span class="fic">☁</span>'
        + `<span class="fname" title="${esc(t.title || '')}">${esc(t.title || 'Без названия')}</span>`
        + `<span class="fsize">${esc((t.mode || 'cloud') === 'lan_file' ? 'SD' : 'облако')}</span>`
        + '<span class="acts"></span></div>').join('')
        : '<div class="empty compact"><span>В облачной истории пока пусто. Загрузите 3MF перетаскиванием — он уйдёт в облако Bambu.</span></div>');
    } catch (e) {
      host.innerHTML = `<div class="notice bad"><span>✕</span><span>${esc(e.message)}</span></div>`;
    }
    return;
  }
  if (!p.connection.connected) {
    // Не дёргаем FTPS впустую: недоступный принтер отвечает долгим таймаутом.
    host.innerHTML = '<div class="empty compact"><span>Принтер не на связи — список файлов недоступен.</span></div>';
    return;
  }
  host.innerHTML = '<div class="skeleton" aria-busy="true" style="height:60px"></div>';
  try {
    const data = await get('/api/printer/files', { printer_id: p.id, path: filesPath || '/' });
    if (data.error) { host.innerHTML = `<div class="notice"><span>ℹ</span><span>${esc(data.error)}</span></div>`; return; }
    renderFileList(data);
  } catch (e) {
    host.innerHTML = `<div class="notice bad"><span>✕</span><span>${esc(e.message)}</span></div>`;
  }
}

function renderFileList(data) {
  const host = $('pr_files');
  filesPath = data.path || filesPath || '/';
  const crumbs = data.crumbs || [{ name: 'SD', path: '/' }];
  const items = data.files || [];
  filesCache = items;
  const crumbHtml = '<div class="file-crumbs" id="pr_file_crumbs">'
    + crumbs.map((c) => `<button type="button" class="btn sm ghost" data-sd-path="${esc(c.path)}">${esc(c.name)}</button>`).join('<span class="muted"> / </span>')
    + '</div>';
  const rows = items.map((f) => {
    const printable = f.printable || (!f.dir && /\.(3mf|gcode(?:\.3mf)?)$/i.test(String(f.name || '')) && f.kind !== 'media');
    const icon = f.dir ? '📁' : (f.kind === 'media' ? '🎞' : fileIcon(f.name));
    return `<div class="file-row${f.dir ? ' dir' : ''}">`
      + `<span class="fic">${icon}</span>`
      + (f.dir
        ? `<button type="button" class="fname link" data-sd-path="${esc(f.path)}">${esc(f.name)}</button>`
        : `<span class="fname" title="${esc(f.path || f.name)}">${esc(f.name)}</span>`)
      + `<span class="fsize">${f.dir ? 'папка' : esc(sizeText(f.size))}</span>`
      + '<span class="acts">'
      + (printable ? `<button class="btn sm ghost" type="button" data-preview-file="${esc(f.path || f.name)}">Превью</button>` : '')
      + (printable ? `<button class="btn sm primary" type="button" data-print-file="${esc(f.path || f.name)}">Печать</button>` : '')
      + (!f.dir ? `<button class="icon-btn sm danger" type="button" data-del-file="${esc(f.path || f.name)}">×</button>` : '')
      + '</span></div>';
  }).join('');
  host.innerHTML = crumbHtml + (items.length ? rows
    : '<div class="empty compact"><span>В этой папке пусто.</span></div>');
}

async function uploadFile(file) {
  const p = active();
  if (!p) return fail(new Error('Сначала добавьте принтер'));
  if (!/\.(3mf|gcode(?:\.3mf)?)$/i.test(file.name)) return fail(new Error('Поддерживаются только 3MF и G-code'));
  const form = new FormData();
  form.append('file', file);
  form.append('printer_id', p.id);
  const cloud = p.connection.mode === 'cloud';
  toast(cloud ? 'Загружаем в облако Bambu' : 'Загружаем на принтер', file.name, 'info');
  try {
    const res = await api('/api/printer/upload', { method: 'POST', body: form });
    const est = res && res.estimate ? res.estimate : {};
    const bits = [];
    if (num(est.minutes)) bits.push(minutesText(est.minutes));
    if (num(est.grams)) bits.push('~' + nfmt(est.grams) + ' г');
    if (est.material) bits.push(est.material);
    if (est.color) bits.push(est.color);
    toast(res && res.cloud ? 'Файл в облаке Bambu' : 'Файл загружен',
      bits.length ? 'Оценка из 3MF: ' + bits.join(' · ') : file.name);
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
  if (PF.modules.workshop && PF.modules.workshop.loadPresets) {
    PF.modules.workshop.loadPresets();
  }
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
    plate_preset_id: ($('pj_preset') && $('pj_preset').value) || '',
    no_auto: !!($('pj_no_auto') && $('pj_no_auto').checked),
  };
}

/* Очередь и журнал печати вынесены в queue.js (13.1, Б2):
   рендер, поиск, группировка, drag-приоритет и паспорт живут там.
   Здесь остаются действия над заданиями и данные парка. */

async function preflightAndConfirmJob(job, printerId) {
  const check = await post('/api/printer/preflight', {
    printer_id: printerId, file: job.file || job.name || '', plate: num(job.plate, 1) || 1,
    ams_mapping: (() => { try { return JSON.parse(job.ams_mapping || '[]'); } catch (e) { return []; } })(),
  });
  const blocks = check.blocks || [];
  if (blocks.length) throw new Error('Preflight блокирует старт: ' + blocks.map((x) => x.title || x.detail).join('; '));
  const warns = (check.warns || []).map((x) => `⚠ ${x.title || x.detail}`).join('\n');
  const text = `Запустить «${job.name || job.file || 'задание'}»? Принтер начнёт нагрев и движение.`
    + (warns ? `\n\nPreflight предупреждения:\n${warns}\n\nПодтвердите, что оператор проверил их.` : '');
  if (!confirmDanger(text)) return false;
  return true;
}

async function startNextJob() {
  const next = (PF.state.jobs.queue || []).find((j) => j.state === 'queued');
  if (!next) return toast('Очередь пуста');
  const printerId = next.printer_id || PF.state.activePrinter || '';
  if (!printerId) return fail(new Error('Сначала выберите или добавьте принтер'));
  try {
    if (!await preflightAndConfirmJob(next, printerId)) return;
        await post('/api/jobs/start', { id: next.id, printer_id: printerId,
      confirmed: true, preflight_acknowledged: true,
      start_request_id: (window.crypto && window.crypto.randomUUID)
        ? window.crypto.randomUUID() : `start-${Date.now()}` });
    toast('Следующее задание запущено', next.name || next.file || 'Печать');
    await PF.refreshCore();
    setTimeout(PF.poll, 1200);
  } catch (e) { fail(e); }
}

/* Паспорт печати переехал в queue.js (13.1, Б2). */

/* ==================================================== профиль принтера */
function openPrinterModal(id) {
  editingPrinter = id || null;
  PF.tempCloudDevice = '';
  const p = id ? PF.printer(id) : null;
  $('printer_modal_title').textContent = p ? 'Настройка: ' + p.name : 'Новый принтер';
  $('pf_name').value = p ? p.name : 'Основной P1S';
  $('pf_model').value = (p && p.model) || 'P1S';
  $('pf_mode').value = (p && (p.mode || 'cloud')) || 'cloud';
  $('pf_host').value = (p && p.host) || '';
  $('pf_serial').value = (p && p.serial) || '';
  $('pf_access_code').value = '';
  $('pf_access_code').placeholder = p && p.has_access_code
    ? 'Сохранён · оставьте пустым, чтобы не менять' : '8 символов с экрана принтера (нужен для режима «Локальная сеть»)';
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
  const mode = $('pf_mode').value || 'cloud';
  const payload = {
    id: editingPrinter || '',
    name: $('pf_name').value.trim() || 'Принтер',
    model: $('pf_model').value,
    mode,
    host: $('pf_host').value.trim(),
    serial: $('pf_serial').value.trim(),
    access_code: $('pf_access_code').value,
    cloud_device: PF.tempCloudDevice || '',
    has_ams: +$('pf_has_ams').value,
    enabled: +$('pf_enabled').value,
    notes: $('pf_notes').value.trim(),
    guard_enabled: +$('pf_guard').value,
    camera_demo: +$('pf_camera_demo').value,
    nozzle_size: $('pf_nozzle_size').value.trim() || '0.4',
  };
  if (mode !== 'cloud' && !payload.host) return fail(new Error('Для локальной сети укажите IP-адрес принтера'));
  if (mode !== 'cloud' && !payload.serial) return fail(new Error('Укажите серийный номер принтера'));
  if (mode === 'cloud' && !payload.serial && !payload.cloud_device) return fail(new Error('Укажите серийный номер или выберите принтер из облака'));
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
  box.innerHTML = '<div class="skeleton" aria-busy="true" style="height:44px"></div>';
  try {
    const data = await get('/api/printer/discover');
    const list = data.found || [];
    const cloud = data.cloud || [];
    let html = list.length ? list.map((p) => `<button type="button" data-found='${esc(JSON.stringify(p))}'>`
      + `<b>${esc(p.name || p.model || 'Bambu Lab')}</b><span>${esc(p.host)} · ${esc(p.serial || 'серийный номер не найден')}</span></button>`).join('') : '';
    if (cloud.length) {
      html += `<div class="card-head" style="margin-top:12px"><h4>☁ Аккаунт Bambu Cloud</h4></div>`
        + cloud.map((p) => `<button type="button" data-cloud='${esc(p.serial)}'>`
          + `<b>☁ ${esc(p.name || 'Принтер')}</b><span>${esc(p.model || '')} · ${esc(p.serial)}${p.online ? ' · в сети' : ' · не в сети'}</span></button>`).join('');
    } else {
      html += '<div class="notice"><span>☁</span><span>Облачных принтеров нет: выполните вход в Bambu Cloud (Настройки → Bambu Cloud), а принтер должен быть привязан к аккаунту.</span></div>';
    }
    if (!list.length) {
      html += '<div class="notice"><span>ℹ</span><span>В локальной сети автоматически не найдено. Введите IP и серийный номер вручную (Settings → WLAN на принтере).</span></div>';
    }
    box.innerHTML = html;
  } catch (e) {
    box.innerHTML = `<div class="notice bad"><span>✕</span><span>${esc(e.message)}</span></div>`;
  }
}

/* ====================================================== задание вручную */
function openJob() {
  queueLocalFile = null;
  $('jf_name').value = '';
  $('jf_file').value = '';
  $('jf_local_file').value = '';
  $('jf_file_hint').textContent = 'Файл сохранится локально и попадёт в очередь без предварительной загрузки на принтер.';
  $('jf_plate').value = '1';
  $('jf_priority').value = '0';
  $('jf_printer_id').innerHTML = '<option value="">Любой свободный</option>' + PF.state.printers
    .map((p) => `<option value="${esc(p.id)}">${esc(p.name)}</option>`).join('');
  $('jf_order_id').innerHTML = '<option value="">Без заказа</option>' + PF.state.orders
    .filter((o) => !PF.isFinal(o)).map((o) => `<option value="${esc(o.id)}">№${esc(o.number)} · ${esc(o.product)}</option>`).join('');
  openModal('job_modal');
}

/* ============================================== 8.5: видео печати (#61) */
let kfTimer = 0;
let kfFrames = [];
let kfIndex = 0;
function stopKeyframes() {
  if (kfTimer) { clearInterval(kfTimer); kfTimer = 0; }
  $('kf_play').hidden = true;
  $('kf_stop').hidden = true;
}
document.addEventListener('close', (e) => {
  if (e.target && e.target.id === 'keyframes_modal') stopKeyframes();
});
async function openKeyframes() {
  const p = active();
  if (!p) return;
  const job = (PF.state.jobs.queue || []).find((j) => j.printer_id === p.id && j.state === 'running')
    || (p.job && p.job.id ? p.job : null);
  const jobId = job && (job.id || job.job_id);
  $('kf_title').textContent = job && job.name ? job.name : (p.printer.task || 'Видео печати');
  $('kf_img').src = '';
  $('kf_cap').textContent = 'Загружаем кадры…';
  openModal('keyframes_modal');
  if (!jobId) {
    $('kf_cap').textContent = 'Нет активного задания — кадры появятся, когда включите кейфреймы в Настройках → «8.5 — умный цех».';
    return;
  }
  try {
    const d = await get('/api/job/keyframes', { id: jobId });
    kfFrames = d.frames || [];
    if (!kfFrames.length) {
      $('kf_cap').textContent = 'Пока нет кадров. Включите кейфреймы (мин. 0,5 мин) — и таймлайн соберётся сам.';
      return;
    }
    kfIndex = 0;
    showKfFrame();
    $('kf_seek').max = String(kfFrames.length - 1);
    $('kf_play').hidden = false;
  } catch (e) { $('kf_cap').textContent = e.message; }
}
function showKfFrame() {
  const name = kfFrames[kfIndex];
  if (!name) return;
  const p = active();
  $('kf_img').src = `/api/job/keyframe.jpg?id=${encodeURIComponent((p && p.id) || '')}&name=${encodeURIComponent(name)}`;
  $('kf_cap').textContent = `Кадр ${kfIndex + 1} из ${kfFrames.length}`;
  $('kf_seek').value = String(kfIndex);
}
$('kf_play').addEventListener('click', () => {
  if (kfTimer) return;
  kfTimer = setInterval(() => {
    kfIndex = (kfIndex + 1) % kfFrames.length;
    showKfFrame();
  }, 1200);
  $('kf_play').hidden = true;
  $('kf_stop').hidden = false;
});
$('kf_stop').addEventListener('click', stopKeyframes);
$('kf_seek').addEventListener('input', (e) => {
  kfIndex = num(e.target.value);
  showKfFrame();
});

/* ============================================== 8.5: стол (#10) */
let bedRef = null;
async function loadBedReference() {
  try { bedRef = await get('/api/bed/reference'); } catch (e) { bedRef = null; }
  renderBedStatus(active());
}
function renderBedStatus(p) {
  const el = $('pr_bed_status');
  if (!el) return;
  if (!bedRef || !bedRef.has) { el.hidden = true; return; }
  el.hidden = false;
  $('pr_bed_status_text').textContent = `Эталон пустого стола снят${p ? ` для «${p.name}»` : ''}. После финиша система проверит: не забыли ли деталь.`;
}
async function captureBedReference() {
  const p = active();
  if (!p) return;
  if (!confirmDanger(`Снять эталон пустого стола для «${p.name}»? Убедитесь, что деталь снята.`)) return;
  try {
    await post('/api/bed/reference', { printer_id: p.id });
    toast('Эталон стола сохранён', 'Проверка «деталь на столе» включена для этого принтера');
    await loadBedReference();
  } catch (e) { fail(e); }
}

/* ============================================================= события */
/* О4: компактная раскладка — плотнее карточки, скрыты подписи-подсказки. */
const DENSITY_KEY = 'pf_printers_density';
function applyDensity(on) {
  const view = $('view-printers');
  if (view) view.classList.toggle('dense', on);
  const btn = $('pr_density');
  if (btn) btn.textContent = on ? '▦ Просторно' : '⇥ Компактно';
  U.store.set(DENSITY_KEY, on ? '1' : '0');
}

function bind() {
  const park = $('pr_park') || $('pr_tabs');
  if (park) park.addEventListener('click', (e) => {
    const btn = e.target.closest('[data-printer]');
    if (!btn) return;
    PF.state.activePrinter = btn.dataset.printer;
    renderLive();
    loadFiles();
    loadEvents();
  });
  const dens = $('pr_density');
  if (dens) {
    applyDensity(U.store.get(DENSITY_KEY, '0') === '1');
    dens.addEventListener('click', () => {
      const view = $('view-printers');
      applyDensity(!(view && view.classList.contains('dense')));
    });
  } else applyDensity(U.store.get(DENSITY_KEY, '0') === '1');
  // ПР5: клик по кадру открывает крупный план (как кнопка «Развернуть»)
  const frame = document.querySelector('.cam-frame');
  if (frame) frame.addEventListener('click', (e) => {
    if (e.target.closest('button, a')) return;
    const p = active();
    if (!p || !(p.camera || {}).available) return;
    $('cam_full').src = camUrl(p, true);
    openModal('cam_modal');
  });

  document.addEventListener('click', async (e) => {
    const cmd = e.target.closest('[data-cmd]');
    if (cmd) {
      const name = cmd.dataset.cmd;
      if (name === 'keyframes') { openKeyframes(); return; }
      if (name === 'bed_ref') { captureBedReference(); return; }
      // 13.1 (29): «Кадр» — текущий кадр камеры в просмотрщике, без сохранения
      if (name === 'snapshot') { openCameraSnapshot(); return; }
      const value = cmd.dataset.value !== undefined ? num(cmd.dataset.value) : undefined;
      command(name, value, { label: cmd.textContent.trim() });
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
      const map = { nozzle_temp: 'pr_set_nozzle', bed_temp: 'pr_set_bed', part_fan: 'pr_set_fan',
                    speed_pct: 'pr_set_speed', flow: 'pr_set_flow' };
      command(set.dataset.set, num($(map[set.dataset.set]).value), { label: set.dataset.set });
      return;
    }
    const load = e.target.closest('[data-ams-load]');
    if (load) { command('load_filament', num(load.dataset.amsLoad), { confirm: 'Подать филамент из этого слота? Сопло нагреется.' }); return; }
    const amsEdit = e.target.closest('[data-ams-edit]');
    if (amsEdit) {
      const [unit, tray] = amsEdit.dataset.amsEdit.split(':');
      const typed = await ask({
        title: 'Пластик в слоте AMS',
        sub: `Слот ${unit}:${tray}`,
        fields: [{
          name: 'type', label: 'Тип', type: 'text',
          value: amsEdit.dataset.type || '',
          placeholder: 'PLA, PETG, ABS, TPU…',
          hint: 'Как на катушке. Только метка слота, не температуры.',
        }],
        ok: 'Записать',
      });
      const type = String(typed || '').trim();
      if (!type) return;
      command('ams_filament', { ams_id: num(unit), tray_id: num(tray), type: type.toUpperCase(), color: amsEdit.dataset.color },
        { confirm: `Записать материал ${type.toUpperCase()} в слот AMS?`, label: 'AMS ' + type });
      return;
    }
    const sd = e.target.closest('[data-sd-path]');
    if (sd) {
      filesPath = sd.dataset.sdPath || '/';
      loadFiles();
      return;
    }
    const prev = e.target.closest('[data-preview-file]');
    if (prev) {
      const p = active();
      if (!p) return;
      get('/api/printer/preview', { printer_id: p.id, path: prev.dataset.previewFile })
        .then((info) => {
          const bits = [];
          if (num(info.minutes)) bits.push(minutesText(info.minutes));
          if (num(info.grams)) bits.push('~' + nfmt(info.grams) + ' г');
          if (info.material) bits.push(info.material);
          if (info.color) bits.push(info.color);
          toast('Превью ' + (info.name || ''), bits.join(' · ') || 'оценка из файла', 'info');
        })
        .catch(fail);
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
      const job = (PF.state.jobs.queue || []).find((x) => x.id === js.dataset.jobStart)
        || (PF.state.jobs.history || []).find((x) => x.id === js.dataset.jobStart);
      if (!job) return fail(new Error('Задание не найдено в текущем списке'));
      try {
        await U.withBusy(js, async () => {
          if (!await preflightAndConfirmJob(job, PF.state.activePrinter || job.printer_id || '')) return;
          await post('/api/jobs/start', { id: job.id, printer_id: PF.state.activePrinter || job.printer_id || '',
            confirmed: true, preflight_acknowledged: true });
          toast('Задание запущено'); PF.refreshCore();
        });
      } catch (err) { fail(err); }
      return;
    }
    const clone = e.target.closest('[data-job-clone]');
    if (clone) {
      post('/api/workshop/clone', { id: clone.dataset.jobClone })
        .then(() => { toast('Копия в очереди'); PF.refreshCore(); }).catch(fail);
      return;
    }
    const noauto = e.target.closest('[data-job-noauto]');
    if (noauto) {
      const job = (PF.state.jobs.queue || []).find((x) => x.id === noauto.dataset.jobNoauto);
      post('/api/workshop/no-auto', { id: noauto.dataset.jobNoauto, no_auto: !num(job && job.no_auto) })
        .then(() => PF.refreshCore()).catch(fail);
      return;
    }
    const jc = e.target.closest('[data-job-cancel]');
    if (jc) {
      if (!confirmDanger('Отменить задание? Если оно печатается — печать будет остановлена.')) return;
      post('/api/jobs/cancel', { id: jc.dataset.jobCancel })
        .then(() => {
          // В96: отмена не «сжигает» работу — вернёт копию задания в очередь.
          U.toastUndo('Задание отменено', 'Вернуть его копию в очередь?',
            () => post('/api/workshop/clone', { id: jc.dataset.jobCancel })
              .then(() => { toast('Копия вернулась в очередь'); PF.refreshCore(); }).catch(fail));
          PF.refreshCore();
        }).catch(fail);
      return;
    }
    const found = e.target.closest('[data-found]');
    if (found) {
      const p = JSON.parse(found.dataset.found);
      $('pf_host').value = p.host || '';
      $('pf_serial').value = p.serial || '';
      if (p.name && !$('pf_name').value) $('pf_name').value = p.name;
      toast('Данные подставлены', 'Введите Access Code и сохраните');
      return;
    }
    const cloudDev = e.target.closest('[data-cloud]');
    if (cloudDev) {
      // Принтер из аккаунта Bambu: IP и Access Code сервер подставит сам.
      PF.tempCloudDevice = cloudDev.dataset.cloud;
      $('pf_mode').value = 'cloud';
      $('pf_serial').value = cloudDev.dataset.cloud;
      $('pf_host').value = '';
      if (!$('pf_name').value) $('pf_name').value = cloudDev.querySelector('b') ? cloudDev.querySelector('b').textContent.replace('☁ ', '') : '';
      toast('Принтер из облака выбран', 'Access Code подставится автоматически — сохраните');
      return;
    }
    const convOrder = e.target.closest('[data-convert-order]');
    if (convOrder) {
      convertActiveToOrder(convOrder.dataset.convertOrder || PF.state.activePrinter);
      return;
    }
    const jobConv = e.target.closest('[data-job-convert]');
    if (jobConv) {
      convertJobToOrder(jobConv.dataset.jobConvert);
      return;
    }
    const linkOrder = e.target.closest('[data-link-order]');
    if (linkOrder) {
      openLinkOrder({ printerId: linkOrder.dataset.linkOrder || PF.state.activePrinter }).catch(() => {});
      return;
    }
    const jobLink = e.target.closest('[data-job-link]');
    if (jobLink) {
      openLinkOrder({ jobId: jobLink.dataset.jobLink }).catch(() => {});
      return;
    }
    const ordOpen = e.target.closest('[data-order-open]');
    if (ordOpen) {
      const orderId = ordOpen.dataset.orderOpen;
      if (orderId && PF.modules.ops && PF.modules.ops.openOrder) {
        PF.go('orders');
        PF.modules.ops.openOrder(orderId);
      }
      return;
    }
  });

  $('pr_speed_apply').addEventListener('click', () =>
    command('speed', num($('pr_speed_sel').value, 2), { label: 'скорость' }));
  $('pr_skip_apply').addEventListener('click', () =>
    command('skip_objects', [num($('pr_skip_obj').value, 1)], {
      confirm: 'Исключить этот объект из печати? Он больше не будет печататься.',
      label: 'пропустить объект' }));
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
  /* 119: разметка стола и проекция плиты */
  $('pr_map_btn').addEventListener('click', openBedMapModal);
  $('pr_map_toggle').addEventListener('click', () => {
    const p = active();
    if (!p) return fail(new Error('Принтер ещё не добавлен'));
    bedMapOn[p.id] = !bedMapOn[p.id];
    $('pr_map_toggle').classList.toggle('active', !!bedMapOn[p.id]);
    bedMapKey = '';
    updateBedProjection(p);
    toast(bedMapOn[p.id] ? 'Проекция плиты включена' : 'Проекция плиты выключена',
      bedMapOn[p.id] ? 'Контуры объектов задания появятся поверх видео' : '', 'info');
  });
  $('bedmap_stage').addEventListener('click', (ev) => {
    if (bedmapPts.length >= 4) return;
    const img = $('bedmap_img');
    const r = img.getBoundingClientRect();
    if (!r.width || !r.height) return;
    bedmapPts.push([
      Math.min(1, Math.max(0, (ev.clientX - r.left) / r.width)),
      Math.min(1, Math.max(0, (ev.clientY - r.top) / r.height)),
    ]);
    bedmapRender();
  });
  $('bedmap_save').addEventListener('click', saveBedMap);
  $('bedmap_reset').addEventListener('click', resetBedMap);
  bindBedMapClicks();
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
  $('pr_cam_diag').addEventListener('click', async () => {
    const out = $('pr_cam_diag_out');
    const p = active();
    if (!p) return fail(new Error('Принтер ещё не добавлен'));
    out.innerHTML = '<p class="muted" style="font-size:12px">Проверяю порт 6000, TLS и первый кадр…</p>';
    try {
      const res = await get('/api/camera/diagnose?printer_id=' + encodeURIComponent(p.id));
      out.innerHTML = (res.steps || []).map((s) => `<div style="padding:3px 0;font-size:12.5px">
        <b style="color:${s.ok ? 'var(--ok)' : 'var(--bad)'}">${s.ok ? '✓' : '✕'}</b> ${esc(s.step)} — ${esc(s.text)}</div>`).join('')
        + `<p class="muted" style="font-size:12px;margin-top:6px"><b>${esc(res.summary)}</b></p>`;
    } catch (e) { fail(e); }
  });
  $('pr_cam_rtsp').addEventListener('click', async () => {
    const p = active();
    if (!p) return fail(new Error('Принтер ещё не добавлен'));
    try {
      const res = await get('/api/printer/rtsp-link?printer_id=' + encodeURIComponent(p.id));
      if (!res.link) return toast('RTSP недоступен', res.error || 'Нужны IP и Access Code', 'warn');
      const out = $('pr_cam_diag_out');
      out.innerHTML = `<div class="notice"><span>🎥</span><span>RTSP-поток для внешнего плеера (VLC: Медиа → Открыть сетевой адрес):<br><code style="word-break:break-all">${esc(res.link)}</code><br><small class="muted">Ссылка содержит Access Code — не передавайте её посторонним.</small></span></div>`;
      try { await navigator.clipboard.writeText(res.link); toast('RTSP-ссылка скопирована', 'Вставьте в VLC'); } catch (e) { /* без буфера обмена */ }
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
    try {
      const payload = printPayload();
      const check = await post('/api/printer/preflight', payload);
      if ((check.blocks || []).length) throw new Error(
        'Preflight блокирует старт: ' + check.blocks.map((x) => x.title || x.detail).join('; '));
      const warns = (check.warns || []).map((x) => `⚠ ${x.title || x.detail}`).join('\n');
      if (!confirmDanger('Запустить печать? Принтер немедленно начнёт нагрев и движение.'
        + (warns ? `\n\nPreflight предупреждения:\n${warns}\n\nПодтвердите проверку.` : ''))) return;
      payload.confirmed = true;
      payload.preflight_acknowledged = true;
      await post('/api/printer/print', payload);
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
  $('queue_next').addEventListener('click', startNextJob);
  $('jf_local_file').addEventListener('change', (e) => {
    const file = e.target.files && e.target.files[0];
    queueLocalFile = file || null;
    if (file) {
      $('jf_file').value = file.name;
      $('jf_file_hint').textContent = `${file.name} · сохранится в локальную очередь (${Math.round(file.size / 1024)} КБ)`;
    } else {
      $('jf_file_hint').textContent = 'Файл сохранится локально и попадёт в очередь без предварительной загрузки на принтер.';
    }
  });
  $('job_save').addEventListener('click', async () => {
    const file = $('jf_file').value.trim();
    if (!file && !queueLocalFile) return fail(new Error('Выберите файл с компьютера или укажите имя файла на SD-карте'));
    if (queueLocalFile && !/\.(3mf|gcode(?:\.3mf)?)$/i.test(queueLocalFile.name)) {
      return fail(new Error('Поддерживаются только 3MF и G-code'));
    }
    const values = {
      name: $('jf_name').value.trim() || file || (queueLocalFile && queueLocalFile.name),
      file,
      printer_id: $('jf_printer_id').value,
      order_id: $('jf_order_id').value,
      plate: num($('jf_plate').value, 1) || 1,
      priority: num($('jf_priority').value),
      use_ams: $('jf_use_ams').checked,
      bed_level: $('jf_bed_level').checked,
      source: queueLocalFile ? 'local-upload' : 'manual',
    };
    try {
      if (queueLocalFile) {
        const form = new FormData();
        form.append('file', queueLocalFile);
        Object.entries(values).forEach(([key, value]) => form.append(key, String(value ?? '')));
        form.append('allow_auto_start', 'true');
        await api('/api/jobs/upload', { method: 'POST', body: form });
      } else {
        await post('/api/jobs/enqueue', values);
      }
      queueLocalFile = null;
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
    box.innerHTML = '<div class="skeleton" aria-busy="true" style="height:44px"></div>';
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
    eventsCache = list;                                  // ПР6: для «последней ошибки» в Здоровье
    host.innerHTML = list.length ? list.map((e) => `<div class="event ${esc(e.kind)}"><span class="edot"></span>`
      + `<span class="etext"><b>${esc(e.title)}</b><small>${esc(e.detail || '')}</small></span>`
      + `<time>${esc(dateTimeText(e.at))}</time></div>`).join('')
      : '<div class="empty compact"><span>Событий пока нет.</span></div>';
    const p = active();
    if (p && !((p.printer || {}).problems || []).length) renderHealth(p);
  } catch (err) {
    eventsCache = [];
    host.innerHTML = '<div class="empty compact"><span>Журнал недоступен.</span></div>';
  }
}

async function convertActiveToOrder(printerId) {
  printerId = printerId || PF.state.activePrinter;
  try {
    toast('Создаём заказ из печати...', '', 'info');
    const res = await post('/api/printer/convert-to-order', { printer_id: printerId });
    if (res && res.order) {
      toast(res.created ? 'Заказ создан из печати' : 'Заказ уже привязан', `Заказ №${res.order.number} · ${res.order.product || ''}`);
      await PF.refreshCore();
      if (PF.modules.ops && PF.modules.ops.openOrder) {
        PF.go('orders');
        PF.modules.ops.openOrder(res.order.id);
      }
    }
  } catch (e) { fail(e); }
}

async function convertJobToOrder(jobId) {
  try {
    toast('Создаём заказ из задания...', '', 'info');
    const res = await post('/api/jobs/convert-to-order', { job_id: jobId });
    if (res && res.order) {
      toast(res.created ? 'Заказ создан из задания' : 'Заказ уже привязан', `Заказ №${res.order.number} · ${res.order.product || ''}`);
      await PF.refreshCore();
      if (PF.modules.ops && PF.modules.ops.openOrder) {
        PF.go('orders');
        PF.modules.ops.openOrder(res.order.id);
      }
    }
  } catch (e) { fail(e); }
}

/* -------------- Привязка печати/задания к существующему заказу --------------
 * Нужен отдельный экран, потому что после сбоя питания или ручной распечатки
 * автоматическое сопоставление файла с заказом часто промахивается: файл
 * называется иначе, заказа ещё не было в базе или его номер не попал в имя.
 * «Новый заказ» здесь не помогает — тогда получается дубль, а исходный
 * заказ так и остаётся без печати. Кнопки «🔗 Привязать к заказу» открывают
 * этот пикер и вызывают /api/printer/link-to-order или /api/jobs/link-to-order. */

let linkOrderTarget = null; // {printerId} или {jobId}
let linkOrderChoice = ''; // выбранный order_id

let linkOrdersCache = [];
let linkOrdersLoaded = false;
async function renderLinkOrderList(query) {
  const host = $('link_order_list');
  if (!host) return;
  const q = String(query || '').trim().toLowerCase();
  // Свежий список незакрытых заказов прямо с коннектора: иначе в списке
  // может застрять уже удалённый/закрытый заказ, и привязка дала бы
  // «Заказ не найден» или оживила бы закрытый заказ.
  if (!linkOrdersLoaded) {
    try {
      const fresh = await get('/api/orders', {});
      linkOrdersCache = (fresh.orders || []).filter((o) => !PF.isFinal(o));
    } catch (e) {
      linkOrdersCache = (PF.state.orders || []).filter((o) => !PF.isFinal(o));
    }
    linkOrdersLoaded = true;
  }
  const orders = linkOrdersCache;
  const matched = q
    ? orders.filter((o) => [o.number, o.product, o.customer_name, o.phone, o.file, o.notes]
        .some((v) => String(v || '').toLowerCase().includes(q)))
    : orders;
  if (!matched.length) {
    host.innerHTML = q
      ? '<div class="empty compact"><span>Ничего не найдено. Уберите фильтр или заведите новый заказ.</span></div>'
      : '<div class="empty compact"><span>Активных заказов нет. Создайте заказ или используйте «✨ Новый заказ».</span></div>';
    return;
  }
  // Топ-30 самых свежих — этого достаточно и не тормозит на большой базе.
  host.innerHTML = matched.slice(0, 30).map((o) => {
    const sel = o.id === linkOrderChoice ? ' selected' : '';
    const due = o.due ? ` · срок ${dateText(o.due)}` : '';
    const cust = o.customer_name ? ` · ${esc(o.customer_name)}` : '';
    return `<label class="pick-row${sel}" data-pick-order="${esc(o.id)}" style="display:flex;gap:10px;align-items:center;padding:8px 10px;border:1px solid var(--line);border-radius:10px;margin-bottom:6px;cursor:pointer${sel ? ';background:var(--accent-soft)' : ''}">`
      + `<input type="radio" name="pick_order" value="${esc(o.id)}"${sel ? ' checked' : ''} style="margin:0">`
      + `<div style="flex:1;min-width:0"><b>№${esc(o.number)} · ${esc(o.product || 'без названия')}</b>`
      + `<small class="muted" style="display:block">${esc((PF.status(o.status) || {}).name || o.status || '')}${cust}${due}</small></div>`
      + `</label>`;
  }).join('');
}

async function openLinkOrder(target) {
  linkOrderTarget = target || null;
  linkOrderChoice = '';
  linkOrdersCache = [];
  linkOrdersLoaded = false;
  $('link_order_submit').disabled = true;
  const search = $('link_order_search');
  if (search) search.value = '';
  const title = $('link_order_title');
  const sub = $('link_order_sub');
  if (target && target.printerId) {
    const p = PF.state.printers.find((x) => x.id === target.printerId);
    const live = PF.livePrinter(target.printerId);
    const task = (live && live.printer && live.printer.task) || '';
    if (title) title.textContent = 'Привязать текущую печать к заказу';
    if (sub) sub.textContent = `${p ? p.name : 'Принтер'}${task ? ' · ' + task : ''}`;
  } else if (target && target.jobId) {
    const j = (PF.state.jobs.queue || []).concat(PF.state.jobs.history || []).find((x) => x.id === target.jobId);
    if (title) title.textContent = 'Привязать задание к заказу';
    if (sub) sub.textContent = j ? (j.name || j.file || 'Задание') : 'Задание';
  }
  await renderLinkOrderList('');
  openModal('link_order_modal');
}

async function submitLinkOrder() {
  if (!linkOrderTarget || !linkOrderChoice) return;
  try {
    let res;
    if (linkOrderTarget.printerId) {
      res = await post('/api/printer/link-to-order', {
        printer_id: linkOrderTarget.printerId, order_id: linkOrderChoice,
      });
    } else if (linkOrderTarget.jobId) {
      res = await post('/api/jobs/link-to-order', {
        job_id: linkOrderTarget.jobId, order_id: linkOrderChoice,
      });
    }
    if (res && res.order) {
      closeModal('link_order_modal');
      toast('Печать привязана к заказу', `Заказ №${res.order.number} · ${res.order.product || ''}`);
      await PF.refreshCore();
      if (PF.modules.ops && PF.modules.ops.openOrder) {
        PF.go('orders');
        PF.modules.ops.openOrder(res.order.id);
      }
    }
  } catch (e) { fail(e); }
}

function bindLinkOrder() {
  const list = $('link_order_list');
  if (list) {
    list.addEventListener('click', (e) => {
      const row = e.target.closest('[data-pick-order]');
      if (!row) return;
      linkOrderChoice = row.dataset.pickOrder;
      $('link_order_submit').disabled = !linkOrderChoice;
      renderLinkOrderList(($('link_order_search') || {}).value || '').catch(() => {});
    });
  }
  const search = $('link_order_search');
  if (search) {
    search.addEventListener('input', U.debounce(() => renderLinkOrderList(search.value), 120));
  }
  const submit = $('link_order_submit');
  if (submit) submit.addEventListener('click', (e) => { e.preventDefault(); submitLinkOrder(); });
}

/* =============================================================== старт */
PF.on('ready', () => { bindAmsProfiles(); bindSchedule();
  bind();
  bindLinkOrder();
  renderTabs();
  $('queue_auto').checked = !!PF.state.settings.auto_queue;
  const tag = $('nav_printers_tag');
  const total = PF.state.printers.length;
  tag.hidden = !total;
  tag.textContent = String(total);
  loadBedReference();
});
PF.on('live', PF.whenView('printers', () => { renderLive(); }));
PF.on('printers', PF.whenView('printers', () => { renderTabs(); }));
PF.on('view', (d) => {
  if (d.view === 'printers') { loadFiles(); loadEvents(); }
});

PF.modules.printer = { command, openJob, loadFiles, renderLive, openPrinterModal, fillPrintModal, convertActiveToOrder, convertJobToOrder };
})();
