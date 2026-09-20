/* Тракт AMS (18.11): стойка катушек и путь филамента до сопла одной картинкой.
 * window.PFAmsPath — чистый рендер по снимку ``p.ams`` из телеметрии.
 *
 * Что показывает: каждую катушку в своём слоте (цвет, материал, остаток
 * кольцом), внешнюю катушку отдельно, трубки от слотов в общий хаб и от
 * хаба к экструдеру. Активная трубка «течёт» (анимированный штрих) — видно,
 * из какого слота станок печатает прямо сейчас. Пустой слот — пунктир.
 * Влажность и температура блока — под картинкой: сушить или нет.
 *
 * Перерисовка только при смене подписи (signature) — анимация не сбрасывается
 * каждым кадром телеметрии.
 */
(function () {
  'use strict';
  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, (m) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[m])); }
  function num(v, d) { const n = Number(v); return Number.isFinite(n) ? n : (d === undefined ? 0 : d); }

  function unitsOf(ams) {
    const trays = (ams && Array.isArray(ams.trays)) ? ams.trays : [];
    const byUnit = new Map();
    let external = null;
    trays.forEach((t) => {
      if (String(t.id) === '254' || Number(t.unit) === 255) { external = t; return; }
      const u = num(t.unit, 0);
      if (!byUnit.has(u)) byUnit.set(u, []);
      byUnit.get(u).push(t);
    });
    const declared = Math.max(num(ams && ams.units, 0), byUnit.size);
    const units = [];
    for (let u = 0; u < declared; u++) {
      const list = (byUnit.get(u) || []).slice().sort((a, b) => num(a.slot) - num(b.slot));
      const slots = [];
      for (let s = 0; s < 4; s++) slots.push(list.find((t) => num(t.slot) === s) || { id: `${u}${s}`, unit: u, slot: s, present: false, label: `AMS ${u + 1} · слот ${s + 1}` });
      units.push({ unit: u, slots });
    }
    return { units, external };
  }
  function signature(ams) {
    const { units, external } = unitsOf(ams);
    const part = (t) => [t.id, t.present ? 1 : 0, t.active ? 1 : 0, t.color || '', t.type || '', t.remain == null ? '' : Math.round(num(t.remain))].join(':');
    return [units.map((u) => u.slots.map(part).join(',')).join('|'), external ? part(external) : '-', ams && ams.humidity, ams && ams.temperature].join('#');
  }
  function activeTray(ams) {
    const trays = (ams && ams.trays) || [];
    return trays.find((t) => t.active) || null;
  }
  function humidityText(h) {
    const v = num(h, NaN);
    if (!Number.isFinite(v)) return '';
    // AMS отдаёт индекс 1–5 (1 — сухо), иногда проценты.
    if (v <= 5) return ['', 'сухо', 'нормально', 'влажновато', 'влажно', 'очень влажно'][Math.round(v)] || '';
    return `${Math.round(v)} % RH`;
  }
  function humidityKind(h) {
    const v = num(h, NaN);
    if (!Number.isFinite(v)) return '';
    if (v <= 5) return v <= 2 ? 'ok' : (v === 3 ? 'warn' : 'bad');
    return v < 40 ? 'ok' : (v < 60 ? 'warn' : 'bad');
  }

  /* Одна катушка: круг цвета + кольцо остатка + метка материала. */
  function spool(t, cx, cy, r) {
    const present = t.present !== false && (t.present || t.type || t.uuid);
    const color = t.color || '#94a3b8';
    const remain = t.remain == null ? null : Math.max(0, Math.min(100, num(t.remain)));
    const L = 2 * Math.PI * (r + 4);
    const title = `${t.label || ('Слот ' + (num(t.slot) + 1))}: ${present ? (t.type || 'пластик без RFID') : 'пусто'}${remain != null ? ` · остаток ${Math.round(remain)} %` : ''}${t.active ? ' · печатает сейчас' : ''}`;
    const cls = ['ams-spool', present ? 'present' : 'empty', t.active ? 'active' : '', t.generic ? 'generic' : ''].filter(Boolean).join(' ');
    return `<g class="${cls}" data-tray="${esc(t.id)}"><title>${esc(title)}</title>` +
      (remain != null && present
        ? `<circle class="ams-remain-track" cx="${cx}" cy="${cy}" r="${r + 4}"/>` +
          `<circle class="ams-remain" cx="${cx}" cy="${cy}" r="${r + 4}" stroke-dasharray="${(L * remain / 100).toFixed(1)} ${L.toFixed(1)}" transform="rotate(-90 ${cx} ${cy})"/>`
        : '') +
      `<circle class="ams-spool-body" cx="${cx}" cy="${cy}" r="${r}" ${present ? `fill="${esc(color)}"` : ''}/>` +
      `<circle class="ams-spool-hole" cx="${cx}" cy="${cy}" r="${(r * 0.32).toFixed(1)}"/>` +
      `<text class="ams-spool-type" x="${cx}" y="${cy + r + 15}" text-anchor="middle">${esc(present ? ((t.type || 'без RFID').slice(0, 7)) : '—')}</text>` +
      '</g>';
  }

  function render(ams, opts) {
    opts = opts || {};
    const { units, external } = unitsOf(ams);
    const act = activeTray(ams);
    const hasAny = units.length || external;
    if (!hasAny) {
      return '<div class="ams-path-empty muted">AMS не подключён — печать с внешней катушки. Слоты появятся, как только станок пришлёт отчёт.</div>';
    }
    const UW = 150, GAP = 14, EXT_W = external ? 56 : 0, NOZ_W = 64, H = 112;
    const W = EXT_W + units.length * (UW + GAP) + NOZ_W;
    const hubY = 20, spoolY = 62, r = 13;
    const nozX = W - 30;
    let svg = `<svg class="ams-path-svg" viewBox="0 0 ${W} ${H}" role="img" aria-label="Тракт AMS">`;
    // общий хаб к соплу
    const hubStart = EXT_W + 8;
    svg += `<path class="ams-tube trunk${act && !(external && act.id === external.id) ? ' flow' : ''}" d="M ${hubStart} ${hubY} H ${nozX - 14} Q ${nozX} ${hubY} ${nozX} ${hubY + 14} V ${spoolY - 12}"/>`;
    if (external) {
      const ex = 26;
      svg += `<path class="ams-tube ext${external.active ? ' flow' : ''}" d="M ${ex} ${spoolY - r - 4} V ${hubY + 14} Q ${ex} ${hubY} ${ex + 14} ${hubY} H ${hubStart}"/>`;
      svg += spool(Object.assign({ label: 'Внешняя катушка' }, external), ex, spoolY, r);
      svg += `<text class="ams-unit-title" x="${ex}" y="${H - 4}" text-anchor="middle">внешняя</text>`;
    }
    units.forEach((u, i) => {
      const x0 = EXT_W + i * (UW + GAP);
      svg += `<rect class="ams-unit-box" x="${x0 + 2}" y="${hubY + 8}" width="${UW - 4}" height="${H - hubY - 18}" rx="9"/>`;
      svg += `<text class="ams-unit-title" x="${x0 + UW / 2}" y="${H - 4}" text-anchor="middle">AMS ${u.unit + 1}</text>`;
      u.slots.forEach((t, s) => {
        const cx = x0 + 24 + s * 34;
        const present = t.present !== false && (t.present || t.type || t.uuid);
        svg += `<path class="ams-tube slot${t.active ? ' flow' : ''}${present ? '' : ' empty'}" d="M ${cx} ${spoolY - r - 4} V ${hubY + 12} Q ${cx} ${hubY} ${cx + 10} ${hubY}"/>`;
        svg += spool(t, cx, spoolY, r);
      });
    });
    // экструдер
    svg += `<g class="ams-nozzle${act ? ' hot' : ''}"><title>Экструдер${act ? ': ' + esc(act.label || '') : ''}</title>` +
      `<rect x="${nozX - 12}" y="${spoolY - 12}" width="24" height="22" rx="4" class="ams-nozzle-body"/>` +
      `<path d="M ${nozX - 7} ${spoolY + 10} L ${nozX - 3} ${spoolY + 20} H ${nozX + 3} L ${nozX + 7} ${spoolY + 10} Z" class="ams-nozzle-tip"/>` +
      (act ? `<circle class="ams-nozzle-drop" cx="${nozX}" cy="${spoolY + 25}" r="2.4" fill="${esc(act.color || '#f97316')}"/>` : '') +
      `<text class="ams-unit-title" x="${nozX}" y="${H - 4}" text-anchor="middle">сопло</text></g>`;
    svg += '</svg>';

    const hum = humidityText(ams && ams.humidity);
    const humKind = humidityKind(ams && ams.humidity);
    const temp = num(ams && ams.temperature, NaN);
    const chips = [];
    if (act) chips.push(`<span class="ams-chip act"><i style="background:${esc(act.color || '#94a3b8')}"></i>Сейчас: <b>${esc(act.label || 'слот')}</b> · ${esc(act.type || 'пластик')}${act.remain != null ? ` · остаток ${Math.round(num(act.remain))} %` : ''}</span>`);
    else chips.push('<span class="ams-chip">Филамент не заправлен в экструдер</span>');
    if (hum) chips.push(`<span class="ams-chip ${humKind}" title="Влажность внутри AMS: сушите катушки при «влажно»">💧 ${esc(hum)}</span>`);
    if (Number.isFinite(temp)) chips.push(`<span class="ams-chip" title="Температура внутри AMS">🌡 ${Math.round(temp)}°C</span>`);
    const low = ((ams && ams.trays) || []).filter((t) => t.present !== false && t.remain != null && num(t.remain) <= 15);
    if (low.length) chips.push(`<span class="ams-chip warn" title="${esc(low.map((t) => t.label).join(', '))}">⚠ Заканчивается: ${low.length}</span>`);
    return `<div class="ams-path${opts.compact ? ' compact' : ''}">${svg}<div class="ams-path-chips">${chips.join('')}</div></div>`;
  }

  /* Перерисовать контейнер только если картинка изменилась. */
  function mount(host, ams, opts) {
    if (!host) return false;
    const sig = signature(ams);
    if (host.dataset.amsSig === sig && host.firstChild) return false;
    host.dataset.amsSig = sig;
    host.innerHTML = render(ams, opts);
    return true;
  }

  window.PFAmsPath = { render, mount, signature, unitsOf, activeTray, humidityText };
})();
