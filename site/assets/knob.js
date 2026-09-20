/* Регулятор скорости (18.11): поворотная ручка с четырьмя фиксированными
 * делениями Bambu Lab — Тихо 50 % · Стандарт 100 % · Спорт 124 % ·
 * Ludicrous 166 %. window.PFKnob.
 *
 * Почему ручка, а не четыре кнопки: у станка оператор крутит колесо мыши
 * или тянет пальцем на планшете, не целясь в мелкие кнопки; деление
 * «щёлкает» (звук tick), а стрелка показывает, какой режим сейчас
 * подтверждён телеметрией (стрелка) и какой запрошен (подсветка). Пока
 * станок не подтвердил — ручка мигает «ожидание».
 *
 * Управление: перетаскивание по кругу, колесо мыши (±1 деление, применение
 * через 400 мс), клик по делению, клавиши ← → Home End (ручка фокусируема).
 */
(function () {
  'use strict';
  const LEVELS = [
    { level: 1, key: 'silent', label: 'Тихо', pct: 50, hint: 'Тихий режим — 50 % скорости, меньше шума ночью' },
    { level: 2, key: 'standard', label: 'Стандарт', pct: 100, hint: 'Стандарт — 100 %, профиль слайсера как есть' },
    { level: 3, key: 'sport', label: 'Спорт', pct: 124, hint: 'Спорт — 124 %, быстрее, чуть хуже поверхность' },
    { level: 4, key: 'ludicrous', label: 'Ludicrous', pct: 166, hint: 'Ludicrous — 166 %, только для черновых деталей' },
  ];
  const A0 = -135, A1 = 135;            // рабочий сектор ручки, градусы
  const NS = 'http://www.w3.org/2000/svg';

  function angleOf(level) {
    const i = Math.max(0, Math.min(LEVELS.length - 1, level - 1));
    return A0 + (A1 - A0) * i / (LEVELS.length - 1);
  }
  function levelOfAngle(deg) {
    const t = (deg - A0) / (A1 - A0);
    return Math.max(1, Math.min(LEVELS.length, Math.round(t * (LEVELS.length - 1)) + 1));
  }
  function polar(cx, cy, r, deg) {
    const a = (deg - 90) * Math.PI / 180;
    return [cx + r * Math.cos(a), cy + r * Math.sin(a)];
  }
  function arc(cx, cy, r, from, to) {
    const [x0, y0] = polar(cx, cy, r, from);
    const [x1, y1] = polar(cx, cy, r, to);
    const large = Math.abs(to - from) > 180 ? 1 : 0;
    return `M ${x0.toFixed(2)} ${y0.toFixed(2)} A ${r} ${r} 0 ${large} 1 ${x1.toFixed(2)} ${y1.toFixed(2)}`;
  }
  function el(tag, attrs, parent) {
    const node = document.createElementNS(NS, tag);
    Object.keys(attrs || {}).forEach((k) => node.setAttribute(k, attrs[k]));
    if (parent) parent.appendChild(node);
    return node;
  }
  function sound(name) { if (window.PFSound) { try { window.PFSound.play(name); } catch (e) { /* без звука */ } } }

  function create(host, opts) {
    opts = opts || {};
    if (!host) return null;
    if (host._knob) { host._knob.set(opts.value || 2, { silent: true }); return host._knob; }
    const size = Number(opts.size) || 92;
    const cx = 50, cy = 50, R = 38;
    host.classList.add('pf-knob');
    host.innerHTML = '';
    host.setAttribute('role', 'slider');
    host.setAttribute('tabindex', '0');
    host.setAttribute('aria-valuemin', '1');
    host.setAttribute('aria-valuemax', String(LEVELS.length));
    host.setAttribute('aria-label', 'Скорость печати');

    const svg = el('svg', { viewBox: '0 0 100 100', width: size, height: size, class: 'pf-knob-svg' }, host);
    el('path', { d: arc(cx, cy, R, A0, A1), class: 'pf-knob-track' }, svg);
    const fill = el('path', { d: arc(cx, cy, R, A0, A0 + 0.01), class: 'pf-knob-fill' }, svg);
    // деления
    LEVELS.forEach((lv) => {
      const a = angleOf(lv.level);
      const [x0, y0] = polar(cx, cy, R + 5, a);
      const [x1, y1] = polar(cx, cy, R - 4, a);
      el('line', { x1: x0.toFixed(2), y1: y0.toFixed(2), x2: x1.toFixed(2), y2: y1.toFixed(2), class: 'pf-knob-detent', 'data-level': lv.level }, svg);
    });
    const body = el('circle', { cx, cy, r: 24, class: 'pf-knob-body' }, svg);
    const needle = el('g', { class: 'pf-knob-needle' }, svg);
    el('line', { x1: cx, y1: cy - 8, x2: cx, y2: cy - 21, class: 'pf-knob-needle-line' }, needle);
    el('circle', { cx, cy: cy - 21, r: 2.6, class: 'pf-knob-needle-dot' }, needle);
    const pctText = el('text', { x: cx, y: cy + 4, class: 'pf-knob-pct', 'text-anchor': 'middle' }, svg);
    pctText.textContent = '100%';

    const caption = document.createElement('div');
    caption.className = 'pf-knob-caption';
    caption.innerHTML = '<b data-knob-label></b><small data-knob-state></small>';
    host.appendChild(caption);

    const labels = document.createElement('div');
    labels.className = 'pf-knob-labels';
    labels.innerHTML = LEVELS.map((lv) => `<button type="button" class="pf-knob-lbl" data-knob-level="${lv.level}" title="${lv.hint}">${lv.label}<small>${lv.pct}%</small></button>`).join('');
    host.appendChild(labels);

    const state = { value: Number(opts.value) || 2, shown: Number(opts.value) || 2, pending: 0, dragging: false, wheelTimer: 0, pendingSince: 0 };
    const labelEl = caption.querySelector('[data-knob-label]');
    const stateEl = caption.querySelector('[data-knob-state]');

    function paint() {
      const lv = LEVELS[state.shown - 1] || LEVELS[1];
      const a = angleOf(state.shown);
      needle.setAttribute('transform', `rotate(${a} ${cx} ${cy})`);
      fill.setAttribute('d', arc(cx, cy, R, A0, Math.max(A0 + 0.01, a)));
      pctText.textContent = lv.pct + '%';
      labelEl.textContent = lv.label;
      host.setAttribute('aria-valuenow', String(state.shown));
      host.setAttribute('aria-valuetext', `${lv.label}, ${lv.pct} процентов`);
      host.classList.toggle('pending', !!state.pending);
      host.classList.toggle('ludicrous', state.shown === 4);
      host.title = lv.hint + '. Колесо мыши, стрелки ← → или перетащите стрелку';
      svg.querySelectorAll('.pf-knob-detent').forEach((d) => d.classList.toggle('on', Number(d.dataset.level) <= state.shown));
      labels.querySelectorAll('.pf-knob-lbl').forEach((b) => b.classList.toggle('on', Number(b.dataset.knobLevel) === state.shown));
      stateEl.textContent = state.pending ? 'ждём подтверждения станка…' : (opts.stateText || 'подтверждено станком');
    }
    function show(level, withTick) {
      level = Math.max(1, Math.min(LEVELS.length, Number(level) || 2));
      if (level === state.shown) return;
      state.shown = level;
      if (withTick) sound('tick');
      paint();
    }
    function apply(level) {
      level = Math.max(1, Math.min(LEVELS.length, Number(level) || 2));
      if (level === state.value && !state.pending) { show(level); return; }
      state.pending = level;
      state.pendingSince = Date.now();
      show(level);
      paint();
      if (typeof opts.onChange === 'function') {
        try { opts.onChange(level, LEVELS[level - 1]); } catch (e) { state.pending = 0; paint(); }
      }
    }
    /* Телеметрия подтвердила уровень: снимаем ожидание. Пока идёт
     * перетаскивание или ожидание длится меньше 12 с — стрелку не дёргаем. */
    function set(level, o) {
      level = Math.max(1, Math.min(LEVELS.length, Number(level) || 2));
      state.value = level;
      if (state.dragging) return;
      if (state.pending) {
        if (state.pending === level || Date.now() - state.pendingSince > 12000) state.pending = 0;
        else return;
      }
      show(level, false);
      paint();
    }
    function queueWheel(delta) {
      const next = Math.max(1, Math.min(LEVELS.length, state.shown + delta));
      show(next, true);
      clearTimeout(state.wheelTimer);
      state.wheelTimer = setTimeout(() => apply(state.shown), 420);
    }

    // --- перетаскивание по кругу
    function angleFromEvent(e) {
      const r = svg.getBoundingClientRect();
      const x = e.clientX - (r.left + r.width / 2);
      const y = e.clientY - (r.top + r.height / 2);
      let deg = Math.atan2(x, -y) * 180 / Math.PI;      // 0° вверх, по часовой
      if (deg > 180) deg -= 360;
      return Math.max(A0, Math.min(A1, deg));
    }
    svg.addEventListener('pointerdown', (e) => {
      if (e.button !== 0 && e.pointerType === 'mouse') return;
      state.dragging = true;
      host.classList.add('dragging');
      try { svg.setPointerCapture(e.pointerId); } catch (err) { /* старый браузер */ }
      show(levelOfAngle(angleFromEvent(e)), true);
      e.preventDefault();
    });
    svg.addEventListener('pointermove', (e) => {
      if (!state.dragging) return;
      show(levelOfAngle(angleFromEvent(e)), true);
    });
    function endDrag(e) {
      if (!state.dragging) return;
      state.dragging = false;
      host.classList.remove('dragging');
      try { svg.releasePointerCapture(e.pointerId); } catch (err) { /* уже отпущен */ }
      apply(state.shown);
    }
    svg.addEventListener('pointerup', endDrag);
    svg.addEventListener('pointercancel', endDrag);
    host.addEventListener('wheel', (e) => {
      e.preventDefault();
      queueWheel(e.deltaY < 0 || e.deltaX < 0 ? 1 : -1);
    }, { passive: false });
    host.addEventListener('keydown', (e) => {
      const map = { ArrowRight: 1, ArrowUp: 1, ArrowLeft: -1, ArrowDown: -1 };
      if (map[e.key]) { e.preventDefault(); queueWheel(map[e.key]); }
      else if (e.key === 'Home') { e.preventDefault(); show(1, true); apply(1); }
      else if (e.key === 'End') { e.preventDefault(); show(LEVELS.length, true); apply(LEVELS.length); }
      else if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); apply(state.shown); }
    });
    labels.addEventListener('click', (e) => {
      const b = e.target.closest('[data-knob-level]');
      if (!b) return;
      e.preventDefault();
      show(Number(b.dataset.knobLevel), true);
      apply(Number(b.dataset.knobLevel));
    });

    paint();
    const api = { el: host, set, apply, value: () => state.value, shown: () => state.shown, pending: () => state.pending, LEVELS,
      fail() { state.pending = 0; show(state.value); paint(); } };
    host._knob = api;
    return api;
  }

  window.PFKnob = { create, LEVELS, angleOf, levelOfAngle };
})();
