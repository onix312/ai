/* PrintFlow 13.1 — Web Worker для парсинга STL (идея 47 З2).
   Тяжёлый разбор больших моделей уходит из основного потока: интерфейс
   не подвисает, пока считаются миллионы треугольников.
   Файл подключается как new Worker('stl-worker.js') и обрабатывает
   {buffer} → {vertices, normals, triangles} (копия parseSTL из stl-viewer.js —
   без сборщиков, синтаксис проверяется scripts/check.py). */
'use strict';

function parseSTL(buffer) {
  const reader = new DataView(buffer);
  const triangles = reader.getUint32(80, true);
  const vertices = new Float32Array(triangles * 9);
  const normals = new Float32Array(triangles * 9);
  let offset = 84;
  for (let i = 0; i < triangles; i++) {
    const nx = reader.getFloat32(offset, true); offset += 4;
    const ny = reader.getFloat32(offset, true); offset += 4;
    const nz = reader.getFloat32(offset, true); offset += 4;
    for (let v = 0; v < 3; v++) {
      const idx = i * 9 + v * 3;
      vertices[idx] = reader.getFloat32(offset, true); offset += 4;
      vertices[idx + 1] = reader.getFloat32(offset, true); offset += 4;
      vertices[idx + 2] = reader.getFloat32(offset, true); offset += 4;
      normals[idx] = nx;
      normals[idx + 1] = ny;
      normals[idx + 2] = nz;
    }
    offset += 2;
  }
  return { vertices, normals, triangles };
}

self.onmessage = (e) => {
  try {
    const result = parseSTL(e.data.buffer);
    // Float32Array передаётся по структурированному клону — для миллионов
    // вершин это дольше transferable, поэтому отдаём буферы по ссылке.
    self.postMessage({
      ok: true,
      vertices: result.vertices.buffer,
      normals: result.normals.buffer,
      triangles: result.triangles,
    }, [result.vertices.buffer, result.normals.buffer]);
  } catch (error) {
    self.postMessage({ ok: false, error: String(error && error.message || error) });
  }
};
