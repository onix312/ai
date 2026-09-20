/* GPU-сечения (18.12): мгновенный разбор STL в браузере до нарезки.
 * window.PFGpuSlice.
 *
 * Что делает: режет модель на те же плоскости, что и движок Stage 1
 * (первый слой + шаг), сшивает сечения в контуры и показывает слои, свесы,
 * тонкие места, пятно контакта и оценку пластика — за доли секунды, без
 * похода на сервер. Тяжёлая часть (пересечение треугольников с плоскостями,
 * O(треугольники × слои)) идёт compute-шейдером WebGPU; если WebGPU нет
 * (Firefox, старый Safari, запрет политикой) — тот же алгоритм считается в
 * фоновом потоке (Web Worker) на процессоре. Результат одинаковый, разница
 * только во времени, и виджет честно пишет, где считал.
 *
 * Это не замена нарезке: периметры, заполнение и G-code по-прежнему делает
 * сервер («План» / «Нарезать»). См. docs/adr/0003-gpu-сечения-в-браузере.md.
 *
 * Чистые функции (parseSTL, planes, sliceCPU, chain, analyze) не трогают DOM
 * и проверяются в node (connector/tests/test_gpuslice.py).
 */
(function () {
  'use strict';

  const OVERHANG_DEG = 45;                 // свес: грань вниз положе 45° от вертикали
  const COS_LIMIT = Math.cos(OVERHANG_DEG * Math.PI / 180);
  const QUANT = 1000;                      // сшивка контуров с точностью 0.001 мм
  const MAX_SEGMENTS = 3000000;            // предел буфера отрезков (48 МБ на GPU)
  const MAX_TRIANGLES = 1500000;           // как MAX_TRIANGLES движка Stage 1
  const HELP = 'Сечения считаются в браузере до нарезки: ползунок листает слои будущей печати, заливка — тело детали на этом слое, розовые отрезки — грани, нависающие круче 45° (там, вероятно, нужны поддержки). Полоска под ползунком — площадь сечения по слоям: жёлтые метки — тонкие места (уже двух периметров), розовые — слои со свесами. Масса — оценка по объёму, стенкам и заполнению из настроек слайсера; точную даст «План» или «Нарезать».';

  function num(v, d) { const n = Number(v); return Number.isFinite(n) ? n : (d === undefined ? 0 : d); }
  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, (m) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[m])); }
  function fmt(v, d) { return num(v).toLocaleString('ru-RU', { maximumFractionDigits: d === undefined ? 1 : d, minimumFractionDigits: 0 }); }

  /* ============================================================== STL */
  function isBinarySTL(buffer) {
    if (buffer.byteLength < 84) return false;
    const view = new DataView(buffer);
    const count = view.getUint32(80, true);
    if (84 + count * 50 === buffer.byteLength) return true;
    const head = String.fromCharCode.apply(null, new Uint8Array(buffer, 0, Math.min(5, buffer.byteLength)));
    return head.toLowerCase() !== 'solid';
  }

  /* Бинарный или текстовый STL → плоский Float32Array (9 чисел на треугольник). */
  function parseSTL(buffer) {
    if (!(buffer instanceof ArrayBuffer)) throw new Error('Ожидается ArrayBuffer с STL');
    let positions;
    let ascii = false;
    if (isBinarySTL(buffer)) {
      const view = new DataView(buffer);
      const count = view.getUint32(80, true);
      if (count > MAX_TRIANGLES) throw new Error(`Слишком много треугольников: ${count.toLocaleString('ru-RU')} > ${MAX_TRIANGLES.toLocaleString('ru-RU')}`);
      positions = new Float32Array(count * 9);
      let off = 84;
      for (let i = 0; i < count; i++) {
        off += 12;                       // нормаль из файла не нужна: считаем сами
        const b = i * 9;
        for (let k = 0; k < 9; k++) { positions[b + k] = view.getFloat32(off, true); off += 4; }
        off += 2;
      }
    } else {
      ascii = true;
      const text = new TextDecoder('latin1').decode(buffer);
      const out = [];
      const re = /vertex\s+([-+\d.eE]+)\s+([-+\d.eE]+)\s+([-+\d.eE]+)/g;
      let m;
      while ((m = re.exec(text))) out.push(+m[1], +m[2], +m[3]);
      if (out.length % 9) throw new Error('Последняя грань неполная: STL-файл обрезан');
      if (out.length / 9 > MAX_TRIANGLES) throw new Error('Слишком много треугольников для разбора в браузере');
      positions = Float32Array.from(out);
    }
    if (!positions.length) throw new Error('В файле нет треугольников — это не STL?');
    const min = [Infinity, Infinity, Infinity], max = [-Infinity, -Infinity, -Infinity];
    for (let i = 0; i < positions.length; i += 3) {
      for (let k = 0; k < 3; k++) {
        const v = positions[i + k];
        if (v < min[k]) min[k] = v;
        if (v > max[k]) max[k] = v;
      }
    }
    return { positions, triangles: positions.length / 9, ascii, bbox: { min, max, size: [max[0] - min[0], max[1] - min[1], max[2] - min[2]] } };
  }

  /* ============================================================ плоскости */
  /* Те же высоты, что _plane_heights движка: слой 0 — первый слой, дальше шаг. */
  function planes(height, first, step) {
    first = Math.max(0.02, num(first, 0.2));
    step = Math.max(0.02, num(step, 0.2));
    const z = [], top = [], h = [];
    let i = 0;
    for (;;) {
      const t = i === 0 ? first : first + i * step;
      const b = i === 0 ? 0 : first + (i - 1) * step;
      top.push(t); z.push((t + b) / 2); h.push(t - b);
      if (t >= height - 1e-9 || i > 50000) break;
      i++;
    }
    return { z: Float64Array.from(z), top: Float64Array.from(top), h: Float64Array.from(h), first, step, count: z.length };
  }

  /* Диапазон индексов плоскостей внутри [zlo, zhi] относительно низа модели. */
  function planeRange(zlo, zhi, first, step, count) {
    let lo;
    if (zlo <= first * 0.5) lo = 0;
    else lo = Math.max(1, Math.ceil((zlo - first) / step + 0.5));
    let hi;
    if (zhi < first * 0.5) hi = -1;
    else { const f = Math.floor((zhi - first) / step + 0.5); hi = f < 1 ? 0 : f; }
    if (hi >= count) hi = count - 1;
    return [lo, hi];
  }
  function planeZ(i, first, step) { return i === 0 ? first * 0.5 : first + (i - 0.5) * step; }

  /* ========================================================= сечения (CPU)
     positions — треугольники, уже опущенные на стол (z ≥ 0). Отрезки
     ориентированы так, что тело слева: наружные контуры против часовой,
     отверстия — по часовой; знак площади после сшивки даёт классификацию. */
  function sliceCPU(positions, first, step, count, cosLimit, cap) {
    cosLimit = cosLimit === undefined ? COS_LIMIT : cosLimit;
    cap = cap || MAX_SEGMENTS;
    let size = Math.min(cap, 65536);
    let segs = new Float32Array(size * 4);
    let layer = new Uint32Array(size);
    let flag = new Uint8Array(size);
    let n = 0;
    let overflow = false;
    let overhangArea = 0, downArea = 0;
    const tri = positions.length / 9;
    const px = [0, 0, 0], py = [0, 0, 0];
    for (let t = 0; t < tri; t++) {
      const b = t * 9;
      const ax = positions[b], ay = positions[b + 1], az = positions[b + 2];
      const bx = positions[b + 3], by = positions[b + 4], bz = positions[b + 5];
      const cx = positions[b + 6], cy = positions[b + 7], cz = positions[b + 8];
      const zlo = Math.min(az, bz, cz), zhi = Math.max(az, bz, cz);
      // нормаль
      const ux = bx - ax, uy = by - ay, uz = bz - az, vx = cx - ax, vy = cy - ay, vz = cz - az;
      const nx = uy * vz - uz * vy, ny = uz * vx - ux * vz, nz = ux * vy - uy * vx;
      const len = Math.hypot(nx, ny, nz);
      let over = 0;
      if (len > 0 && nz / len < -cosLimit) {
        const area = len / 2;
        downArea += area;
        if (zlo > 1e-3) { over = 1; overhangArea += area; }
      }
      if (zhi - zlo <= 0) continue;
      const r = planeRange(zlo, zhi, first, step, count);
      for (let i = r[0]; i <= r[1]; i++) {
        const z = planeZ(i, first, step);
        let k = 0;
        // ребро a→b
        if ((az <= z && z < bz) || (bz <= z && z < az)) { const s = (z - az) / (bz - az); px[k] = ax + ux * s; py[k] = ay + uy * s; k++; }
        if ((bz <= z && z < cz) || (cz <= z && z < bz)) { const s = (z - bz) / (cz - bz); px[k] = bx + (cx - bx) * s; py[k] = by + (cy - by) * s; k++; }
        if (k < 2 && ((cz <= z && z < az) || (az <= z && z < cz))) { const s = (z - cz) / (az - cz); px[k] = cx + (ax - cx) * s; py[k] = cy + (ay - cy) * s; k++; }
        if (k !== 2) continue;
        if (n >= size) {
          if (size >= cap) { overflow = true; break; }
          size = Math.min(cap, size * 2);
          const s2 = new Float32Array(size * 4); s2.set(segs); segs = s2;
          const l2 = new Uint32Array(size); l2.set(layer); layer = l2;
          const f2 = new Uint8Array(size); f2.set(flag); flag = f2;
        }
        // ориентация: направление up × n = (-ny, nx)
        let x1 = px[0], y1 = py[0], x2 = px[1], y2 = py[1];
        if ((x2 - x1) * (-ny) + (y2 - y1) * nx < 0) { x1 = px[1]; y1 = py[1]; x2 = px[0]; y2 = py[0]; }
        const o = n * 4;
        segs[o] = x1; segs[o + 1] = y1; segs[o + 2] = x2; segs[o + 3] = y2;
        layer[n] = i; flag[n] = over; n++;
      }
      if (overflow) break;
    }
    return { segs: segs.subarray(0, n * 4), layer: layer.subarray(0, n), flag: flag.subarray(0, n), count: n, overflow, overhangArea, downArea };
  }

  /* Фоновый поток: тот же sliceCPU, собранный из исходников функций. */
  function workerSource() {
    return 'const COS_LIMIT=' + COS_LIMIT + ';const MAX_SEGMENTS=' + MAX_SEGMENTS + ';'
      + planeRange.toString() + '\n' + planeZ.toString() + '\n' + sliceCPU.toString() + '\n'
      + 'self.onmessage=(e)=>{const d=e.data;try{const r=sliceCPU(d.positions,d.first,d.step,d.count,d.cosLimit,d.cap);'
      + 'self.postMessage({ok:true,r},[r.segs.buffer]);}catch(err){self.postMessage({ok:false,error:String(err&&err.message||err)});}};';
  }
  function sliceInWorker(positions, pl) {
    return new Promise((resolve, reject) => {
      let url = '';
      try {
        url = URL.createObjectURL(new Blob([workerSource()], { type: 'text/javascript' }));
        const w = new Worker(url);
        const done = () => { try { w.terminate(); URL.revokeObjectURL(url); } catch (e) { /* пусто */ } };
        w.onmessage = (e) => { done(); if (e.data && e.data.ok) resolve(e.data.r); else reject(new Error(e.data && e.data.error || 'поток упал')); };
        w.onerror = (e) => { done(); reject(new Error(e.message || 'поток упал')); };
        const copy = positions.slice();
        w.postMessage({ positions: copy, first: pl.first, step: pl.step, count: pl.count, cosLimit: COS_LIMIT, cap: MAX_SEGMENTS }, [copy.buffer]);
      } catch (err) { reject(err); }
    });
  }

  /* ========================================================= сечения (GPU) */
  const WGSL = `
struct Params { tri_count: u32, layer_count: u32, cap: u32, _pad: u32, first: f32, step: f32, cos_limit: f32, _pad2: f32 };
@group(0) @binding(0) var<storage, read> tris: array<f32>;
@group(0) @binding(1) var<uniform> P: Params;
@group(0) @binding(2) var<storage, read_write> segs: array<f32>;
@group(0) @binding(3) var<storage, read_write> meta: array<u32>;
@group(0) @binding(4) var<storage, read_write> counters: array<atomic<u32>>;

fn plane_z(i: u32) -> f32 {
  if (i == 0u) { return P.first * 0.5; }
  return P.first + (f32(i) - 0.5) * P.step;
}
fn lo_index(z: f32) -> u32 {
  if (z <= P.first * 0.5) { return 0u; }
  return u32(max(ceil((z - P.first) / P.step + 0.5), 1.0));
}
fn hi_index(z: f32) -> i32 {
  if (z < P.first * 0.5) { return -1; }
  let f = floor((z - P.first) / P.step + 0.5);
  if (f < 1.0) { return 0; }
  return i32(f);
}
fn edge_hit(p: vec3<f32>, q: vec3<f32>, z: f32) -> vec3<f32> {
  if ((p.z <= z && z < q.z) || (q.z <= z && z < p.z)) {
    let s = (z - p.z) / (q.z - p.z);
    return vec3<f32>(p.x + (q.x - p.x) * s, p.y + (q.y - p.y) * s, 1.0);
  }
  return vec3<f32>(0.0, 0.0, 0.0);
}
@compute @workgroup_size(64)
fn main(@builtin(global_invocation_id) gid: vec3<u32>) {
  let t = gid.x;
  if (t >= P.tri_count) { return; }
  let b = t * 9u;
  let a = vec3<f32>(tris[b], tris[b + 1u], tris[b + 2u]);
  let c1 = vec3<f32>(tris[b + 3u], tris[b + 4u], tris[b + 5u]);
  let c2 = vec3<f32>(tris[b + 6u], tris[b + 7u], tris[b + 8u]);
  let zlo = min(a.z, min(c1.z, c2.z));
  let zhi = max(a.z, max(c1.z, c2.z));
  if (zhi - zlo <= 0.0) { return; }
  let n = cross(c1 - a, c2 - a);
  let len = length(n);
  var flag = 0u;
  if (len > 0.0 && n.z / len < -P.cos_limit && zlo > 0.001) { flag = 1u; }
  var i = lo_index(zlo);
  let hi = hi_index(zhi);
  loop {
    if (i32(i) > hi || i >= P.layer_count) { break; }
    let z = plane_z(i);
    var px: array<f32, 3>;
    var py: array<f32, 3>;
    var k = 0u;
    var h = edge_hit(a, c1, z);
    if (h.z > 0.5) { px[k] = h.x; py[k] = h.y; k = k + 1u; }
    h = edge_hit(c1, c2, z);
    if (h.z > 0.5) { px[k] = h.x; py[k] = h.y; k = k + 1u; }
    if (k < 2u) {
      h = edge_hit(c2, a, z);
      if (h.z > 0.5) { px[k] = h.x; py[k] = h.y; k = k + 1u; }
    }
    if (k == 2u) {
      var x1 = px[0]; var y1 = py[0]; var x2 = px[1]; var y2 = py[1];
      if ((x2 - x1) * (-n.y) + (y2 - y1) * n.x < 0.0) { x1 = px[1]; y1 = py[1]; x2 = px[0]; y2 = py[0]; }
      let idx = atomicAdd(&counters[0], 1u);
      if (idx < P.cap) {
        segs[idx * 4u] = x1; segs[idx * 4u + 1u] = y1; segs[idx * 4u + 2u] = x2; segs[idx * 4u + 3u] = y2;
        meta[idx] = (i << 1u) | flag;
      } else {
        atomicStore(&counters[1], 1u);
      }
    }
    i = i + 1u;
  }
}`;

  let devicePromise = null;
  function gpuDevice() {
    if (devicePromise) return devicePromise;
    devicePromise = (async () => {
      if (typeof navigator === 'undefined' || !navigator.gpu) return null;
      const adapter = await navigator.gpu.requestAdapter();
      if (!adapter) return null;
      const device = await adapter.requestDevice();
      device.lost.then(() => { devicePromise = null; });
      return device;
    })().catch(() => { devicePromise = null; return null; });
    return devicePromise;
  }

  async function sliceGPU(device, positions, pl) {
    const G = GPUBufferUsage;
    const triCount = positions.length / 9;
    // Грубая оценка числа отрезков: каждое ребро режется по числу плоскостей.
    const cap = Math.min(MAX_SEGMENTS, Math.max(65536, triCount * 6));
    const triBuf = device.createBuffer({ size: positions.byteLength, usage: G.STORAGE | G.COPY_DST });
    device.queue.writeBuffer(triBuf, 0, positions);
    const paramData = new ArrayBuffer(32);
    const u32 = new Uint32Array(paramData), f32 = new Float32Array(paramData);
    u32[0] = triCount; u32[1] = pl.count; u32[2] = cap; u32[3] = 0;
    f32[4] = pl.first; f32[5] = pl.step; f32[6] = COS_LIMIT; f32[7] = 0;
    const paramBuf = device.createBuffer({ size: 32, usage: G.UNIFORM | G.COPY_DST });
    device.queue.writeBuffer(paramBuf, 0, paramData);
    const segBuf = device.createBuffer({ size: cap * 16, usage: G.STORAGE | G.COPY_SRC });
    const metaBuf = device.createBuffer({ size: cap * 4, usage: G.STORAGE | G.COPY_SRC });
    const cntBuf = device.createBuffer({ size: 8, usage: G.STORAGE | G.COPY_SRC | G.COPY_DST });
    device.queue.writeBuffer(cntBuf, 0, new Uint32Array([0, 0]));
    const module = device.createShaderModule({ code: WGSL });
    const pipeline = await device.createComputePipelineAsync({ layout: 'auto', compute: { module, entryPoint: 'main' } });
    const bind = device.createBindGroup({ layout: pipeline.getBindGroupLayout(0), entries: [
      { binding: 0, resource: { buffer: triBuf } }, { binding: 1, resource: { buffer: paramBuf } },
      { binding: 2, resource: { buffer: segBuf } }, { binding: 3, resource: { buffer: metaBuf } },
      { binding: 4, resource: { buffer: cntBuf } }] });
    const cntRead = device.createBuffer({ size: 8, usage: G.COPY_DST | G.MAP_READ });
    let enc = device.createCommandEncoder();
    const pass = enc.beginComputePass();
    pass.setPipeline(pipeline); pass.setBindGroup(0, bind);
    pass.dispatchWorkgroups(Math.ceil(triCount / 64));
    pass.end();
    enc.copyBufferToBuffer(cntBuf, 0, cntRead, 0, 8);
    device.queue.submit([enc.finish()]);
    await cntRead.mapAsync(GPUMapMode.READ);
    const cnt = new Uint32Array(cntRead.getMappedRange().slice(0));
    cntRead.unmap();
    const n = Math.min(cnt[0], cap), overflow = cnt[1] === 1 || cnt[0] > cap;
    const segRead = device.createBuffer({ size: Math.max(16, n * 16), usage: G.COPY_DST | G.MAP_READ });
    const metaRead = device.createBuffer({ size: Math.max(4, n * 4), usage: G.COPY_DST | G.MAP_READ });
    enc = device.createCommandEncoder();
    if (n) { enc.copyBufferToBuffer(segBuf, 0, segRead, 0, n * 16); enc.copyBufferToBuffer(metaBuf, 0, metaRead, 0, n * 4); }
    device.queue.submit([enc.finish()]);
    await Promise.all([segRead.mapAsync(GPUMapMode.READ), metaRead.mapAsync(GPUMapMode.READ)]);
    const segs = new Float32Array(segRead.getMappedRange().slice(0, n * 16));
    const meta = new Uint32Array(metaRead.getMappedRange().slice(0, n * 4));
    segRead.unmap(); metaRead.unmap();
    [triBuf, paramBuf, segBuf, metaBuf, cntBuf, cntRead, segRead, metaRead].forEach((b) => { try { b.destroy(); } catch (e) { /* пусто */ } });
    const layer = new Uint32Array(n), flag = new Uint8Array(n);
    for (let i = 0; i < n; i++) { layer[i] = meta[i] >>> 1; flag[i] = meta[i] & 1; }
    // Площадь свесов — на процессоре: O(треугольники), без буферов.
    const oh = overhangAreas(positions);
    return { segs, layer, flag, count: n, overflow, overhangArea: oh.overhangArea, downArea: oh.downArea };
  }

  function overhangAreas(positions, cosLimit) {
    cosLimit = cosLimit === undefined ? COS_LIMIT : cosLimit;
    let overhangArea = 0, downArea = 0;
    for (let b = 0; b < positions.length; b += 9) {
      const ax = positions[b], ay = positions[b + 1], az = positions[b + 2];
      const ux = positions[b + 3] - ax, uy = positions[b + 4] - ay, uz = positions[b + 5] - az;
      const vx = positions[b + 6] - ax, vy = positions[b + 7] - ay, vz = positions[b + 8] - az;
      const nx = uy * vz - uz * vy, ny = uz * vx - ux * vz, nz = ux * vy - uy * vx;
      const len = Math.hypot(nx, ny, nz);
      if (len > 0 && nz / len < -cosLimit) {
        downArea += len / 2;
        if (Math.min(az, positions[b + 5], positions[b + 8]) > 1e-3) overhangArea += len / 2;
      }
    }
    return { overhangArea, downArea };
  }

  /* ================================================== сшивка контуров */
  function groupByLayer(res, count) {
    const per = new Uint32Array(count + 1);
    for (let i = 0; i < res.count; i++) per[res.layer[i] + 1]++;
    for (let i = 1; i <= count; i++) per[i] += per[i - 1];
    const order = new Uint32Array(res.count);
    const cursor = per.slice(0, count);
    for (let i = 0; i < res.count; i++) order[cursor[res.layer[i]]++] = i;
    return { start: per, order };
  }

  function key(x, y) { return (Math.round(x * QUANT) + 524288) * 1048576 + (Math.round(y * QUANT) + 524288); }

  /* Отрезки одного слоя → замкнутые контуры. Сначала по направлению
     (конец → начало следующего), при разрыве — в обратную сторону. */
  function chain(segs, idx) {
    const n = idx ? idx.length : segs.length / 4;
    const at = (k) => (idx ? idx[k] : k) * 4;
    const starts = new Map(), ends = new Map();
    const push = (map, k, i) => { const a = map.get(k); if (a) a.push(i); else map.set(k, [i]); };
    for (let k = 0; k < n; k++) { const o = at(k); push(starts, key(segs[o], segs[o + 1]), k); push(ends, key(segs[o + 2], segs[o + 3]), k); }
    const used = new Uint8Array(n);
    const take = (map, k) => {
      const a = map.get(k);
      if (!a) return -1;
      while (a.length) { const i = a.pop(); if (!used[i]) return i; }
      return -1;
    };
    const near = (map, x, y) => {
      const i = take(map, key(x, y));
      if (i >= 0) return i;
      for (let dx = -1; dx <= 1; dx++) for (let dy = -1; dy <= 1; dy++) {
        if (!dx && !dy) continue;
        const j = take(map, key(x + dx / QUANT, y + dy / QUANT));
        if (j >= 0) return j;
      }
      return -1;
    };
    const loops = [];
    let open = 0;
    for (let s = 0; s < n; s++) {
      if (used[s]) continue;
      used[s] = 1;
      let o = at(s);
      const pts = [segs[o], segs[o + 1], segs[o + 2], segs[o + 3]];
      const x0 = pts[0], y0 = pts[1];
      let cx = pts[2], cy = pts[3];
      let closed = false;
      for (let guard = 0; guard <= n; guard++) {
        if (Math.abs(cx - x0) * QUANT < 1.5 && Math.abs(cy - y0) * QUANT < 1.5 && pts.length >= 6) { closed = true; break; }
        let i = near(starts, cx, cy), rev = false;
        if (i < 0) { i = near(ends, cx, cy); rev = true; }
        if (i < 0) break;
        used[i] = 1;
        o = at(i);
        if (rev) { cx = segs[o]; cy = segs[o + 1]; } else { cx = segs[o + 2]; cy = segs[o + 3]; }
        pts.push(cx, cy);
      }
      if (closed) { pts.length -= 2; loops.push(Float64Array.from(pts)); } else { open++; if (pts.length >= 6) loops.push(Float64Array.from(pts)); }
    }
    return { loops, open };
  }

  function loopArea(p) {
    let a = 0;
    for (let i = 0, n = p.length; i < n; i += 2) { const j = (i + 2) % n; a += p[i] * p[j + 1] - p[j] * p[i + 1]; }
    return a / 2;
  }
  function loopLength(p) {
    let l = 0;
    for (let i = 0, n = p.length; i < n; i += 2) { const j = (i + 2) % n; l += Math.hypot(p[j] - p[i], p[j + 1] - p[i + 1]); }
    return l;
  }

  /* ============================================================ анализ */
  function analyze(model, pl, res, settings) {
    settings = settings || {};
    const extW = num(settings.extrusion_width_mm, 0.45) || 0.45;
    const walls = Math.max(1, Math.round(num(settings.walls, 3)));
    const infill = Math.min(100, Math.max(0, num(settings.infill_percent, 15))) / 100;
    const density = num(settings.density_g_cm3, 1.24) || 1.24;
    const topN = Math.max(1, Math.round(num(settings.top_solid_layers, 4)));
    const botN = Math.max(1, Math.round(num(settings.bottom_solid_layers, 3)));
    const g = groupByLayer(res, pl.count);
    const layers = [];
    let negative = 0, openTotal = 0, volume = 0;
    for (let i = 0; i < pl.count; i++) {
      const idx = g.order.subarray(g.start[i], g.start[i + 1]);
      let over = 0;
      for (let k = 0; k < idx.length; k++) over += res.flag[idx[k]];
      const c = chain(res.segs, idx);
      let area = 0, perimeter = 0, outer = 0, holes = 0;
      for (const lp of c.loops) { const a = loopArea(lp); area += a; perimeter += loopLength(lp); if (a > 0) outer++; else if (a < 0) holes++; }
      if (area < 0) negative++;
      area = Math.abs(area);
      openTotal += c.open;
      const meanWidth = perimeter > 0 ? 2 * area / perimeter : 0;
      volume += area * pl.h[i];
      layers.push({ i: i + 1, z: +pl.top[i].toFixed(3), h: +pl.h[i].toFixed(3), area, perimeter, outer, holes, open: c.open, meanWidth, thin: area > 0 && meanWidth < 2 * extW, overhang: over, segs: idx.length, loops: c.loops });
    }
    if (negative > layers.length / 2) { for (const l of layers) { const o = l.outer; l.outer = l.holes; l.holes = o; } }
    // оценка массы: стенки + сплошные + заполнение
    let wallsV = 0, solidV = 0;
    for (let i = 0; i < layers.length; i++) {
      const l = layers[i];
      const w = Math.min(l.area, l.perimeter * walls * extW);
      wallsV += w * l.h;
      const prev = i > 0 ? layers[i - 1].area : 0, next = i + 1 < layers.length ? layers[i + 1].area : 0;
      let solid = 0;
      if (i < botN || i >= layers.length - topN) solid = l.area;
      else solid = Math.max(0, l.area - next) + Math.max(0, l.area - prev);
      solidV += Math.min(Math.max(0, l.area - w), solid) * l.h;
    }
    const innerV = Math.max(0, volume - wallsV - solidV);
    const massFull = volume / 1000 * density;
    const massEst = (wallsV + solidV + innerV * infill) / 1000 * density;
    // предупреждения
    const warnings = [];
    const size = model.bbox.size;
    const bed = settings.bed_mm || [256, 256];
    const maxZ = num(settings.max_print_height_mm, 256) || 256;
    const fits = size[0] <= bed[0] + 1e-6 && size[1] <= bed[1] + 1e-6 && size[2] <= maxZ + 1e-6;
    if (!fits) warnings.push({ kind: 'bad', text: `Модель ${fmt(size[0])} × ${fmt(size[1])} × ${fmt(size[2])} мм не влезает: стол ${fmt(bed[0], 0)} × ${fmt(bed[1], 0)} мм, высота до ${fmt(maxZ, 0)} мм` });
    if (res.overflow) warnings.push({ kind: 'warn', text: 'Отрезков больше предела буфера — картинка верхних слоёв может быть неполной; нарезке это не мешает' });
    if (openTotal) warnings.push({ kind: 'warn', text: `Контуры не сомкнулись в ${openTotal} местах: сетка рваная — нарезка предупредит, деталь может выйти с пропусками` });
    if (negative > layers.length / 2) warnings.push({ kind: 'warn', text: 'Нормали сетки вывернуты внутрь — движок распознает, но лучше пересохранить модель' });
    const thin = layers.filter((l) => l.thin);
    if (thin.length) {
      const first = thin[0], last = thin[thin.length - 1];
      const minW = Math.min.apply(null, thin.map((l) => l.meanWidth));
      warnings.push({ kind: 'warn', text: `Тонкие места: ${thin.length} ${plural(thin.length, 'слой', 'слоя', 'слоёв')} (Z ${fmt(first.z, 2)}–${fmt(last.z, 2)} мм) — средняя толщина сечения ${fmt(minW, 2)} мм, меньше двух периметров ${fmt(2 * extW, 2)} мм` });
    }
    const overLayers = layers.filter((l) => l.overhang > 0);
    if (res.overhangArea > 20 || (res.downArea > 0 && res.overhangArea / res.downArea > 0.05 && res.overhangArea > 5)) {
      warnings.push({ kind: 'warn', text: `Свесы круче ${OVERHANG_DEG}°: ${fmt(res.overhangArea, 0)} мм² на ${overLayers.length} ${plural(overLayers.length, 'слое', 'слоях', 'слоях')}${overLayers.length ? ` (первый — Z ${fmt(overLayers[0].z, 2)} мм)` : ''} — включите поддержки или поверните модель` });
    }
    const base = layers[0] ? layers[0].area : 0;
    const maxArea = layers.reduce((m, l) => Math.max(m, l.area), 0);
    if (base > 0 && size[2] > 20 && base < 30) warnings.push({ kind: 'warn', text: `Пятно контакта ${fmt(base, 0)} мм² при высоте ${fmt(size[2], 0)} мм — деталь может оторваться; включите кайму` });
    else if (base > 0 && maxArea > 0 && base / maxArea < 0.3 && size[2] > 10) warnings.push({ kind: 'info', text: `Дно — ${fmt(base / maxArea * 100, 0)} % от самого широкого сечения: модель стоит на узком основании, подумайте о повороте` });
    if (layers[0] && layers[0].outer > 1) warnings.push({ kind: 'info', text: `На первом слое ${layers[0].outer} ${plural(layers[0].outer, 'остров', 'острова', 'островов')}: печатаются как отдельные детали` });
    return {
      layers, count: layers.length, height: size[2], size, fits,
      volume_mm3: volume, volume_cm3: volume / 1000, massFull, massEst, wallsV, solidV, innerV,
      overhangArea: res.overhangArea, downArea: res.downArea, openLoops: openTotal, inverted: negative > layers.length / 2,
      segments: res.count, overflow: res.overflow, warnings, extW, walls, infill, density,
      groups: g, res,
    };
  }
  function plural(n, one, few, many) { n = Math.abs(n) % 100; const d = n % 10; if (n > 10 && n < 20) return many; if (d > 1 && d < 5) return few; if (d === 1) return one; return many; }

  /* Модель на стол: низ в z = 0, центр XY в нуле (как place_mesh). */
  function place(model) {
    const p = model.positions, mn = model.bbox.min, mx = model.bbox.max;
    const dx = (mn[0] + mx[0]) / 2, dy = (mn[1] + mx[1]) / 2, dz = mn[2];
    const out = new Float32Array(p.length);
    for (let i = 0; i < p.length; i += 3) { out[i] = p[i] - dx; out[i + 1] = p[i + 1] - dy; out[i + 2] = p[i + 2] - dz; }
    return out;
  }

  /* ========================================================== конвейер */
  function backendPref() { try { return localStorage.getItem('pf_gpuslice_backend') || 'auto'; } catch (e) { return 'auto'; } }
  function setBackendPref(v) { try { localStorage.setItem('pf_gpuslice_backend', v); } catch (e) { /* пусто */ } }

  async function run(buffer, opts) {
    opts = opts || {};
    const t0 = (typeof performance !== 'undefined' ? performance.now() : Date.now());
    const model = parseSTL(buffer);
    const settings = opts.settings || {};
    const placed = place(model);
    const pl = planes(model.bbox.size[2], settings.first_layer_height, settings.layer_height);
    if (pl.count > 20000) throw new Error(`Слишком много слоёв (${pl.count}): проверьте высоту слоя`);
    const t1 = (typeof performance !== 'undefined' ? performance.now() : Date.now());
    let backend = 'cpu', res = null, note = '';
    const pref = opts.backend || backendPref();
    if (pref !== 'cpu') {
      try {
        const device = await gpuDevice();
        if (device) { res = await sliceGPU(device, placed, pl); backend = 'gpu'; }
        else if (pref === 'gpu') note = 'WebGPU в этом браузере недоступен — посчитано на процессоре';
      } catch (err) { note = `WebGPU отказал (${err && err.message ? err.message : err}) — посчитано на процессоре`; }
    }
    if (!res) {
      if (typeof Worker !== 'undefined' && typeof Blob !== 'undefined' && !opts.inline) {
        try { res = await sliceInWorker(placed, pl); backend = 'cpu-worker'; } catch (e) { res = null; }
      }
      if (!res) { res = sliceCPU(placed, pl.first, pl.step, pl.count); backend = 'cpu'; }
    }
    const t2 = (typeof performance !== 'undefined' ? performance.now() : Date.now());
    const out = analyze(model, pl, res, settings);
    const t3 = (typeof performance !== 'undefined' ? performance.now() : Date.now());
    out.model = { triangles: model.triangles, ascii: model.ascii, bbox: model.bbox };
    out.backend = backend; out.note = note;
    out.timing = { parse: t1 - t0, slice: t2 - t1, analyze: t3 - t2, total: t3 - t0 };
    return out;
  }

  /* ============================================================ виджет */
  function template() {
    return '<div class="gs">'
      + '<div class="gs-head"><div><b>GPU-сечения</b> <span class="gs-badge" data-gs-badge>…</span> <span class="gs-sub" data-gs-sub></span></div>'
      + '<div class="gs-tools"><label class="gs-pref" title="Где считать сечения">считать: <select data-gs-backend><option value="auto">авто (GPU, иначе процессор)</option><option value="gpu">только GPU</option><option value="cpu">процессор</option></select></label>'
      + '<button type="button" class="icon-btn sm" data-gs-help aria-label="Как читать GPU-сечения" title="Как читать">?</button></div></div>'
      + `<div class="gs-help hidden" data-gs-help-box>${esc(HELP)}</div>`
      + '<div class="gs-state" data-gs-state></div>'
      + '<div class="gs-body hidden" data-gs-body>'
      + '<div class="gs-main"><canvas class="gs-canvas" data-gs-canvas width="560" height="380"></canvas>'
      + '<div class="gs-side" data-gs-side></div></div>'
      + '<div class="gs-slider"><input type="range" min="1" max="1" value="1" data-gs-range aria-label="Слой"><span class="gs-pos" data-gs-pos></span></div>'
      + '<canvas class="gs-strip" data-gs-strip height="34"></canvas>'
      + '<div class="gs-stats" data-gs-stats></div>'
      + '<ul class="gs-warn" data-gs-warn></ul>'
      + '</div></div>';
  }

  function drawLayer(canvas, layer, prev, bbox) {
    if (!canvas || !canvas.getContext) return;
    const ctx = canvas.getContext('2d');
    const W = canvas.width, H = canvas.height;
    ctx.clearRect(0, 0, W, H);
    const sx = bbox.size[0] || 1, sy = bbox.size[1] || 1;
    const pad = 16;
    const scale = Math.min((W - pad * 2) / sx, (H - pad * 2) / sy);
    const ox = W / 2, oy = H / 2;
    const X = (x) => ox + x * scale, Y = (y) => oy - y * scale;
    // стол-сетка
    ctx.strokeStyle = 'rgba(148,163,184,.18)'; ctx.lineWidth = 1;
    const stepMm = scale > 12 ? 5 : scale > 4 ? 10 : 50;
    for (let x = -Math.ceil(sx / 2 / stepMm) * stepMm; x <= sx / 2; x += stepMm) { ctx.beginPath(); ctx.moveTo(X(x), 0); ctx.lineTo(X(x), H); ctx.stroke(); }
    for (let y = -Math.ceil(sy / 2 / stepMm) * stepMm; y <= sy / 2; y += stepMm) { ctx.beginPath(); ctx.moveTo(0, Y(y)); ctx.lineTo(W, Y(y)); ctx.stroke(); }
    const path = (loops) => { const p = new Path2D(); for (const lp of loops) { if (lp.length < 6) continue; p.moveTo(X(lp[0]), Y(lp[1])); for (let i = 2; i < lp.length; i += 2) p.lineTo(X(lp[i]), Y(lp[i + 1])); p.closePath(); } return p; };
    if (prev && prev.loops.length) { ctx.fillStyle = 'rgba(148,163,184,.22)'; ctx.fill(path(prev.loops), 'evenodd'); }
    if (layer && layer.loops.length) {
      const p = path(layer.loops);
      ctx.fillStyle = layer.thin ? 'rgba(251,191,36,.45)' : 'rgba(99,102,241,.35)';
      ctx.fill(p, 'evenodd');
      ctx.strokeStyle = layer.thin ? '#f59e0b' : '#6366f1'; ctx.lineWidth = 1.5; ctx.stroke(p);
    }
    if (layer && layer.overhangSegs && layer.overhangSegs.length) {
      ctx.strokeStyle = '#ec4899'; ctx.lineWidth = 2.5; ctx.beginPath();
      const s = layer.overhangSegs;
      for (let i = 0; i < s.length; i += 4) { ctx.moveTo(X(s[i]), Y(s[i + 1])); ctx.lineTo(X(s[i + 2]), Y(s[i + 3])); }
      ctx.stroke();
    }
    ctx.fillStyle = 'rgba(100,116,139,.9)'; ctx.font = '11px system-ui, sans-serif';
    ctx.fillText(`${stepMm} мм`, 6, H - 6);
  }

  function drawStrip(canvas, layers, selected) {
    if (!canvas || !canvas.getContext) return;
    const W = canvas.width = Math.max(200, canvas.clientWidth || canvas.width), H = canvas.height;
    const ctx = canvas.getContext('2d');
    ctx.clearRect(0, 0, W, H);
    const max = layers.reduce((m, l) => Math.max(m, l.area), 0) || 1;
    const per = W / layers.length;
    for (let i = 0; i < layers.length; i++) {
      const l = layers[i];
      const h = Math.max(1, (l.area / max) * (H - 6));
      ctx.fillStyle = l.thin ? 'rgba(245,158,11,.85)' : l.overhang ? 'rgba(236,72,153,.75)' : 'rgba(99,102,241,.45)';
      ctx.fillRect(i * per, H - h, Math.max(1, per - (per > 3 ? 1 : 0)), h);
    }
    if (selected) { ctx.fillStyle = '#f97316'; ctx.fillRect((selected - 1) * per, 0, Math.max(2, per), H); }
  }

  function mount(host, opts) {
    if (!host) return null;
    opts = opts || {};
    host.innerHTML = template();
    const q = (s) => host.querySelector(s);
    const els = { badge: q('[data-gs-badge]'), sub: q('[data-gs-sub]'), state: q('[data-gs-state]'), body: q('[data-gs-body]'), canvas: q('[data-gs-canvas]'), side: q('[data-gs-side]'), range: q('[data-gs-range]'), pos: q('[data-gs-pos]'), strip: q('[data-gs-strip]'), stats: q('[data-gs-stats]'), warn: q('[data-gs-warn]'), backend: q('[data-gs-backend]'), help: q('[data-gs-help]'), helpBox: q('[data-gs-help-box]') };
    const st = { result: null, selected: 1, cache: new Map() };
    if (els.backend) { els.backend.value = backendPref(); els.backend.addEventListener('change', () => { setBackendPref(els.backend.value); if (opts.buffer) api.load(opts.buffer, opts); }); }
    if (els.help) els.help.addEventListener('click', () => els.helpBox.classList.toggle('hidden'));
    function overhangSegsOf(index) {
      if (st.cache.has(index)) return st.cache.get(index);
      const r = st.result, res = r.res;
      const g = r.groups;
      const out = [];
      for (let k = g.start[index]; k < g.start[index + 1]; k++) { const s = g.order[k]; if (res.flag[s]) { const o = s * 4; out.push(res.segs[o], res.segs[o + 1], res.segs[o + 2], res.segs[o + 3]); } }
      st.cache.set(index, out);
      return out;
    }
    function select(n) {
      const r = st.result; if (!r) return;
      n = Math.max(1, Math.min(r.count, Math.round(n)));
      st.selected = n;
      const layer = r.layers[n - 1], prev = n > 1 ? r.layers[n - 2] : null;
      layer.overhangSegs = overhangSegsOf(n - 1);
      drawLayer(els.canvas, layer, prev, r.model.bbox);
      drawStrip(els.strip, r.layers, n);
      els.range.value = String(n);
      els.pos.textContent = `слой ${n} из ${r.count} · Z ${fmt(layer.z, 2)} мм`;
      els.side.innerHTML = `<div><span>Площадь</span><b>${fmt(layer.area, 0)} мм²</b></div>`
        + `<div><span>Периметр</span><b>${fmt(layer.perimeter, 0)} мм</b></div>`
        + `<div><span>Островов / отверстий</span><b>${layer.outer} / ${layer.holes}</b></div>`
        + `<div><span>Средняя толщина</span><b class="${layer.thin ? 'gs-bad' : ''}">${fmt(layer.meanWidth, 2)} мм${layer.thin ? ' · тонко' : ''}</b></div>`
        + (layer.overhang ? `<div><span>Свесы</span><b class="gs-over">${layer.overhang} ${plural(layer.overhang, 'грань', 'грани', 'граней')}</b></div>` : '')
        + (layer.open ? `<div><span>Разрывов контура</span><b class="gs-bad">${layer.open}</b></div>` : '');
    }
    els.range.addEventListener('input', () => select(Number(els.range.value)));
    const api = {
      async load(buffer, o) {
        o = o || {};
        opts.buffer = buffer;
        els.body.classList.add('hidden');
        els.badge.textContent = '…'; els.badge.className = 'gs-badge';
        els.state.innerHTML = `<span class="spin"></span> Режем ${esc(o.name || 'модель')} на слои…`;
        els.sub.textContent = '';
        try {
          const r = await run(buffer, { settings: o.settings, backend: els.backend ? els.backend.value : undefined });
          st.result = r; st.cache.clear();
          els.state.innerHTML = '';
          els.body.classList.remove('hidden');
          const where = r.backend === 'gpu' ? 'WebGPU' : r.backend === 'cpu-worker' ? 'процессор · фон' : 'процессор';
          els.badge.textContent = where; els.badge.className = 'gs-badge ' + (r.backend === 'gpu' ? 'gpu' : 'cpu');
          els.sub.textContent = `${fmt(r.model.triangles, 0)} треуг. · ${r.count} слоёв · ${fmt(r.segments, 0)} отрезков · ${fmt(r.timing.slice, 0)} мс сечения, ${fmt(r.timing.total, 0)} мс всего${r.note ? ' · ' + r.note : ''}`;
          els.range.max = String(r.count);
          els.stats.innerHTML = `<div><span>Размер</span><b>${fmt(r.size[0])} × ${fmt(r.size[1])} × ${fmt(r.size[2])} мм</b></div>`
            + `<div><span>Стол</span><b class="${r.fits ? 'gs-ok' : 'gs-bad'}">${r.fits ? 'влезает' : 'не влезает'}</b></div>`
            + `<div><span>Объём</span><b>${fmt(r.volume_cm3, 2)} см³</b></div>`
            + `<div><span>Пластик ≈</span><b>${fmt(r.massEst, 1)} г</b> <small>при ${fmt(r.infill * 100, 0)} % · ${r.walls} стен.; сплошная деталь ${fmt(r.massFull, 1)} г</small></div>`
            + `<div><span>Свесы > ${OVERHANG_DEG}°</span><b class="${r.overhangArea > 20 ? 'gs-over' : ''}">${fmt(r.overhangArea, 0)} мм²</b></div>`
            + `<div><span>Тонких слоёв</span><b class="${r.layers.some((l) => l.thin) ? 'gs-bad' : ''}">${r.layers.filter((l) => l.thin).length}</b></div>`;
          els.warn.innerHTML = r.warnings.length
            ? r.warnings.map((w) => `<li class="${esc(w.kind)}">${esc(w.text)}</li>`).join('')
            : '<li class="ok">Проблем не видно: сетка замкнута, стол подходит, свесов и тонких мест нет</li>';
          select(Math.max(1, Math.min(r.count, o.layer || Math.ceil(r.count / 2))));
        } catch (err) {
          els.state.innerHTML = `<span class="gs-err">${esc(err && err.message ? err.message : err)}</span>`;
          els.badge.textContent = 'ошибка'; els.badge.className = 'gs-badge bad';
        }
        return st.result;
      },
      select, state: st,
    };
    host._gpuslice = api;
    if (opts.buffer) api.load(opts.buffer, opts);
    return api;
  }

  window.PFGpuSlice = { parseSTL, planes, planeRange, planeZ, sliceCPU, sliceGPU, gpuDevice, chain, loopArea, loopLength, groupByLayer, analyze, overhangAreas, place, run, mount, WGSL, COS_LIMIT, OVERHANG_DEG, MAX_SEGMENTS, HELP, workerSource, backendPref, setBackendPref };
})();
