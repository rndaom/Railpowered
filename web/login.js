/*
  Railpowered sign-in page.

  Served as /static/login.js (the CSP allows scripts from 'self' only). It is
  loaded from <head> so the saved colour theme applies before first paint; the
  rest waits for the DOM. The server redirects failed sign-ins back here with
  ?error=1 (wrong key) or ?error=rate (too many attempts), and renders a short
  message into #login-error so it shows without JavaScript; this script swaps in
  the fuller wording below. The dashboard sends people here with ?error=expired
  when their session ends.
*/
(function () {
  'use strict';

  var THEME_KEY = 'railpowered.theme';
  var THEME_COLORS = { light: '#f3f4f6', dark: '#090c10' };

  function readTheme() {
    try {
      var v = window.localStorage.getItem(THEME_KEY);
      return v === 'light' || v === 'dark' ? v : 'system';
    } catch (_) {
      return 'system';
    }
  }

  function saveTheme(choice) {
    try {
      if (choice === 'system') window.localStorage.removeItem(THEME_KEY);
      else window.localStorage.setItem(THEME_KEY, choice);
    } catch (_) { /* storage blocked: the choice lasts for this page only */ }
  }

  function applyTheme(choice) {
    var root = document.documentElement;
    if (choice === 'light' || choice === 'dark') root.setAttribute('data-theme', choice);
    else root.removeAttribute('data-theme');
    var metas = document.querySelectorAll('meta[name="theme-color"]');
    for (var i = 0; i < metas.length; i++) {
      var media = metas[i].getAttribute('media') || '';
      var mode = choice === 'system' ? (media.indexOf('dark') !== -1 ? 'dark' : 'light') : choice;
      metas[i].setAttribute('content', THEME_COLORS[mode]);
    }
  }

  applyTheme(readTheme());

  var MESSAGES = {
    '1': { text: 'That key didn\'t work. Check for typos and extra spaces, then try again.', tone: 'bad', icon: '#i-alert-circle' },
    // The limit counts this network and, as a backstop, everyone together, so don't blame "this network".
    rate: { text: 'Too many wrong keys were tried recently. Wait about 10 minutes, then try again.', tone: 'bad', icon: '#i-clock' },
    expired: { text: 'You were signed out because the key changed or the session ran out. Sign in again to pick up where you left off.', tone: 'info', icon: '#i-info' },
  };

  var SVG_NS = 'http://www.w3.org/2000/svg';

  function svgIcon(href) {
    var svg = document.createElementNS(SVG_NS, 'svg');
    svg.setAttribute('class', 'icon');
    svg.setAttribute('aria-hidden', 'true');
    var use = document.createElementNS(SVG_NS, 'use');
    use.setAttribute('href', href);
    svg.appendChild(use);
    return svg;
  }

  // Same structure as the dashboard's field errors: an icon, then the text.
  function showFieldError(node, text) {
    var span = document.createElement('span');
    span.textContent = text;
    node.replaceChildren(svgIcon('#i-alert-circle'), span);
    node.hidden = false;
  }

  function boot() {
    var form = document.getElementById('login-form');
    var input = document.getElementById('key');
    var toggle = document.getElementById('key-toggle');
    var submit = document.getElementById('login-submit');
    var banner = document.getElementById('login-error');
    var fieldError = document.getElementById('key-error');

    var params = new URLSearchParams(window.location.search);
    var code = params.get('error');
    // Served without the server's substitution (a static preview), the placeholder is still there.
    if (banner.getAttribute('data-message') === '{{error}}') banner.setAttribute('data-message', '');
    if (code) {
      var message = MESSAGES[code] || MESSAGES['1'];
      document.getElementById('login-error-text').textContent = message.text;
      banner.setAttribute('data-message', message.text);
      banner.className = 'callout tone-' + message.tone + ' login-error';
      banner.querySelector('use').setAttribute('href', message.icon);
      if (message.tone === 'info') banner.setAttribute('role', 'status');
      if (code !== 'rate' && code !== 'expired') input.setAttribute('aria-invalid', 'true');
      document.title = (code === 'expired' ? 'Sign in again' : 'Try again') + ' · Railpowered';
      // Drop the error from the address bar so a reload starts clean.
      if (window.history && window.history.replaceState) {
        window.history.replaceState(null, '', window.location.pathname);
      }
    }

    toggle.addEventListener('click', function () {
      var show = input.type === 'password';
      input.type = show ? 'text' : 'password';
      toggle.setAttribute('aria-pressed', String(show));
      toggle.setAttribute('aria-label', show ? 'Hide key' : 'Show key');
      toggle.querySelector('use').setAttribute('href', show ? '#i-eye-off' : '#i-eye');
      input.focus();
    });

    input.addEventListener('input', function () {
      input.removeAttribute('aria-invalid');
      fieldError.hidden = true;
    });

    form.addEventListener('submit', function (e) {
      if (submit.getAttribute('aria-busy') === 'true') {
        e.preventDefault();
        return;
      }
      if (!input.value.trim()) {
        e.preventDefault();
        showFieldError(fieldError, 'Enter your dashboard key.');
        input.setAttribute('aria-invalid', 'true');
        input.focus();
        return;
      }
      // The form posts normally; this only stops double submits and shows progress.
      submit.setAttribute('aria-busy', 'true');
      var spin = document.createElement('span');
      spin.className = 'spinner';
      spin.setAttribute('aria-hidden', 'true');
      var label = document.createElement('span');
      label.textContent = 'Signing in…';
      submit.replaceChildren(spin, label);
      window.setTimeout(function () { submit.disabled = true; }, 0);
    });

    // Coming back with the browser's Back button can restore the busy button.
    window.addEventListener('pageshow', function (e) {
      if (!e.persisted) return;
      submit.disabled = false;
      submit.removeAttribute('aria-busy');
      var label = document.createElement('span');
      label.textContent = 'Sign in';
      submit.replaceChildren(label, svgIcon('#i-arrow-right'));
    });

    var current = readTheme();
    var buttons = document.querySelectorAll('[data-theme-choice]');
    Array.prototype.forEach.call(buttons, function (b) {
      b.setAttribute('aria-pressed', String(b.getAttribute('data-theme-choice') === current));
      b.addEventListener('click', function () {
        var choice = b.getAttribute('data-theme-choice');
        saveTheme(choice);
        applyTheme(choice);
        Array.prototype.forEach.call(buttons, function (x) { x.setAttribute('aria-pressed', String(x === b)); });
      });
    });

    if (code && code !== 'rate') {
      input.focus();
    }
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
