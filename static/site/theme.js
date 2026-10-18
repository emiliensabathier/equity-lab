/* GENERATED from emiliensabathier/site src/site/theme.js. Do not edit. */
// Loaded synchronously in <head>: applies the saved theme before first paint, then wires the
// nav button (hidden until this script runs, so it never shows without JavaScript).
// The choice is a cookie shared by emiliensabathier.fr and its subdomains (Terminal, analyse.).
(function () {
  var root = document.documentElement;
  var saved = /(?:^|;\s*)es-theme=(light|dark)(?:;|$)/.exec(document.cookie || '');
  if (saved) root.setAttribute('data-theme', saved[1]);

  var PATH = '; Path=/';
  function suffix() {
    var s = '; SameSite=Lax';
    if (/(^|\.)emiliensabathier\.fr$/.test(location.hostname)) s += '; Domain=.emiliensabathier.fr';
    if (location.protocol === 'https:') s += '; Secure';
    return s;
  }
  function current() {
    return root.getAttribute('data-theme') || 'system';
  }

  function setup() {
    var button = document.querySelector('[data-theme-toggle]');
    if (!button) return;
    function paint() {
      button.textContent = button.getAttribute('data-label-' + current());
    }
    button.hidden = false;
    paint();
    button.addEventListener('click', function () {
      var next = { system: 'light', light: 'dark', dark: 'system' }[current()];
      if (next === 'system') {
        root.removeAttribute('data-theme');
        document.cookie = 'es-theme=' + PATH + '; Max-Age=0' + suffix();
      } else {
        root.setAttribute('data-theme', next);
        document.cookie = 'es-theme=' + next + PATH + '; Max-Age=31536000' + suffix();
      }
      // The Terminal (/terminal/, same origin) reads this key before its first paint.
      try { localStorage.setItem('worldmonitor-theme', next === 'system' ? 'auto' : next); } catch (e) { /* storage blocked */ }
      paint();
    });
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', setup);
  else setup();
})();
