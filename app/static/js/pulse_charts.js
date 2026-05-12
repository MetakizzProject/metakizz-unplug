/* Pulse — Chart.js dark theme + helpers shared by every page.
   Loaded after Chart.js (which admin_base.html already provides).
   Pages call PulseCharts.setup() once on DOMContentLoaded.        */

window.PulseCharts = (function () {
  const COLORS = {
    accent:   '#2EDB99',
    burning:  '#DC2626',
    hot:      '#F97316',
    warm:     '#FFC857',
    cool:     '#60A5FA',
    cold:     '#6B7280',
    customer: '#A78BFA',
    fg:       '#E8F0EC',
    muted:    '#9CA3AF',
    grid:     'rgba(46, 219, 153, 0.10)',
  };

  function setup() {
    if (!window.Chart) return;
    Chart.defaults.color = COLORS.muted;
    Chart.defaults.font.family = 'Share Tech Mono, monospace';
    Chart.defaults.font.size = 11;
    Chart.defaults.borderColor = COLORS.grid;
    Chart.defaults.scale.grid.color = COLORS.grid;
    Chart.defaults.scale.ticks.color = COLORS.muted;
    Chart.defaults.plugins.tooltip.backgroundColor = 'rgba(0,0,0,0.92)';
    Chart.defaults.plugins.tooltip.titleColor = COLORS.accent;
    Chart.defaults.plugins.tooltip.bodyColor = COLORS.fg;
    Chart.defaults.plugins.tooltip.borderColor = COLORS.grid;
    Chart.defaults.plugins.tooltip.borderWidth = 1;
  }

  function formatEur(cents) {
    const eur = (cents || 0) / 100;
    return '€' + eur.toLocaleString('es-ES', { maximumFractionDigits: 0 });
  }

  function formatInt(n) {
    return (n || 0).toLocaleString('es-ES');
  }

  function deltaLabel(value, prev) {
    if (prev === null || prev === undefined) return '';
    const diff = (value || 0) - prev;
    const sign = diff >= 0 ? '+' : '';
    return sign + formatInt(diff);
  }

  return { COLORS, setup, formatEur, formatInt, deltaLabel };
})();

if (document.readyState !== 'loading') {
  PulseCharts.setup();
} else {
  document.addEventListener('DOMContentLoaded', PulseCharts.setup);
}
