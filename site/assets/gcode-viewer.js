/* PrintFlow 12.5.0 — G-code viewer (SVG, без зависимостей).
   Парсит G-code послойно, рисует полилинии с цветом филамента.
   Идея 1, 4, 5, 6 из каталога 3D-визуализации. */
(() => {
'use strict';

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? '').replace(/[&<>\"']/g, (c) =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

/**
 * Парсит G-code и возвращает структуру слоёв.
 * @param {string} gcode - текст G-code
 * @returns {{ layers: Array<{ z: number, lines: Array<{ x1, y1, x2, y2, type }> }>, stats: { layers, time, weight } }}
 */
function parseGcode(gcode) {
  const lines = gcode.split('\n');
  const layers = [];
  let currentLayer = { z: 0, lines: [] };
  let x = 0, y = 0, z = 0;
  let lastType = 'move';
  let totalTime = 0;
  let totalWeight = 0;

  for (const line of lines) {
    const comment = line.indexOf(';');
    const cmd = (comment >= 0 ? line.slice(0, comment) : line).trim();
    if (!cmd) continue;

    // Парсим время из комментария
    if (line.includes('; estimated printing time')) {
      const match = line.match(/(\d+)h\s*(\d+)m/);
      if (match) totalTime = parseInt(match[1]) * 60 + parseInt(match[2]);
    }

    // Парсим вес из комментария
    if (line.includes('; total filament weight')) {
      const match = line.match(/([\d.]+)\s*g/);
      if (match) totalWeight = parseFloat(match[1]);
    }

    // Определяем тип движения
    if (cmd.startsWith('G1') || cmd.startsWith('G0')) {
      const params = cmd.split(/\s+/).slice(1);
      let newX = x, newY = y, newZ = z;
      let extrude = false;

      for (const p of params) {
        if (p.startsWith('X')) newX = parseFloat(p.slice(1));
        else if (p.startsWith('Y')) newY = parseFloat(p.slice(1));
        else if (p.startsWith('Z')) newZ = parseFloat(p.slice(1));
        else if (p.startsWith('E')) extrude = parseFloat(p.slice(1)) > 0;
      }

      // Новый слой
      if (newZ !== z && newZ > 0) {
        if (currentLayer.lines.length > 0) layers.push(currentLayer);
        currentLayer = { z: newZ, lines: [] };
        z = newZ;
      }

      // Добавляем линию если есть экструзия
      if (extrude && (newX !== x || newY !== y)) {
        const type = cmd.startsWith('G0') ? 'travel' : 'extrude';
        currentLayer.lines.push({ x1: x, y1: y, x2: newX, y2: newY, type });
      }

      x = newX;
      y = newY;
    }
  }

  if (currentLayer.lines.length > 0) layers.push(currentLayer);

  return {
    layers,
    stats: {
      layers: layers.length,
      time: totalTime,
      weight: totalWeight
    }
  };
}

/**
 * Рендерит слой G-code как SVG.
 * @param {Array} layers - массив слоёв
 * @param {number} layerIdx - индекс слоя для рендера
 * @param {object} opts - опции { color, width, height }
 * @returns {string} SVG-строка
 */
function renderLayer(layers, layerIdx, opts = {}) {
  if (!layers.length || layerIdx < 0 || layerIdx >= layers.length) {
    return '<svg viewBox="0 0 100 100"><text x="50" y="50" text-anchor="middle" fill="#999">Нет данных</text></svg>';
  }

  const layer = layers[layerIdx];
  const color = opts.color || '#4f46e5';
  const width = opts.width || 400;
  const height = opts.height || 400;

  // Находим bounding box
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
  for (const l of layers) {
    for (const line of l.lines) {
      minX = Math.min(minX, line.x1, line.x2);
      minY = Math.min(minY, line.y1, line.y2);
      maxX = Math.max(maxX, line.x1, line.x2);
      maxY = Math.max(maxY, line.y1, line.y2);
    }
  }

  const dx = maxX - minX || 1;
  const dy = maxY - minY || 1;
  const scale = Math.min((width - 20) / dx, (height - 20) / dy);
  const ox = (width - dx * scale) / 2 - minX * scale;
  const oy = (height - dy * scale) / 2 - minY * scale;

  const paths = layer.lines
    .filter(l => l.type === 'extrude')
    .map(l => {
      const x1 = l.x1 * scale + ox;
      const y1 = l.y1 * scale + oy;
      const x2 = l.x2 * scale + ox;
      const y2 = l.y2 * scale + oy;
      return `<line x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}" stroke="${color}" stroke-width="1" stroke-linecap="round"/>`;
    }).join('');

  return `<svg viewBox="0 0 ${width} ${height}" xmlns="http://www.w3.org/2000/svg">` +
    `<rect width="${width}" height="${height}" fill="#f9fafb"/>` +
    paths +
    `</svg>`;
}

/**
 * Создаёт интерактивный viewer с слайдером слоёв.
 * @param {HTMLElement} host - контейнер
 * @param {string} gcode - текст G-code
 */
function createViewer(host, gcode) {
  const parsed = parseGcode(gcode);
  if (!parsed.layers.length) {
    host.innerHTML = '<div class="empty">G-code пуст или не содержит слоёв.</div>';
    return;
  }

  let currentLayer = 0;
  const total = parsed.layers.length;

  host.innerHTML = `
    <div class="gcode-viewer">
      <div class="gcode-canvas" id="gcode_canvas"></div>
      <div class="gcode-controls">
        <label>Слой <input type="range" id="gcode_slider" min="0" max="${total - 1}" value="0" step="1">
          <span id="gcode_layer_num">1</span> / ${total}</label>
        <button id="gcode_play" class="btn sm">▶ Play</button>
      </div>
      <div class="gcode-stats">
        <span>Слоёв: ${parsed.stats.layers}</span>
        <span>Время: ${parsed.stats.time ? parsed.stats.time + ' мин' : '—'}</span>
        <span>Вес: ${parsed.stats.weight ? parsed.stats.weight + ' г' : '—'}</span>
      </div>
    </div>
  `;

  const canvas = $('gcode_canvas');
  const slider = $('gcode_slider');
  const layerNum = $('gcode_layer_num');
  const playBtn = $('gcode_play');

  function update() {
    canvas.innerHTML = renderLayer(parsed.layers, currentLayer, { width: 400, height: 400 });
    layerNum.textContent = String(currentLayer + 1);
    slider.value = String(currentLayer);
  }

  slider.oninput = () => {
    currentLayer = parseInt(slider.value, 10);
    update();
  };

  let playing = false;
  let playInterval = null;

  playBtn.onclick = () => {
    if (playing) {
      clearInterval(playInterval);
      playBtn.textContent = '▶ Play';
      playing = false;
    } else {
      playing = true;
      playBtn.textContent = '⏸ Pause';
      playInterval = setInterval(() => {
        currentLayer = (currentLayer + 1) % total;
        update();
      }, 100);
    }
  };

  update();
}

// Экспорт в PF
if (typeof window !== 'undefined') {
  window.GCodeViewer = { parse: parseGcode, render: renderLayer, create: createViewer };
}

})();
