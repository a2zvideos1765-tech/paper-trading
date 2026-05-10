// Theme toggle: light/dark with localStorage persistence.
// Default = system preference. Other scripts (charts.js) listen for
// 'themechange' events on window so they can repaint themselves.
//
// Inlined into <head> as a regular script (NOT defer) so it runs before
// the body paints — avoids the dreaded "white flash before dark mode".

(function () {
  function readPreferred() {
    const saved = localStorage.getItem('theme');
    if (saved === 'light' || saved === 'dark') return saved;
    try {
      return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
    } catch (e) {
      return 'light';
    }
  }

  function apply(theme) {
    document.documentElement.setAttribute('data-theme', theme);
  }

  apply(readPreferred());

  window.toggleTheme = function () {
    const next = document.documentElement.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
    apply(next);
    localStorage.setItem('theme', next);
    window.dispatchEvent(new CustomEvent('themechange', { detail: next }));
  };

  // Re-broadcast when the system preference changes (only if the user hasn't
  // explicitly chosen — i.e. localStorage is empty). This keeps the dashboard
  // in sync if you flip your OS theme mid-session.
  if (window.matchMedia) {
    window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', e => {
      if (!localStorage.getItem('theme')) {
        const t = e.matches ? 'dark' : 'light';
        apply(t);
        window.dispatchEvent(new CustomEvent('themechange', { detail: t }));
      }
    });
  }
})();
