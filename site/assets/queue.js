/* PrintFlow 13.1 — вкладка «Очередь печати» (Б2: вынесено из printer.js).
   Рендер заданий и журнала, умный поиск по двум режимам, группировка по
   принтерам, перестановка приоритета drag-n-drop, живая подсветка активного
   задания, виртуализация журнала (порции), пасхалка-талисман в пустой
   очереди. Действия над заданиями (старт/отмена/клонирование) остаются
   в printer.js — здесь только представление. */
(() => {
'use strict';
const U = PF.ui;
const { $, $$, esc, num, clamp, money, nfmt, minutesText, dateTimeText, debounce, toast, fail, openModal } = U;
const { get, post } = PF.api;

let queueFilter = 'all';
let queueMode = 'jobs';            // 'jobs' | 'history' — куда ищет строка
let queueGrouped = false;          // группировка по принтерам
let historyLimit = 24;             // виртуализация журнала: порция
let lastQueueCount = -1;
let dragJobId = null;

function jobStateChip(state) {
  const map = {
    queued: ['outline', 'В очереди'], uploading: ['accent', 'Загрузка файла'],
    starting: ['accent', 'Стартует'], running: ['accent', 'Печатает'],
    done: ['ok', 'Готово'], failed: ['bad', 'Брак'], cancelled: ['warn', 'Отменено'],
  };
  const [cls, label] = map[state] || ['outline', state];
  return `<span class="chip ${cls}">${esc(label)}</span>`;
}

/* ======================================================== поиск (1) */
function searchText() {
  return String(($('queue_search') || {}).value || '').trim().toLowerCase();
}
function matchJob(j, q) {
  if (!q) return true;
  const printer = PF.state.printers.find((p) => p.id === j.printer_id);
  const order = j.order;
  return [j.name, j.file, printer && printer.name, order && order.number, j.mixed_label]
    .filter(Boolean).some((v) => String(v).toLowerCase().includes(q));
}

/* ==================================================== рендер очереди */
function renderQueue() {
  const queue = PF.state.jobs.queue || [];
  const running = queue.filter((j) => j.state === 'running').length;
  text('queue_sub', `${queue.length} в работе и в очереди · ${running} печатается`);

  const tag = $('nav_queue_tag');
  tag.hidden = !queue.length;
  tag.textContent = String(queue.length);
  tag.className = 'tag' + (running ? ' live' : '');
  if (queue.length !== lastQueueCount) {          // 16: подскок счётчика
    lastQueueCount = queue.length;
    U.bump(tag);
  }

  $$('#queue_filter button').forEach((b) => b.classList.toggle('on', b.dataset.filter === queueFilter));
  const groupBtn = $('queue_group');
  if (groupBtn) groupBtn.classList.toggle('on', queueGrouped);
  $$('#queue_search_mode button').forEach((b) => b.classList.toggle('on', b.dataset.qmode === queueMode));

  const q = searchText();
  let shown = queue.filter((j) =>
    (queueFilter === 'all'
      || (queueFilter === 'queued' && j.state === 'queued')
      || (queueFilter === 'active' && ['uploading', 'starting', 'running'].includes(j.state))
      || (queueFilter === 'unassigned' && j.state === 'queued' && !j.printer_id))
    && matchJob(j, q));

  const host = $('queue_list');
  if (!shown.length) {
    host.innerHTML = queue.length
      ? '<div class="empty compact"><span>⌕</span><b>В этом фильтре заданий нет</b><span>Смените фильтр или поисковый запрос.</span></div>'
      : printerTalisman();
    return;
  }
  const body = queueGrouped ? renderGrouped(shown) : shown.map(queueItemHtml).join('');
  host.innerHTML = body;
  U.stagger(host);
  decorateQueuePlates();
  // 7: живое задание подсвечено и видно сразу при входе на вкладку
  const live = host.querySelector('.queue-item.live');
  if (live) {
    host.querySelectorAll('.queue-item.live').forEach((el) => el.classList.add('pulse'));
    if (document.querySelector('#view-queue.on')) {
      setTimeout(() => { if (live.scrollIntoView) live.scrollIntoView({ block: 'nearest', behavior: 'smooth' }); }, 60);
    }
  }
  bindQueueDrag();
}

function printerTalisman() {
  // 38: пасхалка — принтер «печатает» пустой лист в пустой очереди
  return `<div class="empty talisman-empty"><svg class="talisman" viewBox="0 0 120 96" aria-hidden="true">`
    + `<path class="tl-body" d="M26 42h68a8 8 0 0 1 8 8v22a8 8 0 0 1-8 8H26a8 8 0 0 1-8-8V50a8 8 0 0 1 8-8z"/>`
    + `<path class="tl-spool" d="M46 44v18M74 44v18"/>`
    + `<path class="tl-fil" d="M46 62c6-8 22-8 28 0s22 8 28 0"/>`
    + `<path class="tl-paper" d="M88 22h12l6 6v26H88z"/><path class="tl-line" d="M92 34h8M92 40h8M92 46h5"/>`
    + `<path class="tl-print" d="M96 18h4l5 5v9h-9z"/></svg>`
    + `<b>Очередь пуста</b><span>Пока принтер «печатает» пустой лист — добавьте файл в очередь.</span>`
    + `<button class="btn sm primary" type="button" data-empty-click="queue_add">+ Задание</button></div>`;
}

function queueItemHtml(j) {
  const i = (PF.state.jobs.queue || []).indexOf(j);
  const printer = PF.state.printers.find((p) => p.id === j.printer_id);
  const order = j.order;
  const queuedPos = j.state === 'queued'
    ? (PF.state.jobs.queue || []).filter((x) => x.state === 'queued').indexOf(j) + 1 : 0;
  const progress = clamp(num(j.progress), 0, 100);
  // 6: плашка дефицита пластика — если на задание не хватает катушки
  const spool = j.spool_id ? (PF.state.spools || []).find((s) => s.id === j.spool_id) : null;
  const needG = num(j.est_grams);
  const haveG = spool ? num(spool.remaining_grams) : 0;
  const deficit = spool && haveG > 0 && needG > haveG + 5;
  // 5: мини-таймлайн «цифровой нити» задания — точки с временами
  const steps = [
    j.created_at ? ['загружено', j.created_at] : null,
    j.started_at ? ['старт', j.started_at] : null,
    j.finished_at ? ['финиш', j.finished_at] : null,
  ].filter(Boolean);
  const tl = steps.length > 1
    ? `<span class="q-timeline" title="${steps.map(([n, t]) => `${n} ${dateTimeText(t)}`).join(' · ')}">`
      + steps.map(([n]) => `<i data-step="${esc(n)}"></i>`).join('<em></em>') + '</span>' : '';
  return `<div class="queue-item${j.state === 'running' ? ' running live' : ''}" draggable="${j.state === 'queued' ? 'true' : 'false'}"`
    + ` data-job-id="${esc(j.id)}" data-pos="${i}">`
      + `<div class="q-plate" data-plate="${esc(j.file || '')}" title="Превью плиты из файла задания">▦</div>`
      + `<span class="qnum">${i + 1}</span><div class="qbody"><b>${esc(j.name || j.file || 'Задание')}</b>`
    + `<small>${jobStateChip(j.state)} ${esc(printer ? printer.name : 'любой принтер')}`
    + (queuedPos ? ` · <span class="chip outline">№ ${queuedPos} в очереди</span>` : '')
    + (order ? ` · <a href="#orders" class="order-link" data-order-open="${esc(order.id || '')}">заказ №${esc(order.number)}</a>` : '')
    + (j.mixed_label ? ` · <span class="chip outline" title="Смешанная плита">${esc(j.mixed_label)}</span>` : '')
    + (num(j.no_auto) ? ' · без автостарта' : '')
    + (num(j.est_minutes) ? ` · оценка ${minutesText(j.est_minutes)}${needG ? ' · ~' + nfmt(needG) + ' г' : ''}` : '')
    + tl
    + '</small>'
    + (deficit ? `<div class="notice warn q-deficit"><span>◍</span><span>Мало ${esc(spool.material || 'пластика')} ${esc(spool.color_name || '')} — на задание нужно ${nfmt(needG - haveG)} г сверх остатка (${nfmt(haveG)} г)</span></div>` : '')
    + (j.state === 'running'
      ? `<div class="bar thin" style="margin-top:6px"><i style="width:${progress}%"></i></div>`
      : (j.state === 'queued'
        ? `<div class="bar thin qbar-ghost" style="margin-top:6px" title="Порядок можно менять перетаскиванием"><i style="width:${queuedPos ? Math.max(4, 100 / Math.max(1, queuedPos)) : 0}%"></i></div>` : ''))
    + '</div><div class="acts">'
    + (!order ? `<button class="btn sm primary" type="button" data-job-link="${esc(j.id)}" title="Привязать это задание к уже существующему заказу"><i data-icon="link">🔗</i> К заказу</button>` : '')
    + (!order ? `<button class="btn sm ghost" type="button" data-job-convert="${esc(j.id)}" title="Создать новый заказ из задания"><i data-icon="sparkles">✨</i> Новый</button>` : '')
    + (j.state === 'queued' ? `<button class="btn sm primary" type="button" data-job-start="${esc(j.id)}">Печать</button>` : '')
    + `<button class="btn sm ghost" type="button" data-job-clone="${esc(j.id)}" title="Копия в очередь"><i data-icon="copy">⧉</i></button>`
    + (j.state === 'queued' ? `<button class="btn sm ghost" type="button" data-job-noauto="${esc(j.id)}" title="Автостарт">${num(j.no_auto) ? 'авто вкл' : 'без авто'}</button>` : '')
    + `<button class="icon-btn sm danger" type="button" data-job-cancel="${esc(j.id)}" title="Отменить">×</button>`
    + '</div></div>';
}

/* ================================================= превью плит (В25) */
/* Миниатюра берётся один раз на файл и кэшируется в памяти вкладки:
   живое обновление очереди не должно штормить коннектор распаковками. */
const plateCache = new Map();
const platePending = new Set();

function decorateQueuePlates() {
  const host = $('queue_list');
  if (!host) return;
  const cells = $$('[data-plate]', host);
  cells.forEach((cell) => {
    const name = cell.dataset.plate || '';
    if (!/\.3mf$/i.test(name)) return;                 // превью только у 3MF
    if (plateCache.has(name)) {
      const b64 = plateCache.get(name);
      if (b64) {
        const img = document.createElement('img');
        img.alt = '';
        img.src = 'data:image/png;base64,' + b64;
        cell.textContent = '';
        cell.appendChild(img);
      }
      return;                                          // нет превью — глyф остаётся
    }
    if (platePending.has(name)) return;
    platePending.add(name);
    get('/api/jobs/plate', { name }).then((data) => {
      const b64 = (data && data.b64) || '';
      plateCache.set(name, b64);
      if (b64 && cell.isConnected) {
        const img = document.createElement('img');
        img.alt = '';
        img.src = 'data:image/png;base64,' + b64;
        cell.textContent = '';
        cell.appendChild(img);
      }
    }).catch(() => {
      plateCache.set(name, '');
    }).finally(() => platePending.delete(name));
  });
}

/* ================================================= группировка (3) */
function renderGrouped(list) {
  const groups = new Map();
  list.forEach((j) => {
    const printer = PF.state.printers.find((p) => p.id === j.printer_id);
    const key = printer ? printer.id : '—';
    if (!groups.has(key)) groups.set(key, { name: printer ? printer.name : 'Без принтера', items: [] });
    groups.get(key).items.push(j);
  });
  return [...groups.entries()].map(([key, g]) =>
    `<div class="queue-group" data-group="${esc(key)}">`
    + `<div class="queue-group-head"><span class="qg-dot"></span><b>${esc(g.name)}</b>`
    + `<span class="chip outline">${g.items.length}</span></div>`
    + g.items.map(queueItemHtml).join('') + '</div>').join('');
}

/* ========================================== drag-n-drop приоритет (4) */
function bindQueueDrag() {
  const host = $('queue_list');
  if (!host) return;
  $$('.queue-item[draggable="true"]', host).forEach((item) => {
    item.addEventListener('dragstart', () => {
      dragJobId = item.dataset.jobId;
      item.classList.add('dragging');
      host.classList.add('dragging-any');
    });
    item.addEventListener('dragend', () => {
      item.classList.remove('dragging');
      dragJobId = null;
      host.classList.remove('dragging-any');
    });
    item.addEventListener('dragover', (e) => {
      if (!dragJobId || dragJobId === item.dataset.jobId) return;
      e.preventDefault();
      item.classList.add('drop-target');
    });
    item.addEventListener('dragleave', () => item.classList.remove('drop-target'));
    item.addEventListener('drop', async (e) => {
      e.preventDefault();
      item.classList.remove('drop-target');
      if (!dragJobId || dragJobId === item.dataset.jobId) return;
      const queue = PF.state.jobs.queue || [];
      const from = queue.findIndex((x) => x.id === dragJobId);
      const to = queue.findIndex((x) => x.id === item.dataset.jobId);
      if (from < 0 || to < 0 || from === to) return;
      const direction = from > to ? 'up' : 'down';
      try {
        await post('/api/jobs/reorder', { id: dragJobId, direction });
        toast('Порядок обновлён', direction === 'up' ? 'Задание поднято выше' : 'Задание опущено ниже');
        PF.refreshCore();
      } catch (err) { fail(err); }
    });
  });
}

/* ======================================================== журнал (8,9) */
function renderHistory() {
  const history = PF.state.jobs.history || [];
  const q = queueMode === 'history' ? searchText() : '';
  const hq = q ? history.filter((j) => String(j.name || j.file || '').toLowerCase().includes(q)) : history;
  const count = $('history_count');
  if (count) {
    count.hidden = !hq.length;
    count.textContent = `${hq.length} записей`;
  }
  const slice = hq.slice(0, historyLimit);
  const host = $('queue_history');
  host.innerHTML = slice.length ? slice.map((j) => `<div class="tx-row" data-passport="${esc(j.id)}" title="Паспорт печати — план против факта">`
    + `<span class="tx-ic ${j.state === 'done' ? 'income' : 'expense'}">${j.state === 'done' ? '✓' : '✕'}</span>`
    + `<div class="tx-body"><b>${esc(j.name || j.file || 'Печать')}</b>`
    + `<small>${esc(dateTimeText(j.finished_at))} · ${minutesText(j.duration_min)} · ${nfmt(j.grams)} г`
    + (num(j.est_minutes) ? ' · оценка была ' + minutesText(j.est_minutes) : '') + '</small></div>'
    + `<span class="amt">${money(j.cost)}</span></div>`).join('')
    : (hq
      ? '<div class="empty compact"><span>⌕</span><b>Ничего не найдено</b><span>Попробуйте другое слово.</span></div>'
      : '<div class="empty"><span class="big">✓</span><b>Журнал пока пуст</b><span>Сюда попадут завершённые печати: время, граммы, себестоимость и паспорт план/факт.</span></div>');
  if (hq.length > historyLimit) {
    const more = document.createElement('button');
    more.className = 'btn sm ghost queue-more';
    more.type = 'button';
    more.textContent = `Показать ещё ${Math.min(50, hq.length - historyLimit)} из ${hq.length - historyLimit}`;
    more.addEventListener('click', () => { historyLimit += 50; renderHistory(); });
    host.appendChild(more);
  }
  $$('#queue_history [data-passport]').forEach((row) => {
    row.addEventListener('click', () => openPassport(row.dataset.passport));
  });
}

/* ============================================================ паспорт */
async function openPassport(jobId) {
  try {
    const p = await get('/api/job/passport?id=' + encodeURIComponent(jobId));
    const j = p.job || {};
    $('jp_title').textContent = j.name || j.file || 'Печать';
    $('jp_sub').textContent = p.order && p.order.number
      ? `Заказ №${p.order.number} · ${p.order.product || ''}` : 'Без привязки к заказу';
    const pvf = p.plan_vs_fact || {};
    const parts = [];
    parts.push(`<table class="data"><thead><tr><th>Параметр</th><th class="right">План (слайсер)</th><th class="right">Факт</th><th class="right">Отклонение</th></tr></thead><tbody>
      <tr><td>Время печати</td><td class="right">${pvf.minutes ? minutesText(pvf.minutes.plan) : '—'}</td><td class="right">${minutesText(num(j.duration_min))}</td><td class="right">${pvf.minutes ? pvf.minutes.diff_pct + '%' : '—'}</td></tr>
      <tr><td>Пластик</td><td class="right">${pvf.grams ? nfmt(pvf.grams.plan, 1) + ' г' : '—'}</td><td class="right">${nfmt(num(j.grams), 1)} г</td><td class="right">${pvf.grams ? pvf.grams.diff_pct + '%' : '—'}</td></tr>
      </tbody></table>`);
    if (pvf.minutes) parts.push(`<p class="muted">${esc(pvf.minutes.verdict)} · ${esc(pvf.grams.verdict)}</p>`);
    if (p.error_decoded && p.error_decoded.title) {
      parts.push(`<div class="notice bad"><span>✕</span><span><b>${esc(p.error_decoded.title)}</b><br>${esc(p.error_decoded.advice || '')}</span></div>`);
    }
    if ((p.guard || []).length) {
      parts.push('<h3 style="margin:12px 0 4px">Сторож за время печати</h3>');
      p.guard.forEach((g) => {
        const actions = g.data && g.data.actions && g.data.actions.length ? '<br><small>Сделано: ' + esc(g.data.actions.join(', ')) + '</small>' : '';
        parts.push(`<div class="notice warn"><span>⚠</span><span><b>${esc(g.title)}</b>${g.detail ? '<br>' + esc(g.detail) : ''}${actions}</span></div>`);
      });
    }
    if ((p.photos || []).length) {
      parts.push('<h3 style="margin:12px 0 4px">Фото</h3><div style="display:flex;gap:8px;flex-wrap:wrap">'
        + p.photos.map((ph) => `<img src="/api/order/photo.jpg?photo_id=${esc(ph.id)}" style="width:120px;border-radius:8px" alt="">`).join('') + '</div>');
    }
    $('jp_body').innerHTML = parts.join('');
    openModal('job_passport_modal');
  } catch (e) { fail(e); }
}

/* ============================================================ события */
function bind() {
  const search = $('queue_search');
  if (search) search.addEventListener('input', debounce(render, 200));
  const mode = $('queue_search_mode');
  if (mode) mode.addEventListener('click', (e) => {
    const btn = e.target.closest('[data-qmode]');
    if (!btn) return;
    queueMode = btn.dataset.qmode || 'jobs';
    historyLimit = 24;
    render();
  });
  const group = $('queue_group');
  if (group) group.addEventListener('click', () => {
    queueGrouped = !queueGrouped;
    renderQueue();
  });
  const filter = $('queue_filter');
  if (filter) filter.addEventListener('click', (e) => {
    const button = e.target.closest('[data-filter]');
    if (!button) return;
    queueFilter = button.dataset.filter || 'all';
    renderQueue();
  });
}
function text(id, v) { const el = $(id); if (el) el.textContent = v; }

/* 13.1 (14): «всплеск» финиша — задание ушло в журнал: конфетти + тост. */
let prevJobStates = {};
function checkFinished(jobs) {
  const now = {};
  jobs.forEach((j) => { now[j.id] = j.state; });
  Object.keys(prevJobStates).forEach((id) => {
    const before = prevJobStates[id];
    const after = now[id];
    if (after === 'done' && (before === 'running' || before === 'starting' || before === 'uploading')) {
      const origin = document.querySelector(`.queue-item[data-job-id="${id}"]`)
        || document.querySelector('#view-queue') || null;
      U.confetti(origin);
      toast('Печать завершена', 'Задание перешло в журнал печати', 'ok', {
        label: 'Журнал',
        run: () => PF.go('queue'),
      });
    }
  });
  prevJobStates = now;
}

function render() {
  checkFinished(PF.state.jobs.queue || []);
  renderQueue();
  renderHistory();
}

PF.on('ready', () => { bind(); render(); });
PF.on('data', PF.whenView('queue', render));
PF.on('view', (d) => {
  if (d.view === 'queue') { historyLimit = 24; render(); }
});

PF.queue = { render, renderQueue, renderHistory, openPassport };
})();
