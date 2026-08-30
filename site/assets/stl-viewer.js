/* PrintFlow 12.5.0 — STL/3MF viewer (Three.js).
   Требует three.min.js в site/assets/. Если Three.js не загружен — показывает
   placeholder с инструкцией. Идеи 11, 12, 14, 15, 16, 17, 18 из каталога 3D. */
(() => {
'use strict';

const $ = (id) => document.getElementById(id);

/**
 * Парсит бинарный STL и возвращает геометрию.
 * @param {ArrayBuffer} buffer - бинарные данные STL
 * @returns {{ vertices: Float32Array, normals: Float32Array, triangles: number }}
 */
function parseSTL(buffer) {
  const reader = new DataView(buffer);
  // Пропускаем 80 байт заголовка
  const triangles = reader.getUint32(80, true);
  const vertices = new Float32Array(triangles * 9); // 3 вершины × 3 координаты
  const normals = new Float32Array(triangles * 9);

  let offset = 84;
  for (let i = 0; i < triangles; i++) {
    // Нормаль
    const nx = reader.getFloat32(offset, true); offset += 4;
    const ny = reader.getFloat32(offset, true); offset += 4;
    const nz = reader.getFloat32(offset, true); offset += 4;

    // 3 вершины
    for (let v = 0; v < 3; v++) {
      const idx = i * 9 + v * 3;
      vertices[idx] = reader.getFloat32(offset, true); offset += 4;
      vertices[idx + 1] = reader.getFloat32(offset, true); offset += 4;
      vertices[idx + 2] = reader.getFloat32(offset, true); offset += 4;
      normals[idx] = nx;
      normals[idx + 1] = ny;
      normals[idx + 2] = nz;
    }

    // Attribute byte count (2 байта)
    offset += 2;
  }

  return { vertices, normals, triangles };
}

/**
 * Создаёт 3D viewer с Three.js.
 * @param {HTMLElement} host - контейнер
 * @param {ArrayBuffer} stlBuffer - бинарные данные STL
 * @param {object} opts - опции { width, height, color }
 */
function createViewer(host, stlBuffer, opts = {}) {
  if (typeof THREE === 'undefined') {
    host.innerHTML = `
      <div class="stl-viewer-placeholder">
        <h3>3D viewer не загружен</h3>
        <p>Для просмотра STL/3MF моделей требуется Three.js.</p>
        <p><b>Как добавить:</b></p>
        <ol>
          <li>Скачайте <code>three.min.js</code> (~600 КБ) с <a href="https://threejs.org" target="_blank">threejs.org</a></li>
          <li>Поместите в <code>site/assets/three.min.js</code></li>
          <li>Добавьте <code>&lt;script src="/assets/three.min.js"&gt;&lt;/script&gt;</code> в HTML</li>
        </ol>
        <p>Или используйте G-code viewer (без зависимостей).</p>
      </div>
    `;
    return;
  }

  const width = opts.width || 500;
  const height = opts.height || 500;
  const color = opts.color || 0x4f46e5;

  const parsed = parseSTL(stlBuffer);

  // Создаём сцену
  const scene = new THREE.Scene();
  scene.background = new THREE.Color(0xf9fafb);

  const camera = new THREE.PerspectiveCamera(45, width / height, 0.1, 1000);
  camera.position.z = 5;

  const renderer = new THREE.WebGLRenderer({ antialias: true });
  renderer.setSize(width, height);
  host.appendChild(renderer.domElement);

  // Создаём геометрию
  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute('position', new THREE.BufferAttribute(parsed.vertices, 3));
  geometry.setAttribute('normal', new THREE.BufferAttribute(parsed.normals, 3));

  const material = new THREE.MeshPhongMaterial({ color, specular: 0x111111, shininess: 30 });
  const mesh = new THREE.Mesh(geometry, material);
  scene.add(mesh);

  // Освещение
  const light1 = new THREE.DirectionalLight(0xffffff, 0.8);
  light1.position.set(1, 1, 1);
  scene.add(light1);
  const light2 = new THREE.DirectionalLight(0xffffff, 0.4);
  light2.position.set(-1, -1, -1);
  scene.add(light2);
  scene.add(new THREE.AmbientLight(0x404040));

  // Центрируем модель
  geometry.computeBoundingBox();
  const box = geometry.boundingBox;
  const center = new THREE.Vector3();
  box.getCenter(center);
  mesh.position.sub(center);

  // Масштабируем чтобы влезла
  const size = new THREE.Vector3();
  box.getSize(size);
  const maxDim = Math.max(size.x, size.y, size.z);
  const scale = 2 / maxDim;
  mesh.scale.set(scale, scale, scale);

  // Управление мышью (простое вращение)
  let isDragging = false;
  let prevX = 0, prevY = 0;

  renderer.domElement.onmousedown = (e) => {
    isDragging = true;
    prevX = e.clientX;
    prevY = e.clientY;
  };

  renderer.domElement.onmousemove = (e) => {
    if (!isDragging) return;
    const dx = e.clientX - prevX;
    const dy = e.clientY - prevY;
    mesh.rotation.y += dx * 0.01;
    mesh.rotation.x += dy * 0.01;
    prevX = e.clientX;
    prevY = e.clientY;
  };

  renderer.domElement.onmouseup = () => { isDragging = false; };
  renderer.domElement.onmouseleave = () => { isDragging = false; };

  // Зум колесом
  renderer.domElement.onwheel = (e) => {
    e.preventDefault();
    camera.position.z += e.deltaY * 0.01;
    camera.position.z = Math.max(1, Math.min(10, camera.position.z));
  };

  // Анимация
  function animate() {
    requestAnimationFrame(animate);
    renderer.render(scene, camera);
  }
  animate();

  // Статистика
  const stats = document.createElement('div');
  stats.className = 'stl-stats';
  stats.innerHTML = `
    <span>Треугольников: ${parsed.triangles}</span>
    <span>Размер: ${size.x.toFixed(1)} × ${size.y.toFixed(1)} × ${size.z.toFixed(1)} мм</span>
    <span>Объём: ~${(size.x * size.y * size.z * 0.5).toFixed(1)} см³</span>
  `;
  host.appendChild(stats);
}

// Экспорт
if (typeof window !== 'undefined') {
  window.STLViewer = { parse: parseSTL, create: createViewer };
}

})();
