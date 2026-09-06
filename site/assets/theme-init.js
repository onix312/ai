/* PrintFlow 17.0 — единый бутстрап темы для ВСЕХ страниц.
   Вставляется инлайн в <head> до отрисовки (см. site/_theme_head.html /
   копия в каждой странице), чтобы не было вспышки светлой темы.
   Решение заказчика (трекер №1): по умолчанию ТЁМНАЯ; переключатель
   «система/светлая/тёмная» действует на всех страницах.

   Источники выбора:
     1. localStorage['pf_theme']  — явный выбор пользователя (system/light/dark),
        его пишет панель (core.js) и тумблеры на страницах;
     2. иначе — тёмная (дефолт 17.0).
   Аттрибуты на <html>: data-theme (light|dark), data-accent, data-density. */
(function () {
  try {
    var pref = 'dark';
    try {
      var saved = localStorage.getItem('pf_theme');
      if (saved === 'system' || saved === 'light' || saved === 'dark') pref = saved;
    } catch (e) { /* приватный режим — остаётся тёмная */ }
    var dark = pref === 'dark' ||
      (pref === 'system' && window.matchMedia &&
        window.matchMedia('(prefers-color-scheme: dark)').matches) ||
      (pref === 'system' && !(window.matchMedia)); /* нет matchMedia — тёмная */
    var root = document.documentElement;
    root.dataset.theme = dark ? 'dark' : 'light';
    var accent = 'indigo';
    try {
      var a = localStorage.getItem('pf_accent');
      if (a) accent = a;
    } catch (e) {}
    root.dataset.accent = accent;
    var density = '';
    try { density = localStorage.getItem('pf_density') || ''; } catch (e) {}
    if (density) root.dataset.density = density;
    root.dataset.pfThemePref = pref;
  } catch (e) { /* бутстрап не должен ломать страницу */ }
})();
