/* NOZZA — минимальный генератор QR-кодов (byte mode, версии 1–10).
   Без зависимостей и сборщика: работает и с диска, и с сервера.
   Использование: QR.svg("https://…", { size: 120, margin: 2, ecl: "M" }) -> строка SVG. */
(() => {
'use strict';

/* --------------------------------------------------- арифметика GF(256) */
const EXP = new Uint8Array(512), LOG = new Uint8Array(256);
(() => {
  let x = 1;
  for (let i = 0; i < 255; i++) {
    EXP[i] = x; LOG[x] = i;
    x <<= 1;
    if (x & 0x100) x ^= 0x11d;
  }
  for (let i = 255; i < 512; i++) EXP[i] = EXP[i - 255];
})();
const mul = (a, b) => (a === 0 || b === 0) ? 0 : EXP[LOG[a] + LOG[b]];

function rsPoly(deg) {
  let poly = [1];
  for (let i = 0; i < deg; i++) {
    const next = new Array(poly.length + 1).fill(0);
    for (let j = 0; j < poly.length; j++) {
      next[j] ^= mul(poly[j], 1);
      next[j + 1] ^= mul(poly[j], EXP[i]);
    }
    poly = next;
  }
  return poly;
}
function rsEncode(data, ecLen) {
  const gen = rsPoly(ecLen);
  const res = new Array(ecLen).fill(0);
  for (const byte of data) {
    const factor = byte ^ res[0];
    res.shift(); res.push(0);
    for (let i = 0; i < ecLen; i++) res[i] ^= mul(gen[i + 1], factor);
  }
  return res;
}

/* ------------------------------------------- таблицы блоков (версии 1–10) */
/* [총 codewords, ecPerBlock, group1blocks, group1data, group2blocks, group2data] */
const RS = {
  L: [[26,7,1,19,0,0],[44,10,1,34,0,0],[70,15,1,55,0,0],[100,20,1,80,0,0],[134,26,1,108,0,0],
      [172,18,2,68,0,0],[196,20,2,78,0,0],[242,24,2,97,0,0],[292,30,2,116,0,0],[346,18,2,68,2,69]],
  M: [[26,10,1,16,0,0],[44,16,1,28,0,0],[70,26,1,44,0,0],[100,18,2,32,0,0],[134,24,2,43,0,0],
      [172,16,4,27,0,0],[196,18,4,31,0,0],[242,22,2,38,2,39],[292,22,3,36,2,37],[346,26,4,43,1,44]],
  Q: [[26,13,1,13,0,0],[44,22,1,22,0,0],[70,18,2,17,0,0],[100,26,2,24,0,0],[134,18,2,15,2,16],
      [172,24,4,19,0,0],[196,18,2,14,4,15],[242,22,4,18,2,19],[292,20,4,16,4,17],[346,24,6,19,2,20]],
  H: [[26,17,1,9,0,0],[44,28,1,16,0,0],[70,22,2,13,0,0],[100,16,4,9,0,0],[134,22,2,11,2,12],
      [172,28,4,15,0,0],[196,26,4,13,1,14],[242,26,4,14,2,15],[292,24,4,12,4,13],[346,28,6,15,2,16]],
};
const ALIGN = [[], [], [6,18], [6,22], [6,26], [6,30], [6,34], [6,22,38], [6,24,42], [6,26,46], [6,28,50]];

/* ------------------------------------------------------------ построение */
function utf8(str) {
  if (typeof TextEncoder !== 'undefined') return Array.from(new TextEncoder().encode(str));
  const out = [], esc = encodeURIComponent(str);
  for (let i = 0; i < esc.length; i++) {
    if (esc[i] === '%') { out.push(parseInt(esc.substr(i + 1, 2), 16)); i += 2; }
    else out.push(esc.charCodeAt(i));
  }
  return out;
}

function buildData(bytes, version, ecl) {
  const [, ecLen, g1, d1, g2, d2] = RS[ecl][version - 1];
  const capacity = g1 * d1 + g2 * d2;
  const bits = [];
  const push = (val, len) => { for (let i = len - 1; i >= 0; i--) bits.push((val >> i) & 1); };
  push(0b0100, 4);                                  // byte mode
  push(bytes.length, version < 10 ? 8 : 16);        // длина
  for (const b of bytes) push(b, 8);
  const total = capacity * 8;
  push(0, Math.min(4, total - bits.length));        // терминатор
  while (bits.length % 8) bits.push(0);
  const words = [];
  for (let i = 0; i < bits.length; i += 8) {
    words.push(bits.slice(i, i + 8).reduce((a, b) => (a << 1) | b, 0));
  }
  const PAD = [0xec, 0x11];
  for (let i = 0; words.length < capacity; i++) words.push(PAD[i % 2]);

  // блоки и чередование
  const blocks = [], ecBlocks = [];
  let pos = 0;
  for (let i = 0; i < g1 + g2; i++) {
    const len = i < g1 ? d1 : d2;
    const block = words.slice(pos, pos + len); pos += len;
    blocks.push(block);
    ecBlocks.push(rsEncode(block, ecLen));
  }
  const out = [];
  const maxData = Math.max(d1, d2);
  for (let i = 0; i < maxData; i++) {
    for (const b of blocks) if (i < b.length) out.push(b[i]);
  }
  for (let i = 0; i < ecLen; i++) {
    for (const b of ecBlocks) out.push(b[i]);
  }
  return out;
}

function makeMatrix(version) {
  const size = version * 4 + 17;
  const m = Array.from({ length: size }, () => new Array(size).fill(null));

  const finder = (r, c) => {
    for (let i = -1; i <= 7; i++) for (let j = -1; j <= 7; j++) {
      const rr = r + i, cc = c + j;
      if (rr < 0 || cc < 0 || rr >= size || cc >= size) continue;
      const inner = i >= 0 && i <= 6 && j >= 0 && j <= 6
        && (i === 0 || i === 6 || j === 0 || j === 6 || (i >= 2 && i <= 4 && j >= 2 && j <= 4));
      m[rr][cc] = inner ? 1 : 0;
    }
  };
  finder(0, 0); finder(0, size - 7); finder(size - 7, 0);

  for (let i = 8; i < size - 8; i++) {
    const v = i % 2 === 0 ? 1 : 0;
    m[6][i] = v; m[i][6] = v;
  }
  const centers = ALIGN[version];
  const last = centers.length ? centers[centers.length - 1] : 0;
  for (const r of centers) for (const c of centers) {
    const onFinder = (r === 6 && c === 6) || (r === 6 && c === last) || (r === last && c === 6);
    if (onFinder) continue;
    for (let i = -2; i <= 2; i++) for (let j = -2; j <= 2; j++) {
      m[r + i][c + j] = (Math.abs(i) === 2 || Math.abs(j) === 2 || (i === 0 && j === 0)) ? 1 : 0;
    }
  }
  if (version >= 7) {                       // резерв под сведения о версии
    for (let i = 0; i < 6; i++) for (let j = 0; j < 3; j++) {
      m[i][size - 11 + j] = 0; m[size - 11 + j][i] = 0;
    }
  }
  m[size - 8][8] = 1;   // dark module
  return m;
}

function reserveFormat(m, size) {
  const res = [];
  for (let i = 0; i < 9; i++) {
    if (m[8][i] === null) { m[8][i] = 0; res.push([8, i]); }
    if (m[i][8] === null) { m[i][8] = 0; res.push([i, 8]); }
  }
  for (let i = size - 8; i < size; i++) {
    if (m[8][i] === null) { m[8][i] = 0; res.push([8, i]); }
    if (m[i][8] === null) { m[i][8] = 0; res.push([i, 8]); }
  }
  return res;
}

function placeData(m, size, data) {
  let bitIdx = 0;
  const bit = () => {
    const byte = data[bitIdx >> 3];
    const b = byte === undefined ? 0 : (byte >> (7 - (bitIdx & 7))) & 1;
    bitIdx++;
    return b;
  };
  let upward = true;
  for (let col = size - 1; col > 0; col -= 2) {
    if (col === 6) col--;
    for (let n = 0; n < size; n++) {
      const row = upward ? size - 1 - n : n;
      for (let k = 0; k < 2; k++) {
        const c = col - k;
        if (m[row][c] !== null) continue;
        m[row][c] = bit();
      }
    }
    upward = !upward;
  }
}

const MASKS = [
  (r, c) => (r + c) % 2 === 0,
  (r) => r % 2 === 0,
  (r, c) => c % 3 === 0,
  (r, c) => (r + c) % 3 === 0,
  (r, c) => (Math.floor(r / 2) + Math.floor(c / 3)) % 2 === 0,
  (r, c) => ((r * c) % 2) + ((r * c) % 3) === 0,
  (r, c) => (((r * c) % 2) + ((r * c) % 3)) % 2 === 0,
  (r, c) => (((r + c) % 2) + ((r * c) % 3)) % 2 === 0,
];

const ECL_BITS = { L: 1, M: 0, Q: 3, H: 2 };
function formatBits(ecl, mask) {
  let data = (ECL_BITS[ecl] << 3) | mask;
  let rem = data;
  for (let i = 0; i < 10; i++) rem = (rem << 1) ^ (((rem >> 9) & 1) * 0x537);
  return ((data << 10) | rem) ^ 0x5412;
}

function applyFormat(m, size, ecl, mask) {
  const bits = formatBits(ecl, mask);
  const get = (i) => (bits >> i) & 1;
  for (let i = 0; i <= 5; i++) m[i][8] = get(i);
  m[7][8] = get(6); m[8][8] = get(7); m[8][7] = get(8);
  for (let i = 9; i < 15; i++) m[8][14 - i] = get(i);
  for (let i = 0; i < 8; i++) m[8][size - 1 - i] = get(i);
  for (let i = 8; i < 15; i++) m[size - 15 + i][8] = get(i);
  m[size - 8][8] = 1;
}

function versionBits(version) {
  let rem = version;
  for (let i = 0; i < 12; i++) rem = (rem << 1) ^ (((rem >> 11) & 1) * 0x1f25);
  return (version << 12) | rem;
}

function applyVersion(m, size, version) {
  if (version < 7) return;
  const bits = versionBits(version);
  for (let i = 0; i < 18; i++) {
    const b = (bits >> i) & 1;
    const r = Math.floor(i / 3), c = i % 3;
    m[r][size - 11 + c] = b;
    m[size - 11 + c][r] = b;
  }
}

function penalty(m, size) {
  let score = 0;
  // ряды и столбцы одного цвета
  for (let i = 0; i < size; i++) {
    for (const line of [m[i], m.map((r) => r[i])]) {
      let run = 1;
      for (let j = 1; j < size; j++) {
        if (line[j] === line[j - 1]) { run++; } else { if (run >= 5) score += run - 2; run = 1; }
      }
      if (run >= 5) score += run - 2;
    }
  }
  // блоки 2×2
  for (let r = 0; r < size - 1; r++) for (let c = 0; c < size - 1; c++) {
    const v = m[r][c];
    if (v === m[r][c + 1] && v === m[r + 1][c] && v === m[r + 1][c + 1]) score += 3;
  }
  // паттерны, похожие на поисковые
  const FIND = [1,0,1,1,1,0,1];
  const runFinder = (line) => {
    let sc = 0;
    for (let i = 0; i + 7 <= size; i++) {
      let hit = true;
      for (let k = 0; k < 7; k++) if (line[i + k] !== FIND[k]) { hit = false; break; }
      if (!hit) continue;
      let before = true, after = true;
      for (let k = 1; k <= 4; k++) {
        if (i - k >= 0 && line[i - k]) before = false;
        if (i + 6 + k < size && line[i + 6 + k]) after = false;
      }
      if (before || after) sc += 40;
    }
    return sc;
  };
  for (let i = 0; i < size; i++) {
    score += runFinder(m[i]);
    score += runFinder(m.map((r) => r[i]));
  }
  // доля тёмных
  let dark = 0;
  for (let r = 0; r < size; r++) for (let c = 0; c < size; c++) if (m[r][c]) dark++;
  const pct = dark * 100 / (size * size);
  score += Math.floor(Math.abs(pct - 50) / 5) * 10;
  return score;
}

function encode(text, ecl) {
  const bytes = utf8(text);
  let version = 0;
  for (let v = 1; v <= 10; v++) {
    const [, , g1, d1, g2, d2] = RS[ecl][v - 1];
    const cap = g1 * d1 + g2 * d2 - (v < 10 ? 2 : 3);
    if (bytes.length <= cap) { version = v; break; }
  }
  if (!version) throw new Error('QR: слишком длинный текст');

  const data = buildData(bytes, version, ecl);
  const size = version * 4 + 17;
  const base = makeMatrix(version);
  reserveFormat(base, size);
  const reserved = base.map((row) => row.map((v) => v !== null));
  placeData(base, size, data);

  let best = null, bestScore = Infinity;
  for (let mask = 0; mask < 8; mask++) {
    const m = base.map((row) => row.slice());
    for (let r = 0; r < size; r++) for (let c = 0; c < size; c++) {
      if (!reserved[r][c] && MASKS[mask](r, c)) m[r][c] ^= 1;
    }
    applyFormat(m, size, ecl, mask);
    applyVersion(m, size, version);
    const s = penalty(m, size);
    if (s < bestScore) { bestScore = s; best = m; }
  }
  return best;
}

/* ------------------------------------------------------------- рендеринг */
function svg(text, opts) {
  const o = opts || {};
  const ecl = o.ecl || 'M';
  const margin = o.margin === undefined ? 2 : o.margin;
  const dark = o.dark || '#111827';
  const light = o.light || '#ffffff';
  let m;
  try { m = encode(String(text || ''), ecl); }
  catch (e) { return ''; }
  const n = m.length, total = n + margin * 2;
  const parts = [];
  for (let r = 0; r < n; r++) {
    let c = 0;
    while (c < n) {
      if (!m[r][c]) { c++; continue; }
      let len = 1;
      while (c + len < n && m[r][c + len]) len++;
      parts.push(`M${c + margin} ${r + margin}h${len}v1h-${len}z`);
      c += len;
    }
  }
  const px = o.size ? ` width="${o.size}" height="${o.size}"` : '';
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${total} ${total}"${px} shape-rendering="crispEdges">`
    + `<rect width="${total}" height="${total}" fill="${light}"/>`
    + `<path d="${parts.join('')}" fill="${dark}"/></svg>`;
}

function dataUri(text, opts) {
  const s = svg(text, opts);
  return s ? 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(s) : '';
}

window.QR = { svg, dataUri, encode };
})();
