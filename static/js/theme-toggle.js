/* ═══ Theme Toggle — QuantumBrain v1.0 ═══ */
(function() {
  'use strict';

  const THEME_KEY = 'grinch_theme';
  const DEFAULT_THEME = 'dark';

  function getSavedTheme() {
    try {
      return localStorage.getItem(THEME_KEY) || DEFAULT_THEME;
    } catch (e) {
      return DEFAULT_THEME;
    }
  }

  function setTheme(theme) {
    document.body.setAttribute('data-theme', theme);
    try {
      localStorage.setItem(THEME_KEY, theme);
    } catch (e) {}
    // Notify AI perf tracker
    if (window.AI_PERF) {
      window.AI_PERF.theme = theme;
    }
  }

  function toggleTheme() {
    const current = document.body.getAttribute('data-theme') || DEFAULT_THEME;
    const next = current === 'dark' ? 'light' : 'dark';
    setTheme(next);
    // Dispatch custom event for other components
    document.dispatchEvent(new CustomEvent('themechange', { detail: { theme: next } }));
  }

  // Expose globally
  window.toggleTheme = toggleTheme;
  window.setTheme = setTheme;

  // Apply saved theme on load
  if (document.readyState !== 'loading') {
    setTheme(getSavedTheme());
  } else {
    document.addEventListener('DOMContentLoaded', () => setTheme(getSavedTheme()));
  }

  console.log('[Theme] QuantumBrain Theme Toggle initialized');
})();
