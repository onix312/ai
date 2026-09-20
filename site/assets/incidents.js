/* Шторка инцидентов (18.11): все тревоги фермы в одном месте, с действием
 * под рукой. window.PFIncidents.
 *
 * Источники: живые проблемы станков из телеметрии (HMS-коды, «нет связи»,
 * пауза) и журнал событий (ошибки, защита, брак, мало филамента,
 * обслуживание). Каждая строка знает, куда вести: «Станок», «Очередь»,
 * «Склад пластика», «Заказы», «Настройки» — и, если станок печатает с
 * ошибкой, даёт «Пауза» прямо из шторки.
 *
 * Непрочитанное считается по id событий (localStorage, на устройство):
 * колокольчик в шапке показывает число тревог и предупреждений, которых
 * оператор ещё не видел, плюс активные проблемы станков.
 */
(function () {
  'use strict';
  const SEEN_KEY = 'pf_incidents_seen';
  const FILTER_KEY = 'pf_incidents_filter';
  const BAD_KINDS = new Set(['error', 'guard', 'defect', 'loss', 'security']);
  const WARN_KINDS = new Set(['filament_low', 'maintenance', 'pause', 'dry']);
  const PRINTER_KINDS = new Set(['complete', 'error', 'guard', 'pause', 'print_start', 'defect', 'command', 'ams', 'printer', 'farmloop', 'job']);
  const BAD_WORDS = /ошибк|сбой|не удалось|обрыв|потер|авари|брак|стоп|прерв|fatal|error/i;
  const WARN_WORDS = /предупр|вниман|мало|заканчива|просроч|истек|повтор|пауз|ждёт|ожида|warn/i;
  const ROUTES = [
    [/^(filament_low|spool|mat|dry|stock|inventory)$/, 'inventory', 'Склад пластика'],
    [/^(queue|upload|watch|slicer|studio|job|print_start)$/, 'queue', 'Очередь'],
    [/^(order|orders|lead|crm|customer|bot)$/, 'orders', 'Заказы'],
    [/^(finance|money)$/, 'finance', 'Финансы'],
    [/^(shelf)$/, 'shelf', 'Стеллаж'],
    [/^(batch|production)$/, 'batches', 'Партии'],
    [/^(farmloop|rule)$/, 'conveyor', 'Конвейер'],
    [/^(system|update|backup|settings|security|cloud|import|doc)$/, 'settings', 'Настройки'],
  ];
  const SEV_LABEL = { bad: 'Тревога', warn: 'Внимание', info: 'Инфо' };

  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, (m) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[m])); }
  const ui = () => (window.PF && PF.ui) || {};
  function ago(iso) { const f = ui().agoText; return f ? f(iso) : String(iso || ''); }
  function printerName(id) { const p = window.PF && PF.printer && PF.printer(id); return p ? p.name : ''; }

  function severityOf(e) {
    const kind = String(e.kind || '');
    if (BAD_KINDS.has(kind)) return 'bad';
    if (WARN_KINDS.has(kind)) return 'warn';
    const text = `${e.title || ''} ${e.detail || ''}`;
    if (BAD_WORDS.test(text) && !/без ошибок|нет ошибок/i.test(text)) return kind === 'complete' ? 'info' : 'bad';
    if (WARN_WORDS.test(text)) return 'warn';
    return 'info';
  }
  function routeOf(e) {
    if (e.printer_id || PRINTER_KINDS.has(String(e.kind || ''))) return ['printers', 'Станок'];
    for (const [re, view, label] of ROUTES) if (re.test(String(e.kind || ''))) return [view, label];
    return ['dashboard', 'Обзор'];
  }

  /* Живые проблемы станков — из последнего кадра телеметрии. */
  function liveItems() {
    const live = window.PF && PF.state && PF.state.live;
    const out = [];
    ((live && live.printers) || []).forEach((p) => {
      const info = p.printer || {};
      const conn = p.connection || {};
      if (conn.configured && !conn.connected && p.enabled !== false) {
        out.push({ id: `live-conn-${p.id}`, live: true, severity: 'warn', kind: 'link', title: `Нет связи: ${p.name}`,
          detail: conn.last_error || 'Станок не отвечает по MQTT', printer_id: p.id, at: '' });
      }
      (info.problems || []).forEach((pr) => {
        const sev = pr.severity === 'fatal' || pr.severity === 'error' ? 'bad' : (pr.severity === 'warn' ? 'warn' : 'info');
        out.push({ id: `live-hms-${p.id}-${pr.code}`, live: true, severity: sev, kind: 'hms', title: `${p.name}: ${pr.title || pr.code}`,
          detail: pr.advice || pr.code || '', printer_id: p.id, at: '', running: info.state === 'RUNNING' });
      });
      if (info.state === 'PAUSE') {
        out.push({ id: `live-pause-${p.id}`, live: true, severity: 'warn', kind: 'pause', title: `${p.name} на паузе`,
          detail: info.task ? `Задание «${info.task}» ждёт оператора` : 'Печать остановлена, станок ждёт', printer_id: p.id, at: '', paused: true });
      }
    });
    return out;
  }
  function eventItems() {
    const list = (window.PF && PF.state && PF.state.events) || [];
    const out = [];
    let prev = null;
    list.forEach((e) => {
      const item = { id: Number(e.id) || 0, live: false, severity: severityOf(e), kind: e.kind, title: e.title || e.kind, detail: e.detail || '', printer_id: e.printer_id || '', at: e.at || '', count: 1 };
      if (prev && prev.kind === item.kind && prev.title === item.title && prev.printer_id === item.printer_id && prev.severity === item.severity) { prev.count += 1; return; }
      out.push(item);
      prev = item;
    });
    return out;
  }

  let seen = Number(localStorage.getItem(SEEN_KEY) || 0) || 0;
  let filter = localStorage.getItem(FILTER_KEY) || 'all';
  let printersOnly = false;
  let opened = false;
  let fetched = false;

  function unread() {
    const ev = eventItems().filter((e) => e.severity !== 'info' && e.id > seen);
    const lv = liveItems().filter((e) => e.severity !== 'info');
    return { events: ev.length, live: lv.length, worst: [...ev, ...lv].some((e) => e.severity === 'bad') ? 'bad' : ([...ev, ...lv].length ? 'warn' : '') };
  }
  function syncBell() {
    const btn = document.getElementById('incidents_btn');
    if (!btn) return;
    const u = unread();
    const total = u.events + u.live;
    let badge = btn.querySelector('.inc-badge');
    if (!badge) { badge = document.createElement('i'); badge.className = 'inc-badge'; btn.appendChild(badge); }
    badge.textContent = total > 99 ? '99+' : String(total);
    badge.hidden = !total;
    btn.classList.toggle('bad', u.worst === 'bad');
    btn.classList.toggle('warn', u.worst === 'warn');
    btn.title = total ? `Инциденты: ${total} требуют внимания` : 'Инциденты: всё спокойно';
    btn.setAttribute('aria-label', btn.title);
  }

  function actionButtons(e) {
    const [view, label] = routeOf(e);
    const btns = [];
    if (e.printer_id && (e.running || e.paused === false) && e.severity === 'bad') btns.push(`<button class="btn xs" type="button" data-inc-pause="${esc(e.printer_id)}">⏸ Пауза</button>`);
    if (e.paused) btns.push(`<button class="btn xs primary" type="button" data-inc-resume="${esc(e.printer_id)}">▶ Продолжить</button>`);
    btns.push(`<button class="btn xs ghost" type="button" data-inc-go="${esc(view)}" data-inc-pid="${esc(e.printer_id || '')}">${esc(label)} →</button>`);
    return btns.join('');
  }
  function row(e) {
    const who = e.printer_id ? printerName(e.printer_id) : '';
    return `<div class="inc-row ${e.severity}${!e.live && e.id > seen ? ' new' : ''}" data-inc-id="${esc(e.id)}">` +
      `<span class="inc-dot" title="${SEV_LABEL[e.severity]}"></span>` +
      `<div class="inc-main"><div class="inc-title"><b>${esc(e.title)}</b>${e.count > 1 ? `<span class="inc-count">×${e.count}</span>` : ''}${e.live ? '<span class="inc-live">сейчас</span>' : ''}</div>` +
      `${e.detail ? `<div class="inc-detail">${esc(e.detail)}</div>` : ''}` +
      `<div class="inc-meta">${who && !String(e.title).includes(who) ? `<span>${esc(who)}</span>` : ''}${e.at ? `<span>${esc(ago(e.at))}</span>` : ''}<span class="inc-kind">${esc(e.kind || '')}</span></div>` +
      `<div class="inc-actions">${actionButtons(e)}</div></div></div>`;
  }
  function render() {
    const box = document.getElementById('incidents_list');
    if (!box) return;
    let items = [...liveItems(), ...eventItems()];
    if (filter !== 'all') items = items.filter((e) => e.severity === filter);
    if (printersOnly) items = items.filter((e) => e.printer_id);
    const counts = { all: 0, bad: 0, warn: 0, info: 0 };
    [...liveItems(), ...eventItems()].forEach((e) => { counts.all++; counts[e.severity]++; });
    document.querySelectorAll('[data-inc-filter]').forEach((b) => {
      const k = b.dataset.incFilter;
      b.classList.toggle('on', k === filter);
      const n = b.querySelector('small');
      if (n) n.textContent = counts[k] ? String(counts[k]) : '';
    });
    const only = document.getElementById('incidents_printers_only');
    if (only) only.checked = printersOnly;
    if (!items.length) {
      box.innerHTML = `<div class="inc-empty"><span class="ic">✓</span><b>${filter === 'all' ? 'Тревог нет — цех работает штатно' : 'В этой группе пусто'}</b><small>Сюда попадают ошибки станков, защита печати, брак, мало филамента и паузы. Действие — сразу в строке.</small></div>`;
      return;
    }
    box.innerHTML = items.map(row).join('');
  }
  function markAllRead() {
    const ids = eventItems().map((e) => e.id);
    seen = Math.max(seen, ...(ids.length ? ids : [0]));
    try { localStorage.setItem(SEEN_KEY, String(seen)); } catch (e) { /* приватный режим */ }
    syncBell();
    render();
  }
  function open() {
    const d = document.getElementById('incidents_drawer');
    if (!d) return;
    d.classList.add('open');
    d.setAttribute('aria-hidden', 'false');
    document.body.classList.add('inc-open');
    opened = true;
    if (!fetched && window.PF && PF.api && PF.api.get) {
      fetched = true;
      PF.api.get('/api/events', { limit: 150 }).then((data) => {
        if (data && Array.isArray(data.events) && data.events.length > ((PF.state.events || []).length)) { PF.state.events = data.events; render(); syncBell(); }
      }).catch(() => { fetched = false; });
    }
    render();
    const first = d.querySelector('[data-inc-filter]');
    if (first) first.focus();
  }
  function close() {
    const d = document.getElementById('incidents_drawer');
    if (!d) return;
    d.classList.remove('open');
    d.setAttribute('aria-hidden', 'true');
    document.body.classList.remove('inc-open');
    opened = false;
    // Закрыли шторку — всё, что в ней было, считается увиденным.
    markAllRead();
  }
  function toggle() { return opened ? close() : open(); }

  async function command(printerId, cmd) {
    if (!window.PF || !PF.api) return;
    const ask = cmd === 'pause' ? 'Поставить печать на паузу?' : 'Продолжить печать?';
    const confirmDanger = ui().confirmDanger || window.confirm;
    if (!confirmDanger(ask)) return;
    try {
      await PF.api.post('/api/printer/command', { printer_id: printerId, command: cmd, confirmed: true });
      if (ui().toast) ui().toast('Команда отправлена', cmd === 'pause' ? 'Пауза' : 'Продолжить');
      if (window.PFSound) PFSound.play('click');
      if (typeof PF.poll === 'function') setTimeout(PF.poll, 500);
    } catch (err) { if (ui().fail) ui().fail(err); }
  }

  document.addEventListener('click', (e) => {
    const t = e.target;
    if (!t || !t.closest) return;
    if (t.closest('#incidents_btn')) { e.preventDefault(); toggle(); return; }
    if (t.closest('[data-inc-close]')) { e.preventDefault(); close(); return; }
    if (t.closest('[data-inc-readall]')) { e.preventDefault(); markAllRead(); return; }
    const f = t.closest('[data-inc-filter]');
    if (f) { filter = f.dataset.incFilter; try { localStorage.setItem(FILTER_KEY, filter); } catch (err) { /* ок */ } render(); return; }
    const go = t.closest('[data-inc-go]');
    if (go) {
      e.preventDefault();
      if (go.dataset.incPid && window.PF) PF.state.activePrinter = go.dataset.incPid;
      close();
      if (window.PF && typeof PF.go === 'function') PF.go(go.dataset.incGo);
      return;
    }
    const pause = t.closest('[data-inc-pause]');
    if (pause) { e.preventDefault(); command(pause.dataset.incPause, 'pause'); return; }
    const resume = t.closest('[data-inc-resume]');
    if (resume) { e.preventDefault(); command(resume.dataset.incResume, 'resume'); return; }
    // клик по подложке закрывает
    if (opened && t.id === 'incidents_drawer') close();
  });
  document.addEventListener('change', (e) => {
    if (e.target && e.target.id === 'incidents_printers_only') { printersOnly = !!e.target.checked; render(); }
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && opened) { close(); return; }
    if (e.key.toLowerCase() === 'i' && e.altKey) { e.preventDefault(); toggle(); }
  });

  function mount() {
    syncBell();
    if (window.PF && typeof PF.on === 'function') {
      PF.on('events', () => { syncBell(); if (opened) render(); });
      PF.on('live', () => { syncBell(); if (opened) render(); });
      PF.on('notify', (row) => {
        // Новое тревожное событие — короткий тост с кнопкой в шторку, даже если она закрыта.
        if (!row || opened) return;
        const sev = severityOf(row);
        if (sev === 'info' || !ui().toast) return;
        ui().toast(row.title || 'Инцидент', row.detail || '', sev === 'bad' ? 'bad' : 'warn', { label: 'Инциденты', run: open });
      });
    }
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', mount);
  else mount();

  window.PFIncidents = { open, close, toggle, render, markAllRead, severityOf, routeOf, liveItems, eventItems, unread };
})();
