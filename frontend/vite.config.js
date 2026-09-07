// PrintFlow 17.0 — конфиг сборки.
// Точка входа — src/index.js: регистрирует Lit-компоненты панели как
// кастомные элементы (<pf-…>). Собранный файл кладётся в site/assets/dist,
// который раздаётся коннектором как статика и коммитится в репозиторий.
import { defineConfig } from 'vite';
import { resolve } from 'path';

export default defineConfig({
  base: '/assets/dist/',
  build: {
    outDir: resolve(__dirname, '../site/assets/dist'),
    emptyOutDir: true,
    target: 'es2020',
    minify: 'terser',
    assetsDir: '.',
    entryFileNames: 'printflow-app.js',
    chunkFileNames: 'chunks/[name].js',
    assetFileNames: '[name][extname]',
    rollupOptions: {
      input: {
        app: resolve(__dirname, 'src/index.js'),
      },
      output: {
        entryFileNames: 'printflow-app.js',
        chunkFileNames: 'chunks/[name].js',
        assetFileNames: '[name][extname]',
      },
    },
  },
});
