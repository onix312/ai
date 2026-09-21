/* Шкала слоёв (18.11): ползунок по слоям печатающегося G-code с картинкой
 * слоя. window.PFLayers.
 *
 * Данные — /api/gcode/layers (индекс: слои, высоты, пластик, M73) и
 * /api/gcode/layer?layer=N (отрезки слоя по корзинам). Пока бэкенд разбирает
 * файл, виджет показывает прогресс и опрашивает повторно.
 *
 * Поведение: режим «Следить» держит ползунок на текущем слое из телеметрии;
 * стоит потянуть ползунок — режим выключается, «● Следить» возвращает.
 * Под ползунком — полоска расхода пластика по слоям: пики видны заранее
 * (сплошные слои, мосты). Предыдущий слой рисуется бледно под текущим.
 */
(function () {
  'use strict';
  const COLORS = {
    wall_outer: '#f97316', wall_inner: '#fbbf24', infill: '#a78bfa', solid: '#38bdf8',
    support: '#22c55e', skirt: '#94a3b8', bridge: '#ec4899', other: '#64748b',
  };
  const ORDER = ['skirt', 'support', 'infill', 'solid', 'bridge', 'wall_inner', 'wall_outer', 'other'];
  const LABELS = {
    wall_outer: 'Наружная стенка', wall_inner: 'Внутренняя стенка', infill: 'Заполнение', solid: 'Сплошные слои',
    support: 'Поддержка', skirt: 'Юбка / башня', bridge: 'Мосты и свесы', other: 'Прочее',
  };
  const HELP = 'Ползунок листает слои файла, который печатается: картинка показывает, что кладёт сопло на выбранном слое (цвета — корзины ниже), бледно — предыдущий слой. Полоска под ползунком — расход пластика по слоям: пики означают сплошные слои и мосты. «● Следить» держит ползунок на текущем слое станка; потяните ползунок — режим выключится. Проценты и остаток времени — расчёт слайсера (M73), а не станка.';

  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, (m) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[m])); }
  function num(v, d) { const n = Number(v); return Number.isFinite(n) ? n : (d === undefined ? 0 : d); }
  function minutes(m) {
    m = Math.round(num(m));
    if (!m) return '—';
    const h = Math.floor(m / 60), mm = m % 60;
    return h ? `${h} ч ${mm ? mm + ' мин' : ''}`.trim() : `${mm} мин`;
  }
  const apiGet = (path, query) => (window.PF && PF.api && PF.api.get) ? PF.api.get(path, query) : Promise.reject(new Error('нет API'));

  /* Рисует слой на канве: paths = {bucket: [x1,y1,x2,y2,...]}. */
  function draw(canvas, detail, prev, bbox, opts) {
    if (!canvas || !canvas.getContext) return;
    const ctx = canvas.getContext('2d');
    if (!ctx) return;
    opts = opts || {};
    const dpr = window.devicePixelRatio || 1;
    const cssW = Math.max(120, canvas.clientWidth || 260);
    const cssH = Math.max(120, canvas.clientHeight || 220);
    if (canvas.width !== Math.round(cssW * dpr) || canvas.height !== Math.round(cssH * dpr)) {
      canvas.width = Math.round(cssW * dpr);
      canvas.height = Math.round(cssH * dpr);
    }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, cssW, cssH);
    let box = (bbox && bbox.length === 4) ? bbox : null;
    if (!box || !box.every((v) => Number.isFinite(Number(v)))) box = [0, 0, 256, 256];
    // защита от вырожденного bbox [0,0,0,0] во время building
    if (!(box[2] > box[0]) || !(box[3] > box[1])) box = [0, 0, 256, 256];
    const pad = 10;
    const bw = Math.max(1, box[2] - box[0]), bh = Math.max(1, box[3] - box[1]);
    const scale = Math.min((cssW - pad * 2) / bw, (cssH - pad * 2) / bh);
    const ox = (cssW - bw * scale) / 2, oy = (cssH - bh * scale) / 2;
    const X = (x) => ox + (x - box[0]) * scale;
    const Y = (y) => cssH - (oy + (y - box[1]) * scale);   // front=0 внизу кадра (совпадает с bed_projection: front-left=(0,plate_h))
    // сетка стола (шаг 50 мм в пределах рамки)
    ctx.strokeStyle = 'rgba(148,163,184,.18)';
    ctx.lineWidth = 1;
    ctx.beginPath();
    for (let g = Math.ceil(box[0] / 50) * 50; g <= box[2]; g += 50) { ctx.moveTo(X(g), Y(box[1])); ctx.lineTo(X(g), Y(box[3])); }
    for (let g = Math.ceil(box[1] / 50) * 50; g <= box[3]; g += 50) { ctx.moveTo(X(box[0]), Y(g)); ctx.lineTo(X(box[2]), Y(g)); }
    ctx.stroke();
    ctx.lineCap = 'round';
    ctx.lineJoin = 'round';
    const strokePaths = (paths, alpha, width) => {
      ORDER.forEach((key) => {
        const arr = paths && paths[key];
        if (!arr || arr.length < 4) return;
        ctx.globalAlpha = alpha;
        ctx.strokeStyle = COLORS[key] || COLORS.other;
        ctx.lineWidth = width * (key === 'wall_outer' ? 1.35 : key === 'infill' ? 0.75 : 1);
        ctx.beginPath();
        for (let i = 0; i + 3 < arr.length; i += 4) {
          ctx.moveTo(X(arr[i]), Y(arr[i + 1]));
          ctx.lineTo(X(arr[i + 2]), Y(arr[i + 3]));
        }
        ctx.stroke();
      });
      ctx.globalAlpha = 1;
    };
    const w = Math.max(0.8, Math.min(2.2, scale * 0.45));
    if (prev && prev.paths) strokePaths(prev.paths, 0.22, w);
    if (detail && detail.paths) strokePaths(detail.paths, 1, w);
  }

  /* Полоска расхода пластика по слоям + метки текущего и выбранного. */
  function drawStrip(canvas, layers, current, selected) {
    if (!canvas || !canvas.getContext) return;
    const ctx = canvas.getContext('2d');
    if (!ctx) return;
    const dpr = window.devicePixelRatio || 1;
    const cssW = Math.max(60, canvas.clientWidth || 300), cssH = Math.max(10, canvas.clientHeight || 22);
    if (canvas.width !== Math.round(cssW * dpr) || canvas.height !== Math.round(cssH * dpr)) { canvas.width = Math.round(cssW * dpr); canvas.height = Math.round(cssH * dpr); }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, cssW, cssH);
    const n = layers.length;
    if (!n) return;
    let max = 0;
    layers.forEach((l) => { if (l.ext > max) max = l.ext; });
    if (max <= 0) max = 1;
    const cols = Math.max(1, Math.min(n, Math.floor(cssW) || 1));
    const per = n / cols;
    ctx.fillStyle = 'rgba(99,102,241,.35)';
    for (let c = 0; c < cols; c++) {
      let s = 0, k = 0;
      const from = Math.floor(c * per);
      const to = Math.min(n, Math.floor((c + 1) * per));
      for (let i = from; i < to || k === 0; i++) { s += num(layers[i] && layers[i].ext); k++; if (i >= n - 1) break; }
      const h = Math.max(1, (s / Math.max(1, k)) / max * (cssH - 2));
      const x = c * (cssW / cols);
      const isPast = current && current > 0 && Math.floor(c * per) < current;
      ctx.fillStyle = isPast ? 'rgba(99,102,241,.55)' : 'rgba(148,163,184,.35)';
      ctx.fillRect(x, cssH - h, Math.max(1, cssW / cols - 0.5), h);
    }
    const mark = (layer, color) => {
      if (!layer || layer <= 0) return;
      const x = ((layer - 1) / Math.max(1, n - 1)) * (cssW - 1) + 0.5;
      ctx.strokeStyle = color;
      ctx.lineWidth = 2;
      ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, cssH); ctx.stroke();
    };
    if (current) mark(current, '#10b981');
    if (selected && selected !== current) mark(selected, '#f97316');
  }

  function template() {
    return '<div class="lay">' +
      '<div class="lay-head"><div><b>Шкала слоёв</b> <span class="lay-sub" data-lay-sub>ищем G-code задания…</span></div>' +
      '<div class="lay-tools"><button type="button" class="btn xs on" data-lay-follow title="Держать ползунок на текущем слое станка">● Следить</button>' +
      '<button type="button" class="icon-btn sm" data-lay-help aria-label="Как пользоваться шкалой слоёв" title="Как пользоваться">?</button></div></div>' +
      `<div class="lay-help hidden" data-lay-help-box>${esc(HELP)}</div>` +
      '<div class="lay-state" data-lay-state></div>' +
      '<div class="lay-body hidden" data-lay-body>' +
      '<div class="lay-canvas-wrap"><canvas class="lay-canvas" data-lay-canvas></canvas><div class="lay-badge" data-lay-badge></div></div>' +
      '<div class="lay-side" data-lay-side></div>' +
      '</div>' +
      '<div class="lay-slider hidden" data-lay-slider><canvas class="lay-strip" data-lay-strip></canvas>' +
      '<input type="range" min="1" max="1" value="1" step="1" data-lay-range aria-label="Слой">' +
      '<div class="lay-ticks"><span>1</span><span data-lay-max>—</span></div></div>' +
      '</div>';
  }

  function mount(host, opts) {
    if (!host) return null;
    opts = opts || {};
    const key = (opts.printerId || '') + '|' + (opts.file || '');
    if (host._layers && host._layers.key === key) return host._layers;
    if (host._layers) host._layers.destroy();
    host.innerHTML = template();
    const q = (sel) => host.querySelector(sel);
    const els = {
      sub: q('[data-lay-sub]'), state: q('[data-lay-state]'), body: q('[data-lay-body]'), canvas: q('[data-lay-canvas]'),
      badge: q('[data-lay-badge]'), side: q('[data-lay-side]'), slider: q('[data-lay-slider]'), strip: q('[data-lay-strip]'),
      range: q('[data-lay-range]'), max: q('[data-lay-max]'), follow: q('[data-lay-follow]'), help: q('[data-lay-help]'), helpBox: q('[data-lay-help-box]'),
    };
    const st = { key, index: null, follow: true, current: 0, selected: 0, cache: new Map(), pending: 0, timer: 0, poll: 0, dead: false, drawTimer: 0, lastDetail: null, lastPrev: null };
    const query = opts.file ? { file: opts.file } : { printer_id: opts.printerId };

    function setState(html) { els.state.innerHTML = html || ''; els.state.classList.toggle('hidden', !html); }
    function showEmpty(text) {
      setState(`<div class="lay-empty">${esc(text)}</div>`);
      els.body.classList.add('hidden');
      els.slider.classList.add('hidden');
      els.sub.textContent = '';
    }
    function loadIndex() {
      if (st.dead) return;
      apiGet('/api/gcode/layers', query).then((data) => {
        if (st.dead) return;
        if (!data || data.has === false) { showEmpty((data && data.reason) || 'Слои недоступны'); return; }
        if (data.building) {
          setState(`<div class="lay-empty"><span class="spin"></span> Разбираем G-code… слоёв найдено: <b>${num(data.total)}</b></div>`);
          els.sub.textContent = data.source ? `файл ${data.source}` : '';
          st.poll = setTimeout(loadIndex, 1500);
          return;
        }
        if (data.error) { showEmpty('Не удалось разобрать G-code: ' + data.error); return; }
        st.index = data;
        const total = (data.layers || []).length;
        if (!total) { showEmpty('В файле не нашлось ни одного слоя с экструзией'); return; }
        setState('');
        els.body.classList.remove('hidden');
        els.slider.classList.remove('hidden');
        els.range.max = String(total);
        els.max.textContent = String(total);
        const start = st.current ? Math.min(total, st.current) : 1;
        select(start, true);
        drawStrip(els.strip, data.layers, st.current, st.selected);
      }).catch((err) => {
        if (st.dead) return;
        showEmpty('Слои недоступны: ' + (err && err.message ? err.message : 'нет связи'));
      });
    }
    function fetchLayer(n) {
      if (st.cache.has(n)) return Promise.resolve(st.cache.get(n));
      return apiGet('/api/gcode/layer', Object.assign({ layer: n }, query)).then((d) => {
        if (d && d.paths) {
          st.cache.set(n, d);
          if (st.cache.size > 48) st.cache.delete(st.cache.keys().next().value);
        }
        return d;
      });
    }
    function renderSide(summary, detail) {
      const idx = st.index || {};
      const total = (idx.layers || []).length;
      const parts = [];
      parts.push(`<div class="lay-kv"><span>Слой</span><b>${summary.i} / ${total}</b></div>`);
      parts.push(`<div class="lay-kv"><span>Высота Z</span><b>${num(summary.z).toFixed(2)} мм</b> <small>слой ${num(summary.h).toFixed(2)}</small></div>`);
      parts.push(`<div class="lay-kv"><span>Пластик на слое</span><b>${num(summary.ext) >= 1000 ? (num(summary.ext) / 1000).toFixed(2) + ' м' : Math.round(num(summary.ext)) + ' мм'}</b></div>`);
      if (summary.pct != null) parts.push(`<div class="lay-kv"><span>По расчёту слайсера</span><b>${Math.round(num(summary.pct))} %</b>${summary.rem != null ? ` <small>осталось ${esc(minutes(summary.rem))}</small>` : ''}</div>`);
      const types = summary.types || {};
      const chips = ORDER.slice().reverse().filter((k) => types[k]).map((k) => `<span class="lay-chip"><i style="background:${COLORS[k]}"></i>${esc(LABELS[k])}</span>`);
      parts.push(`<div class="lay-chips">${chips.join('') || '<span class="muted">нет экструзии</span>'}</div>`);
      const cur = st.current;
      if (cur && summary.i !== cur) parts.push(`<div class="lay-note">${summary.i < cur ? 'Этот слой уже напечатан' : `Впереди: ещё ${summary.i - cur} слоёв до него`}</div>`);
      else if (cur) parts.push('<div class="lay-note ok">Станок печатает этот слой сейчас</div>');
      els.side.innerHTML = parts.join('');
      els.badge.textContent = `#${summary.i} · Z ${num(summary.z).toFixed(2)}`;
      els.sub.textContent = `${idx.source ? idx.source + ' · ' : ''}${total} слоёв${idx.truncated ? ' (файл обрезан)' : ''}`;
    }
    function select(n, fromCode) {
      const idx = st.index;
      if (!idx) return;
      const total = (idx.layers || []).length;
      n = Math.max(1, Math.min(total, Math.round(num(n, 1))));
      st.selected = n;
      if (String(els.range.value) !== String(n)) els.range.value = String(n);
      const summary = idx.layers[n - 1];
      renderSide(summary, null);
      drawStrip(els.strip, idx.layers, st.current, st.selected);
      const token = ++st.pending;
      clearTimeout(st.drawTimer);
      st.drawTimer = setTimeout(() => {
        Promise.all([fetchLayer(n), n > 1 ? fetchLayer(n - 1).catch(() => null) : Promise.resolve(null)]).then(([d, p]) => {
          if (token !== st.pending || st.dead) return;
          st.lastDetail = d; st.lastPrev = p;
          draw(els.canvas, d, p, idx.bbox);
        }).catch(() => { /* слой не пришёл — картинка прежняя */ });
      }, fromCode ? 0 : 70);
    }
    function setFollow(on) {
      st.follow = !!on;
      els.follow.classList.toggle('on', st.follow);
      els.follow.textContent = st.follow ? '● Следить' : '○ Следить';
      els.follow.title = st.follow ? 'Ползунок следует за станком. Нажмите, чтобы листать вручную' : 'Вернуться к текущему слою и следовать за станком';
      if (st.follow && st.current) select(st.current, true);
    }
    els.range.addEventListener('input', () => { if (st.follow) setFollow(false); select(els.range.value); });
    els.range.addEventListener('keydown', (e) => { if (e.key === 'Home' || e.key === 'End') { e.preventDefault(); select(e.key === 'Home' ? 1 : num(els.range.max, 1)); setFollow(false); } });
    els.follow.addEventListener('click', () => setFollow(!st.follow));
    els.help.addEventListener('click', () => els.helpBox.classList.toggle('hidden'));
    const onResize = () => { if (st.lastDetail) draw(els.canvas, st.lastDetail, st.lastPrev, st.index && st.index.bbox); if (st.index) drawStrip(els.strip, st.index.layers, st.current, st.selected); };
    window.addEventListener('resize', onResize);

    const api = {
      key,
      /* Телеметрия: текущий слой станка. */
      update(t) {
        const layer = Math.max(0, Math.round(num(t && t.layer)));
        if (layer === st.current) return;
        st.current = layer;
        if (!st.index) return;
        if (st.follow && layer) select(layer, true);
        else { drawStrip(els.strip, st.index.layers, st.current, st.selected); const s = st.index.layers[st.selected - 1]; if (s) renderSide(s, null); }
      },
      reload() { st.index = null; st.cache.clear(); clearTimeout(st.poll); loadIndex(); },
      destroy() { st.dead = true; clearTimeout(st.poll); clearTimeout(st.drawTimer); window.removeEventListener('resize', onResize); host._layers = null; },
      state: st,
    };
    host._layers = api;
    if (opts.layer) st.current = Math.round(num(opts.layer));
    loadIndex();
    return api;
  }

  window.PFLayers = { mount, draw, drawStrip, COLORS, LABELS, ORDER, HELP };
})();
