/* PrintFlow 14.0 — Центр смены: воронка обращений, inbox, оборудование,
   план/факт производства и безопасная песочница правил.

   14.0 (Б2/З10): файл переписан с нуля. Прежняя версия не импортировала
   ни одного хелпера — `get`, `post`, `toast`, `fail` вызывались как
   необъявленные глобалы, поэтому раздел не мог загрузить данные в принципе
   (ReferenceError в первом же обработчике), а `esc` был продублирован
   своим `esc10`. Теперь: PF.api/PF.ui, шаблонизатор PF.html с
   автоэкранированием (55), единый форматтер PF.fmt (66), ленивая загрузка
   при первом входе в раздел (47) и рендер только видимой вкладки (45). */
(() => {
'use strict';
const U = PF.ui;
const { $, esc, num, toast, fail, debounce, emptyHtml, html, raw, render, fmt } = U;
const { get, post } = PF.api;

const STAGES = {
  new: 'Новое', qualifying: 'Уточнить', quoted: 'Расчёт',
  awaiting: 'Ждём согласование', order: 'Заказ', won: 'Успешно', lost: 'Потеряно',
};
const STAGE_ORDER = Object.keys(STAGES);
let overview = null;
let production = null;

/* ============================================================ воронка */
function stageOptions(current) {
  return html`${STAGE_ORDER.map((key) => html`
    <option value="${key}"${key === (current || 'new') ? raw(' selected') : raw('')}>${STAGES[key]}</option>`)}`;
}

function renderPipeline(items) {
  const counts = {};
  (items || []).forEach((row) => { counts[row.stage] = num(row.count); });
  render($('ops10_pipeline'), STAGE_ORDER.map((key) => html`
    <div class="kpi"><small>${STAGES[key]}</small><b>${counts[key] || 0}</b><span>обращений</span></div>`).join(''));
}

/* ========================================================= inbox (CRM) */
function inboxRow(row) {
  const name = row.name || row.username || ('Чат ' + row.chat_id);
  return html`
    <article class="ops10-item">
      <div class="ops10-item-head">
        <div><b>${name}</b><small class="muted"> · ${row.chat_id} · ${fmt.stamp(row.at || row.last_seen)}</small></div>
        <span class="tag ${row.unread ? 'warn' : ''}">${row.unread ? 'новое' : 'прочитано'}</span>
      </div>
      <p>${row.text || 'Нет текста последнего сообщения'}</p>
      <div class="ops10-actions">
        <select data-inbox-stage="${row.chat_id}" aria-label="Стадия обращения">${stageOptions(row.pipeline_stage)}</select>
        <button class="btn sm" data-inbox-read="${row.chat_id}" type="button">Прочитано</button>
        <a class="btn sm ghost" href="#clientbot" data-view="clientbot">Открыть диалог</a>
      </div>
    </article>`;
}

function renderInbox(items) {
  const host = $('ops10_inbox');
  if (!host) return;
  if (!items || !items.length) { render(host, emptyHtml('Обращений пока нет', 'Когда покупатель напишет в бот, диалог появится здесь.')); return; }
  render(host, items.map(inboxRow).join(''));
  host.querySelectorAll('[data-inbox-stage]').forEach((select) => {
    select.addEventListener('change', async () => {
      try {
        await post('/api/ops10/inbox/stage', { chat_id: select.dataset.inboxStage, stage: select.value });
        toast('Стадия сохранена', STAGES[select.value] || select.value);
        await load();
      } catch (e) { fail(e); }
    });
  });
  host.querySelectorAll('[data-inbox-read]').forEach((button) => {
    button.addEventListener('click', async () => {
      try {
        await post('/api/client-bot/read', { chat_id: button.dataset.inboxRead, actor: 'panel' });
        await load();
      } catch (e) { fail(e); }
    });
  });
}

/* ======================================================== оборудование */
function renderPrinters(printers) {
  const host = $('ops10_printers');
  if (!host) return;
  if (!printers || !printers.length) {
    render(host, emptyHtml('Принтеры не подключены', 'Добавьте Bambu Lab в разделе «Принтеры» — здесь появится телеметрия.'));
    return;
  }
  render(host, printers.map((item) => {
    const info = item.printer || {};
    const conn = item.connection || {};
    const ams = item.ams || {};
    const trays = (ams.trays || []).filter((t) => t.active || t.remain != null).slice(0, 8);
    const progress = Math.max(0, Math.min(100, num(info.progress)));
    const low = trays.filter((t) => num(t.remain) < 15).length;
    return html`
      <article class="ops10-printer">
        <div class="ops10-item-head">
          <b>${item.name || info.name || 'Принтер'}</b>
          <span class="tag ${conn.connected ? 'ok' : 'bad'}">${conn.connected ? 'онлайн' : 'офлайн'}</span>
        </div>
        <small>${info.state_label || info.state || 'Ожидание'} · ${info.task || 'нет активной печати'}</small>
        <div class="bar"><i style="width:${progress}%"></i></div>
        <small>Печать: ${progress}% · AMS: ${trays.length ? trays.length + ' слотов' : 'нет данных'}${low ? ' · мало пластика: ' + low : ''}</small>
        <div class="chips">${trays.length
          ? trays.map((t) => html`<span class="tag">${t.material || '—'} ${t.color || ''} · ${t.remain == null ? '—' : Math.round(num(t.remain)) + '%'}</span>`).join('')
          : raw('<span class="muted">Слоты AMS пока не переданы</span>')}</div>
      </article>`;
  }).join(''));
}

/* ========================================================== план/факт */
function renderProduction() {
  const stats = (production && production.planfact) || {};
  const fact = $('ops10_planfact');
  const list = $('ops10_production_list');
  if (!fact || !list) return;
  render(fact, html`
    <span class="tag">Завершено: ${num(stats.total)}</span>
    <span class="tag">Факт: ${Math.round(num(stats.fact_minutes))} мин</span>
    <span class="tag">Пластик: ${fmt.grams(stats.fact_grams)}</span>
    <span class="tag ${num(stats.failed) ? 'warn' : ''}">Брак/сбой: ${num(stats.failed)}</span>`);
  const jobs = ((production && production.queue) || []).slice(0, 8);
  render(list, jobs.length
    ? jobs.map((job) => html`
      <div class="ops10-production-row">
        <span><b>${job.name || 'Задание'}</b><small class="muted"> · ${job.state || 'queued'} · ${job.printer_id || 'парк'}</small></span>
        <span>${num(job.est_minutes) ? Math.round(num(job.est_minutes)) + ' мин' : 'оценка —'}</span>
      </div>`).join('')
    : emptyHtml('Очередь свободна', 'Задания появятся здесь сразу после добавления в очередь.'));
}

/* ======================================================== правила (DSL) */
function renderRules(rules, runs) {
  const host = $('ops10_rules');
  const log = $('ops10_runs');
  if (!host || !log) return;
  render(host, (rules || []).length
    ? rules.map((rule) => html`
      <div class="ops10-rule">
        <div><b>${rule.name}</b><small>${rule.event} → ${rule.action} · ${rule.enabled ? 'включено' : 'выключено'}</small></div>
        <div class="ops10-actions">
          <button class="btn sm" data-rule-dry="${rule.id}" type="button">Проверить</button>
          <label class="switch"><input type="checkbox" data-rule-enabled="${rule.id}"${num(rule.enabled) ? raw(' checked') : raw('')}><i></i></label>
        </div>
      </div>`).join('')
    : emptyHtml('Правил нет', 'Добавьте их в Настройках → Автоматизация.'));

  host.querySelectorAll('[data-rule-enabled]').forEach((box) => {
    box.addEventListener('change', async () => {
      try {
        await post('/api/rules/toggle', { id: box.dataset.ruleEnabled, enabled: box.checked });
        await load();
      } catch (e) { fail(e); }
    });
  });
  host.querySelectorAll('[data-rule-dry]').forEach((button) => {
    button.addEventListener('click', async () => {
      try {
        const data = await post('/api/rules/simulate', {
          rule_id: button.dataset.ruleDry,
          context: { name: 'тестовый объект', detail: 'dry-run из Центра смены',
            number: '1001', product: 'тест', status: 'ready', days: 14, grams: 50, pct: 10 },
        });
        const hit = (data.items || []).find((item) => item.matched);
        toast('Dry-run выполнен',
          hit ? (hit.preview || 'Действие будет выполнено') : 'Условия не совпали',
          hit ? 'info' : 'warn');
        await load();
      } catch (e) { fail(e); }
    });
  });

  render(log, (runs || []).length
    ? runs.slice(0, 12).map((row) => html`
      <div class="ops10-run">
        <b>${row.mode === 'dry_run' ? 'Dry-run' : 'Факт'}</b> · ${row.action} ·
        ${row.matched ? 'условие совпало' : 'условие не совпало'}<br>
        <span class="muted">${fmt.stamp(row.at)} · ${row.preview || 'без текста'}</span>
      </div>`).join('')
    : emptyHtml('Проверок пока нет', 'Нажмите «Проверить» у правила — сценарий пройдёт без изменений в цехе.'));
}

/* ============================================================= загрузка */
async function load() {
  try {
    const [head, body] = await Promise.all([
      get('/api/ops10/overview'),
      get('/api/ops10/production'),
    ]);
    overview = head || {};
    production = body || {};
    renderPipeline(overview.pipeline);
    renderPrinters(overview.printers || []);
    renderInbox(overview.inbox || []);
    renderRules(overview.rules || [], overview.rule_runs || []);
    renderProduction();
  } catch (e) {
    const host = $('ops10_inbox');
    if (host) {
      render(host, html`<div class="notice bad"><span>✕</span><span>${e && e.message ? e.message : 'Не удалось загрузить данные'}</span></div>`);
    }
  }
}

async function simulateQueue() {
  try {
    const data = await post('/api/ops10/queue/simulate', { job_ids: [] });
    const minutes = num(data.total_minutes);
    toast('Симуляция очереди',
      minutes ? `Суммарная оценка: ${Math.round(minutes)} мин. Запуск не выполнен.` : 'Очередь пуста',
      'info');
  } catch (e) { fail(e); }
}

function bind() {
  const refresh = $('ops10_refresh');
  if (refresh) refresh.addEventListener('click', () => { load(); });
  const simulate = $('ops10_simulate_queue');
  if (simulate) simulate.addEventListener('click', simulateQueue);
  // Живые данные: обновляем только когда раздел действительно открыт (45),
  // и не чаще раза в 4 секунды — телеметрия приходит пачками.
  PF.on('live', debounce(() => { if (PF.viewOn('ops10')) load(); }, 4000));
}

PF.module('ops10', () => {
  bind();
  if (PF.viewOn('ops10') || location.hash === '#ops10') load();
});
PF.on('view', (detail) => { if (detail && detail.view === 'ops10') load(); });
})();
