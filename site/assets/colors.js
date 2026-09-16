/* Многоцветные катушки (18.5, М1): единый свотч для всех экранов, где цвет
   катушки — рабочий сигнал (не перепутать бобины): склад, слоты AMS,
   этикетка, карточка катушки, чипы вариаций. Один цвет — как и раньше;
   несколько — градиент по мотку: для «радуги» конический (секторы), для
   остальных линейный.
   Подключается до страничных скриптов на каждой странице отдельно —
   сборщика в проекте нет, и это осознанное правило. */
window.PFSpoolColor = (() => {
  function colorsOf(s) {
    if (!s) return [];
    if (Array.isArray(s.colors)) return s.colors.filter(Boolean);
    const raw = s.colors_json || '';
    if (!raw) return [];
    try {
      const arr = JSON.parse(raw);
      return Array.isArray(arr) ? arr.map(String).filter(Boolean) : [];
    } catch (e) {
      return String(raw).split(',').map((c) => c.trim()).filter(Boolean);
    }
  }

  function swatchCss(s, fallback) {
    const colors = colorsOf(s);
    if (colors.length > 1) {
      const step = 100 / (colors.length - 1);
      const stops = colors
        .map((c, i) => c + ' ' + Math.round(i * step) + '%')
        .join(', ');
      const fn = s.color_kind === 'rainbow' ? 'conic' : 'linear';
      return fn + '-gradient(135deg, ' + stops + ')';
    }
    return (s && s.color_hex) || fallback || '#8a94a6';
  }

  function kindLabel(kind) {
    return ({ gradient: 'градиент', rainbow: 'радуга', silk: 'шёлк-2' })[kind] || '';
  }

  return { colorsOf, swatchCss, kindLabel };
})();
