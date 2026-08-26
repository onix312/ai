/* PrintFlow 10.0: единый центр смены, inbox и безопасная песочница правил. */
(() => {
  'use strict';
  const $ = (id) => document.getElementById(id);
  const esc10 = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const stages = {new:'Новое', qualifying:'Уточнить', quoted:'Расчёт', awaiting:'Ждём согласование', order:'Заказ', won:'Успешно', lost:'Потеряно'};
  const stageOptions = (current) => Object.entries(stages).map(([key, label]) => `<option value="${key}"${key === (current || 'new') ? ' selected' : ''}>${label}</option>`).join('');
  const fmtAt = (v) => v ? new Date(v).toLocaleString('ru-RU', {day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'}) : '—';

  function renderPipeline(items) {
    const host = $('ops10_pipeline'); if (!host) return;
    const counts = Object.fromEntries((items || []).map((x) => [x.stage, x.count]));
    host.innerHTML = Object.entries(stages).map(([key, label]) => `<div class="kpi"><small>${label}</small><b>${counts[key] || 0}</b><span>обращений</span></div>`).join('');
  }
  function renderInbox(items) {
    const host = $('ops10_inbox'); if (!host) return;
    if (!items.length) { host.innerHTML = '<div class="empty compact"><span>Обращений пока нет.</span></div>'; return; }
    host.innerHTML = items.map((x) => `<article class="ops10-item">
      <div class="ops10-item-head"><div><b>${esc10(x.name || x.username || ('Чат ' + x.chat_id))}</b><small class="muted"> · ${esc10(x.chat_id)} · ${fmtAt(x.at || x.last_seen)}</small></div><span class="tag ${x.unread ? 'warn' : ''}">${x.unread ? 'новое' : 'прочитано'}</span></div>
      <p>${esc10(x.text || 'Нет текста последнего сообщения')}</p>
      <div class="ops10-actions"><select data-inbox-stage="${esc10(x.chat_id)}" aria-label="Стадия обращения">${stageOptions(x.pipeline_stage)}</select><button class="btn sm" data-inbox-read="${esc10(x.chat_id)}" type="button">Прочитано</button><a class="btn sm ghost" href="#clientbot" data-view="clientbot">Открыть диалог</a></div>
    </article>`).join('');
    host.querySelectorAll('[data-inbox-stage]').forEach((select) => select.addEventListener('change', async () => {
      try { await post('/api/ops10/inbox/stage', {chat_id: select.dataset.inboxStage, stage: select.value}); toast('Стадия сохранена', stages[select.value]); load(); }
      catch (e) { fail(e); }
    }));
    host.querySelectorAll('[data-inbox-read]').forEach((button) => button.addEventListener('click', async () => {
      try { await post('/api/client-bot/read', {chat_id: button.dataset.inboxRead, actor:'panel'}); load(); }
      catch (e) { fail(e); }
    }));
  }
  function renderPrinters(printers) {
    const host = $('ops10_printers'); if (!host) return;
    if (!printers || !printers.length) { host.innerHTML = '<div class="empty compact"><span>Принтеры не подключены. Добавьте Bambu Lab в разделе «Принтеры».</span></div>'; return; }
    host.innerHTML = printers.map((p) => {
      const info = p.printer || {}, conn = p.connection || {}, ams = p.ams || {}, job = p.job || {};
      const trays = ams.trays || [], active = trays.filter((t) => t.active || t.remain != null).slice(0, 8);
      const progress = Math.max(0, Math.min(100, Number(info.progress || 0)));
      const low = active.filter((t) => Number.isFinite(Number(t.remain)) && Number(t.remain) < 15).length;
      return `<article class="ops10-printer"><div class="ops10-item-head"><b>${esc10(p.name || info.name || 'Принтер')}</b><span class="tag ${conn.connected ? 'ok' : 'bad'}">${conn.connected ? 'онлайн' : 'офлайн'}</span></div><small>${esc10(info.state_label || info.state || 'Ожидание')} · ${esc10(info.task || 'нет активной печати')}</small><div class="bar"><i style="width:${progress}%"></i></div><small>Печать: ${progress}% · AMS: ${active.length ? active.length + ' слотов' : 'нет данных'}${low ? ' · мало пластика: ' + low : ''}</small><div class="chips">${active.map((t) => `<span class="tag">${esc10(t.material || '—')} ${esc10(t.color || '')} · ${t.remain == null ? '—' : Math.round(Number(t.remain)) + '%'}</span>`).join('') || '<span class="muted">Слоты AMS пока не переданы</span>'}</div></article>`;
    }).join('');
  }
  function renderRules(rules, runs) {
    const host = $('ops10_rules'), log = $('ops10_runs'); if (!host || !log) return;
    host.innerHTML = rules.length ? rules.map((r) => `<div class="ops10-rule"><div><b>${esc10(r.name)}</b><small>${esc10(r.event)} → ${esc10(r.action)} · ${r.enabled ? 'включено' : 'выключено'}</small></div><div class="ops10-actions"><button class="btn sm" data-rule-dry="${esc10(r.id)}" type="button">Проверить</button><label class="switch"><input type="checkbox" data-rule-enabled="${esc10(r.id)}"${Number(r.enabled) ? ' checked' : ''}><i></i></label></div></div>`).join('') : '<div class="empty compact"><span>Правил нет. Добавьте их в Настройках → Автоматизация.</span></div>';
    host.querySelectorAll('[data-rule-enabled]').forEach((el) => el.addEventListener('change', async () => { try { await post('/api/rules/toggle', {id:el.dataset.ruleEnabled, enabled:el.checked}); load(); } catch (e) { fail(e); } }));
    host.querySelectorAll('[data-rule-dry]').forEach((el) => el.addEventListener('click', async () => {
      try { const data = await post('/api/rules/simulate', {rule_id:el.dataset.ruleDry, context:{name:'тестовый объект', detail:'dry-run из Центра смены', number:'1001', product:'тест', status:'ready', days:14, grams:50, pct:10}}); const hit = (data.items || []).find((x) => x.matched); toast('Dry-run выполнен', hit ? hit.preview || 'Действие будет выполнено' : 'Условия не совпали', hit ? 'info' : 'warn'); load(); }
      catch (e) { fail(e); }
    }));
    log.innerHTML = runs.length ? runs.slice(0, 12).map((r) => `<div class="ops10-run"><b>${esc10(r.mode === 'dry_run' ? 'Dry-run' : 'Факт')}</b> · ${esc10(r.action)} · ${r.matched ? 'условие совпало' : 'не совпало'}<br><span class="muted">${fmtAt(r.at)} · ${esc10(r.preview || 'без текста')}</span></div>`).join('') : '<div class="empty compact"><span>Проверок пока нет.</span></div>';
  }
  async function load() {
    try { const data = await get('/api/ops10/overview'); renderPipeline(data.pipeline); renderPrinters(data.printers || []); renderInbox(data.inbox || []); renderRules(data.rules || [], data.rule_runs || []); }
    catch (e) { const host = $('ops10_inbox'); if (host) host.innerHTML = `<div class="notice bad"><span>✕</span><span>${esc10(e.message)}</span></div>`; }
  }
  function init() {
    if (!$('view-ops10')) return;
    $('ops10_refresh')?.addEventListener('click', load);
    PF.on?.('ready', load);
    if (location.hash === '#ops10') load();
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
})();
