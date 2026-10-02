/* PrintFlow 17.0 — единый бутстрап темы для ВСЕХ страниц.
   Вставляется инлайн в <head> до отрисовки (см. site/_theme_head.html /
   копия в каждой странице), чтобы не было вспышки светлой темы.
   После утверждения D01 по умолчанию СВЕТЛАЯ; переключатель
   «система/светлая/тёмная» действует на всех страницах.

   Источники выбора:
     1. localStorage['pf_theme']  — явный выбор пользователя (system/light/dark),
        его пишет панель (core.js) и тумблеры на страницах;
     2. иначе — светлая (D01).
   Аттрибуты на <html>: data-theme (light|dark), data-accent, data-density. */
(function () {
  try {
    var pref = 'light';
    try {
      var saved = localStorage.getItem('pf_theme');
      if (saved === 'system' || saved === 'light' || saved === 'dark') pref = saved;
    } catch (e) { /* приватный режим — остаётся светлая */ }
    var dark = pref === 'dark' ||
      (pref === 'system' && window.matchMedia &&
        window.matchMedia('(prefers-color-scheme: dark)').matches) ||
      (pref === 'system' && !(window.matchMedia)); /* нет matchMedia — тёмная */
    var root = document.documentElement;
    root.dataset.theme = dark ? 'dark' : 'light';
    var accent = 'violet';
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
