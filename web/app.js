/*
  Railpowered dashboard.

  Plain JavaScript, no dependencies, served as /static/app.js so the page can
  run under a strict CSP (script-src 'self'). It is loaded from <head> so the
  saved colour theme is applied before first paint; everything else waits for
  DOMContentLoaded.

  Data never goes through innerHTML: every node is built with createElement and
  textContent, and handlers are attached with addEventListener.
*/
(function () {
  'use strict';

  /* ================================================================ storage + theme */

  var KEYS = {
    theme: 'railpowered.theme',
    firstRun: 'railpowered.firstrun-hidden',
    history: 'railpowered.console-history',
  };

  var store = {
    get: function (key) {
      try { return window.localStorage.getItem(key); } catch (_) { return null; }
    },
    set: function (key, value) {
      try {
        if (value == null) window.localStorage.removeItem(key);
        else window.localStorage.setItem(key, value);
      } catch (_) { /* private mode or blocked storage: the setting just isn't remembered */ }
    },
  };

  // Per-tab memory that survives a trip through the sign-in page.
  var SESSION_KEYS = {
    returnTo: 'railpowered.return-to',
    settingsDraft: 'railpowered.settings-draft',
  };

  var sessionStore = {
    get: function (key) {
      try { return window.sessionStorage.getItem(key); } catch (_) { return null; }
    },
    set: function (key, value) {
      try {
        if (value == null) window.sessionStorage.removeItem(key);
        else window.sessionStorage.setItem(key, value);
      } catch (_) { /* storage blocked: nothing is carried over */ }
    },
  };

  var THEME_COLORS = { light: '#f3f4f6', dark: '#090c10' };

  function readThemeChoice() {
    var v = store.get(KEYS.theme);
    return v === 'light' || v === 'dark' ? v : 'system';
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

  applyTheme(readThemeChoice());

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();

  /* ================================================================ constants */

  var NAME_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$/;
  var NAME_RULE = 'Use letters, numbers, dots, dashes and underscores (up to 32), starting with a letter or number.';
  var PLAYER_RE = /^[A-Za-z0-9_]{1,16}$/;
  var SVG_NS = 'http://www.w3.org/2000/svg';
  var VIEWS = ['overview', 'setups', 'worlds', 'players', 'settings', 'console', 'help'];
  var VIEW_TITLES = {
    overview: 'Overview', setups: 'Setups', worlds: 'Worlds & backups', players: 'Players',
    settings: 'Settings', console: 'Console', help: 'Help',
  };
  var TYPE_LABEL = { vanilla: 'Vanilla', fabric: 'Fabric', forge: 'Forge', modpack: 'Modpack' };
  var TYPE_ICON = { vanilla: 'i-cube', fabric: 'i-feather', forge: 'i-hammer', modpack: 'i-package' };
  var RUNNING = { starting: true, online: true };
  var BUSY_PHASES = { starting: true, stopping: true, preparing: true };
  var MAX_LOG_LINES = 1000;

  var PHASES = {
    sleeping: {
      label: 'Sleeping', icon: 'i-moon',
      copy: function () { return 'Wakes up by itself when a friend joins. You can also start it now.'; },
      sub: function () { return 'Wakes when someone joins'; },
    },
    preparing: {
      label: 'Preparing', icon: 'i-package',
      copy: function (s) {
        var label = s.task && s.task.state === 'running' && s.task.label;
        return (label ? label + '. ' : 'Installing files. ') + 'Joining is paused until this finishes.';
      },
      sub: function (s) { return (s.task && s.task.state === 'running' && s.task.label) || 'Installing files'; },
    },
    starting: {
      label: 'Starting', icon: 'i-zap',
      copy: function () { return 'Minecraft is loading the world. Friends can join in a moment.'; },
      sub: function () { return 'Loading the world'; },
    },
    online: {
      label: 'Online', icon: 'i-signal',
      copy: function (s) {
        var n = s.player_count || 0;
        if (!s.join_address) {
          // Without a public address nobody outside Railway can reach it, whatever the whitelist says.
          if (n === 0) return 'Minecraft is running. Once you add a public address, friends can join.';
          return plural(n, 'player is', 'players are') + ' on right now.';
        }
        var who = s.whitelist_enabled ? 'Anyone on the whitelist' : 'Anyone with the address';
        if (n === 0) return who + ' can join now.';
        return plural(n, 'player is', 'players are') + ' on right now. ' + who + ' can join too.';
      },
      sub: function (s) { return (s.player_count || 0) + ' of ' + (s.max_players || 0) + ' players online'; },
    },
    stopping: {
      label: 'Stopping', icon: 'i-hourglass',
      copy: function () { return 'Saving the world and shutting down.'; },
      sub: function () { return 'Saving the world'; },
    },
    crashed: {
      label: 'Crashed', icon: 'i-alert',
      copy: function (s) {
        switch (crashKind(s)) {
          case 'runtime': return 'Minecraft stopped unexpectedly. It still wakes up when someone joins, or you can start it again now.';
          case 'starting': return 'Minecraft failed while starting. Joining doesn\'t wake it for 5 minutes after that, but you can start it again here at any time.';
          case 'start': return 'Minecraft couldn\'t start, so joining won\'t wake it for now. Fix what\'s below, then start it again here.';
          default: return 'Minecraft stopped unexpectedly. You can start it again now.';
        }
      },
      sub: function (s) { var kind = crashKind(s); return kind === 'start' || kind === 'starting' ? 'Couldn\'t start' : 'Stopped unexpectedly'; },
    },
    unknown: {
      label: 'Checking…', icon: 'i-loader',
      copy: function () { return 'Asking your server how it\'s doing.'; },
      sub: function () { return 'Connecting to your server'; },
    },
  };

  // Whether a join still wakes a crashed server depends on how it went down
  // (mc_host/process.py wake_blocked): a crash after it was online wakes on the
  // next join; a crash while starting (or a start that timed out) blocks wake-ups
  // for 5 minutes; a start that failed outright (no exit code) blocks them for at
  // least that long, and for good when a retry can't help (a missing Java, a
  // world from a newer version) until it's started from the dashboard.
  function crashKind(s) {
    var exit = s && s.last_exit;
    if (!exit) return '';
    // The backend says it directly (last_exit.wake); the rest is for older servers.
    if (exit.wake === 'join') return 'runtime';
    if (exit.wake === 'later') return 'starting';
    if (exit.wake === 'manual') return 'start';
    if (exit.code == null) return 'start';
    if (/^Crashed while starting|didn't finish starting/.test(String(exit.reason || ''))) return 'starting';
    return 'runtime';
  }

  /* ================================================================ state */

  var state = {
    status: null,
    statusAt: 0,
    connected: true,
    route: { view: '', anchor: '' },
    versions: null,
    versionsPromise: null,
    setups: null,
    worlds: null,
    backups: null,
    disk: null,
    players: null,
    settings: null,
    settingsPeek: null,     // /api/settings fetched for the New setup form while Settings hasn't loaded
    settingsDirty: false,
    storage: null,          // GET /api/storage: { items, total }
    uploading: null,        // { abort } while a modpack or backup upload is in flight
    // What started a task from this tab, by taskIdent(), so a task that fails
    // with world_downgrade can be confirmed and sent again: { world, retry(), newWorld() }.
    taskRequests: {},
    removedPlayers: {},     // kind -> { name, timer }: just removed, with an inline Undo
    pending: {},            // server actions in flight: { start: true, ... }
    // Task ids restart at t1 when the backend restarts, so tasks are tracked by
    // taskIdent() (id + start time), never by the id alone.
    watchedTasks: {},       // tasks seen running (so we can report how they ended)
    notifiedTasks: {},      // tasks whose outcome was already announced
    dismissedTasks: {},     // taskKey()s the user closed in the banner
    taskFirstSeen: {},
    editingSetup: null,     // { id, draft, focusOnRender, sel }
    setupForm: null,        // set up in initSetupForm
    loaded: {},             // which views have loaded their data at least once
  };

  /* ================================================================ tiny DOM helpers */

  function $(sel, root) { return (root || document).querySelector(sel); }
  function $$(sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); }

  function el(tag, attrs) {
    var node = document.createElement(tag);
    if (attrs) {
      Object.keys(attrs).forEach(function (key) {
        var v = attrs[key];
        if (v == null || v === false) return;
        if (key === 'class') node.className = v;
        else if (key === 'text') node.textContent = v;
        else if (key === 'dataset') Object.keys(v).forEach(function (k) { node.dataset[k] = v[k]; });
        else if (key === 'style') Object.keys(v).forEach(function (k) { node.style.setProperty(k, v[k]); });
        else if (key.slice(0, 2) === 'on' && typeof v === 'function') node.addEventListener(key.slice(2), v);
        else if (v === true) node.setAttribute(key, '');
        else node.setAttribute(key, String(v));
      });
    }
    for (var i = 2; i < arguments.length; i++) appendKid(node, arguments[i]);
    return node;
  }

  function appendKid(node, kid) {
    if (kid == null || kid === false) return;
    if (Array.isArray(kid)) { kid.forEach(function (k) { appendKid(node, k); }); return; }
    node.appendChild(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }

  // Like replaceChildren, but skips null/false the way el() does.
  function fill(node) {
    node.replaceChildren();
    for (var i = 1; i < arguments.length; i++) appendKid(node, arguments[i]);
    return node;
  }

  // Re-rendering a list removes the row that had focus (say, a Delete button).
  // Send focus to the card's heading instead of letting it fall to <body>.
  function keepFocus(selectors, render) {
    var heading = null;
    selectors.forEach(function (sel) {
      var host = $(sel);
      if (host && host.contains(document.activeElement)) {
        var card = host.closest('.card');
        heading = card && card.querySelector('h2');
      }
    });
    render();
    var active = document.activeElement;
    if (heading && heading.isConnected && (!active || active === document.body)) {
      if (!heading.hasAttribute('tabindex')) heading.setAttribute('tabindex', '-1');
      heading.focus({ preventScroll: true });
    }
  }

  function icon(name, extra) {
    var svg = document.createElementNS(SVG_NS, 'svg');
    svg.setAttribute('class', 'icon' + (extra ? ' ' + extra : ''));
    svg.setAttribute('aria-hidden', 'true');
    var use = document.createElementNS(SVG_NS, 'use');
    use.setAttribute('href', '#' + name);
    svg.appendChild(use);
    return svg;
  }

  function spinner(extra) { return el('span', { class: 'spinner' + (extra ? ' ' + extra : ''), 'aria-hidden': 'true' }); }

  function setText(node, text) { if (node && node.textContent !== text) node.textContent = text; }

  function setIcon(svg, name) {
    var use = svg && svg.querySelector('use');
    if (use && use.getAttribute('href') !== '#' + name) use.setAttribute('href', '#' + name);
  }

  function btnClass(style) {
    switch (style) {
      case 'primary': return 'btn btn-primary';
      case 'danger': return 'btn btn-danger';
      case 'ghost': return 'btn btn-ghost';
      case 'ghost-danger': return 'btn btn-ghost-danger';
      case 'outline-danger': return 'btn btn-outline-danger';
      default: return 'btn';
    }
  }

  function emptyState(iconName, title, text, action) {
    return el('div', { class: 'empty' },
      el('span', { class: 'empty-icon' }, icon(iconName)),
      el('strong', null, title),
      text ? el('p', null, text) : null,
      action || null);
  }

  function errorState(title, err, retry) {
    return emptyState('i-alert-circle', title, err && err.message ? err.message : 'Something went wrong.',
      retry ? el('button', { class: 'btn btn-sm', type: 'button', onclick: retry }, icon('i-restart', 'icon-sm'), 'Try again') : null);
  }

  function skeletonRows(n) {
    var wrap = el('div', { class: 'skeleton-rows', 'aria-hidden': 'true' });
    for (var i = 0; i < (n || 3); i++) {
      wrap.appendChild(el('span', { class: 'skeleton', style: { width: (45 + ((i * 23) % 40)) + '%' } }));
    }
    return wrap;
  }

  function showFieldError(input, errorEl, message) {
    if (message) {
      errorEl.replaceChildren(icon('i-alert-circle'), el('span', null, message));
      errorEl.hidden = false;
      input.setAttribute('aria-invalid', 'true');
    } else {
      errorEl.hidden = true;
      errorEl.replaceChildren();
      input.removeAttribute('aria-invalid');
    }
  }

  /* ================================================================ formatting */

  function plural(n, one, many) { return n + ' ' + (n === 1 ? one : many); }

  function formatBytes(n) {
    n = Number(n) || 0;
    var units = ['bytes', 'KB', 'MB', 'GB', 'TB'];
    var i = 0;
    while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
    if (i === 0) return n + (n === 1 ? ' byte' : ' bytes');
    return (n < 10 ? n.toFixed(1) : Math.round(n)) + ' ' + units[i];
  }

  function formatDuration(sec) {
    sec = Math.max(0, Math.floor(Number(sec) || 0));
    var d = Math.floor(sec / 86400), h = Math.floor((sec % 86400) / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
    if (d > 0) return d + 'd ' + h + 'h';
    if (h > 0) return h + 'h ' + m + 'm';
    if (m > 0) return m + 'm ' + (s < 10 ? '0' : '') + s + 's';
    return s + 's';
  }

  function formatClock(sec) {
    sec = Math.max(0, Math.ceil(Number(sec) || 0));
    var h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
    var mm = h > 0 && m < 10 ? '0' + m : String(m);
    return (h > 0 ? h + ':' : '') + mm + ':' + (s < 10 ? '0' : '') + s;
  }

  function formatMinutes(min) {
    min = Number(min) || 0;
    if (min <= 0) return 'never';
    if (min < 60) return plural(min, 'minute', 'minutes');
    var h = Math.floor(min / 60), m = min % 60;
    return plural(h, 'hour', 'hours') + (m ? ' ' + plural(m, 'minute', 'minutes') : '');
  }

  // Compact form for tight spots like the hero facts: "10 min", "1 h 30 min", "2 h".
  function formatMinutesShort(min) {
    min = Math.round(Number(min) || 0);
    if (min <= 0) return 'Off';
    if (min < 60) return min + ' min';
    var h = Math.floor(min / 60), m = min % 60;
    return h + ' h' + (m ? ' ' + m + ' min' : '');
  }

  function toDate(v) {
    if (v == null || v === '') return null;
    if (typeof v === 'number') return new Date(v < 1e12 ? v * 1000 : v);
    var n = Number(v);
    if (!isNaN(n) && String(v).trim() !== '') return new Date(n < 1e12 ? n * 1000 : n);
    var d = new Date(v);
    return isNaN(d.getTime()) ? null : d;
  }

  var rtf = (typeof Intl !== 'undefined' && Intl.RelativeTimeFormat) ? new Intl.RelativeTimeFormat(undefined, { numeric: 'auto' }) : null;

  function relativeTime(v) {
    var d = toDate(v);
    if (!d) return '';
    var diff = (d.getTime() - Date.now()) / 1000;
    var abs = Math.abs(diff);
    if (abs < 45) return 'just now';
    if (!rtf) return d.toLocaleString();
    if (abs < 3600) return rtf.format(Math.round(diff / 60), 'minute');
    if (abs < 86400) return rtf.format(Math.round(diff / 3600), 'hour');
    if (abs < 86400 * 30) return rtf.format(Math.round(diff / 86400), 'day');
    return d.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
  }

  // The year is shown only when it isn't this year, so last year's backup can't pass for this year's.
  function formatDateTime(v) {
    var d = toDate(v);
    if (!d) return '';
    var opts = { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' };
    if (d.getFullYear() !== new Date().getFullYear()) opts.year = 'numeric';
    return d.toLocaleString(undefined, opts);
  }

  function formatTime(v) {
    var d = toDate(v);
    if (!d) return '';
    var sameDay = d.toDateString() === new Date().toDateString();
    return sameDay ? d.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' }) : formatDateTime(d);
  }

  function isRecent(v, days) {
    var d = toDate(v);
    return !!d && Math.abs(Date.now() - d.getTime()) < days * 86400000;
  }

  // Row metadata: parts that start with an icon separate themselves; plain
  // text parts get a quiet dot in front so neighbouring facts don't run together.
  function metaParts(parts) {
    var out = [];
    parts.filter(Boolean).forEach(function (part, i) {
      var leadsWithIcon = part.firstChild && part.firstChild.nodeType === 1 && part.firstChild.namespaceURI === SVG_NS;
      if (i > 0 && !leadsWithIcon) out.push(el('span', { class: 'sep', 'aria-hidden': 'true' }, '·'));
      out.push(part);
    });
    return out;
  }

  function capitalize(s) { s = String(s || ''); return s.charAt(0).toUpperCase() + s.slice(1); }

  function humanize(s) { return capitalize(String(s || '').replace(/[-_]+/g, ' ')); }

  function compareVersions(a, b) {
    // Weekly snapshot ids (24w14a) carry no version number; the server's list
    // is in release order, so use its positions when both ids are in it.
    var rank = state.versionRank || {};
    if (a !== b && rank[a] !== undefined && rank[b] !== undefined) return rank[a] < rank[b] ? 1 : -1;
    var pa = String(a || '').split(/[^0-9]+/).filter(Boolean).map(Number);
    var pb = String(b || '').split(/[^0-9]+/).filter(Boolean).map(Number);
    for (var i = 0; i < Math.max(pa.length, pb.length); i++) {
      var x = pa[i] || 0, y = pb[i] || 0;
      if (x !== y) return x < y ? -1 : 1;
    }
    return 0;
  }

  function slugName(raw) {
    var s = String(raw || '').trim().replace(/\.(mrpack|zip)$/i, '');
    s = s.replace(/[^A-Za-z0-9._-]+/g, '-').replace(/-{2,}/g, '-').replace(/^[^A-Za-z0-9]+/, '').replace(/[-.]+$/, '');
    return s.slice(0, 32).replace(/[-.]+$/, '');
  }

  // base, or base-2, base-3… : the first one not in names (compared without case).
  function uniqueName(base, names) {
    var taken = {};
    names.forEach(function (n) { taken[String(n).toLowerCase()] = true; });
    var name = slugName(base);
    if (!name || !taken[name.toLowerCase()]) return name;
    for (var i = 2; i < 100; i++) {
      var suffix = '-' + i;
      var candidate = name.slice(0, 32 - suffix.length) + suffix;
      if (!taken[candidate.toLowerCase()]) return candidate;
    }
    return name;
  }

  function uniqueWorldName(base) {
    return uniqueName(slugName(base) || 'world', (state.worlds || []).map(function (w) { return w.name; }));
  }

  // Installed modpack folders (lower-cased) -> names of the setups using each.
  function packInstances() {
    var map = {};
    (state.setups || []).forEach(function (s) {
      if (!s.instance) return;
      var k = String(s.instance).toLowerCase();
      (map[k] = map[k] || []).push(s.name || setupSummary(s));
    });
    return map;
  }

  // A guessed pack name never points at a pack that is already installed;
  // replacing one is only done when the admin types that name on purpose.
  function guessPackName(raw) {
    return uniqueName(raw, Object.keys(packInstances()));
  }

  function versionLabel(setup) {
    if (!setup) return '';
    var v = setup.resolved_version || setup.minecraft_version || '';
    if (setup.minecraft_version === 'latest') return v && v !== 'latest' ? v + ' (latest)' : 'Latest';
    return v;
  }

  function isConcreteVersion(v) { return /\d/.test(String(v || '')); }

  // The exact number friends must pick in their launcher, or '' when it isn't known
  // yet (an "Always latest" setup that has never resolved a release).
  function exactVersion(setup) {
    if (!setup) return '';
    var v = setup.resolved_version || (setup.minecraft_version !== 'latest' ? setup.minecraft_version : '');
    return isConcreteVersion(v) ? String(v) : '';
  }

  var UNKNOWN_LATEST = 'the latest release (start the server once to see the exact number)';

  function setupSummary(setup) {
    if (!setup) return '';
    var parts = [];
    parts.push(setup.type === 'modpack' ? 'Modpack' + (setup.instance ? ' ' + setup.instance : '') : (TYPE_LABEL[setup.type] || capitalize(setup.type)));
    var v = versionLabel(setup);
    if (v) parts.push('Minecraft ' + v);
    if (setup.level_name) parts.push('World: ' + setup.level_name);
    return parts.join(' · ');
  }

  /* ================================================================ identicon avatars */

  function hashString(str) {
    var h = 2166136261;
    str = String(str || '').toLowerCase();
    for (var i = 0; i < str.length; i++) {
      h ^= str.charCodeAt(i);
      h = Math.imul(h, 16777619);
    }
    return h >>> 0;
  }

  // A 5x5 mirrored pixel face, coloured from the name. Generated locally because
  // the CSP only allows images from 'self' and data: URIs.
  function avatar(name, large) {
    var h = hashString(name);
    var wrap = el('span', { class: 'avatar' + (large ? ' lg' : ''), 'aria-hidden': 'true' });
    wrap.style.setProperty('--av-h', String(h % 360));
    var svg = document.createElementNS(SVG_NS, 'svg');
    svg.setAttribute('viewBox', '0 0 7 7');
    svg.setAttribute('shape-rendering', 'crispEdges');
    var bits = h >>> 9;
    var any = false;
    for (var y = 0; y < 5; y++) {
      for (var x = 0; x < 3; x++) {
        if ((bits >> (y * 3 + x)) & 1) {
          any = true;
          addPixel(svg, x + 1, y + 1);
          if (x < 2) addPixel(svg, 5 - x, y + 1);
        }
      }
    }
    if (!any) addPixel(svg, 3, 3);
    wrap.appendChild(svg);
    return wrap;
  }

  function addPixel(svg, x, y) {
    var r = document.createElementNS(SVG_NS, 'rect');
    r.setAttribute('x', x); r.setAttribute('y', y); r.setAttribute('width', 1); r.setAttribute('height', 1);
    r.setAttribute('fill', 'currentColor');
    svg.appendChild(r);
  }

  /* ================================================================ API */

  function ApiError(message, opts) {
    this.name = 'ApiError';
    this.message = message;
    opts = opts || {};
    this.status = opts.status || 0;
    this.code = opts.code || '';
    this.data = opts.data || null;
  }
  ApiError.prototype = Object.create(Error.prototype);
  ApiError.prototype.constructor = ApiError;

  var redirecting = false;

  function goLogin() {
    if (redirecting) return;
    redirecting = true;
    stopPolling();
    // Remember where the user was (and any unsaved settings) so signing in
    // again brings them back to the same place instead of a cold start.
    var hash = window.location.hash || '';
    if (/^#\/[a-z]/.test(hash)) sessionStore.set(SESSION_KEYS.returnTo, hash);
    stashSettingsDraft();
    window.location.replace('/login?error=expired');
  }

  function fallbackMessage(status) {
    if (status === 400) return 'That request wasn\'t valid. Check the details and try again.';
    if (status === 403) return 'The server refused this request. Reload the page and try again.';
    if (status === 404) return 'That item no longer exists. It may have been changed in another tab.';
    if (status === 409) return 'That can\'t be done right now. Wait a moment and try again.';
    if (status === 413) return 'That file is too large for this server.';
    if (status === 429) return 'Too many requests. Wait a moment and try again.';
    if (status === 502 || status === 503 || status === 504) return 'The server is restarting or unreachable. Try again in a minute.';
    return 'Something went wrong on the server. The console may have details.';
  }

  function parseError(status, text) {
    var data = null;
    if (text) { try { data = JSON.parse(text); } catch (_) { data = null; } }
    var message = (data && typeof data.error === 'string' && data.error.trim()) || fallbackMessage(status);
    return new ApiError(message, { status: status, code: (data && data.code) || '', data: data });
  }

  function api(method, path, body) {
    var headers = { Accept: 'application/json' };
    var opts = { method: method, headers: headers, credentials: 'same-origin', cache: 'no-store' };
    if (method !== 'GET' && method !== 'HEAD') headers['X-Railpowered'] = '1';
    if (body !== undefined) {
      headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(body);
    }
    return fetch(path, opts).then(function (res) {
      if (res.status === 401) {
        goLogin();
        throw new ApiError('Your session has ended. Sign in again.', { status: 401, code: 'unauthorized' });
      }
      return res.text().catch(function () { return ''; }).then(function (text) {
        if (!res.ok) throw parseError(res.status, text);
        if (!text) return {};
        try { return JSON.parse(text); } catch (_) { return {}; }
      });
    }, function () {
      throw new ApiError('Couldn\'t reach your server. Check your connection and try again.', { status: 0, code: 'network' });
    });
  }

  // Raw upload with progress (fetch has no upload progress events). The
  // returned promise has an abort() method for the Cancel button.
  function uploadFile(path, file, onProgress) {
    var xhr = new XMLHttpRequest();
    var promise = new Promise(function (resolve, reject) {
      xhr.open('POST', path, true);
      xhr.withCredentials = true;
      xhr.setRequestHeader('X-Railpowered', '1');
      xhr.setRequestHeader('Accept', 'application/json');
      xhr.setRequestHeader('Content-Type', 'application/octet-stream');
      xhr.upload.addEventListener('progress', function (e) {
        if (e.lengthComputable && onProgress) onProgress(e.loaded, e.total);
      });
      xhr.addEventListener('load', function () {
        if (xhr.status === 401) { goLogin(); reject(new ApiError('Your session has ended. Sign in again.', { status: 401 })); return; }
        if (xhr.status < 200 || xhr.status >= 300) { reject(parseError(xhr.status, xhr.responseText)); return; }
        var data = {};
        try { data = xhr.responseText ? JSON.parse(xhr.responseText) : {}; } catch (_) { data = {}; }
        resolve(data);
      });
      xhr.addEventListener('error', function () {
        // The server refuses an upload it can't take (too big, volume full,
        // another task running) before reading it and closes the connection,
        // which most browsers report as a plain network error.
        reject(new ApiError('The upload stopped before it finished. Either the connection dropped, or the server ' +
          'refused the file (for example because the volume is short on space). Check the Console, then try again.', { code: 'network' }));
      });
      xhr.addEventListener('abort', function () { reject(new ApiError('Upload cancelled.', { code: 'aborted' })); });
      xhr.send(file);
    });
    promise.abort = function () { xhr.abort(); };
    return promise;
  }

  /* ================================================================ toasts + announcer */

  // Messages that arrive together (a phase change and a task result, say) are
  // read as one, rather than the second silently replacing the first.
  var announceQueue = [];
  var announceTimer = null;

  function announce(text) {
    var node = $('#announcer');
    if (!node || !text) return;
    announceQueue.push(text);
    if (announceTimer) return;
    node.textContent = '';
    announceTimer = window.setTimeout(function () {
      node.textContent = announceQueue.join(' ');
      announceQueue = [];
      announceTimer = null;
    }, 60);
  }

  function toast(opts) {
    var tone = opts.tone || 'info';
    var iconName = { ok: 'i-check-circle', bad: 'i-alert-circle', warn: 'i-alert', info: 'i-info' }[tone] || 'i-info';
    // No role on the toast itself: #toasts is the (polite) live region, so each
    // toast is announced once.
    var node = el('div', { class: 'toast tone-' + tone },
      icon(iconName),
      el('div', { class: 'toast-body' },
        el('p', { class: 'toast-title' }, opts.title),
        opts.text ? el('p', { class: 'toast-text' }, opts.text) : null,
        opts.actions && opts.actions.length ? el('div', { class: 'toast-actions' }, opts.actions.map(function (a) {
          return el('button', {
            class: 'btn btn-sm', type: 'button',
            onclick: function () { dismiss(); a.run(); },
          }, a.label);
        })) : null),
      el('button', { class: 'icon-btn', type: 'button', 'aria-label': 'Dismiss notification', onclick: function () { dismiss(); } }, icon('i-x', 'icon-sm')));
    var host = $('#toasts');
    host.appendChild(node);
    while (host.children.length > 4) host.removeChild(host.firstChild);
    // Toasts with a button stay longer, and none of them close while the
    // pointer or keyboard focus is on them.
    var hasActions = !!(opts.actions && opts.actions.length);
    var timeout = opts.timeout != null ? opts.timeout : (hasActions ? 15000 : (tone === 'bad' ? 9000 : 4500));
    var timer = timeout > 0 ? window.setTimeout(dismiss, timeout) : null;
    var hovered = false, focused = false;
    function pause() { if (timer) { window.clearTimeout(timer); timer = null; } }
    function resume() { if (!timer && !hovered && !focused && timeout > 0 && node.isConnected) timer = window.setTimeout(dismiss, 4000); }
    node.addEventListener('mouseenter', function () { hovered = true; pause(); });
    node.addEventListener('mouseleave', function () { hovered = false; resume(); });
    node.addEventListener('focusin', function () { focused = true; pause(); });
    node.addEventListener('focusout', function (e) {
      if (e.relatedTarget && node.contains(e.relatedTarget)) return;
      focused = false;
      resume();
    });
    function dismiss() {
      if (timer) window.clearTimeout(timer);
      timer = null;
      timeout = 0;
      if (!node.isConnected) return;
      // Don't strand keyboard focus on a toast that's about to disappear.
      if (node.contains(document.activeElement)) $('#main').focus({ preventScroll: true });
      node.classList.add('is-leaving');
      window.setTimeout(function () { if (node.isConnected) node.remove(); }, 220);
    }
    return dismiss;
  }

  function toastError(title, err) {
    if (err && err.status === 401) return;
    toast({ tone: 'bad', title: title, text: err && err.message ? err.message : 'Something went wrong.' });
  }

  /* ================================================================ dialog */

  var dialogState = { resolve: null, returnFocus: null };

  function openDialog(opts) {
    var dlg = $('#dialog');
    if (dlg.open) closeDialog(null);
    var buttons = {};
    var foot = el('div', { class: 'dialog-foot' });
    (opts.actions || []).forEach(function (a) {
      var b = el('button', {
        type: 'button',
        class: btnClass(a.style),
        disabled: a.disabled || null,
        'data-default': a.isDefault ? 'true' : null,
      }, a.icon ? icon(a.icon) : null, el('span', null, a.label));
      b.addEventListener('click', function () { closeDialog(a.value); });
      buttons[String(a.value)] = b;
      foot.appendChild(b);
    });
    var body = el('div', { class: 'dialog-body', id: 'dialog-body' }, opts.body);
    dlg.classList.toggle('is-wide', !!opts.wide);
    dlg.replaceChildren(el('div', { class: 'dialog-inner' },
      el('div', { class: 'dialog-head' },
        el('div', { class: 'dialog-icon tone-' + (opts.tone || 'primary') }, icon(opts.icon || 'i-info')),
        el('h2', { class: 'dialog-title', id: 'dialog-title' }, opts.title)),
      body,
      foot));
    return new Promise(function (resolve) {
      dialogState.resolve = resolve;
      dialogState.returnFocus = document.activeElement;
      dlg.showModal();
      var focusTarget = opts.focus ? opts.focus(buttons) : null;
      if (!focusTarget) {
        var cancel = buttons['null'];
        focusTarget = cancel || foot.querySelector('button:not([disabled])');
      }
      if (focusTarget) focusTarget.focus();
      if (opts.onOpen) opts.onOpen(buttons, body);
    });
  }

  function closeDialog(value) {
    var dlg = $('#dialog');
    var resolve = dialogState.resolve;
    dialogState.resolve = null;
    if (dlg.open) dlg.close();
    var back = dialogState.returnFocus;
    dialogState.returnFocus = null;
    // The opener may have been re-rendered while the dialog was open.
    var action = back && back.getAttribute ? back.getAttribute('data-action') : null;
    if (back && !back.isConnected && action) back = $('#hero-actions [data-action="' + action + '"]');
    if (back && back.isConnected && typeof back.focus === 'function') {
      try { back.focus({ preventScroll: true }); } catch (_) { back.focus(); }
    }
    if (resolve) resolve(value);
  }

  function initDialog() {
    var dlg = $('#dialog');
    dlg.addEventListener('cancel', function (e) { e.preventDefault(); closeDialog(null); });
    dlg.addEventListener('close', function () {
      if (dialogState.resolve) { var r = dialogState.resolve; dialogState.resolve = null; r(null); }
    });
    // Close on a backdrop click only when the press started on the backdrop
    // too: dragging a text selection out of an input must not close the dialog.
    var pressedBackdrop = false;
    dlg.addEventListener('pointerdown', function (e) { pressedBackdrop = e.target === dlg; });
    dlg.addEventListener('click', function (e) {
      var fromBackdrop = pressedBackdrop;
      pressedBackdrop = false;
      if (e.target === dlg && fromBackdrop) closeDialog(null);
    });
    dlg.addEventListener('keydown', function (e) {
      if (e.key !== 'Enter' || e.target.tagName !== 'INPUT' || e.target.type === 'radio' || e.target.type === 'checkbox') return;
      var def = dlg.querySelector('button[data-default="true"]');
      if (def && !def.disabled) { e.preventDefault(); def.click(); }
    });
  }

  function confirmDialog(opts) {
    return openDialog({
      tone: opts.tone || 'primary',
      icon: opts.icon || (opts.tone === 'danger' ? 'i-alert' : 'i-info'),
      title: opts.title,
      body: opts.body,
      actions: [
        { label: opts.cancelLabel || 'Cancel', value: null, style: 'ghost' },
        { label: opts.confirmLabel || 'Continue', value: 'ok', style: opts.tone === 'danger' ? 'danger' : 'primary', icon: opts.confirmIcon, isDefault: true },
      ],
    }).then(function (v) { return v === 'ok'; });
  }

  /* ================================================================ busy buttons */

  function runBusy(button, busyLabel, fn) {
    if (button && button.getAttribute('aria-busy') === 'true') return Promise.resolve();
    var saved = null, wasDisabled = false;
    if (button) {
      saved = Array.prototype.slice.call(button.childNodes);
      wasDisabled = button.disabled;
      button.disabled = true;
      button.setAttribute('aria-busy', 'true');
      button.replaceChildren(spinner(), el('span', null, busyLabel));
    }
    return Promise.resolve().then(fn).finally(function () {
      if (!button) return;
      button.replaceChildren.apply(button, saved);
      button.disabled = wasDisabled;
      button.removeAttribute('aria-busy');
    });
  }

  /* ================================================================ router + shell */

  function parseRoute() {
    var hash = window.location.hash || '';
    // Only "#/view" hashes are routes. In-page anchors such as "#main" (the
    // skip link without JavaScript) keep the current view.
    if (hash && hash.indexOf('#/') !== 0 && state.route.view) return { view: state.route.view, anchor: '' };
    var m = /^#\/([a-z]+)(?:\/([A-Za-z0-9_-]+))?/.exec(hash);
    var view = m && VIEWS.indexOf(m[1]) !== -1 ? m[1] : 'overview';
    return { view: view, anchor: m && m[2] ? m[2] : '' };
  }

  function navigate(initial) {
    var route = parseRoute();
    var changed = route.view !== state.route.view;
    state.route = route;
    VIEWS.forEach(function (v) {
      var section = document.getElementById('view-' + v);
      if (section) section.hidden = v !== route.view;
    });
    $$('[data-nav]').forEach(function (link) {
      if (link.getAttribute('data-nav') === route.view) link.setAttribute('aria-current', 'page');
      else link.removeAttribute('aria-current');
    });
    // The phone tab bar has no tab for these; light up "More" instead.
    var more = $('#tab-more');
    if (more) more.classList.toggle('is-current', ['settings', 'console', 'help'].indexOf(route.view) !== -1);
    closeDrawer(false);
    updateTitle();
    if (changed) {
      if (!initial) {
        window.scrollTo(0, 0);
        var heading = document.querySelector('#view-' + route.view + ' h1');
        if (heading) heading.focus({ preventScroll: true });
      }
      enterView(route.view);
    }
    if (route.anchor) {
      window.setTimeout(function () {
        var target = document.getElementById(route.anchor) || document.getElementById('faq-' + route.anchor);
        if (!target) return;
        if (target.tagName === 'DETAILS') target.open = true;
        target.scrollIntoView({ behavior: prefersReducedMotion() ? 'auto' : 'smooth', block: 'start' });
      }, 30);
    }
  }

  function prefersReducedMotion() {
    return window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  }

  function enterView(view) {
    if (view === 'overview' || view === 'console') pollLogs();
    if (view === 'console') {
      renderFullLog();
      window.requestAnimationFrame(function () { if (logState.follow) scrollLogToBottom(); });
      updateConsoleInput();
    }
    if (view === 'setups') { loadSetups(); loadWorlds(); loadVersions(); loadStorage(); }
    if (view === 'worlds') { loadWorlds(); loadBackups(); }
    if (view === 'players') { loadPlayers(); renderPlayersOnline(); }
    if (view === 'settings') loadSettings();
    if (view === 'help') renderHelp();
  }

  function updateTitle() {
    var s = state.status;
    var phase = s && state.connected ? (PHASES[s.phase] || PHASES.unknown).label : (state.connected ? 'Railpowered' : 'Reconnecting');
    var viewTitle = VIEW_TITLES[state.route.view] || '';
    document.title = (s ? phase + ' · ' : '') + (viewTitle && state.route.view !== 'overview' ? viewTitle + ' · ' : '') + 'Railpowered';
  }

  var drawerMedia = window.matchMedia ? window.matchMedia('(max-width: 959px)') : null;

  var drawerOpener = null;

  function openDrawer(e) {
    var app = $('#app');
    drawerOpener = (e && e.currentTarget) || $('#drawer-open');
    app.classList.add('drawer-open');
    $$('[aria-controls="sidebar"]').forEach(function (b) { b.setAttribute('aria-expanded', 'true'); });
    setInert(true);
    var first = $('#sidebar .nav-link[aria-current="page"]') || $('#sidebar .nav-link');
    if (first) first.focus();
  }

  function closeDrawer(restoreFocus) {
    var app = $('#app');
    if (!app.classList.contains('drawer-open')) return;
    app.classList.remove('drawer-open');
    $$('[aria-controls="sidebar"]').forEach(function (b) { b.setAttribute('aria-expanded', 'false'); });
    setInert(false);
    if (restoreFocus) (drawerOpener || $('#drawer-open')).focus();
    drawerOpener = null;
  }

  function setInert(on) {
    ['#main', '.tabbar', '.topbar'].forEach(function (sel) {
      var node = $(sel);
      if (!node) return;
      if (on) node.setAttribute('inert', ''); else node.removeAttribute('inert');
    });
  }

  function initShell() {
    $('#drawer-open').addEventListener('click', openDrawer);
    $('#tab-more').addEventListener('click', openDrawer);
    $('#drawer-close').addEventListener('click', function () { closeDrawer(true); });
    $('#drawer-scrim').addEventListener('click', function () { closeDrawer(true); });
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && $('#app').classList.contains('drawer-open')) closeDrawer(true);
    });
    if (drawerMedia) {
      var onChange = function () { if (!drawerMedia.matches) closeDrawer(false); };
      if (drawerMedia.addEventListener) drawerMedia.addEventListener('change', onChange);
      else if (drawerMedia.addListener) drawerMedia.addListener(onChange);
    }

    var choice = readThemeChoice();
    $$('[data-theme-choice]').forEach(function (b) {
      b.setAttribute('aria-pressed', String(b.getAttribute('data-theme-choice') === choice));
      b.addEventListener('click', function () {
        var c = b.getAttribute('data-theme-choice');
        store.set(KEYS.theme, c === 'system' ? null : c);
        applyTheme(c);
        $$('[data-theme-choice]').forEach(function (x) { x.setAttribute('aria-pressed', String(x === b)); });
      });
    });

    $('.skip-link').addEventListener('click', function (e) {
      e.preventDefault();
      $('#main').focus();
    });

    $('#logout').addEventListener('click', function () {
      var button = this;
      runBusy(button, 'Signing out…', function () {
        return fetch('/logout', {
          method: 'POST',
          credentials: 'same-origin',
          headers: { 'X-Railpowered': '1', 'Content-Type': 'application/x-www-form-urlencoded' },
          body: 'x_railpowered=1',
          redirect: 'manual',
        }).catch(function () { return null; }).then(function (res) {
          // Success is the redirect to /login. Anything else (a 403, or Railway's
          // edge answering 502 mid-redeploy) means the session is still valid,
          // so say so instead of flashing the sign-in page.
          if (res && (res.type === 'opaqueredirect' || res.ok)) {
            redirecting = true;
            stopPolling();
            window.location.replace('/login');
            return new Promise(function () {}); // keep the button busy while the page changes
          }
          toast({
            tone: 'bad', title: 'Couldn\'t sign out',
            text: res ? 'The server didn\'t confirm it, so you\'re still signed in. Try again.' : 'Couldn\'t reach your server, so you\'re still signed in. Try again.',
          });
        });
      });
    });

    $('#logout-all').addEventListener('click', function () {
      var button = this;
      confirmDialog({
        tone: 'danger', icon: 'i-shield',
        title: 'Sign out everywhere?',
        body: [
          el('p', null, 'Every browser signed in to this dashboard is signed out, this one included. Anyone who wants back in needs the dashboard key.'),
          el('p', null, 'If someone else might know the key, change ', el('code', null, 'ADMIN_KEY'), ' in Railway too. Signing out doesn\'t stop them from signing in again with it.'),
        ],
        confirmLabel: 'Sign out everywhere', confirmIcon: 'i-logout',
      }).then(function (ok) {
        if (!ok) return;
        runBusy(button, 'Signing out…', function () {
          return api('POST', '/api/sessions/revoke-all', {}).then(function () {
            redirecting = true;
            stopPolling();
            window.location.replace('/login');
            return new Promise(function () {}); // keep the button busy while the page changes
          }, function (err) {
            if (err.status === 401) return; // already signed out: goLogin() takes over
            toastError('Couldn\'t sign out everywhere', err);
          });
        });
      });
    });

    $('#jump-new-setup').addEventListener('click', function (e) {
      e.preventDefault();
      var target = $('#new-setup');
      target.scrollIntoView({ behavior: prefersReducedMotion() ? 'auto' : 'smooth', block: 'start' });
      var checked = $('#setup-form input[name="type"]:checked');
      if (checked) checked.focus({ preventScroll: true });
    });

    window.addEventListener('hashchange', function () { navigate(false); });
    window.addEventListener('beforeunload', function (e) {
      if (redirecting) return; // goLogin keeps the settings draft for after sign-in
      if (state.settingsDirty || state.uploading) { e.preventDefault(); e.returnValue = ''; }
    });
  }

  /* ================================================================ status polling */

  var timers = { status: null, logs: null, tick: null, slow: null };
  var inflight = { status: false, logs: false };
  var failures = 0;

  function stopPolling() {
    Object.keys(timers).forEach(function (k) {
      if (timers[k]) { window.clearTimeout(timers[k]); window.clearInterval(timers[k]); timers[k] = null; }
    });
  }

  function statusDelay() {
    if (failures > 0) return Math.min(15000, 2500 * Math.pow(2, failures - 1));
    var s = state.status;
    if (!s) return 2000;
    if (BUSY_PHASES[s.phase] || (s.task && s.task.state === 'running') || Object.keys(state.pending).length) return 1500;
    return 2500;
  }

  function scheduleStatus(delay) {
    window.clearTimeout(timers.status);
    timers.status = null;
    if (document.hidden || redirecting) return;
    timers.status = window.setTimeout(pollStatus, delay == null ? statusDelay() : delay);
  }

  function pollStatus() {
    if (inflight.status || redirecting) return Promise.resolve();
    inflight.status = true;
    window.clearTimeout(timers.status);
    return api('GET', '/api/status').then(function (s) {
      failures = 0;
      setConnected(true);
      onStatus(s);
    }, function (err) {
      if (err.status === 401) return;
      failures++;
      if (failures >= 2 || err.status === 0) setConnected(false, err);
    }).finally(function () {
      inflight.status = false;
      scheduleStatus();
    });
  }

  // Finished tasks stay in the status for a few minutes. Remember which ones the
  // user closed, for this browser session, so a reload doesn't bring them back.
  var DISMISSED_KEY = 'railpowered.dismissed-tasks';

  // A task's identity. Ids come from a per-process counter, so after a
  // redeploy a new "t1" must not be mistaken for the old one.
  function taskIdent(task) { return task.id + '@' + (task.started_at || ''); }

  function taskKey(task) { return taskIdent(task) + ':' + task.state + ':' + (task.finished_at || ''); }

  function readDismissed() {
    try { return JSON.parse(window.sessionStorage.getItem(DISMISSED_KEY) || '[]'); } catch (_) { return []; }
  }

  function isTaskDismissed(task) {
    var key = taskKey(task);
    return !!state.dismissedTasks[key] || readDismissed().indexOf(key) !== -1;
  }

  function dismissTask(task) {
    var key = taskKey(task);
    state.dismissedTasks[key] = true;
    try {
      var list = readDismissed().filter(function (k) { return k !== key; });
      list.push(key);
      window.sessionStorage.setItem(DISMISSED_KEY, JSON.stringify(list.slice(-20)));
    } catch (_) { /* storage blocked: it stays hidden until a reload */ }
  }

  function setConnected(ok, err) {
    if (state.connected === ok && ok) return;
    state.connected = ok;
    var banner = $('#conn-banner');
    // Keep showing the last known status, but make it look out of date.
    $('#hero').classList.toggle('is-stale', !ok);
    if (!ok) {
      var answered = err && err.status >= 500;
      setText($('#conn-title'), answered ? 'The dashboard hit an error' : 'Can\'t reach Railpowered');
      setText($('#conn-text'), answered
        ? 'The server answered with an error. If Railway is redeploying, this clears up in a minute or two.'
        : 'The dashboard will keep trying. If Railway is redeploying, this clears up in a minute or two.');
    }
    withStableScroll(function () { banner.hidden = ok; });
    renderPills();
    updateTitle();
  }

  // The banners sit above every view and stick to the top of the screen.
  // When one appears, changes size or goes away, scroll by the same amount so
  // the content under the pointer stays put; the sticky banner stays in sight.
  function withStableScroll(change) {
    var ref = document.getElementById('view-' + state.route.view);
    if (!ref || ref.hidden) { change(); return; }
    var before = ref.getBoundingClientRect().top;
    change();
    var shift = ref.getBoundingClientRect().top - before; // read after layout, so native scroll anchoring is included
    if (shift) window.scrollBy(0, shift);
  }

  function onVisibility() {
    if (document.hidden) {
      window.clearTimeout(timers.status); timers.status = null;
      window.clearTimeout(timers.logs); timers.logs = null;
    } else if (!redirecting) {
      pollStatus();
      if (state.route.view === 'console' || state.route.view === 'overview') pollLogs();
      refreshVisibleData();
    }
  }

  // Slow refresh for list data on the visible view, so changes made in-game or in
  // another tab show up without a reload. Never touches forms the user is editing.
  function refreshVisibleData() {
    if (document.hidden || redirecting) return;
    var v = state.route.view;
    if (v === 'worlds') { loadWorlds(true); loadBackups(true); }
    if (v === 'players') loadPlayers(true);
    if (v === 'setups') { loadSetups(true); loadWorlds(true); loadStorage(true); }
  }

  // Stays up (no dismiss) while the server ignores a weak ADMIN_KEY.
  function renderKeyBanner(warning) {
    var banner = $('#key-banner');
    if (!banner) return;
    var text = typeof warning === 'string' ? warning : '';
    if (text) setText($('#key-text'), text);
    if (banner.hidden === !text) return;
    withStableScroll(function () { banner.hidden = !text; });
  }

  function onStatus(s) {
    var prev = state.status;
    s.phase = PHASES[s.phase] ? s.phase : 'unknown';
    s.idle = s.idle || {};
    s.players = Array.isArray(s.players) ? s.players : [];
    state.status = s;
    state.statusAt = Date.now();

    if (prev && prev.phase !== s.phase) {
      announce('Server is ' + PHASES[s.phase].label.toLowerCase().replace('…', '') + '.');
    }
    // Minecraft reads server.properties when it starts, so once it has gone
    // down (or restarted between two polls), saved settings are in effect.
    if (prev && state.settings && state.settings.restart_required && serverRestarted(prev, s)) {
      state.settings.restart_required = false;
    }
    trackTask(prev ? prev.task : null, s.task);
    renderKeyBanner(s.admin_key_warning);

    renderStatus();

    if (prev) {
      var prevSetup = prev.setup && prev.setup.id, nextSetup = s.setup && s.setup.id;
      if (prevSetup !== nextSetup && state.setups) loadSetups(true);
      if (prev.whitelist_enabled !== s.whitelist_enabled && state.players) loadPlayers(true);
    }
    var crowdChanged = !prev || prev.phase !== s.phase || (prev.players || []).join('\n') !== s.players.join('\n');
    if (crowdChanged && state.route.view === 'players') renderPlayersOnline();
    if (crowdChanged && state.setupForm) renderSetupImpact();
    if (state.route.view === 'help') renderHelp();
    if (state.route.view === 'settings') renderRestartBanner();
    // Until the player lists load, the whitelist switch shows what the status says.
    if (!state.players) $('#wl-switch').checked = !!s.whitelist_enabled;
  }

  function serverRestarted(prev, s) {
    if (prev.phase === s.phase) return prev.uptime != null && s.uptime != null && s.uptime < prev.uptime;
    if (!RUNNING[s.phase]) return true;              // stopping, asleep, crashed, preparing
    return s.phase === 'starting' && prev.phase === 'online'; // a restart seen between polls
  }

  /* ================================================================ task tracking */

  // The task banner is the one place a task's outcome is shown (it sticks to
  // the top of the screen on every view); screen readers hear it once through
  // the announcer. A toast would only repeat it.
  function trackTask(prev, next) {
    if (!next || !next.id) return;
    var ident = taskIdent(next);
    if (!state.taskFirstSeen[ident]) state.taskFirstSeen[ident] = Date.now();
    if (next.state === 'running') {
      state.watchedTasks[ident] = true;
      return;
    }
    if (state.notifiedTasks[ident]) return;
    var wasRunning = state.watchedTasks[ident] || (prev && taskIdent(prev) === ident && prev.state === 'running');
    if (!wasRunning) return; // finished before this page loaded: the banner shows it
    state.notifiedTasks[ident] = true;
    var label = next.label || 'Task';
    if (next.state === 'done') announce(label + ': done. ' + (next.note ? next.note : taskDoneText(next)));
    else announce(label + ': failed. ' + (next.error || 'The console has the details.'));
    onTaskFinished(next);
  }

  // Remember how to send a task's request again (see state.taskRequests).
  function rememberTaskRequest(task, request) {
    if (!task || !task.id || !request) return;
    state.taskRequests[taskIdent(task)] = request;
  }

  function taskDowngradeRequest(task) {
    if (!task || task.state !== 'failed' || task.code !== 'world_downgrade') return null;
    return state.taskRequests[taskIdent(task)] || null;
  }

  // A task that found the world was played on a newer version (the check runs
  // once the exact version is known, e.g. after a modpack download): ask the
  // same question as before a switch, then send the same request again.
  function offerTaskDowngrade(task) {
    var request = taskDowngradeRequest(task);
    if (!request) return;
    var err = { message: task.error || 'This world was played on a newer version of Minecraft.', data: task.data || {} };
    downgradeDialog(err, request.world).then(function (choice) {
      if (choice !== 'continue' && choice !== 'new') return;
      delete state.taskRequests[taskIdent(task)];
      dismissTask(task);
      renderTaskBanner();
      if (choice === 'continue') request.retry(task.data || {});
      else request.newWorld(task.data || {});
    });
  }

  function taskDoneText(task) {
    switch (task.kind) {
      case 'backup': return 'The backup is in Worlds & backups.';
      case 'restore': return 'The world has been restored.';
      case 'switch': return 'The server is on the new setup.';
      case 'apply':
      case 'modpack': return 'The new setup is ready.';
      default: return '';
    }
  }

  function onTaskFinished(task) {
    if (state.setups || state.route.view === 'setups') loadSetups(true);
    if (state.worlds || state.route.view === 'worlds' || state.route.view === 'setups') loadWorlds(true);
    if (state.backups || state.route.view === 'worlds') loadBackups(true);
    if (state.storage || state.route.view === 'setups') loadStorage(true);
    var form = state.setupForm;
    var downgrade = !!taskDowngradeRequest(task);
    if (form && form.pendingTaskId === taskIdent(task)) {
      form.pendingTaskId = null;
      if (task.state === 'done') resetSetupForm();
      else if (downgrade) setText($('#setup-summary'), 'Nothing was changed yet. Choose how to go on in the dialog, or adjust the details and try again.');
      else setText($('#setup-summary'), 'That didn\'t work. Your details are still here, so you can adjust them and try again.');
      updateSetupSubmit();
    }
    if (task.state !== 'running' && !downgrade) delete state.taskRequests[taskIdent(task)];
    if (downgrade) offerTaskDowngrade(task);
  }

  function taskElapsed(task) {
    var started = toDate(task.started_at);
    var seen = state.taskFirstSeen[taskIdent(task)];
    var t0 = started ? started.getTime() : seen || Date.now();
    if (t0 > Date.now() + 5000) t0 = seen || Date.now();
    return Math.max(0, (Date.now() - t0) / 1000);
  }

  // No live role here: the counters below change every second and would be
  // read out each time. Outcomes are announced once by trackTask().
  function renderTaskBanner() {
    var banner = $('#task-banner');
    withStableScroll(function () { renderTaskBannerNow(banner); });
  }

  function renderTaskBannerNow(banner) {
    var s = state.status;
    var task = s && s.task;
    if (!task || !task.id || isTaskDismissed(task)) {
      banner.hidden = true;
      banner.removeAttribute('data-key');
      return;
    }
    var downgrade = !!taskDowngradeRequest(task);
    var key = taskIdent(task) + ':' + task.state + ':' + (task.label || '') + ':' + (task.error || '') + ':' + (task.note || '') + ':' + downgrade;
    banner.hidden = false;
    if (banner.getAttribute('data-key') === key) { updateTaskTimes(); return; }
    var hadFocus = banner.contains(document.activeElement);
    banner.setAttribute('data-key', key);
    var label = task.label || 'Working';
    var dismissBtn = el('button', {
      class: 'icon-btn', type: 'button', 'aria-label': 'Hide this message',
      onclick: function () { dismissTask(task); renderTaskBanner(); $('#main').focus({ preventScroll: true }); },
    }, icon('i-x', 'icon-sm'));
    if (task.state === 'running') {
      banner.className = 'banner tone-work';
      banner.replaceChildren(
        spinner(),
        el('div', { class: 'banner-body' },
          el('p', { class: 'banner-title' }, label + '…'),
          el('p', { class: 'banner-text' }, el('span', { id: 'task-elapsed', class: 'tabular' }), ' · You can keep using the dashboard while this runs.'),
          el('div', { class: 'task-track', 'aria-hidden': 'true' })));
    } else if (task.state === 'done') {
      var doneText = taskDoneText(task);
      // A note is a warning about the result (say, a restored world that was
      // played on a newer version), so the banner takes the warning tone.
      banner.className = 'banner ' + (task.note ? 'tone-warn' : 'tone-ok');
      banner.replaceChildren(
        icon(task.note ? 'i-alert' : 'i-check-circle'),
        el('div', { class: 'banner-body' },
          el('p', { class: 'banner-title' }, label + (task.note ? ': done, with a warning' : ': done')),
          task.note ? el('p', { class: 'banner-text banner-note' }, task.note) : null,
          el('p', { class: 'banner-text' }, doneText ? doneText + ' ' : '', 'Finished ',
            el('span', { id: 'task-finished', title: formatDateTime(task.finished_at) || null }), '.')),
        el('div', { class: 'banner-actions' }, dismissBtn));
    } else {
      banner.className = 'banner tone-bad';
      banner.replaceChildren(
        icon('i-alert-circle'),
        el('div', { class: 'banner-body' },
          el('p', { class: 'banner-title' }, label + ': failed'),
          el('p', { class: 'banner-text' }, task.error || 'It stopped with an error. The console has the details.'),
          task.note ? el('p', { class: 'banner-text banner-note' }, task.note) : null),
        el('div', { class: 'banner-actions' },
          // The downgrade question was asked in a dialog; this brings it back if it was closed.
          downgrade ? el('button', { class: 'btn btn-sm', type: 'button', onclick: function () { offerTaskDowngrade(task); } },
            icon('i-alert', 'icon-sm'), 'Review options') : null,
          el('a', { class: 'btn btn-sm', href: '#/console' }, icon('i-console', 'icon-sm'), 'Open console'),
          dismissBtn));
    }
    updateTaskTimes();
    if (hadFocus) $('#main').focus({ preventScroll: true });
  }

  // Called every second: the elapsed time of a running task, and how long ago a finished one ended.
  function updateTaskTimes() {
    var task = state.status && state.status.task;
    if (!task) return;
    var elapsed = $('#task-elapsed');
    if (elapsed && task.state === 'running') setText(elapsed, 'Running for ' + formatDuration(taskElapsed(task)));
    var finished = $('#task-finished');
    if (finished && task.state === 'done') setText(finished, relativeTime(task.finished_at) || 'just now');
  }

  /* ================================================================ status rendering */

  function liveStatus() {
    var s = state.status;
    if (!s) return null;
    var dt = state.connected ? Math.max(0, (Date.now() - state.statusAt) / 1000) : 0;
    return {
      uptime: s.uptime != null ? s.uptime + dt : null,
      untilSleep: s.idle && s.idle.seconds_until_sleep != null ? Math.max(0, s.idle.seconds_until_sleep - dt) : null,
    };
  }

  function renderStatus() {
    renderPills();
    renderHero();
    renderTaskBanner();
    renderOverviewCards();
    updateConsoleInput();
    updateSetupSubmit();
    updateBackupButtons();
    updateTitle();
    var count = state.status ? state.status.player_count || 0 : 0;
    var badge = $('#nav-online-count');
    badge.hidden = !(state.status && state.status.phase === 'online' && count > 0);
    setText(badge, String(count));
    if (!badge.hidden) badge.setAttribute('aria-label', plural(count, 'player', 'players') + ' online');
    setText($('#version-line'), (state.status && state.status.product || 'Railpowered') + (state.status && state.status.app_version ? ' ' + state.status.app_version : ''));
  }

  function renderPills() {
    var s = state.status;
    var phase = s ? s.phase : 'unknown';
    var meta = PHASES[phase] || PHASES.unknown;
    var label = state.connected ? meta.label : 'Reconnecting…';
    var sub = state.connected ? (s ? meta.sub(s) : meta.sub({})) : 'Trying to reach your server';
    var shownPhase = state.connected ? phase : 'unknown';
    [$('#topbar-pill'), $('#sidebar-status')].forEach(function (node) {
      node.setAttribute('data-phase', shownPhase);
      setText(node.querySelector('.pill-text'), label);
      node.setAttribute('aria-label', 'Server status: ' + label + '. ' + sub);
    });
    setText($('#sidebar-status-sub'), sub);
  }

  var heroKey = '';

  function renderHero() {
    var s = state.status;
    var hero = $('#hero');
    var phase = s ? s.phase : 'unknown';
    var meta = PHASES[phase] || PHASES.unknown;
    hero.setAttribute('data-phase', phase);
    setIcon($('#hero-icon'), meta.icon);
    setText($('#phase-word'), meta.label);
    setText($('#phase-copy'), s ? meta.copy(s) : meta.copy({}));

    var busyTask = runningTask();
    var key = phase + '|' + Object.keys(state.pending).sort().join(',') + '|' + (s ? (s.player_count > 0 ? 'p' : '') : '') +
      '|' + (busyTask ? busyTask.label : '');
    if (key !== heroKey) {
      heroKey = key;
      renderHeroActions(s);
    }
    if (!s) return;

    renderAddress(s.join_address || '');
    $('#address-callout').hidden = !!s.join_address;

    var factVersion = $('#fact-version');
    var setup = s.setup;
    fill(factVersion,
      setup ? el('span', null, versionLabel(setup) || '—') : el('span', { class: 'subtle' }, 'No setup'),
      setup ? el('span', { class: 'subtle' }, setup.type === 'modpack' ? 'Modpack' : (TYPE_LABEL[setup.type] || setup.type)) : null);

    var players = $('#fact-players');
    var n = s.player_count || 0;
    players.replaceChildren(
      el('span', null, String(RUNNING[s.phase] ? n : 0)),
      el('span', { class: 'subtle' }, 'of ' + (s.max_players || 0)));

    var live = liveStatus();
    var timeLabel = $('#fact-time-label');
    var timeVal = $('#fact-time');
    var timeout = s.idle.timeout_seconds;
    var timeKey;
    if (RUNNING[s.phase] && live.uptime != null) {
      timeKey = 'uptime';
      setText(timeLabel, 'Uptime');
      if (timeVal.getAttribute('data-key') !== timeKey) timeVal.replaceChildren(el('span', { id: 'uptime-value' }));
    } else if (s.phase === 'crashed' && s.last_exit && s.last_exit.at) {
      timeKey = 'exit:' + s.last_exit.at;
      setText(timeLabel, 'Last exit');
      if (timeVal.getAttribute('data-key') !== timeKey) timeVal.replaceChildren(el('span', { id: 'exit-ago', title: formatDateTime(s.last_exit.at) }));
    } else if (!RUNNING[s.phase] && s.idle.wake_paused_until) {
      // Woken lately with nobody joining: joins don't wake it until then.
      var pausedUntil = s.idle.wake_paused_until;
      timeKey = 'paused:' + pausedUntil;
      setText(timeLabel, 'Joins wake it');
      if (timeVal.getAttribute('data-key') !== timeKey) {
        timeVal.replaceChildren(
          el('span', { title: formatDateTime(pausedUntil) },
            'From ' + new Date(pausedUntil * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })),
          el('span', { class: 'subtle' }, 'or press Start'));
      }
    } else {
      // Not "sleeps after": while it's asleep or stopped, that read as if it were about to happen.
      timeKey = 'sleep:' + timeout;
      setText(timeLabel, 'Auto-sleep');
      if (timeVal.getAttribute('data-key') !== timeKey) {
        timeVal.replaceChildren(timeout > 0
          ? el('span', null, 'After ' + formatMinutesShort(timeout / 60))
          : el('span', null, 'Off'));
        if (timeout > 0) timeVal.appendChild(el('span', { class: 'subtle' }, 'idle'));
      }
    }
    timeVal.setAttribute('data-key', timeKey);
    tickHeroTimes(live);

    renderCrashBox(s);
    renderHeroStrip(s, live);
  }

  // Plain-language next steps for the crashes friends' servers actually hit.
  var CRASH_HINTS = [
    { re: /OutOfMemoryError|Java heap space|GC overhead limit/i,
      text: 'Minecraft ran out of memory. Pick a lighter setup (fewer mods, smaller view distance), or give the Railway service more RAM.' },
    { re: /UnsupportedClassVersionError|has been compiled by a more recent version of the Java Runtime/i,
      text: 'Something on this setup needs a newer Java than the one it started with. Try another Minecraft version or loader version.' },
    { re: /FAILED TO BIND TO PORT|Address already in use/i,
      text: 'Another program was using the Minecraft port. Restart the service in Railway, then start the server again.' },
    { re: /Incompatible mods? found|requires .*(?:minecraft|fabric|forge)|Missing or unsupported mandatory dependencies|ModResolutionException/i,
      text: 'A mod doesn\'t match this Minecraft version, or needs another mod that isn\'t installed. Check the pack, or switch to a setup that works.' },
    { re: /Failed to load level|level\.dat|Exception reading .*\.mca|corrupt/i,
      text: 'Minecraft couldn\'t read the world. Restore a backup from Worlds & backups, or start the setup on a new world.' },
  ];

  function renderCrashBox(s) {
    var crash = $('#crash-box');
    if (s.phase !== 'crashed' || !s.last_exit) { crash.hidden = true; crash.removeAttribute('data-key'); return; }
    crash.hidden = false;
    var reason = s.last_exit.reason || '';
    if (crash.getAttribute('data-key') === reason) return;
    crash.setAttribute('data-key', reason);
    setText($('#crash-reason'), reason || 'Minecraft didn\'t print a reason.');
    var hint = CRASH_HINTS.filter(function (h) { return h.re.test(reason); })[0];
    fill($('#crash-next'),
      hint ? el('p', { class: 'crash-hint' }, icon('i-info', 'icon-sm'), el('span', null, hint.text)) : null,
      el('p', { class: 'crash-links' },
        el('a', { href: '#/console' }, 'Open the console for the full log'),
        el('span', { class: 'sep', 'aria-hidden': 'true' }, '·'),
        el('a', { href: '#/help/crashed' }, 'More about crashes')));
  }

  function runningTask() {
    var t = state.status && state.status.task;
    return t && t.state === 'running' ? t : null;
  }

  // Why a change that runs as a task (switch, install, back up, restore) can't
  // start right now, or ''. The server answers 409 busy while another task runs
  // and while Minecraft is starting or stopping.
  function worldOpsWait() {
    var task = runningTask();
    if (task) return 'Available when “' + (task.label || 'the current task') + '” finishes';
    var phase = state.status && state.status.phase;
    if (phase === 'starting') return 'Available once the server is online';
    if (phase === 'stopping') return 'Available once the server has stopped';
    return '';
  }

  // Like the hero's Start and Stop: an unavailable button stays focusable and
  // says why in its title, instead of going dead with `disabled`.
  function markUnavailable(button, reason) {
    if (!button || button.getAttribute('aria-busy') === 'true') return;
    if (reason) {
      button.setAttribute('aria-disabled', 'true');
      button.title = reason;
    } else {
      button.removeAttribute('aria-disabled');
      button.removeAttribute('title');
    }
  }

  function isUnavailable(button) { return !!button && button.getAttribute('aria-disabled') === 'true'; }

  // Unavailable buttons use aria-disabled rather than disabled, so a button
  // keeps keyboard focus while its request runs; focus is put back on the same
  // action after every rebuild instead of falling to <body>.
  function renderHeroActions(s) {
    var host = $('#hero-actions');
    var phase = s ? s.phase : 'unknown';
    var task = runningTask();
    // The server refuses to start, stop or restart while any task runs.
    var taskWait = task ? 'Available when “' + (task.label || 'the current task') + '” finishes' : '';
    var defs = [];
    if (phase === 'sleeping') {
      defs.push({ id: 'start', label: 'Start server', busy: 'Starting…', icon: 'i-power', style: 'primary', wait: taskWait });
    } else if (phase === 'crashed') {
      defs.push({ id: 'log', label: 'View log', icon: 'i-console', style: 'ghost', href: '#/console' });
      defs.push({ id: 'start', label: 'Start again', busy: 'Starting…', icon: 'i-power', style: 'primary', wait: taskWait });
    } else if (phase === 'preparing') {
      defs.push({ id: 'start', label: 'Start server', icon: 'i-power', style: 'primary', wait: taskWait || 'Available when the current task finishes' });
    } else if (phase === 'starting') {
      defs.push({ id: 'stop', label: 'Stop', busy: 'Stopping…', icon: 'i-stop', style: 'secondary', wait: taskWait });
    } else if (phase === 'online') {
      defs.push({ id: 'save', label: 'Save world', busy: 'Saving…', icon: 'i-save', style: 'ghost' });
      defs.push({ id: 'restart', label: 'Restart', busy: 'Restarting…', icon: 'i-restart', style: 'secondary', wait: taskWait });
      defs.push({ id: 'stop', label: 'Stop', busy: 'Stopping…', icon: 'i-stop', style: 'outline-danger', wait: taskWait });
    } else if (phase === 'stopping') {
      defs.push({ id: 'stop', label: 'Stopping…', icon: 'i-stop', style: 'secondary', wait: 'The server is shutting down', spin: true });
    }
    var active = document.activeElement;
    var focusedAction = host.contains(active) ? active.getAttribute('data-action') : null;
    var anyPending = Object.keys(state.pending).length > 0;
    var nodes = defs.map(function (d) {
      if (d.href) return el('a', { class: btnClass(d.style), href: d.href, 'data-action': d.id }, icon(d.icon), el('span', null, d.label));
      var pending = !!state.pending[d.id];
      var off = !!d.wait || anyPending;
      var b = el('button', {
        type: 'button', class: btnClass(d.style) + (d.style === 'primary' ? ' btn-lg' : ''),
        'data-action': d.id,
        'aria-disabled': off ? 'true' : null,
        'aria-busy': pending || d.spin ? 'true' : null,
        title: d.wait || null,
      }, pending || d.spin ? spinner() : icon(d.icon), el('span', null, pending ? d.busy : d.label));
      b.addEventListener('click', function () {
        if (b.getAttribute('aria-disabled') === 'true') return;
        serverAction(d.id);
      });
      return b;
    });
    host.replaceChildren.apply(host, nodes);
    if (focusedAction) {
      var target = host.querySelector('[data-action="' + focusedAction + '"]') ||
        host.querySelector('button:not([aria-disabled="true"]), a');
      if (!target) {
        target = $('#phase-word');
        target.setAttribute('tabindex', '-1');
      }
      target.focus({ preventScroll: true });
    }
  }

  // opts.allowDowngrade: start a world that was played on a newer version
  // (asked for after a 409 world_downgrade; the world is backed up first).
  function serverAction(id, opts) {
    opts = opts || {};
    var s = state.status || {};
    var n = s.player_count || 0;
    var confirmFirst = Promise.resolve(true);
    if ((id === 'stop' || id === 'restart') && s.phase === 'online' && n > 0) {
      confirmFirst = confirmDialog({
        tone: id === 'stop' ? 'danger' : 'primary',
        icon: id === 'stop' ? 'i-stop' : 'i-restart',
        title: id === 'stop' ? 'Stop the server?' : 'Restart the server?',
        body: [
          el('p', null, el('strong', null, plural(n, 'player is', 'players are')), ' online and will be disconnected.'),
          el('p', null, 'The world is saved first. ' + (id === 'stop'
            ? 'The server goes to sleep and wakes up when someone joins.'
            : 'Everyone can rejoin once it\'s back online, usually within a minute or two.')),
        ],
        confirmLabel: id === 'stop' ? 'Stop server' : 'Restart server',
        confirmIcon: id === 'stop' ? 'i-stop' : 'i-restart',
      });
    }
    confirmFirst.then(function (ok) {
      if (!ok) return;
      state.pending[id] = true;
      heroKey = '';
      if (id === 'restart' && state.settings) {
        // The restart applies saved settings, so the "restart to apply" banner can go now.
        state.settings.restart_required = false;
        renderRestartBanner();
      }
      renderHero();
      var path = { start: '/api/server/start', stop: '/api/server/stop', restart: '/api/server/restart', save: '/api/server/save' }[id];
      api('POST', path, opts.allowDowngrade ? { allow_downgrade: true } : undefined).then(function () {
        // The save is only requested here; Minecraft writes in the background and logs "Saved the game".
        if (id === 'save') toast({ tone: 'ok', title: 'Save requested', text: 'Minecraft is writing the world to disk. The console shows when it\'s done.' });
      }, function (err) {
        if (err.code === 'already_running') { toast({ tone: 'info', title: 'Already running', text: err.message }); return; }
        if (id === 'start' && err.code === 'world_downgrade') { offerStartDowngrade(err); return; }
        if (id === 'restart' && state.settings) loadSettings(); // the banner was hidden optimistically
        var titles = { start: 'Couldn\'t start the server', stop: 'Couldn\'t stop the server', restart: 'Couldn\'t restart the server', save: 'Couldn\'t save the world' };
        toastError(titles[id], err);
      }).finally(function () {
        delete state.pending[id];
        heroKey = '';
        pollStatus().then(function () { if (state.status) renderHero(); });
      });
    });
  }

  // The active setup's world was played on a newer version (often a restored
  // backup). Same choices as before a switch: an empty world, or a backup and
  // then open it anyway.
  function offerStartDowngrade(err) {
    var setup = state.status && state.status.setup;
    downgradeDialog(err, setup && setup.level_name).then(function (choice) {
      if (choice === 'continue') { serverAction('start', { allowDowngrade: true }); return; }
      if (choice !== 'new' || !setup || !setup.id) return;
      (state.worlds ? Promise.resolve() : loadWorlds(true)).then(function () {
        state.pending.start = true;
        heroKey = '';
        renderHero();
        return moveSetupToNewWorld(setup, null, { start: true });
      }).finally(function () {
        delete state.pending.start;
        heroKey = '';
        renderHero();
      });
    });
  }

  function renderAddress(address) {
    var dd = $('#fact-address');
    if (dd.getAttribute('data-address') === address && dd.firstChild) return;
    dd.setAttribute('data-address', address);
    if (!address) {
      dd.replaceChildren(el('span', { class: 'address-empty' },
        icon('i-alert', 'icon-sm'),
        el('span', null, 'No public address yet. ', el('a', { href: '#/help/address' }, 'How to add one'))));
      return;
    }
    var copyBtn = el('button', { class: 'btn btn-sm address-copy', type: 'button', 'aria-label': 'Copy join address' },
      icon('i-copy', 'icon-sm'), el('span', { class: 'copy-label' }, 'Copy'));
    // If the address has to wrap on a narrow phone, let it break before the port rather than inside it.
    var colon = address.lastIndexOf(':');
    var code = colon > 0
      ? el('code', { id: 'join-address' }, address.slice(0, colon), el('wbr'), address.slice(colon))
      : el('code', { id: 'join-address' }, address);
    copyBtn.addEventListener('click', function () {
      copyText(address).then(function (ok) {
        if (ok) {
          copyBtn.replaceChildren(icon('i-check', 'icon-sm'), el('span', { class: 'copy-label' }, 'Copied'));
          copyBtn.classList.add('btn-primary');
          announce('Join address copied.');
          window.setTimeout(function () {
            copyBtn.classList.remove('btn-primary');
            copyBtn.replaceChildren(icon('i-copy', 'icon-sm'), el('span', { class: 'copy-label' }, 'Copy'));
          }, 1800);
        } else {
          selectText(code);
          toast({ tone: 'info', title: 'Copy it with the keyboard', text: 'Your browser blocked automatic copying, so the address is selected for you. Press Ctrl+C, or Cmd+C on a Mac.' });
        }
      });
    });
    dd.replaceChildren(el('div', { class: 'address' }, code, copyBtn));
  }

  function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) {
      return navigator.clipboard.writeText(text).then(function () { return true; }, function () { return legacyCopy(text); });
    }
    return Promise.resolve(legacyCopy(text));
  }

  function legacyCopy(text) {
    var ta = el('textarea', { readonly: true, 'aria-hidden': 'true' });
    ta.value = text;
    ta.style.position = 'fixed';
    ta.style.top = '0';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    var ok = false;
    try { ok = document.execCommand('copy'); } catch (_) { ok = false; }
    ta.remove();
    return ok;
  }

  function selectText(node) {
    var range = document.createRange();
    range.selectNodeContents(node);
    var sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(range);
  }

  function renderHeroStrip(s, live) {
    var strip = $('#hero-strip');
    var items = [];
    var timeout = s.idle.timeout_seconds || 0;
    if (s.phase === 'online') {
      if (timeout <= 0) {
        items.push(el('span', { class: 'strip-item' }, icon('i-moon', 'icon-sm'), 'Idle sleep is off. The server stays on until you stop it.'));
      } else if (live.untilSleep != null) {
        var pct = Math.max(0, Math.min(100, (live.untilSleep / timeout) * 100));
        items.push(el('span', { class: 'strip-item' }, icon('i-moon', 'icon-sm'),
          el('span', { id: 'sleep-line' }, sleepLine(live.untilSleep))));
        var meter = el('span', { class: 'idle-meter', 'aria-hidden': 'true' }, el('span', { id: 'sleep-meter' }));
        meter.firstChild.style.width = pct + '%';
        items.push(meter);
      } else {
        items.push(el('span', { class: 'strip-item' }, icon('i-moon', 'icon-sm'),
          'Stays awake while anyone is online, then sleeps after ' + formatMinutes(Math.round(timeout / 60)) + ' with nobody on.'));
      }
    } else if (s.phase === 'sleeping') {
      items.push(el('span', { class: 'strip-item' }, icon('i-zap', 'icon-sm'), 'Wake on join is on. The first join takes about a minute.'));
    } else if (s.phase === 'starting') {
      items.push(el('span', { class: 'strip-item' }, icon('i-clock', 'icon-sm'), 'The sleep timer starts once the server is online.'));
    } else if (s.phase === 'preparing') {
      items.push(el('span', { class: 'strip-item' }, icon('i-info', 'icon-sm'), 'Anyone who tries to join now is asked to try again in a minute.'));
    } else if (s.phase === 'crashed' && s.last_exit) {
      // When it happened is in the facts above; the exit code helps when asking for help.
      if (s.last_exit.code != null) items.push(el('span', { class: 'strip-item' }, icon('i-info', 'icon-sm'), 'Exit code ' + s.last_exit.code));
    }
    var task = runningTask();
    if (task && s.phase !== 'preparing' && s.phase !== 'stopping') {
      items.unshift(el('span', { class: 'strip-item strip-wait' }, icon('i-hourglass', 'icon-sm'),
        (s.phase === 'online' || s.phase === 'starting' ? 'Stop and restart' : 'Starting') + ' wait for “' + (task.label || 'the current task') + '” to finish.'));
    }
    if (s.java) {
      items.push(el('span', { class: 'strip-item strip-end' }, icon('i-coffee', 'icon-sm'), 'Java ' + s.java));
    }
    var key = s.phase + '|' + timeout + '|' + (live.untilSleep != null) + '|' + s.java + '|' + (s.last_exit ? s.last_exit.at : '') +
      '|' + (task ? task.label : '');
    if (strip.getAttribute('data-key') === key && items.length) { tickHero(); return; }
    strip.setAttribute('data-key', key);
    strip.hidden = items.length === 0;
    strip.replaceChildren.apply(strip, items);
  }

  function sleepLine(seconds) {
    if (seconds > 0) return ['Sleeps in ', el('strong', { id: 'sleep-countdown', class: 'tabular' }, formatClock(seconds)), ' if nobody joins'];
    return 'Going to sleep now, since nobody is on.';
  }

  // Once a second: keep countdowns and timers moving between polls.
  function tickHero() {
    var s = state.status;
    if (!s) return;
    var live = liveStatus();
    var line = $('#sleep-line');
    if (line && live.untilSleep != null) {
      var cd = $('#sleep-countdown');
      if (cd && live.untilSleep > 0) setText(cd, formatClock(live.untilSleep));
      else if (cd || live.untilSleep > 0) fill(line, sleepLine(live.untilSleep));
      var meter = $('#sleep-meter');
      var timeout = s.idle.timeout_seconds || 1;
      if (meter) meter.style.width = Math.max(0, Math.min(100, (live.untilSleep / timeout) * 100)) + '%';
    }
    tickHeroTimes(live);
    updateTaskTimes();
  }

  function tickHeroTimes(live) {
    var up = $('#uptime-value');
    if (up && live.uptime != null) setText(up, formatDuration(live.uptime));
    var ago = $('#exit-ago');
    var exit = state.status && state.status.last_exit;
    if (ago && exit && exit.at) setText(ago, capitalize(relativeTime(exit.at) || 'just now'));
  }

  /* ---------------------------------------------------------------- overview cards */

  function renderOverviewCards() {
    var s = state.status;
    if (!s) return;
    var names = RUNNING[s.phase] ? s.players : [];
    setText($('#online-count'), names.length ? '(' + names.length + ')' : '');
    var list = $('#online-list');
    var key = s.phase + '|' + names.join(',') + '|' + (s.join_address ? 'a' : '');
    if (list.getAttribute('data-key') !== key) {
      list.setAttribute('data-key', key);
      if (!names.length) {
        var copy = onlineEmptyCopy(s);
        list.replaceChildren(emptyStateCompact('i-players', copy[0], copy[1]));
      } else {
        fill(list, peopleList(names.slice(0, 12), null),
          names.length > 12 ? el('p', { class: 'subtle more-note' }, 'and ' + (names.length - 12) + ' more') : null);
      }
    }

    var setup = s.setup;
    var cur = $('#current-setup');
    var skey = JSON.stringify([setup, s.java, s.online_mode, s.whitelist_enabled, !!s.join_address]);
    if (cur.getAttribute('data-key') !== skey) {
      cur.setAttribute('data-key', skey);
      if (!setup) {
        cur.replaceChildren(emptyStateCompact('i-setups', 'No active setup', 'Create one in Setups to choose a version.'));
      } else {
        cur.replaceChildren(
          el('div', { class: 'setup-ident' },
            el('span', { class: 'glyph t-' + setup.type }, icon(TYPE_ICON[setup.type] || 'i-cube')),
            el('div', { class: 'min-w-0' },
              el('p', { class: 'setup-ident-name' }, setup.name || setupSummary(setup)),
              el('p', { class: 'subtle text-sm' }, (setup.type === 'modpack' ? 'Modpack' : TYPE_LABEL[setup.type] || setup.type) + ' server'))),
          el('dl', { class: 'facts facts-2' },
            fact('Minecraft', versionLabel(setup) || '—'),
            fact('World', setup.level_name || '—'),
            setup.type === 'modpack' ? fact('Pack', setup.instance || '—') : fact('Java', s.java ? 'Java ' + s.java : '—'),
            fact('Joining', !s.join_address ? 'Nobody yet: no public address' : (s.whitelist_enabled ? 'Whitelist only' : 'Anyone with the address'))));
      }
    }

    var fr = $('#firstrun');
    fr.hidden = store.get(KEYS.firstRun) === '1';
    var exact = exactVersion(setup);
    $$('[data-bind="version"]', fr).forEach(function (n) {
      setText(n, !setup ? 'Java Edition' : (exact ? 'Java Edition ' + exact : 'Java Edition, ' + UNKNOWN_LATEST));
    });
    var hasAddress = !!s.join_address;
    if (fr.getAttribute('data-address') !== String(hasAddress)) {
      fr.setAttribute('data-address', String(hasAddress));
      setText($('#firstrun-share-title'), hasAddress ? 'Share the join address' : 'Add a public address first');
      fill($('#firstrun-share-lead'), hasAddress
        ? 'Friends add it in Minecraft under Multiplayer, Add Server.'
        : ['Friends can\'t reach the server until it has one. ', el('a', { href: '#/help/address' }, 'Here\'s how'), '.']);
    }
    var privateKey = s.whitelist_enabled ? 'on' : 'off';
    var privateTip = $('#firstrun-private');
    if (privateTip.getAttribute('data-key') !== privateKey) {
      privateTip.setAttribute('data-key', privateKey);
      setText($('#firstrun-private-title'), s.whitelist_enabled ? 'It\'s private' : 'Make it private');
      fill(privateTip, s.whitelist_enabled
        ? ['Only players on the whitelist can join. Add your friends in ', el('a', { href: '#/players' }, 'Players'), '.']
        : ['Turn on the whitelist in ', el('a', { href: '#/players' }, 'Players'), ' so only your friends can get in.']);
    }
  }

  // [title, line] for an empty "Online now" list, matching what the server is doing.
  function onlineEmptyCopy(s) {
    switch (s && s.phase) {
      case 'online': return ['Nobody is online right now.', s.join_address
        ? 'Share the join address and your friends will show up here.'
        : 'Once you add a public address, friends can join and show up here.'];
      case 'starting': return ['The server is still starting.', 'Players show up here once it\'s online.'];
      case 'sleeping': return ['The server is asleep.', 'It wakes up when someone joins.'];
      case 'crashed': return ['The server isn\'t running.', 'It crashed. See “What happened” above.'];
      case 'preparing': return ['Installing files.', 'Nobody can join until this finishes.'];
      case 'stopping': return ['The server is stopping.', 'It\'s saving the world and shutting down.'];
      default: return ['Checking on the server…', 'Players show up here while they\'re connected.'];
    }
  }

  function fact(label, value) {
    return el('div', { class: 'fact' }, el('dt', { class: 'eyebrow' }, label), el('dd', { class: 'fact-value' }, value));
  }

  function emptyStateCompact(iconName, title, text) {
    var node = emptyState(iconName, title, text);
    node.classList.add('compact');
    return node;
  }

  function peopleList(names, kind) {
    var online = {};
    ((state.status && RUNNING[state.status.phase] && state.status.players) || []).forEach(function (n) { online[String(n).toLowerCase()] = true; });
    var items = names.map(function (entry) {
      var name = typeof entry === 'string' ? entry : entry.name;
      var isOnline = online[String(name).toLowerCase()];
      return el('li', { class: 'person' },
        avatar(name),
        el('div', { class: 'min-w-0' },
          el('p', { class: 'person-name', title: name }, name),
          kind ? el('p', { class: 'person-sub' }, isOnline ? 'Online now' : (kind === 'ops' ? 'Operator' : 'Can join')) : el('p', { class: 'person-sub' }, 'Playing')),
        kind ? el('button', {
          class: 'btn btn-ghost-danger btn-icon btn-sm', type: 'button',
          'aria-label': kind === 'ops' ? 'Remove ' + name + ' as operator' : 'Remove ' + name + ' from the whitelist',
          title: 'Remove', 'data-remove': kind + ':' + String(name).toLowerCase(),
          onclick: function (e) { removePlayer(kind, name, e.currentTarget); },
        }, icon('i-x', 'icon-sm')) : null);
    });
    var removed = kind && state.removedPlayers[kind];
    if (removed && !names.some(function (e) { return String(typeof e === 'string' ? e : e.name).toLowerCase() === removed.name.toLowerCase(); })) {
      // Undo lives in the list, where the player was, so it is easy to reach from the keyboard.
      items.push(el('li', { class: 'person is-removed' },
        avatar(removed.name),
        el('div', { class: 'min-w-0' },
          el('p', { class: 'person-name', title: removed.name }, removed.name),
          el('p', { class: 'person-sub' }, 'Removed')),
        el('button', {
          class: 'btn btn-sm', type: 'button', 'data-undo': kind,
          'aria-label': 'Undo removing ' + removed.name,
          onclick: function (e) { undoRemovePlayer(kind, e.currentTarget); },
        }, icon('i-rotate', 'icon-sm'), el('span', null, 'Undo'))));
    }
    return el('ul', { class: 'people', role: 'list' }, items);
  }

  /* ================================================================ logs */

  var logState = { lines: [], lastSeq: 0, loaded: false, follow: true, unseen: 0, filter: '', rendered: false };

  function scheduleLogs() {
    window.clearTimeout(timers.logs);
    timers.logs = null;
    if (document.hidden || redirecting) return;
    var v = state.route.view;
    if (v === 'console') timers.logs = window.setTimeout(pollLogs, 1500);
    else if (v === 'overview') timers.logs = window.setTimeout(pollLogs, 4000);
  }

  function pollLogs() {
    if (inflight.logs || redirecting) return;
    inflight.logs = true;
    window.clearTimeout(timers.logs);
    var url = logState.loaded ? '/api/logs?after=' + encodeURIComponent(logState.lastSeq) : '/api/logs';
    api('GET', url).then(function (data) {
      var incoming = Array.isArray(data.lines) ? data.lines : [];
      var last = Number(data.last_seq);
      if (logState.loaded && !isNaN(last) && last < logState.lastSeq) {
        // The buffer restarted (the dashboard process restarted): start over.
        logState.lines = []; logState.lastSeq = 0; logState.loaded = false; logState.rendered = false;
        $('#log').replaceChildren();
        // Someone watching the Console keeps getting lines: show the loading state and append again.
        if (state.route.view === 'console') renderFullLog();
        renderMiniLog();
        inflight.logs = false;
        pollLogs();
        return;
      }
      var fresh = incoming.filter(function (l) { return Number(l.seq) > logState.lastSeq; });
      if (!isNaN(last)) logState.lastSeq = Math.max(logState.lastSeq, last);
      fresh.forEach(function (l) { logState.lastSeq = Math.max(logState.lastSeq, Number(l.seq) || 0); });
      logState.loaded = true;
      if (fresh.length) appendLogs(fresh);
      else if (!logState.lines.length) renderLogEmpty();
    }, function () { /* the status banner covers connection problems */ }).finally(function () {
      inflight.logs = false;
      scheduleLogs();
    });
  }

  // Minecraft prints its own clock and a "[thread/LEVEL]:" tag on every line.
  // The time column already shows the clock, so drop it, and dim the tag.
  var LOG_CLOCK_RE = /^\[\d{2}:\d{2}:\d{2}\]\s*|^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\s+/;
  var LOG_TAG_RE = /^\[([^\]\/]{1,40}\/)?(INFO|WARN|WARNING|ERROR|FATAL|SEVERE|DEBUG)\]:?\s*/;

  function splitLogText(text) {
    var rest = String(text == null ? '' : text).replace(LOG_CLOCK_RE, '');
    var m = LOG_TAG_RE.exec(rest);
    if (!m) return { tag: '', level: '', msg: rest };
    return { tag: m[0].trim(), level: m[2].toLowerCase(), msg: rest.slice(m[0].length) };
  }

  function logLevel(line, parsedLevel) {
    var lvl = String(line.level || parsedLevel || '').toLowerCase();
    if (lvl === 'warn' || lvl === 'warning') return 'warn';
    if (lvl === 'error' || lvl === 'fatal' || lvl === 'severe') return 'error';
    return 'info';
  }

  function logLineNode(line) {
    var parts = splitLogText(line.text);
    var level = logLevel(line, parts.level);
    var x = el('span', { class: 'log-x' });
    if (line.source === 'panel') x.appendChild(el('span', { class: 'log-src', title: 'A message from Railpowered itself' }, 'Railpowered'));
    if (parts.tag) x.appendChild(el('span', { class: 'log-tag' }, parts.tag + ' '));
    x.appendChild(document.createTextNode(parts.msg));
    var row = el('div', { class: 'log-line lvl-' + level }, el('span', { class: 'log-t' }, line.t || ''), x);
    if (logState.filter && !matchesFilter(line)) row.classList.add('is-hidden');
    return row;
  }

  function matchesFilter(line) {
    return String(line.text || '').toLowerCase().indexOf(logState.filter) !== -1;
  }

  function appendLogs(fresh) {
    logState.lines = logState.lines.concat(fresh);
    var overflow = logState.lines.length - MAX_LOG_LINES;
    if (overflow > 0) logState.lines.splice(0, overflow);
    if (logState.rendered) {
      var box = $('#log');
      var empty = box.querySelector('.log-empty:not(.log-nomatch)');
      if (empty) empty.remove();
      var frag = document.createDocumentFragment();
      fresh.forEach(function (l) { frag.appendChild(logLineNode(l)); });
      var noMatch = box.querySelector('.log-nomatch');
      if (noMatch) box.insertBefore(frag, noMatch); else box.appendChild(frag);
      var extra = box.querySelectorAll('.log-line').length - MAX_LOG_LINES;
      for (; extra > 0; extra--) box.removeChild(box.querySelector('.log-line'));
      if (logState.follow) scrollLogToBottom();
      else { logState.unseen += fresh.length; updateJump(); }
      updateLogStats();
    }
    renderMiniLog();
  }

  function renderFullLog() {
    if (logState.rendered) return;
    var box = $('#log');
    if (!logState.loaded) {
      box.replaceChildren(el('div', { class: 'log-empty' }, 'Loading the log…'));
      logState.rendered = true;
      return;
    }
    logState.rendered = true;
    if (!logState.lines.length) { renderLogEmpty(); return; }
    var frag = document.createDocumentFragment();
    logState.lines.forEach(function (l) { frag.appendChild(logLineNode(l)); });
    box.replaceChildren(frag);
    updateLogStats();
  }

  function renderLogEmpty() {
    if (logState.rendered) $('#log').replaceChildren(el('div', { class: 'log-empty' }, 'Nothing logged yet. Start the server and its output appears here.'));
    renderMiniLog();
  }

  function renderMiniLog() {
    var box = $('#mini-log');
    var recent = logState.lines.slice(-6);
    if (!recent.length) {
      box.replaceChildren(el('div', { class: 'log-empty log-empty-sm' }, logState.loaded ? 'Nothing logged yet.' : 'Loading…'));
      return;
    }
    var frag = document.createDocumentFragment();
    recent.forEach(function (l) {
      var node = logLineNode(l);
      node.classList.remove('is-hidden');
      frag.appendChild(node);
    });
    box.replaceChildren(frag);
  }

  function updateLogStats() {
    var total = logState.lines.length;
    var text = plural(total, 'line', 'lines');
    var shown = -1;
    if (logState.filter) {
      shown = logState.lines.filter(matchesFilter).length;
      text = shown + ' of ' + text;
    }
    setText($('#log-stats'), text);
    renderNoMatch(shown === 0 && total > 0);
  }

  // A filter that hides every line gets an explanation, not a blank black box.
  function renderNoMatch(show) {
    var box = $('#log');
    var node = box.querySelector('.log-nomatch');
    if (!show) { if (node) node.remove(); return; }
    var raw = $('#log-filter').value.trim();
    if (node && node.getAttribute('data-filter') === raw) return;
    if (node) node.remove();
    box.appendChild(el('div', { class: 'log-empty log-nomatch', 'data-filter': raw },
      el('p', null, 'No lines match “' + raw + '”.'),
      el('button', {
        class: 'btn btn-sm', type: 'button',
        onclick: function () {
          var input = $('#log-filter');
          input.value = '';
          input.dispatchEvent(new Event('input', { bubbles: true }));
          input.focus();
        },
      }, icon('i-x', 'icon-sm'), 'Clear filter')));
  }

  function scrollLogToBottom() {
    var box = $('#log');
    box.scrollTop = box.scrollHeight;
    logState.unseen = 0;
    updateJump();
  }

  function updateJump() {
    var jump = $('#jump-latest');
    jump.hidden = logState.follow;
    setText(jump.querySelector('span'), logState.unseen > 0 ? plural(logState.unseen, 'new line', 'new lines') : 'Jump to latest');
    var toggle = $('#follow-toggle');
    toggle.setAttribute('aria-pressed', String(logState.follow));
    setIcon(toggle.querySelector('svg'), logState.follow ? 'i-arrow-down' : 'i-pause');
    setText(toggle.querySelector('span'), logState.follow ? 'Following' : 'Paused');
  }

  function setFollow(on) {
    logState.follow = on;
    if (on) scrollLogToBottom();
    updateJump();
  }

  function initLogs() {
    var box = $('#log');
    var ignoreScroll = false;
    box.addEventListener('scroll', function () {
      if (ignoreScroll) return;
      var atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 32;
      if (atBottom && !logState.follow) { logState.follow = true; logState.unseen = 0; updateJump(); }
      else if (!atBottom && logState.follow) { logState.follow = false; updateJump(); }
    }, { passive: true });
    $('#jump-latest').addEventListener('click', function () { setFollow(true); });
    $('#follow-toggle').addEventListener('click', function () { setFollow(!logState.follow); });
    var filterTimer = null;
    $('#log-filter').addEventListener('input', function (e) {
      window.clearTimeout(filterTimer);
      var value = e.target.value;
      filterTimer = window.setTimeout(function () {
        logState.filter = value.trim().toLowerCase();
        var rows = box.querySelectorAll('.log-line');
        var offset = logState.lines.length - rows.length;
        for (var i = 0; i < rows.length; i++) {
          var line = logState.lines[i + offset];
          rows[i].classList.toggle('is-hidden', !!logState.filter && !!line && !matchesFilter(line));
        }
        updateLogStats();
        if (logState.follow) { ignoreScroll = true; scrollLogToBottom(); window.setTimeout(function () { ignoreScroll = false; }, 50); }
      }, 120);
    });
    updateJump();
    initConsole();
  }

  /* ---------------------------------------------------------------- console input */

  var cmdHistory = { items: [], index: -1, draft: '' };

  function initConsole() {
    try { cmdHistory.items = JSON.parse(store.get(KEYS.history) || '[]').filter(function (x) { return typeof x === 'string'; }).slice(-50); } catch (_) { cmdHistory.items = []; }
    var input = $('#console-cmd');
    var form = $('#console-form');
    input.addEventListener('keydown', function (e) {
      if (e.key === 'ArrowUp') {
        if (!cmdHistory.items.length) return;
        e.preventDefault();
        if (cmdHistory.index === -1) { cmdHistory.draft = input.value; cmdHistory.index = cmdHistory.items.length; }
        cmdHistory.index = Math.max(0, cmdHistory.index - 1);
        input.value = cmdHistory.items[cmdHistory.index];
        moveCaretToEnd(input);
      } else if (e.key === 'ArrowDown') {
        if (cmdHistory.index === -1) return;
        e.preventDefault();
        cmdHistory.index++;
        if (cmdHistory.index >= cmdHistory.items.length) { cmdHistory.index = -1; input.value = cmdHistory.draft; }
        else input.value = cmdHistory.items[cmdHistory.index];
        moveCaretToEnd(input);
      }
    });
    form.addEventListener('submit', function (e) {
      e.preventDefault();
      var command = input.value.trim().replace(/^\/+/, '');
      if (!command) { input.focus(); return; }
      if (/[\r\n]/.test(command)) { toast({ tone: 'bad', title: 'One command at a time', text: 'Commands can\'t contain line breaks.' }); return; }
      runBusy($('#console-send'), 'Sending', function () {
        return api('POST', '/api/console', { command: command }).then(function () {
          if (cmdHistory.items[cmdHistory.items.length - 1] !== command) cmdHistory.items.push(command);
          cmdHistory.items = cmdHistory.items.slice(-50);
          cmdHistory.index = -1;
          cmdHistory.draft = '';
          store.set(KEYS.history, JSON.stringify(cmdHistory.items));
          input.value = '';
          setFollow(true);
          window.setTimeout(pollLogs, 250);
          // op, deop and whitelist change the lists on the Players page; fetch them again once the server has applied it.
          if (/^(op|deop|whitelist)\b/i.test(command)) window.setTimeout(function () { loadPlayers(true); }, 1500);
        }, function (err) { toastError('Command not sent', err); });
      }).then(function () { updateConsoleInput(); input.focus(); });
    });
  }

  function moveCaretToEnd(input) {
    window.requestAnimationFrame(function () { var n = input.value.length; input.setSelectionRange(n, n); });
  }

  function updateConsoleInput() {
    var s = state.status;
    var input = $('#console-cmd');
    var send = $('#console-send');
    var running = !!(s && RUNNING[s.phase]);
    input.disabled = !running;
    if (send.getAttribute('aria-busy') !== 'true') send.disabled = !running;
    input.placeholder = running ? 'say Hello from the dashboard' : 'Start the server to send commands';
    setText($('#console-hint'), running
      ? 'Press Enter to send, and the up and down arrows to reuse earlier commands. No slash needed.'
      : 'The console accepts commands while Minecraft is running. Start it from the Overview.');
  }

  /* ================================================================ versions */

  function loadVersions() {
    if (state.versions) return Promise.resolve(state.versions);
    if (state.versionsPromise) return state.versionsPromise;
    var sel = $('#setup-version');
    // The first call can take a few seconds while the server asks Mojang, Fabric and Forge.
    sel.replaceChildren(el('option', { value: '', disabled: true, selected: true }, 'Loading versions…'));
    sel.setAttribute('aria-busy', 'true');
    state.versionsPromise = api('GET', '/api/versions').then(function (data) {
      state.versions = data || {};
      state.versionRank = {};
      (Array.isArray(state.versions.vanilla) ? state.versions.vanilla : []).forEach(function (v, i) {
        if (v && v.id && v.group) state.versionRank[String(v.id)] = i;  // newest first
      });
      sel.removeAttribute('aria-busy');
      populateVersionSelect(false);
      renderUploadLimit();
      return state.versions;
    }, function (err) {
      state.versionsPromise = null;
      sel.removeAttribute('aria-busy');
      sel.replaceChildren(el('option', { value: '' }, 'Couldn\'t load versions'));
      setText($('#version-help'), err.message + ' Reload the page to try again.');
      throw err;
    });
    state.versionsPromise.catch(function () {});
    return state.versionsPromise;
  }

  function versionGroupLabel(id) {
    var parts = String(id).split(/[^0-9]+/).filter(Boolean).map(Number);
    if (!parts.length) return 'Other';
    if (parts[0] >= 2) return parts[0] + '.x';
    return parts[0] + '.' + (parts[1] || 0) + '.x';
  }

  function versionsFor(type) {
    var data = state.versions || {};
    var list = Array.isArray(data[type]) ? data[type] : [];
    return list.map(function (v) {
      if (typeof v === 'string') return { id: v, label: v };
      return { id: String(v.id), label: String(v.label || v.id), group: v.group ? String(v.group) : '', snapshot: !!v.snapshot };
    });
  }

  function populateVersionSelect(keepChoice) {
    var form = state.setupForm;
    var sel = $('#setup-version');
    var type = form ? form.type : 'vanilla';
    if (type === 'modpack') return;
    var previous = keepChoice ? sel.value : '';
    var data = state.versions || {};
    var all = versionsFor(type);
    var hasSnapshots = all.some(function (v) { return v.snapshot; });
    var showSnapshots = hasSnapshots && $('#setup-snapshots').checked;
    $('#setup-snapshots-row').hidden = !hasSnapshots;
    var list = all.filter(function (v) { return showSnapshots || !v.snapshot; });
    var latest = data.latest || '';
    var hasLatestEntry = type !== 'forge' && list.some(function (v) { return v.id === 'latest'; });
    var nodes = [];
    if ((type === 'vanilla' || type === 'fabric') && !hasLatestEntry) {
      nodes.push(el('option', { value: 'latest' }, 'Always latest' + (latest ? ' (now ' + latest + ')' : '')));
    }
    var concrete = list.filter(function (v) { return v.id !== 'latest'; });
    if (hasLatestEntry) {
      var entry = list.filter(function (v) { return v.id === 'latest'; })[0];
      nodes.push(el('option', { value: 'latest' }, entry.label));
    }
    // The server sends its lists newest first and grouped; only bare lists need sorting here.
    if (!concrete.every(function (v) { return v.group; })) {
      concrete.sort(function (a, b) { return compareVersions(b.id, a.id); });
    }
    var groups = {};
    var order = [];
    concrete.forEach(function (v) {
      var g = v.group || versionGroupLabel(v.id);
      if (!groups[g]) { groups[g] = []; order.push(g); }
      groups[g].push(v);
    });
    if (order.length > 1 && concrete.length > 8) {
      order.forEach(function (g) {
        nodes.push(el('optgroup', { label: 'Minecraft ' + g }, groups[g].map(function (v) {
          return el('option', { value: v.id }, v.label);
        })));
      });
    } else {
      concrete.forEach(function (v) { nodes.push(el('option', { value: v.id }, v.label)); });
    }
    if (!nodes.length) nodes.push(el('option', { value: '' }, 'No versions available'));
    sel.replaceChildren.apply(sel, nodes);
    if (previous && $$('option', sel).some(function (o) { return o.value === previous; })) sel.value = previous;
    else if (type === 'forge' || !(type === 'vanilla' || type === 'fabric')) sel.selectedIndex = 0;
    else sel.value = 'latest';

    var help = 'Friends need this exact version selected in their Minecraft launcher.';
    if (type === 'forge' && concrete.length > 8) {
      help = 'Every Minecraft version Forge has a server installer for (' + concrete[concrete.length - 1].id +
        ' to ' + concrete[0].id + '). ' + help;
    } else if (type === 'forge' && concrete.length) {
      help = 'Railpowered supports Forge on ' + concrete.map(function (v) { return v.id; }).join(', ') + '. ' + help;
    }
    if (showSnapshots && type !== 'forge') help += ' Snapshots need the same snapshot in the launcher (enable snapshots under Installations).';
    if (data.offline) help += ' Mojang couldn\'t be reached, so this list may be slightly out of date.';
    if (form) form.versionHelp = help;
    setText($('#version-help'), help);
    onSetupFormChange();
  }

  /* ================================================================ setups */

  function loadSetups(quiet) {
    var host = $('#setups-list');
    if (!state.setups && !quiet) host.replaceChildren(skeletonRows(3));
    return api('GET', '/api/setups').then(function (data) {
      state.setups = Array.isArray(data.setups) ? data.setups : [];
      renderSetups();
      if (state.setupForm) onSetupFormChange();
    }, function (err) {
      if (!state.setups) host.replaceChildren(errorState('Couldn\'t load your setups', err, function () { loadSetups(); }));
      else if (!quiet) toastError('Couldn\'t refresh setups', err);
    });
  }

  function renderSetups() {
    // A background refresh rebuilds the rows. If the rename box has focus,
    // carry its caret and selection over to the new box.
    var ed = state.editingSetup;
    var input = $('#setups-list .inline-edit input');
    if (ed && input && document.activeElement === input) {
      ed.sel = [input.selectionStart, input.selectionEnd, input.selectionDirection];
    }
    keepFocus(['#setups-list'], renderSetupsNow);
  }

  function renderSetupsNow() {
    var host = $('#setups-list');
    var setups = state.setups || [];
    setText($('#setups-count'), setups.length ? '(' + setups.length + ')' : '');
    if (state.editingSetup && !setups.some(function (s) { return s.id === state.editingSetup.id; })) state.editingSetup = null;
    if (!setups.length) {
      host.replaceChildren(emptyState('i-setups', 'No setups yet', 'Create your first one below. It takes a minute or two to download Minecraft.'));
      return;
    }
    var sorted = setups.slice().sort(function (a, b) { return (b.active ? 1 : 0) - (a.active ? 1 : 0); });
    var wait = worldOpsWait();
    host.replaceChildren(el('ul', { class: 'list', role: 'list' }, sorted.map(function (s) { return setupRow(s, wait); })));

    var ed = state.editingSetup;
    var input = ed && host.querySelector('.inline-edit input');
    if (input && (ed.focusOnRender || ed.sel)) {
      input.focus({ preventScroll: true });
      if (ed.focusOnRender) input.select(); // only when editing starts
      else input.setSelectionRange(ed.sel[0], ed.sel[1], ed.sel[2] || 'none');
    }
    if (ed) { ed.focusOnRender = false; ed.sel = null; }
    if (state.focusRenameOf) {
      // Back from editing (saved or cancelled): return to that row's Rename button.
      var rename = host.querySelector('[data-rename="' + cssEscape(state.focusRenameOf) + '"]');
      state.focusRenameOf = null;
      if (rename) rename.focus({ preventScroll: true });
    }
  }

  function cssEscape(v) { return window.CSS && CSS.escape ? CSS.escape(v) : String(v).replace(/["\\]/g, '\\$&'); }

  function stopEditingSetup(id) {
    state.editingSetup = null;
    state.focusRenameOf = id;
    renderSetups();
  }

  // wait: why switching isn't possible right now (worldOpsWait), or ''.
  function setupRow(setup, wait) {
    var editing = state.editingSetup && state.editingSetup.id === setup.id;
    var title;
    if (editing) {
      var input = el('input', { class: 'input', value: state.editingSetup.draft, 'aria-label': 'Setup name', maxlength: '48' });
      input.addEventListener('input', function () { state.editingSetup.draft = input.value; });
      input.addEventListener('keydown', function (e) {
        if (e.key === 'Enter') { e.preventDefault(); saveRename(setup, input.value, saveBtn); }
        if (e.key === 'Escape') { e.preventDefault(); stopEditingSetup(setup.id); }
      });
      var saveBtn = el('button', { class: 'btn btn-primary btn-sm', type: 'button', onclick: function () { saveRename(setup, input.value, saveBtn); } }, 'Save');
      title = el('div', { class: 'inline-edit' }, input, saveBtn,
        el('button', { class: 'btn btn-ghost btn-sm', type: 'button', onclick: function () { stopEditingSetup(setup.id); } }, 'Cancel'));
    } else {
      title = el('p', { class: 'row-title' }, setup.name || setupSummary(setup),
        setup.active ? el('span', { class: 'badge badge-accent' }, icon('i-check'), 'Active') : null);
    }
    var actions = [];
    if (!editing) {
      // Every row has the same three slots, so the buttons line up down the
      // list; the active setup gets invisible stand-ins for the two it can't use.
      var switchBtn = el('button', {
        class: 'btn btn-sm', type: 'button', 'data-switch': setup.id,
        'aria-disabled': wait ? 'true' : null, title: wait || null,
        onclick: function (e) { if (!isUnavailable(e.currentTarget)) activateSetup(setup, e.currentTarget); },
      }, icon('i-arrow-right', 'icon-sm'), el('span', null, 'Switch to this'));
      var deleteBtn = el('button', {
        class: 'btn btn-ghost-danger btn-sm', type: 'button', 'aria-label': 'Delete ' + (setup.name || 'setup'),
        onclick: function (e) { deleteSetup(setup, e.currentTarget); },
      }, icon('i-trash', 'icon-sm'), el('span', null, 'Delete'));
      actions.push(setup.active ? slotPlaceholder(switchBtn) : switchBtn);
      actions.push(el('button', {
        class: 'btn btn-ghost btn-sm', type: 'button', 'aria-label': 'Rename ' + (setup.name || 'setup'), 'data-rename': setup.id,
        onclick: function () { state.editingSetup = { id: setup.id, draft: setup.name || '', focusOnRender: true }; renderSetups(); },
      }, icon('i-edit', 'icon-sm'), el('span', null, 'Rename')));
      actions.push(setup.active ? slotPlaceholder(deleteBtn) : deleteBtn);
    }
    return el('li', { class: 'row' + (setup.active ? ' is-active' : '') },
      el('span', { class: 'glyph t-' + setup.type }, icon(TYPE_ICON[setup.type] || 'i-cube')),
      el('div', { class: 'row-main' }, title,
        el('p', { class: 'row-meta' }, setupMetaParts(setup))),
      el('div', { class: 'row-actions' }, actions));
  }

  // An invisible copy of a button that holds its place in a row of actions.
  function slotPlaceholder(button) {
    var copy = button.cloneNode(true);
    copy.className += ' slot-placeholder';
    copy.disabled = true;
    copy.removeAttribute('aria-label');
    copy.removeAttribute('title');
    copy.setAttribute('aria-hidden', 'true');
    copy.setAttribute('tabindex', '-1');
    return copy;
  }

  function setupMetaParts(setup) {
    var parts = [];
    parts.push(el('span', null, setup.type === 'modpack' ? 'Modpack' + (setup.instance ? ' “' + setup.instance + '”' : '') : (TYPE_LABEL[setup.type] || setup.type)));
    var v = versionLabel(setup);
    if (v) parts.push(el('span', null, 'Minecraft ' + v));
    if (setup.level_name) parts.push(el('span', { title: 'World' }, icon('i-world', 'icon-sm'), setup.level_name));
    return metaParts(parts);
  }

  function saveRename(setup, value, button) {
    var name = String(value || '').trim();
    if (!name) { toast({ tone: 'bad', title: 'Give the setup a name', text: 'The name can\'t be empty.' }); return; }
    if (name === setup.name) { stopEditingSetup(setup.id); return; }
    runBusy(button, 'Saving', function () {
      return api('PATCH', '/api/setups/' + encodeURIComponent(setup.id), { name: name }).then(function () {
        state.editingSetup = null;
        state.focusRenameOf = setup.id;
        toast({ tone: 'ok', title: 'Setup renamed', text: '“' + name + '”' });
        return loadSetups(true).then(function () { pollStatus(); });
      }, function (err) { toastError('Couldn\'t rename the setup', err); });
    });
  }

  function setupTargetVersion(setup) {
    if (setup.minecraft_version !== 'latest') return setup.minecraft_version;
    return setup.resolved_version || (state.versions && state.versions.latest) || '';
  }

  // One decision per switch. A version older than the world's last run is
  // caught here, before asking, so the first dialog already offers the safe
  // choices; the server still checks and answers world_downgrade if it knows better.
  function activateSetup(setup, button, opts) {
    opts = opts || {};
    var s = state.status || {};
    var n = RUNNING[s.phase] ? s.player_count || 0 : 0;
    var crowd = n > 0 ? el('div', { class: 'callout tone-warn' }, icon('i-alert'), el('p', null, el('strong', null, plural(n, 'player is', 'players are') + ' online'), ' and will be disconnected.')) : null;
    var decide;
    if (opts.confirmed) {
      decide = Promise.resolve(opts.allowDowngrade ? 'continue' : 'switch');
    } else {
      var world = (state.worlds || []).filter(function (w) { return w.name === setup.level_name; })[0];
      var target = setupTargetVersion(setup);
      // The backend guards against the newest version that ever opened the
      // world (newest_version), not only the last one.
      var guard = world && (world.newest_version || world.last_version);
      if (guard && isConcreteVersion(target) && compareVersions(target, guard) < 0) {
        decide = downgradeDialog({
          message: '“' + setup.level_name + '” has been played on Minecraft ' + guard + '. “' +
            (setup.name || setupSummary(setup)) + '” runs ' + target + ', and opening the world with an older version can damage it.',
          data: { world: setup.level_name },
        }, setup.level_name, crowd);
      } else {
        decide = confirmDialog({
          icon: 'i-setups',
          title: 'Switch to “' + (setup.name || setupSummary(setup)) + '”?',
          body: [
            el('p', null, setupSummary(setup) + '.'),
            el('p', null, RUNNING[s.phase]
              ? 'The server saves the world, stops, and starts again on this setup. It may need to download files first, which can take a few minutes.'
              : s.phase === 'sleeping'
                ? 'The server stays asleep and uses this setup the next time it wakes up. Any files it needs are downloaded now.'
                : 'The server isn\'t running. It uses this setup the next time it starts, and any files it needs are downloaded now.'),
            crowd,
          ],
          confirmLabel: 'Switch setup',
          confirmIcon: 'i-arrow-right',
        }).then(function (ok) { return ok ? 'switch' : null; });
      }
    }
    return decide.then(function (choice) {
      if (choice === 'new') { moveSetupToNewWorld(setup, button); return; }
      if (choice !== 'switch' && choice !== 'continue') return;
      var allow = choice === 'continue';
      // The busy state ends before any follow-up question, so the follow-up
      // can show its own progress on the same button.
      return runBusy(button, 'Switching…', function () {
        return requestActivate(setup, allow).then(function () { return null; }, function (err) {
          if (err.code === 'world_downgrade') return err;
          toastError('Couldn\'t switch setups', err);
          return null;
        });
      }).then(function (downgrade) {
        if (!downgrade) return;
        return downgradeDialog(downgrade, setup.level_name).then(function (again) {
          if (again === 'continue') return activateSetup(setup, button, { confirmed: true, allowDowngrade: true });
          if (again === 'new') return moveSetupToNewWorld(setup, button);
        });
      });
    });
  }

  function requestActivate(setup, allowDowngrade) {
    var body = allowDowngrade ? { allow_downgrade: true } : {};
    return api('POST', '/api/setups/' + encodeURIComponent(setup.id) + '/activate', body).then(function (data) {
      if (data && data.already_active) { toast({ tone: 'info', title: 'Already active', text: '“' + (setup.name || 'That setup') + '” is the active setup.' }); loadSetups(true); return; }
      // The switch can still find a newer world once it knows the exact version.
      acceptTask(data, {
        world: setup.level_name,
        retry: function () { return activateSetup(setup, null, { confirmed: true, allowDowngrade: true }); },
        newWorld: function () { return moveSetupToNewWorld(setup, null); },
      });
    });
  }

  // "Use a new world" for an existing setup: the user already chose to switch,
  // so create an empty world, point the setup at it and switch, with no second
  // question. If the switch can't start, put the setup back as it was.
  // opts.start: the setup is the active one and the user asked to start the
  // server, so start it (instead of switching) once it points at the new world.
  function moveSetupToNewWorld(setup, button, opts) {
    opts = opts || {};
    var name = uniqueWorldName((setup.level_name || 'world') + '-new');
    var previous = setup.level_name;
    var created = false, patched = false;
    return runBusy(button, opts.start ? 'Starting…' : 'Switching…', function () {
      return api('POST', '/api/worlds', { name: name }).then(function () {
        created = true;
        return api('PATCH', '/api/setups/' + encodeURIComponent(setup.id), { level_name: name });
      }).then(function () {
        patched = true;
        return opts.start ? api('POST', '/api/server/start', {}) : requestActivate(setup, false);
      }).then(function () {
        toast({
          tone: 'ok', title: opts.start ? 'Starting on a new world' : 'Switching on a new world',
          text: '“' + (setup.name || 'The setup') + '” now uses the empty world “' + name + '”. “' + previous + '” is untouched.',
        });
        loadSetups(true);
        loadWorlds(true);
      }).catch(function (err) {
        var undo = Promise.resolve();
        if (patched) undo = undo.then(function () { return api('PATCH', '/api/setups/' + encodeURIComponent(setup.id), { level_name: previous }); });
        if (created) undo = undo.then(function () { return api('DELETE', '/api/worlds/' + encodeURIComponent(name)); });
        return undo.catch(function () { /* best effort; the lists below show what's there */ }).then(function () {
          toastError(opts.start ? 'Couldn\'t start on a new world' : 'Couldn\'t switch to a new world', err);
          loadSetups(true);
          loadWorlds(true);
        });
      }).finally(function () { if (opts.start) pollStatus(); });
    });
  }

  function deleteSetup(setup, button) {
    confirmDialog({
      tone: 'danger', icon: 'i-trash',
      title: 'Delete “' + (setup.name || setupSummary(setup)) + '”?',
      body: [
        el('p', null, 'This removes the setup from your list.'),
        el('p', null, 'The world ', el('strong', null, setup.level_name || ''), ' stays on the volume, along with any downloaded files. You can delete the world separately in Worlds & backups.'),
      ],
      confirmLabel: 'Delete setup', confirmIcon: 'i-trash',
    }).then(function (ok) {
      if (!ok) return;
      runBusy(button, '', function () {
        return api('DELETE', '/api/setups/' + encodeURIComponent(setup.id)).then(function (data) {
          var orphaned = orphanedItems(data);
          toast({
            tone: 'ok', title: 'Setup deleted',
            text: '“' + (setup.name || setupSummary(setup)) + '” is gone. Its world is still there.' + orphanedText(orphaned),
            actions: orphanActions(orphaned),
          });
          if (state.storage) loadStorage(true);
          return loadSetups(true);
        }, function (err) { toastError('Couldn\'t delete the setup', err); });
      });
    });
  }

  /* ---------------------------------------------------------------- downloaded files */

  var STORAGE_TYPES = {
    vanilla: { label: 'Minecraft server', glyph: 'vanilla', icon: 'i-cube' },
    fabric: { label: 'Fabric', glyph: 'fabric', icon: 'i-feather' },
    forge: { label: 'Forge', glyph: 'forge', icon: 'i-hammer' },
    modpack: { label: 'Modpack', glyph: 'modpack', icon: 'i-package' },
    other: { label: 'Other files', glyph: 'world', icon: 'i-file' },
  };

  function storageKindLabel(item) {
    var type = (STORAGE_TYPES[item.type] || STORAGE_TYPES.other).label;
    if (item.kind === 'jar') return item.type === 'fabric' ? 'Fabric installer' : item.type === 'vanilla' ? 'Minecraft server jar' : type + ' jar';
    return item.type === 'modpack' ? 'Modpack install' : type + ' install';
  }

  function usedByNames(item) {
    return (Array.isArray(item.used_by) ? item.used_by : []).map(function (u) { return u && typeof u === 'object' ? u.name || u.id : String(u); });
  }

  function loadStorage(quiet) {
    var host = $('#storage-list');
    if (!state.storage && !quiet) host.replaceChildren(el('li', null, skeletonRows(2)));
    return api('GET', '/api/storage').then(function (data) {
      state.storage = { items: Array.isArray(data.items) ? data.items : [], total: Number(data.total) || 0 };
      renderStorage();
    }, function (err) {
      if (!state.storage) host.replaceChildren(el('li', null, errorState('Couldn\'t load the downloaded files', err, function () { loadStorage(); })));
      else if (!quiet) toastError('Couldn\'t refresh the downloaded files', err);
    });
  }

  function renderStorage() { keepFocus(['#storage-list'], renderStorageNow); }

  function renderStorageNow() {
    var host = $('#storage-list');
    var data = state.storage || { items: [], total: 0 };
    // Unused files first: they're the ones there is something to do about.
    var items = data.items.slice().sort(function (a, b) {
      var ua = usedByNames(a).length ? 1 : 0, ub = usedByNames(b).length ? 1 : 0;
      return ua - ub || (Number(b.size) || 0) - (Number(a.size) || 0);
    });
    setText($('#storage-count'), items.length ? '(' + items.length + ')' : '');
    var unused = items.filter(function (i) { return !usedByNames(i).length; });
    var unusedBytes = unused.reduce(function (sum, i) { return sum + (Number(i.size) || 0); }, 0);
    setText($('#storage-total'), items.length
      ? formatBytes(data.total) + ' in total' + (unused.length ? ' · ' + formatBytes(unusedBytes) + ' unused' : '')
      : '');
    if (!items.length) {
      host.replaceChildren(el('li', null, emptyState('i-disk', 'Nothing downloaded yet', 'Server files show up here once a setup has been installed.')));
      return;
    }
    host.replaceChildren.apply(host, items.map(function (item) {
      var type = STORAGE_TYPES[item.type] || STORAGE_TYPES.other;
      var users = usedByNames(item);
      var deleteBtn = el('button', {
        class: 'btn btn-ghost-danger btn-sm', type: 'button', 'aria-label': 'Delete ' + item.name,
        onclick: function (e) { deleteStorageItems([item], e.currentTarget); },
      }, icon('i-trash', 'icon-sm'), el('span', null, 'Delete'));
      return el('li', { class: 'row' },
        el('span', { class: 'glyph t-' + type.glyph }, icon(type.icon)),
        el('div', { class: 'row-main' },
          el('p', { class: 'row-title' }, item.name,
            users.length ? null : el('span', { class: 'badge badge-warn' }, 'Not used')),
          el('p', { class: 'row-meta' }, metaParts([
            el('span', null, storageKindLabel(item)),
            el('span', null, icon('i-disk', 'icon-sm'), formatBytes(item.size)),
          ]))),
        el('div', { class: 'row-actions' }, users.length ? slotPlaceholder(deleteBtn) : deleteBtn),
        users.length ? el('p', { class: 'row-note' }, 'Used by: ' + users.join(', ') + '. Delete those setups first to remove it.') : null);
    }));
  }

  // The orphaned list of a setup or world delete: files no setup uses any more.
  function orphanedItems(data) {
    return data && Array.isArray(data.orphaned) ? data.orphaned.filter(function (i) { return i && i.kind && i.name; }) : [];
  }

  function orphanedText(items) {
    if (!items.length) return '';
    var bytes = items.reduce(function (sum, i) { return sum + (Number(i.size) || 0); }, 0);
    return ' ' + plural(items.length, 'downloaded file', 'downloaded files') + ' (' + formatBytes(bytes) + ') ' +
      (items.length === 1 ? 'isn\'t' : 'aren\'t') + ' used by any setup now.';
  }

  function orphanActions(items) {
    return items.length ? [{ label: 'Delete unused files', run: function () { deleteStorageItems(items, null); } }] : [];
  }

  // Confirm, then delete one or more unused files. The server refuses (409
  // in_use) anything a setup still uses, so the list is reloaded either way.
  function deleteStorageItems(items, button) {
    var bytes = items.reduce(function (sum, i) { return sum + (Number(i.size) || 0); }, 0);
    var one = items.length === 1;
    confirmDialog({
      tone: 'danger', icon: 'i-trash',
      title: one ? 'Delete “' + items[0].name + '”?' : 'Delete ' + items.length + ' unused files?',
      body: [
        one
          ? el('p', null, storageKindLabel(items[0]) + ', ' + formatBytes(items[0].size) + '. No setup uses it.')
          : el('div', null,
            el('p', null, 'No setup uses these, and deleting them frees ' + formatBytes(bytes) + ':'),
            el('ul', { class: 'used-by' }, items.map(function (i) { return el('li', null, i.name + ' (' + formatBytes(i.size) + ')'); }))),
        el('p', null, 'Worlds and backups aren\'t touched. If you install this version or pack again later, it\'s downloaded again.'),
      ],
      confirmLabel: one ? 'Delete file' : 'Delete files', confirmIcon: 'i-trash',
    }).then(function (ok) {
      if (!ok) return;
      runBusy(button, '', function () {
        var freed = 0, failed = null;
        var chain = Promise.resolve();
        items.forEach(function (item) {
          chain = chain.then(function () {
            return api('DELETE', '/api/storage/' + encodeURIComponent(item.kind) + '/' + encodeURIComponent(item.name)).then(function (data) {
              freed += Number(data.freed) || 0;
              if (Array.isArray(data.items)) state.storage = { items: data.items, total: Number(data.total) || 0 };
            }, function (err) { if (!failed) failed = { item: item, err: err }; });
          });
        });
        return chain.then(function () {
          if (freed) toast({ tone: 'ok', title: one && !failed ? 'File deleted' : 'Files deleted', text: formatBytes(freed) + ' freed on the volume.' });
          if (failed) {
            var users = failed.err.code === 'in_use' ? usedByNames({ used_by: failed.err.data && failed.err.data.used_by }) : [];
            toast({
              tone: 'bad', title: 'Couldn\'t delete “' + failed.item.name + '”',
              text: users.length ? 'It\'s used by ' + users.join(', ') + '. Delete those setups first.' : failed.err.message,
            });
          }
          if (state.storage) renderStorage();
          return loadStorage(true).then(function () { if (state.backups || state.route.view === 'worlds') loadBackups(true); });
        });
      });
    });
  }

  // request: how to send it again if it fails with world_downgrade (see state.taskRequests).
  function acceptTask(data, request) {
    var task = data && data.task;
    if (task && task.id) {
      var ident = taskIdent(task);
      rememberTaskRequest(task, request);
      state.watchedTasks[ident] = true;
      if (!state.taskFirstSeen[ident]) state.taskFirstSeen[ident] = Date.now();
      if (state.status) {
        state.status.task = task;
        renderTaskBanner();
      }
    }
    pollStatus();
    return task;
  }

  // extra: optional node shown under the explanation (e.g. who gets disconnected).
  function downgradeDialog(err, worldName, extra) {
    var d = err.data || {};
    var named = d.world || d.level_name || worldName;
    var world = named ? '“' + named + '”' : 'the old world';
    return openDialog({
      tone: 'warn', icon: 'i-alert',
      title: 'This world is newer than that version',
      wide: true,
      body: [
        el('p', null, err.message),
        el('p', null, 'Minecraft can upgrade a world, but it can\'t take one back to an older version. Trying can lose chunks or break the world.'),
        el('ul', null,
          el('li', null, el('strong', null, 'Use a new world'), ': start fresh, and ' + world + ' stays exactly as it is.'),
          el('li', null, el('strong', null, 'Back up and continue anyway'), ': Railpowered backs the world up first, then opens it with the older version.')),
        extra || null,
      ],
      actions: [
        { label: 'Cancel', value: null, style: 'ghost' },
        { label: 'Back up and continue anyway', value: 'continue', style: 'secondary' },
        { label: 'Use a new world', value: 'new', style: 'primary', icon: 'i-plus', isDefault: true },
      ],
      focus: function (buttons) { return buttons['new']; },
    });
  }

  /* ---------------------------------------------------------------- new setup form */

  function initSetupForm() {
    var form = {
      type: 'vanilla',
      packSource: 'url',
      file: null,
      packNameTouched: false,
      worldNameTouched: false,
      pendingTaskId: null,
      submitting: false,
    };
    state.setupForm = form;
    var root = $('#setup-form');

    $$('input[name="type"]', root).forEach(function (r) {
      r.addEventListener('change', function () {
        if (!r.checked) return;
        form.type = r.value;
        $('#step-version').hidden = form.type === 'modpack';
        $('#step-pack').hidden = form.type !== 'modpack';
        if (form.type !== 'modpack') {
          if (state.versions) populateVersionSelect(true);
          else loadVersions();
        }
        onSetupFormChange();
      });
    });

    $('#setup-version').addEventListener('change', onSetupFormChange);
    $('#setup-snapshots').checked = false;
    $('#setup-snapshots').addEventListener('change', function () {
      populateVersionSelect(true);
      onSetupFormChange();
    });

    $$('[data-pack-source]', root).forEach(function (b) {
      b.addEventListener('click', function () {
        form.packSource = b.getAttribute('data-pack-source');
        $$('[data-pack-source]', root).forEach(function (x) { x.setAttribute('aria-pressed', String(x === b)); });
        $('#pack-url-pane').hidden = form.packSource !== 'url';
        $('#pack-file-pane').hidden = form.packSource !== 'file';
        onSetupFormChange();
      });
    });

    $('#pack-url').addEventListener('input', function () {
      showFieldError($('#pack-url'), $('#pack-url-error'), '');
      if (!form.packNameTouched) {
        var guess = '';
        try {
          var u = new URL($('#pack-url').value.trim());
          var last = decodeURIComponent(u.pathname.split('/').filter(Boolean).pop() || '');
          guess = guessPackName(last);
        } catch (_) { guess = ''; }
        $('#pack-name').value = guess;
        validatePackName(true);
      }
      onSetupFormChange();
    });

    $('#pack-name').addEventListener('input', function () {
      form.packNameTouched = $('#pack-name').value.trim() !== '';
      validatePackName(true);
      onSetupFormChange();
    });

    var fileInput = $('#pack-file');
    var zone = $('#dropzone');
    fileInput.addEventListener('change', function () { setPackFile(fileInput.files && fileInput.files[0]); });
    ['dragenter', 'dragover'].forEach(function (ev) {
      zone.addEventListener(ev, function (e) { e.preventDefault(); zone.classList.add('is-over'); });
    });
    ['dragleave', 'dragend', 'drop'].forEach(function (ev) {
      zone.addEventListener(ev, function () { zone.classList.remove('is-over'); });
    });
    zone.addEventListener('drop', function (e) {
      e.preventDefault();
      var f = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
      if (f) setPackFile(f);
    });
    $('#file-clear').addEventListener('click', function () { setPackFile(null); fileInput.focus(); });
    $('#upload-cancel').addEventListener('click', function () { if (state.uploading) state.uploading.abort(); });

    $$('input[name="world-mode"]', root).forEach(function (r) {
      r.addEventListener('change', function () {
        $('#new-world-wrap').hidden = worldMode() !== 'new';
        $('#existing-world-wrap').hidden = worldMode() !== 'existing';
        onSetupFormChange();
      });
    });
    $('#new-world-name').addEventListener('input', function () {
      form.worldNameTouched = $('#new-world-name').value.trim() !== '';
      validateNewWorld(true);
      onSetupFormChange();
    });
    $('#existing-world').addEventListener('change', onSetupFormChange);
    $('#setup-activate').addEventListener('change', renderSetupImpact);
    root.addEventListener('submit', function (e) { e.preventDefault(); submitSetup(false); });
    onSetupFormChange();
  }

  function worldMode() {
    var r = $('#setup-form input[name="world-mode"]:checked');
    return r ? r.value : 'new';
  }

  function setPackFile(file) {
    var form = state.setupForm;
    var limit = uploadLimit();
    showFieldError($('#pack-file'), $('#pack-file-error'), '');
    if (file && !/\.(mrpack|zip)$/i.test(file.name)) {
      showFieldError($('#pack-file'), $('#pack-file-error'), 'Choose a .mrpack or .zip file.');
      file = null;
    } else if (file && limit && file.size > limit) {
      showFieldError($('#pack-file'), $('#pack-file-error'), '“' + file.name + '” is ' + formatBytes(file.size) +
        ', and this server accepts uploads up to ' + formatBytes(limit) + '. Paste a download link instead, or raise MAX_UPLOAD_MB in Railway.');
      file = null;
    } else if (file && state.disk && state.disk.free_bytes != null && file.size > Number(state.disk.free_bytes)) {
      showFieldError($('#pack-file'), $('#pack-file-error'), '“' + file.name + '” is ' + formatBytes(file.size) +
        ', but the volume only has ' + formatBytes(state.disk.free_bytes) + ' free. Delete old backups or worlds first.');
      file = null;
    }
    form.file = file || null;
    if (!file) $('#pack-file').value = '';
    $('#dropzone').hidden = !!file;
    $('#file-chip').hidden = !file;
    if (file) {
      setText($('#file-name'), file.name);
      setText($('#file-size'), formatBytes(file.size));
      if (!form.packNameTouched) {
        $('#pack-name').value = guessPackName(file.name);
        validatePackName(true);
      }
    }
    onSetupFormChange();
  }

  // The server's upload limit (MAX_UPLOAD_MB), when the API reports it.
  function uploadLimit() {
    var n = Number((state.status && state.status.max_upload_bytes) || (state.versions && state.versions.max_upload_bytes));
    return n > 0 ? n : null;
  }

  function renderUploadLimit() {
    var limit = uploadLimit();
    setText($('#pack-file-help'), limit ? 'or click to choose a file (up to ' + formatBytes(limit) + ')' : 'or click to choose a file');
  }

  function selectedVersion() {
    var v = $('#setup-version').value;
    if (v === 'latest') return (state.versions && state.versions.latest) || 'latest';
    return v;
  }

  function autoWorldName() {
    var form = state.setupForm;
    if (form.type === 'modpack') return uniqueWorldName($('#pack-name').value.trim() || 'modpack');
    var v = selectedVersion();
    return uniqueWorldName(form.type + '-' + (v || 'world'));
  }

  function validatePackName(live) {
    var input = $('#pack-name');
    var value = input.value.trim();
    var msg = '';
    if (!value) msg = live ? '' : 'Give the pack a name.';
    else if (!NAME_RE.test(value)) msg = NAME_RULE;
    var hint = $('#pack-name-error');
    hint.classList.remove('is-warn');
    showFieldError(input, hint, msg);
    if (!msg && value) {
      var users = packInstances()[value.toLowerCase()];
      if (users) {
        // A note, not an error: reinstalling under the same name is allowed (and confirmed at submit).
        hint.replaceChildren(icon('i-info'), el('span', null, 'A pack called “' + value + '” is installed already (used by ' + users.join(', ') +
          '). Installing replaces it once the new download succeeds. Pick another name to keep both.'));
        hint.hidden = false;
        hint.classList.add('is-warn');
        input.removeAttribute('aria-invalid');
        return true;
      }
    }
    return !msg;
  }

  function validateNewWorld(live) {
    var input = $('#new-world-name');
    var value = input.value.trim() || input.placeholder;
    var msg = '';
    if (!NAME_RE.test(value)) msg = NAME_RULE;
    else if ((state.worlds || []).some(function (w) { return w.name.toLowerCase() === value.toLowerCase(); })) {
      msg = 'There\'s already a world called “' + value + '”. Pick another name, or choose “Use a world you already have”.';
    }
    if (live && !input.value.trim()) msg = '';
    showFieldError(input, $('#new-world-error'), msg);
    return !msg;
  }

  function onSetupFormChange() {
    var form = state.setupForm;
    if (!form) return;
    var input = $('#new-world-name');
    var auto = autoWorldName();
    input.placeholder = auto;
    if (!form.worldNameTouched) input.value = '';
    populateExistingWorlds();
    renderExistingWorldNote();
    renderNewWorldOptions();
    var nameInput = $('#setup-name');
    nameInput.placeholder = defaultSetupName();
    // A modpack setup is named after the pack and always switches over, so those two controls only apply to the others.
    var modpack = form.type === 'modpack';
    $('#setup-name-wrap').hidden = modpack;
    $('#setup-activate-row').hidden = modpack;
    $('#pack-finish').hidden = !modpack;
    if (form.versionHelp && form.type !== 'modpack') {
      setText($('#version-help'), form.versionHelp + ($('#setup-version').value === 'latest'
        ? ' With “Always latest”, the server moves up when Mojang ships a new release, and friends then need to update too.'
        : ''));
    }
    setText($('#setup-submit span'), form.type === 'modpack' ? 'Install modpack' : 'Create setup');
    renderSetupImpact();
    renderSetupSteps();
    updateSetupSubmit();
  }

  // A new world is generated with the seed and world type from Settings > New
  // worlds, so say which ones before the admin commits to it.
  function renderNewWorldOptions() {
    var node = $('#new-world-options');
    if (!node) return;
    var data = state.settings || state.settingsPeek;
    if (!data) {
      if (!state.settingsPeekPromise) {
        state.settingsPeekPromise = api('GET', '/api/settings').then(function (d) {
          state.settingsPeek = d || {};
          renderNewWorldOptions();
        }, function () { /* the line keeps its generic wording */ });
      }
      node.replaceChildren('Generated with the seed and world type from ', el('a', { href: '#/settings' }, 'Settings'), '.');
      return;
    }
    var props = data.properties || {};
    var schema = data.schema || {};
    var seed = String(props['level-seed'] == null ? '' : props['level-seed']).trim();
    var type = props['level-type'] == null ? '' : String(props['level-type']);
    var typeLabel = type ? humanize(type) : 'Normal';
    var opts = schema['level-type'] && schema['level-type'].options;
    (opts || []).forEach(function (o) {
      if (o && typeof o === 'object' && String(o.value) === type && o.label) typeLabel = String(o.label);
    });
    node.replaceChildren('Seed: ', el('strong', null, seed || 'random'), ' · Type: ', el('strong', null, typeLabel),
      '. Change these in ', el('a', { href: '#/settings' }, 'Settings, New worlds'), '.');
  }

  function defaultSetupName() {
    var form = state.setupForm;
    if (form.type === 'modpack') return ($('#pack-name').value.trim() || 'My modpack');
    var raw = $('#setup-version').value;
    var v = raw === 'latest' ? '(latest)' : raw;
    return (TYPE_LABEL[form.type] || 'Vanilla') + (v ? ' ' + v : '');
  }

  function populateExistingWorlds() {
    var sel = $('#existing-world');
    var worlds = state.worlds || [];
    var key = worlds.map(function (w) { return w.name + ':' + (w.last_version || '') + ':' + (w.newest_version || '') + ':' + (w.active ? 1 : 0); }).join('|');
    if (sel.getAttribute('data-key') === key) return;
    sel.setAttribute('data-key', key);
    var previous = sel.value;
    if (!worlds.length) {
      sel.replaceChildren(el('option', { value: '' }, state.worlds ? 'No worlds yet' : 'Loading worlds…'));
      return;
    }
    sel.replaceChildren.apply(sel, worlds.map(function (w) {
      var label = w.name + (w.last_version ? ' (Minecraft ' + w.last_version + ')' : ' (never played)') + (w.active ? ', active' : '');
      return el('option', { value: w.name }, label);
    }));
    if (previous && worlds.some(function (w) { return w.name === previous; })) sel.value = previous;
    else {
      var active = worlds.filter(function (w) { return w.active; })[0];
      if (active) sel.value = active.name;
    }
  }

  function renderExistingWorldNote() {
    var note = $('#existing-world-note');
    var form = state.setupForm;
    if (worldMode() !== 'existing') { note.hidden = true; return; }
    var name = $('#existing-world').value;
    var world = (state.worlds || []).filter(function (w) { return w.name === name; })[0];
    if (!world) { note.hidden = true; return; }
    var target = form.type === 'modpack' ? '' : selectedVersion();
    var tone = 'warn', text;
    if (form.type !== 'modpack' && world.last_version && !isConcreteVersion(target)) {
      // No concrete version to compare with yet (the list is loading or failed).
      tone = 'info';
      text = '“' + world.name + '” was last played on ' + world.last_version + '. Pick a version to check whether it can open this world.';
    } else if (form.type === 'modpack') {
      text = 'Modpacks can add blocks and items that vanilla can\'t read. Back up “' + world.name + '” first if you might want it without the pack.';
    } else if (!world.last_version) {
      text = '“' + world.name + '” hasn\'t been played yet, so any version is fine.';
      tone = 'ok';
    } else {
      var cmp = compareVersions(target, world.last_version);
      if (cmp < 0) {
        tone = 'bad';
        text = '“' + world.name + '” was last played on ' + world.last_version + '. Opening it with ' + target + ' could damage it, so Railpowered will stop and ask before doing that.';
      } else if (cmp > 0) {
        text = '“' + world.name + '” was last played on ' + world.last_version + '. Opening it with ' + target + ' upgrades it for good, and older versions can\'t open it afterwards. Back it up first.';
      } else {
        tone = 'ok';
        text = '“' + world.name + '” was last played on this version.';
      }
    }
    // A world opened with an older version after a confirmation remembers the newer one.
    var newest = world.newest_version;
    if (form.type !== 'modpack' && newest && world.last_version && compareVersions(newest, world.last_version) > 0) {
      text += ' It was played on ' + newest + ' before that, so an older version than ' + newest + ' may ask first.';
      if (tone === 'ok') tone = 'info';
    }
    note.className = 'callout tone-' + tone;
    note.replaceChildren(icon(tone === 'ok' ? 'i-check-circle' : tone === 'info' ? 'i-info' : 'i-alert'), el('p', null, text));
    note.hidden = false;
  }

  function renderSetupImpact() {
    var s = state.status;
    var node = $('#setup-impact');
    if (!node || !state.setupForm) return;
    // Modpacks always become the active setup once installed; the server has no "just add it" option for them.
    var modpack = state.setupForm.type === 'modpack';
    var activate = modpack || $('#setup-activate').checked;
    var text;
    var packText;
    if (!activate) text = 'It goes on your list, and you can switch to it whenever you like.';
    else if (s && RUNNING[s.phase]) {
      var n = s.player_count || 0;
      var crowd = n > 0 ? ' ' + plural(n, 'player', 'players') + ' online will be disconnected.' : ' Nobody is online right now.';
      text = 'When it\'s ready, the server restarts on it.' + crowd;
      packText = 'It becomes the active setup, and the server restarts on it.' + crowd;
    } else if (s && s.phase === 'sleeping') {
      text = 'The server is asleep, so nobody is interrupted. The next time someone joins, it starts on the new setup.';
      packText = 'It becomes the active setup. The server is asleep, so nobody is interrupted, and it starts on the modpack the next time someone joins.';
    } else {
      text = 'The server isn\'t running, so nobody is interrupted. It uses the new setup the next time it starts.';
      packText = 'It becomes the active setup. The server isn\'t running, so nobody is interrupted.';
    }
    setText(node, text);
    setText($('#pack-impact'), modpack ? packText : '');
  }

  function renderSetupSteps() {
    var form = state.setupForm;
    $('#step-type').classList.add('is-done');
    var versionOk = form.type === 'modpack' ? true : !!$('#setup-version').value;
    $('#step-version').classList.toggle('is-done', form.type !== 'modpack' && versionOk);
    var packOk = form.type === 'modpack' && NAME_RE.test($('#pack-name').value.trim()) &&
      (form.packSource === 'url' ? /^https:\/\/\S+$/i.test($('#pack-url').value.trim()) : !!form.file);
    $('#step-pack').classList.toggle('is-done', packOk);
    var worldOk = worldMode() === 'existing' ? !!$('#existing-world').value : NAME_RE.test($('#new-world-name').value.trim() || $('#new-world-name').placeholder);
    $('#step-world').classList.toggle('is-done', worldOk);
  }

  function updateSetupSubmit() {
    var form = state.setupForm;
    if (!form) return;
    var btn = $('#setup-submit');
    if (btn.getAttribute('aria-busy') === 'true') return;
    var task = runningTask();
    var phase = state.status && state.status.phase;
    // Only one upload at a time: a backup upload holds up a modpack upload.
    var uploadWait = state.uploadingKind === 'backup' && form.type === 'modpack' && form.packSource === 'file'
      ? 'Available when the backup upload finishes' : '';
    btn.disabled = form.submitting;
    markUnavailable(btn, worldOpsWait() || uploadWait);
    var summary = $('#setup-summary');
    var waitText = '';
    if (task) waitText = 'Another task is running. You can create the setup when it finishes.';
    else if (phase === 'starting') waitText = 'The server is starting. You can create the setup once it\'s online.';
    else if (phase === 'stopping') waitText = 'The server is stopping. You can create the setup once it has stopped.';
    else if (uploadWait) waitText = 'A backup is uploading. You can upload the modpack when it finishes.';
    if (form.pendingTaskId && task && taskIdent(task) === form.pendingTaskId) setText(summary, 'Working on it. You can leave this page. Progress shows at the top.');
    else if (waitText) { setText(summary, waitText); summary.setAttribute('data-wait', ''); }
    else if (!form.pendingTaskId && summary.hasAttribute('data-wait')) setText(summary, '');
    if (!waitText) summary.removeAttribute('data-wait');
  }

  function resetSetupForm() {
    var form = state.setupForm;
    $('#setup-name').value = '';
    $('#new-world-name').value = '';
    $('#pack-url').value = '';
    $('#pack-name').value = '';
    form.packNameTouched = false;
    form.worldNameTouched = false;
    setPackFile(null);
    var newRadio = $('#setup-form input[name="world-mode"][value="new"]');
    newRadio.checked = true;
    $('#new-world-wrap').hidden = false;
    $('#existing-world-wrap').hidden = true;
    setText($('#setup-summary'), '');
    $('#upload-progress').hidden = true;
    onSetupFormChange();
  }

  function collectSetup() {
    var form = state.setupForm;
    var mode = worldMode();
    var levelName = mode === 'existing' ? $('#existing-world').value : ($('#new-world-name').value.trim() || $('#new-world-name').placeholder);
    var body = {
      level_name: levelName,
      create_world: mode === 'new',
      activate: $('#setup-activate').checked,
    };
    // An empty name means "use the suggestion shown in the box".
    body.name = $('#setup-name').value.trim() || $('#setup-name').placeholder;
    if (form.type !== 'modpack') {
      body.type = form.type;
      body.minecraft_version = $('#setup-version').value;
    }
    return body;
  }

  function validateSetupForm() {
    var form = state.setupForm;
    var firstBad = null;
    if (form.type !== 'modpack' && !$('#setup-version').value) {
      toast({ tone: 'bad', title: 'Pick a Minecraft version', text: 'The version list hasn\'t loaded yet.' });
      return false;
    }
    if (form.type === 'modpack') {
      if (form.packSource === 'url') {
        var url = $('#pack-url').value.trim();
        var urlMsg = '';
        if (!url) urlMsg = 'Paste the link to the modpack file.';
        else if (!/^https:\/\/\S+$/i.test(url)) urlMsg = 'The link should start with https://';
        showFieldError($('#pack-url'), $('#pack-url-error'), urlMsg);
        if (urlMsg) firstBad = firstBad || $('#pack-url');
      } else if (!form.file) {
        showFieldError($('#pack-file'), $('#pack-file-error'), 'Choose a .mrpack or .zip file to upload.');
        firstBad = firstBad || $('#pack-file');
      }
      if (!validatePackName(false)) firstBad = firstBad || $('#pack-name');
    }
    if (worldMode() === 'new') {
      if (!validateNewWorld(false)) firstBad = firstBad || $('#new-world-name');
    } else if (!$('#existing-world').value) {
      toast({ tone: 'bad', title: 'Choose a world', text: 'There are no worlds to pick from yet, so start a new one.' });
      return false;
    }
    if (firstBad) { firstBad.focus(); return false; }
    return true;
  }

  // Installing under the name of a pack that is already installed replaces
  // it for every setup that uses it, so that needs a clear yes first.
  function confirmPackReplace() {
    var form = state.setupForm;
    if (form.type !== 'modpack') return Promise.resolve(true);
    var name = $('#pack-name').value.trim();
    var users = packInstances()[name.toLowerCase()];
    if (!users) return Promise.resolve(true);
    return confirmDialog({
      tone: 'danger', icon: 'i-alert',
      title: 'Replace the installed pack “' + name + '”?',
      body: [
        el('p', null, 'A pack called ', el('strong', null, name), ' is installed already. Installing this one replaces its files once the download succeeds.'),
        el('div', { class: 'callout tone-warn' }, icon('i-alert'), el('div', null,
          el('p', null, el('strong', null, users.length === 1 ? 'This setup uses it and will run the new pack:' : 'These setups use it and will run the new pack:')),
          el('ul', { class: 'used-by' }, users.map(function (n) { return el('li', null, n); })))),
        el('p', null, 'To keep both, cancel and give the new pack a different name.'),
      ],
      confirmLabel: 'Replace the pack', confirmIcon: 'i-rotate',
    }).then(function (ok) {
      if (!ok) $('#pack-name').focus();
      return ok;
    });
  }

  // packConfirmed: the pack-name question was already answered for this submit.
  function submitSetup(allowDowngrade, packConfirmed) {
    var form = state.setupForm;
    if (form.submitting) return;
    var btn = $('#setup-submit');
    if (isUnavailable(btn)) {
      setText($('#setup-summary'), btn.title || 'This can\'t start right now.');
      return;
    }
    if (!validateSetupForm()) return;
    var ready = packConfirmed ? Promise.resolve(true) : confirmPackReplace();
    form.submitting = true;
    ready.then(function (ok) {
      form.submitting = false;
      if (ok) sendSetupRequest(setupRequest(), { allowDowngrade: allowDowngrade });
    });
  }

  // The form's request as data, so it can be sent again later (after a
  // downgrade question) exactly as it was, even if the form has changed since.
  function setupRequest() {
    var form = state.setupForm;
    var body = collectSetup();
    if (form.type !== 'modpack') {
      return { kind: 'setup', body: body, world: body.level_name, worldBase: form.type + '-' + (selectedVersion() || 'world') };
    }
    var name = $('#pack-name').value.trim();
    if (form.packSource === 'url') {
      return { kind: 'url', body: { url: $('#pack-url').value.trim(), name: name, level_name: body.level_name }, world: body.level_name, worldBase: name };
    }
    return { kind: 'upload', name: name, world: body.level_name, worldBase: name, file: form.file };
  }

  // The same request on a new, empty world.
  function onNewWorld(req) {
    var next = Object.assign({}, req);
    next.world = uniqueWorldName(req.worldBase || req.world || 'world');
    if (req.body) {
      next.body = Object.assign({}, req.body, { level_name: next.world });
      if (req.kind === 'setup') next.body.create_world = true;
    }
    return next;
  }

  function packUploadQuery(req, allowDowngrade) {
    // filename only tells the server whether this is a .mrpack or a .zip; it is percent-encoded so any name is safe.
    return 'name=' + encodeURIComponent(req.name) + '&level_name=' + encodeURIComponent(req.world) +
      '&filename=' + encodeURIComponent(req.file ? req.file.name : '') + (allowDowngrade ? '&allow_downgrade=1' : '');
  }

  // How a setup or modpack request carries on after a world_downgrade answer,
  // whether it came back at once (409) or from the task. An upload the server
  // kept (upload_token) is confirmed with an empty body instead of sent again.
  function setupFollowUp(req) {
    return {
      world: req.world,
      retry: function (data) {
        return sendSetupRequest(req, { allowDowngrade: true, token: data && data.upload_token });
      },
      newWorld: function (data) {
        var next = onNewWorld(req);
        showNewWorldInForm(next.world);
        return sendSetupRequest(next, { token: data && data.upload_token });
      },
    };
  }

  // Show the form's world step as "new world", named as the request now is.
  function showNewWorldInForm(name) {
    var form = state.setupForm;
    $('#setup-form input[name="world-mode"][value="new"]').checked = true;
    $('#new-world-wrap').hidden = false;
    $('#existing-world-wrap').hidden = true;
    $('#new-world-name').value = name;
    form.worldNameTouched = true;
    onSetupFormChange();
  }

  // opts.allowDowngrade: confirmed opening an older-version world (it's backed up first).
  // opts.token: confirm a modpack upload the server kept, without uploading it again.
  function sendSetupRequest(req, opts) {
    opts = opts || {};
    var form = state.setupForm;
    var allow = !!opts.allowDowngrade;
    var btn = $('#setup-submit');
    form.submitting = true;
    setText($('#setup-summary'), '');
    var request;
    if (req.kind === 'setup') {
      var body = Object.assign({}, req.body);
      if (allow) body.allow_downgrade = true;
      request = runBusy(btn, 'Checking…', function () { return api('POST', '/api/setups', body); });
    } else if (req.kind === 'url') {
      var mp = Object.assign({}, req.body);
      if (allow) mp.allow_downgrade = true;
      request = runBusy(btn, 'Checking…', function () { return api('POST', '/api/modpacks', mp); });
    } else if (opts.token) {
      var confirmPath = '/api/modpacks/upload?' + packUploadQuery(req, allow) + '&token=' + encodeURIComponent(opts.token);
      request = runBusy(btn, 'Checking…', function () { return api('POST', confirmPath); });
    } else {
      var path = '/api/modpacks/upload?' + packUploadQuery(req, allow);
      request = runBusy(btn, 'Uploading…', function () { return startUpload('pack', path, req.file); });
    }
    return request.then(function (data) {
      form.submitting = false;
      var task = acceptTask(data, setupFollowUp(req));
      if (task) form.pendingTaskId = taskIdent(task);
      setText($('#setup-summary'), 'Working on it. You can leave this page. Progress shows at the top.');
      updateSetupSubmit();
      window.scrollTo({ top: 0, behavior: prefersReducedMotion() ? 'auto' : 'smooth' });
    }, function (err) {
      form.submitting = false;
      updateSetupSubmit();
      if (err.code === 'aborted') {
        setText($('#setup-summary'), 'Upload cancelled. Nothing was installed.');
        btn.focus();
        return;
      }
      if (err.code === 'world_downgrade') {
        var follow = setupFollowUp(req);
        var data = err.data || {};
        downgradeDialog(err, req.world).then(function (choice) {
          if (choice === 'continue') follow.retry(data);
          else if (choice === 'new') follow.newWorld(data);
        });
        return;
      }
      if (err.code === 'upload_expired') { askUploadAgain(req, allow); return; }
      if (req.kind === 'upload') {
        setText($('#setup-summary'), 'Nothing was installed. Your details are still here.');
        uploadErrorToast('Couldn\'t upload the modpack', err, function () { sendSetupRequest(req, { allowDowngrade: allow }); });
        return;
      }
      toastError(req.kind === 'setup' ? 'Couldn\'t create the setup' : 'Couldn\'t install the modpack', err);
    });
  }

  // 410 upload_expired: the server kept the uploaded pack for an hour while
  // waiting for an answer, and it's gone now.
  function askUploadAgain(req, allowDowngrade) {
    var hasFile = !!req.file;
    setText($('#setup-summary'), 'Nothing was installed. The uploaded file is no longer on the server.');
    confirmDialog({
      icon: 'i-upload',
      title: 'Upload the modpack again?',
      body: [
        el('p', null, 'The server keeps an uploaded pack for an hour while it waits for your answer, and this one has been removed since. Nothing was installed.'),
        el('p', null, hasFile
          ? ['This page still has ', el('strong', null, req.file.name), ', so it can be sent again with the same choices.']
          : 'Choose the file again to install it.'),
      ],
      confirmLabel: hasFile ? 'Upload again' : 'Choose the file',
      confirmIcon: 'i-upload',
    }).then(function (ok) {
      if (!ok) return;
      if (hasFile) sendSetupRequest(req, { allowDowngrade: allowDowngrade });
      else pickPackFileAgain();
    });
  }

  function pickPackFileAgain() {
    var modpack = $('#setup-form input[name="type"][value="modpack"]');
    if (modpack && !modpack.checked) { modpack.checked = true; modpack.dispatchEvent(new Event('change')); }
    var fileSource = $('#setup-form [data-pack-source="file"]');
    if (fileSource && fileSource.getAttribute('aria-pressed') !== 'true') fileSource.click();
    setPackFile(null);
    window.location.hash = '#/setups';
    $('#pack-file').click();
  }

  /* ---------------------------------------------------------------- uploads (modpacks and backups) */

  // One upload at a time: the server refuses a second one with 409 busy.
  var UPLOAD_UI = {
    pack: { wrap: '#upload-progress', bar: '#upload-bar', pct: '#upload-pct', label: '#upload-label', done: 'Upload complete. Checking the pack…' },
    backup: { wrap: '#backup-upload-progress', bar: '#backup-upload-bar', pct: '#backup-upload-pct', label: '#backup-upload-label', done: 'Upload complete. Checking the backup…' },
  };

  // Upload with progress in the matching progress row; the row's Cancel aborts it.
  function startUpload(kind, path, file) {
    var ui = UPLOAD_UI[kind];
    $(ui.wrap).hidden = false;
    setUploadProgress(kind, 0, file.size);
    var upload = uploadFile(path, file, function (loaded, total) { setUploadProgress(kind, loaded, total); });
    state.uploading = upload;
    state.uploadingKind = kind;
    updateUploadButtons();
    upload.catch(function () {}).then(function () {
      if (state.uploading === upload) { state.uploading = null; state.uploadingKind = null; }
      $(ui.wrap).hidden = true;
      $(ui.bar).setAttribute('aria-valuenow', '0');
      updateUploadButtons();
    });
    return upload;
  }

  function setUploadProgress(kind, loaded, total) {
    var ui = UPLOAD_UI[kind];
    var pct = total ? Math.min(100, Math.round((loaded / total) * 100)) : 0;
    var bar = $(ui.bar);
    bar.setAttribute('aria-valuenow', String(pct));
    bar.firstElementChild.style.width = pct + '%';
    setText($(ui.pct), pct + '%');
    setText($(ui.label), pct >= 100 ? ui.done : 'Uploading ' + formatBytes(loaded) + ' of ' + formatBytes(total));
  }

  // A file the server can't take, said before uploading it (the server checks too).
  function uploadSizeProblem(file) {
    var limit = uploadLimit();
    if (limit && file.size > limit) {
      return '“' + file.name + '” is ' + formatBytes(file.size) + ', and this server accepts uploads up to ' + formatBytes(limit) + '.';
    }
    if (state.disk && state.disk.free_bytes != null && file.size > Number(state.disk.free_bytes)) {
      return '“' + file.name + '” is ' + formatBytes(file.size) + ', but the volume only has ' + formatBytes(state.disk.free_bytes) + ' free.';
    }
    return '';
  }

  // Plain words for the ways the server turns an upload down. again: retries it.
  function uploadErrorToast(title, err, again) {
    if (err && err.status === 401) return;
    var retry = again ? [{ label: 'Try again', run: again }] : [];
    var text = err && err.message ? err.message : 'Something went wrong.';
    switch (err && err.code) {
      case 'busy':
        toast({ tone: 'warn', title: 'Another upload or task is running', text: text, actions: retry });
        return;
      case 'disk_full':
        toast({
          tone: 'bad', title: 'Not enough free space on the volume',
          text: text + ' Delete old backups, worlds or downloaded files, then try again.',
          actions: [{ label: 'Downloaded files', run: function () { window.location.hash = '#/setups/storage'; } }],
        });
        return;
      case 'too_slow':
        toast({ tone: 'bad', title: 'The upload was too slow', text: text + ' A steadier connection helps.', actions: retry });
        return;
      case 'incomplete_body':
        toast({ tone: 'bad', title: 'The upload was cut off', text: text, actions: retry });
        return;
      case 'too_large':
        toast({ tone: 'bad', title: 'That file is too large', text: text + ' Raise MAX_UPLOAD_MB in Railway to allow bigger uploads.' });
        return;
      default:
        if (err && err.status === 413) {
          toast({ tone: 'bad', title: 'That file is too large', text: text });
          return;
        }
        toast({ tone: 'bad', title: title, text: text, actions: err && err.code === 'network' ? retry : [] });
    }
  }

  /* ================================================================ worlds */

  function loadWorlds(quiet) {
    var host = $('#worlds-list');
    if (!state.worlds && !quiet) host.replaceChildren(el('li', null, skeletonRows(3)));
    return api('GET', '/api/worlds').then(function (data) {
      state.worlds = Array.isArray(data.worlds) ? data.worlds : [];
      renderWorlds();
      if (state.setupForm) onSetupFormChange();
    }, function (err) {
      if (!state.worlds) host.replaceChildren(el('li', null, errorState('Couldn\'t load your worlds', err, function () { loadWorlds(); })));
      else if (!quiet) toastError('Couldn\'t refresh worlds', err);
    });
  }

  function renderWorlds() { keepFocus(['#worlds-list'], renderWorldsNow); }

  function renderWorldsNow() {
    var host = $('#worlds-list');
    var worlds = (state.worlds || []).slice().sort(function (a, b) {
      if (a.active !== b.active) return a.active ? -1 : 1;
      var ta = toDate(a.last_played), tb = toDate(b.last_played);
      return (tb ? tb.getTime() : 0) - (ta ? ta.getTime() : 0) || String(a.name).localeCompare(String(b.name));
    });
    setText($('#worlds-count'), worlds.length ? '(' + worlds.length + ')' : '');
    if (!worlds.length) {
      host.replaceChildren(el('li', null, emptyState('i-world', 'No worlds yet', 'Minecraft creates one the first time a setup starts. You can also create an empty one above.')));
      return;
    }
    var wait = worldOpsWait();
    host.replaceChildren.apply(host, worlds.map(function (w) {
      var meta = [];
      meta.push(el('span', null, w.last_version ? 'Last opened on ' + w.last_version : 'Not played yet'));
      // newest_version: the newest Minecraft that ever opened it. Anything older
      // asks first (and takes a backup), even when the last run was older.
      var newest = w.newest_version;
      if (newest && newest !== w.last_version && (!w.last_version || compareVersions(newest, w.last_version) > 0)) {
        meta.push(el('span', { title: 'Opening it with a version older than ' + newest + ' asks first and takes a backup.' },
          icon('i-alert', 'icon-sm'), 'Opened on ' + newest + ' earlier'));
      }
      if (w.last_played) meta.push(el('span', null, icon('i-clock', 'icon-sm'), 'Played ' + relativeTime(w.last_played)));
      meta.push(el('span', null, icon('i-disk', 'icon-sm'), formatBytes(w.size_bytes)));
      var used = Array.isArray(w.used_by) ? w.used_by : [];
      var deleteBtn = el('button', {
        class: 'btn btn-ghost-danger btn-sm', type: 'button', 'aria-label': 'Delete world ' + w.name,
        onclick: function (e) { deleteWorld(w, e.currentTarget); },
      }, icon('i-trash', 'icon-sm'), el('span', null, 'Delete'));
      var actions = [
        el('button', {
          class: 'btn btn-sm', type: 'button', 'data-backup-world': w.name,
          'aria-disabled': wait ? 'true' : null, title: wait || null, 'aria-label': 'Back up ' + w.name,
          onclick: function (e) { if (!isUnavailable(e.currentTarget)) backupWorld(w.name, e.currentTarget); },
        }, icon('i-archive', 'icon-sm'), el('span', null, 'Back up')),
      ];
      // The active world can't be deleted; an invisible stand-in keeps the columns aligned.
      actions.push(w.active ? slotPlaceholder(deleteBtn) : deleteBtn);
      return el('li', { class: 'row' + (w.active ? ' is-active' : '') },
        el('span', { class: 'glyph t-world' }, icon('i-world')),
        el('div', { class: 'row-main' },
          el('p', { class: 'row-title' }, w.name, w.active ? el('span', { class: 'badge badge-accent' }, icon('i-check'), 'In use') : null),
          el('p', { class: 'row-meta' }, metaParts(meta))),
        el('div', { class: 'row-actions' }, actions),
        used.length || w.active ? el('p', { class: 'row-note' },
          w.active ? 'The active setup uses this world, so it can\'t be deleted. ' : '',
          used.length ? 'Used by: ' + used.join(', ') + '.' : '') : null);
    }));
  }

  function initWorlds() {
    $('#world-form').addEventListener('submit', function (e) {
      e.preventDefault();
      var input = $('#world-name');
      var name = input.value.trim();
      var msg = '';
      if (!name) msg = 'Type a name for the new world.';
      else if (!NAME_RE.test(name)) msg = NAME_RULE;
      else if ((state.worlds || []).some(function (w) { return w.name.toLowerCase() === name.toLowerCase(); })) msg = 'There\'s already a world called “' + name + '”.';
      showFieldError(input, $('#world-name-error'), msg);
      if (msg) { input.focus(); return; }
      var btn = e.submitter || $('#world-form button[type="submit"]');
      runBusy(btn, 'Creating…', function () {
        return api('POST', '/api/worlds', { name: name }).then(function () {
          input.value = '';
          toast({ tone: 'ok', title: 'World created', text: '“' + name + '” is empty until a setup uses it.' });
          return loadWorlds(true);
        }, function (err) {
          showFieldError(input, $('#world-name-error'), err.message);
          input.focus();
        });
      });
    });
    $('#world-name').addEventListener('input', function () { showFieldError($('#world-name'), $('#world-name-error'), ''); });
    $('#backup-active').addEventListener('click', function (e) { if (!isUnavailable(e.currentTarget)) backupWorld(null, e.currentTarget); });
    var backupFile = $('#backup-file');
    $('#backup-upload').addEventListener('click', function (e) {
      if (isUnavailable(e.currentTarget)) return;
      backupFile.click();
    });
    backupFile.addEventListener('change', function () {
      var file = backupFile.files && backupFile.files[0];
      backupFile.value = ''; // choosing the same file again still fires change
      if (file) uploadBackup(file);
    });
    $('#backup-upload-cancel').addEventListener('click', function () { if (state.uploading) state.uploading.abort(); });
  }

  // Bring back a backup that was downloaded earlier (say, onto a new volume).
  function uploadBackup(file) {
    var btn = $('#backup-upload');
    var problem = !/\.(tar\.gz|tgz)$/i.test(file.name)
      ? '“' + file.name + '” isn\'t a .tar.gz file. Upload a backup downloaded from Railpowered, or a .tar.gz of one world folder.'
      : uploadSizeProblem(file);
    if (problem) {
      toast({ tone: 'bad', title: 'Can\'t upload that file', text: problem });
      return;
    }
    runBusy(btn, 'Uploading…', function () {
      return startUpload('backup', '/api/backups/upload', file).then(function (data) {
        if (Array.isArray(data.backups)) state.backups = data.backups;
        renderBackups();
        loadBackups(true);
        var backup = data.backup || {};
        var world = backup.world || '';
        var exists = (state.worlds || []).some(function (w) { return w.name === world; });
        toast({
          tone: 'ok', title: 'Backup added',
          text: (world ? 'A backup of “' + world + '”' : 'The backup') + ' is in the list now.' +
            (world && !exists ? ' There\'s no world with that name yet, so restoring it adds one.' : ''),
          actions: backup.name && !backup.damaged ? [{ label: 'Restore it', run: function () {
            restoreBackup(backup, $('[data-restore="' + cssEscape(backup.name) + '"]'));
          } }] : [],
        });
      }, function (err) {
        if (err.code === 'aborted') { toast({ tone: 'info', title: 'Upload cancelled', text: 'Nothing was added.' }); return; }
        if (err.code === 'invalid_backup') {
          toast({ tone: 'bad', title: 'That isn\'t a backup Railpowered can use', text: err.message + ' Upload a backup downloaded from Railpowered, or a .tar.gz of one world folder.' });
          return;
        }
        uploadErrorToast('Couldn\'t upload the backup', err, function () { uploadBackup(file); });
      });
    });
  }

  function deleteWorld(world, button) {
    var used = Array.isArray(world.used_by) ? world.used_by : [];
    var confirmInput = el('input', { class: 'input', id: 'confirm-world', autocomplete: 'off', spellcheck: 'false', 'aria-describedby': 'confirm-world-help' });
    openDialog({
      tone: 'danger', icon: 'i-trash',
      title: 'Delete the world “' + world.name + '”?',
      body: [
        el('p', null, 'This permanently deletes ', el('strong', null, world.name), ' (' + formatBytes(world.size_bytes) + '). It can\'t be undone.'),
        used.length ? el('div', { class: 'callout tone-warn' }, icon('i-alert'), el('div', null,
          el('p', null, el('strong', null, used.length === 1 ? 'This setup uses it and will be removed too:' : 'These setups use it and will be removed too:')),
          el('ul', { class: 'used-by' }, used.map(function (n) { return el('li', null, n); })))) : null,
        el('p', null, 'Backups of this world are kept. Want a copy first? Choose ', el('strong', null, 'Back up first'), '.'),
        el('div', { class: 'field' },
          el('label', { class: 'field-label', for: 'confirm-world' }, 'Type ', el('code', null, world.name), ' to confirm'),
          confirmInput,
          el('p', { class: 'field-help', id: 'confirm-world-help' }, 'This makes sure the right world gets deleted.')),
      ],
      actions: [
        { label: 'Cancel', value: null, style: 'ghost' },
        { label: 'Back up first', value: 'backup', style: 'secondary', icon: 'i-archive' },
        { label: 'Delete world', value: 'delete', style: 'danger', icon: 'i-trash', disabled: true, isDefault: true },
      ],
      focus: function () { return confirmInput; },
      onOpen: function (buttons) {
        confirmInput.addEventListener('input', function () {
          buttons['delete'].disabled = confirmInput.value.trim() !== world.name;
        });
      },
    }).then(function (choice) {
      if (choice === 'backup') { backupWorld(world.name, null); return; }
      if (choice !== 'delete') return;
      runBusy(button, 'Deleting…', function () {
        return api('DELETE', '/api/worlds/' + encodeURIComponent(world.name)).then(function (data) {
          var removed = data && Array.isArray(data.removed_setups) ? data.removed_setups : [];
          var orphaned = orphanedItems(data);
          toast({
            tone: 'ok', title: 'World deleted',
            text: '“' + world.name + '” is gone.' + (removed.length ? ' Also removed: ' + removed.join(', ') + '.' : '') +
              orphanedText(orphaned),
            actions: orphanActions(orphaned),
          });
          if (state.storage) loadStorage(true);
          return Promise.all([loadWorlds(true), state.setups ? loadSetups(true) : null]);
        }, function (err) { toastError('Couldn\'t delete the world', err); });
      });
    });
  }

  /* ================================================================ backups */

  function loadBackups(quiet) {
    var host = $('#backups-list');
    if (!state.backups && !quiet) host.replaceChildren(el('li', null, skeletonRows(2)));
    return api('GET', '/api/backups').then(function (data) {
      state.backups = Array.isArray(data.backups) ? data.backups : [];
      state.disk = data.disk || null;
      renderBackups();
    }, function (err) {
      if (!state.backups) host.replaceChildren(el('li', null, errorState('Couldn\'t load backups', err, function () { loadBackups(); })));
      else if (!quiet) toastError('Couldn\'t refresh backups', err);
    });
  }

  // Railpowered's own copies: before a restore, an upgrade, or opening a world with an older version.
  function isSafetyBackup(b) { return (b.kind && b.kind !== 'manual') || /-pre-(restore|downgrade|upgrade)-/.test(String(b.name)); }

  function renderBackups() { keepFocus(['#backups-list'], renderBackupsNow); }

  function renderBackupsNow() {
    var host = $('#backups-list');
    var list = (state.backups || []).slice().sort(function (a, b) {
      var ta = toDate(a.created_at), tb = toDate(b.created_at);
      return (tb ? tb.getTime() : 0) - (ta ? ta.getTime() : 0);
    });
    setText($('#backups-count'), list.length ? '(' + list.length + ')' : '');
    renderDisk();
    if (!list.length) {
      host.replaceChildren(el('li', null, emptyState('i-archive', 'No backups yet', 'Back up a world before big changes like a version upgrade. It only takes a moment.')));
      return;
    }
    var wait = worldOpsWait();
    host.replaceChildren.apply(host, list.map(function (b) {
      var when = formatDateTime(b.created_at);
      var meta = [];
      if (b.minecraft_version) meta.push(el('span', null, 'Minecraft ' + b.minecraft_version));
      meta.push(el('span', null, icon('i-disk', 'icon-sm'), formatBytes(b.size_bytes)));
      // The title has the date; the meta adds "2 hours ago" only while that says more.
      if (b.created_at && isRecent(b.created_at, 7)) meta.push(el('span', null, icon('i-clock', 'icon-sm'), relativeTime(b.created_at)));
      var damaged = !!b.damaged;
      var restoreTitle = damaged ? 'This backup can\'t be read, so it can\'t be restored' : (wait || null);
      return el('li', { class: 'row' },
        el('span', { class: 'glyph t-backup' }, icon('i-archive')),
        el('div', { class: 'row-main' },
          el('p', { class: 'row-title' }, (b.world || 'Unknown world') + (when ? ' · ' + when : ''),
            damaged ? el('span', { class: 'badge badge-bad', title: 'The archive is damaged or incomplete. You can still download it.' }, icon('i-alert'), 'Damaged') : null,
            isSafetyBackup(b) ? el('span', { class: 'badge badge-work', title: 'Taken automatically before a restore, before an upgrade, or before opening the world with an older version' }, 'Safety copy') : null),
          el('p', { class: 'row-meta' }, metaParts(meta))),
        el('div', { class: 'row-actions' },
          el('a', { class: 'btn btn-sm', href: '/api/backups/' + encodeURIComponent(b.name) + '/download', download: b.name, 'aria-label': 'Download backup ' + b.name },
            icon('i-download', 'icon-sm'), el('span', null, 'Download')),
          el('button', {
            class: 'btn btn-sm', type: 'button', disabled: damaged || null, 'data-restore': b.name, 'data-damaged': damaged ? '' : null,
            'aria-disabled': !damaged && wait ? 'true' : null,
            title: restoreTitle, 'aria-label': 'Restore backup ' + b.name,
            onclick: function (e) { if (!isUnavailable(e.currentTarget)) restoreBackup(b, e.currentTarget); },
          }, icon('i-rotate', 'icon-sm'), el('span', null, 'Restore')),
          el('button', {
            class: 'btn btn-ghost-danger btn-sm', type: 'button', 'aria-label': 'Delete backup ' + b.name,
            onclick: function (e) { deleteBackup(b, e.currentTarget); },
          }, icon('i-trash', 'icon-sm'), el('span', null, 'Delete'))),
        el('p', { class: 'row-note mono' }, b.name));
    }));
  }

  function renderDisk() {
    var d = state.disk;
    var meter = $('#disk-meter');
    if (!d || !d.total_bytes) { meter.hidden = true; return; }
    meter.hidden = false;
    var used = Number(d.used_bytes) || 0, total = Number(d.total_bytes) || 1;
    var free = d.free_bytes != null ? Number(d.free_bytes) : Math.max(0, total - used);
    var pct = Math.max(0, Math.min(100, (used / total) * 100));
    setText($('#disk-used'), 'Volume: ' + formatBytes(used) + ' of ' + formatBytes(total) + ' used');
    setText($('#disk-free'), formatBytes(free) + ' free');
    var bar = $('#disk-bar');
    bar.style.width = pct + '%';
    bar.className = pct > 95 ? 'is-full' : (pct > 80 ? 'is-high' : '');
    $('#disk-bar-wrap').setAttribute('aria-label', Math.round(pct) + '% of the volume is used');
  }

  // Called on every status poll: what can start depends on the task and the phase.
  function updateBackupButtons() {
    var wait = worldOpsWait();
    var btn = $('#backup-active');
    if (btn.getAttribute('aria-busy') !== 'true') {
      btn.disabled = !(state.status && state.status.setup);
      markUnavailable(btn, btn.disabled ? '' : wait);
    }
    $$('[data-backup-world], [data-restore]:not([data-damaged]), #setups-list [data-switch]:not(.slot-placeholder)').forEach(function (b) {
      markUnavailable(b, wait);
    });
    updateUploadButtons();
  }

  function updateUploadButtons() {
    var btn = $('#backup-upload');
    if (btn && btn.getAttribute('aria-busy') !== 'true') {
      markUnavailable(btn, state.uploading ? (state.uploadingKind === 'pack' ? 'Available when the modpack upload finishes' : 'A backup is uploading') : '');
    }
    updateSetupSubmit();
  }

  function backupWorld(world, button) {
    var body = world ? { world: world } : {};
    runBusy(button, 'Starting…', function () {
      return api('POST', '/api/backups', body).then(function (data) {
        acceptTask(data);
        announce('Backing up ' + (world ? '“' + world + '”' : 'the active world') + '. Progress shows at the top of the page.');
      }, function (err) { toastError('Couldn\'t start the backup', err); });
    });
  }

  function restoreBackup(backup, button) {
    // world is null when the archive doesn't say which world it holds; then
    // the only safe choice is a new world, since "replace" would be a guess.
    var world = backup.world || '';
    var wait = worldOpsWait();
    if (wait) {
      toast({ tone: 'info', title: 'Restoring isn\'t possible right now', text: wait + '.' });
      return Promise.resolve();
    }
    var activeWorld = state.status && state.status.setup && state.status.setup.level_name;
    var running = !!(state.status && (RUNNING[state.status.phase] || state.status.phase === 'stopping'));
    var inUse = !!world && running && activeWorld === world;
    var startNew = !world || inUse; // open on the choice that can actually go ahead
    var newName = el('input', { class: 'input', id: 'restore-name', value: uniqueWorldName((world || 'restored') + (world ? '-restored' : '')), autocomplete: 'off', spellcheck: 'false', maxlength: '32' });
    var nameError = el('p', { class: 'field-error', id: 'restore-name-error', hidden: true });
    var replaceRadio = el('input', { type: 'radio', name: 'restore-mode', value: 'replace', checked: !startNew, disabled: !world });
    var newRadio = el('input', { type: 'radio', name: 'restore-mode', value: 'new', checked: startNew });
    var runningNote = el('span', { class: 'field-error is-warn' }, icon('i-alert'),
      el('span', null, 'The server is using “' + world + '” right now. Stop it first to replace this world.'));
    var nameWrap = el('div', { class: 'field field-indent', hidden: true },
      el('label', { class: 'field-label', for: 'restore-name' }, 'New world name'), newName, nameError);
    var label = world ? '“' + world + '”' : 'an unknown world';
    // An uploaded backup (or one whose world was deleted) has no world to replace:
    // restoring it under its own name brings that world back.
    var missing = !!world && !!state.worlds && !state.worlds.some(function (w) { return w.name === world; });
    return openDialog({
      tone: 'primary', icon: 'i-rotate',
      title: 'Restore this backup?',
      body: [
        el('p', null, 'Backup of ', world ? el('strong', null, world) : 'a world whose name isn\'t recorded', backup.created_at ? ' from ' + formatDateTime(backup.created_at) : '', backup.minecraft_version ? ' (Minecraft ' + backup.minecraft_version + ')' : '', '.'),
        world ? el('label', { class: 'radio-line' }, replaceRadio, el('span', { class: 'radio-copy' },
          el('span', null, missing ? 'Restore as ' + label : 'Replace ' + label),
          el('span', { class: 'field-help' }, missing
            ? 'There\'s no world called ' + label + ' right now, so the backup becomes it.'
            : 'Railpowered first saves the current ' + label + ' as a safety backup, so nothing is lost.'),
          inUse ? runningNote : null)) : null,
        el('label', { class: 'radio-line' }, newRadio, el('span', { class: 'radio-copy' },
          el('span', null, missing ? 'Restore under another name' : 'Restore as a new world'),
          el('span', { class: 'field-help' }, world && !missing ? 'Keeps ' + label + ' as it is and adds the backup next to it.' : 'The backup becomes a world of its own, and no existing world is touched.'))),
        nameWrap,
        el('p', { class: 'subtle' }, 'Restoring changes only the world. Setups, settings and player lists stay as they are.'),
      ],
      actions: [
        { label: 'Cancel', value: null, style: 'ghost' },
        { label: 'Restore', value: 'restore', style: 'primary', icon: 'i-rotate', isDefault: true },
      ],
      focus: function (buttons) { return startNew ? newName : buttons.restore; },
      onOpen: function (buttons) {
        function sync() {
          var mode = newRadio.checked ? 'new' : 'replace';
          nameWrap.hidden = mode !== 'new';
          var blocked = mode === 'replace' && inUse;
          var bad = '';
          if (mode === 'new') {
            var v = newName.value.trim();
            if (!NAME_RE.test(v)) bad = NAME_RULE;
            else if ((state.worlds || []).some(function (w) { return w.name.toLowerCase() === v.toLowerCase(); })) bad = 'There\'s already a world with that name.';
          }
          showFieldError(newName, nameError, bad);
          buttons.restore.disabled = blocked || !!bad;
        }
        replaceRadio.addEventListener('change', sync);
        newRadio.addEventListener('change', sync);
        newName.addEventListener('input', sync);
        sync();
      },
    }).then(function (choice) {
      if (choice !== 'restore') return;
      var body = newRadio.checked ? { target: newName.value.trim() } : {};
      runBusy(button, 'Starting…', function () {
        return api('POST', '/api/backups/' + encodeURIComponent(backup.name) + '/restore', body).then(function (data) {
          acceptTask(data);
          announce('Restoring ' + (body.target ? '“' + body.target + '”' : label) + '. Progress shows at the top of the page.');
        }, function (err) { toastError('Couldn\'t restore the backup', err); });
      });
    });
  }

  function deleteBackup(backup, button) {
    confirmDialog({
      tone: 'danger', icon: 'i-trash',
      title: 'Delete this backup?',
      body: [
        el('p', null, 'The backup of ', el('strong', null, backup.world || 'this world'), backup.created_at ? ' from ' + formatDateTime(backup.created_at) : '', ' (' + formatBytes(backup.size_bytes) + ') will be deleted for good.'),
        el('p', null, 'The world itself isn\'t touched.'),
      ],
      confirmLabel: 'Delete backup', confirmIcon: 'i-trash',
    }).then(function (ok) {
      if (!ok) return;
      runBusy(button, '', function () {
        return api('DELETE', '/api/backups/' + encodeURIComponent(backup.name)).then(function () {
          toast({ tone: 'ok', title: 'Backup deleted', text: backup.name });
          return loadBackups(true);
        }, function (err) { toastError('Couldn\'t delete the backup', err); });
      });
    });
  }

  /* ================================================================ players */

  function loadPlayers(quiet) {
    if (!state.players && !quiet) {
      $('#wl-list').replaceChildren(el('li', null, skeletonRows(2)));
      $('#ops-list').replaceChildren(el('li', null, skeletonRows(2)));
    }
    return api('GET', '/api/players').then(function (data) {
      state.players = data || {};
      renderPlayers();
    }, function (err) {
      if (!state.players) {
        $('#wl-list').replaceChildren(el('li', null, errorState('Couldn\'t load players', err, function () { loadPlayers(); })));
        $('#ops-list').replaceChildren();
      } else if (!quiet) toastError('Couldn\'t refresh players', err);
    });
  }

  function listNames(list) {
    return (Array.isArray(list) ? list : []).map(function (x) { return typeof x === 'string' ? { name: x } : x; }).filter(function (x) { return x && x.name; });
  }

  function renderPlayers() { keepFocus(['#wl-list', '#ops-list'], renderPlayersNow); }

  function renderPlayersNow() {
    var p = state.players || {};
    var wl = listNames(p.whitelist), ops = listNames(p.ops);
    var sw = $('#wl-switch');
    if (!state.wlSaving) {
      // The switch stays off-limits until the lists are known.
      sw.checked = !!p.whitelist_enabled;
      sw.disabled = false;
      sw.removeAttribute('aria-busy');
    }
    var badge = $('#wl-badge');
    badge.className = 'badge ' + (p.whitelist_enabled ? 'badge-go' : 'badge-warn');
    badge.replaceChildren(icon(p.whitelist_enabled ? 'i-shield' : 'i-alert'), p.whitelist_enabled ? 'Private' : 'Open to anyone with the address');
    setText($('#wl-help'), p.whitelist_enabled
      ? 'On: only the players on the whitelist (and operators) can join. Everyone else is turned away.'
      : 'Off: anyone who knows the join address can join. Turn this on to keep the server to your friends.');

    var note = $('#mode-note');
    if (p.online_mode === false) {
      note.hidden = false;
      note.className = 'callout tone-warn';
      note.replaceChildren(icon('i-alert'), el('p', null, el('strong', null, 'Online mode is off.'),
        ' Minecraft doesn\'t check accounts, so anyone can join using any name, including yours. Keep the whitelist on, and only make people operators you trust.'));
    } else if (p.whitelist_enabled && !wl.length) {
      note.hidden = false;
      note.className = 'callout tone-warn';
      note.replaceChildren(icon('i-alert'), el('p', null, el('strong', null, 'The whitelist is empty.'), ' Only operators can join until you add someone below.'));
    } else {
      note.hidden = true;
    }

    setText($('#wl-count'), wl.length ? '(' + wl.length + ')' : '');
    setText($('#ops-count'), ops.length ? '(' + ops.length + ')' : '');
    var wlHost = $('#wl-list');
    var opsHost = $('#ops-list');
    if (wl.length || state.removedPlayers.whitelist) wlHost.replaceWith(withId(peopleList(wl, 'whitelist'), 'wl-list'));
    else wlHost.replaceWith(withId(el('ul', { class: 'people', role: 'list' },
      el('li', { class: 'span-all' }, emptyStateCompact('i-shield', 'Nobody on the whitelist', 'Add your friends\' Minecraft usernames.'))), 'wl-list'));
    if (ops.length || state.removedPlayers.ops) opsHost.replaceWith(withId(peopleList(ops, 'ops'), 'ops-list'));
    else opsHost.replaceWith(withId(el('ul', { class: 'people', role: 'list' },
      el('li', { class: 'span-all' }, emptyStateCompact('i-crown', 'No operators', 'Make yourself an operator to use admin commands in-game.'))), 'ops-list'));
  }

  function withId(node, id) { node.id = id; return node; }

  function renderPlayersOnline() {
    var s = state.status;
    var host = $('#players-online');
    var names = s && RUNNING[s.phase] ? s.players : [];
    setText($('#players-online-count'), names.length ? '(' + names.length + ')' : '');
    if (!names.length) {
      var copy = onlineEmptyCopy(s);
      host.replaceChildren(emptyStateCompact('i-players', copy[0].replace(/\.$/, ''), copy[1]));
    } else {
      host.replaceChildren(peopleList(names, null));
    }
    if (state.players) renderPlayers();
  }

  function initPlayers() {
    [['whitelist', 'wl'], ['ops', 'ops']].forEach(function (pair) {
      var kind = pair[0], prefix = pair[1];
      var form = $('#' + prefix + '-form');
      var input = $('#' + prefix + '-name');
      var err = $('#' + prefix + '-error');
      input.addEventListener('input', function () { showFieldError(input, err, ''); });
      form.addEventListener('submit', function (e) {
        e.preventDefault();
        var name = input.value.trim();
        var msg = '';
        if (!name) msg = 'Type a Minecraft username.';
        else if (!PLAYER_RE.test(name)) msg = 'Minecraft usernames only use letters, numbers and underscores (up to 16).';
        showFieldError(input, err, msg);
        if (msg) { input.focus(); return; }
        var btn = form.querySelector('button[type="submit"]');
        runBusy(btn, 'Adding…', function () {
          return api('POST', '/api/players/' + kind, { name: name }).then(function () {
            input.value = '';
            var removed = state.removedPlayers[kind];
            if (removed && removed.name.toLowerCase() === name.toLowerCase()) clearRemovedPlayer(kind, true);
            toast({ tone: 'ok', title: kind === 'ops' ? name + ' is now an operator' : name + ' added to the whitelist' });
            return loadPlayers(true);
          }, function (e2) {
            showFieldError(input, err, e2.message);
            input.focus();
          });
        });
      });
    });

    var sw = $('#wl-switch');
    sw.addEventListener('change', function () {
      var enabled = sw.checked;
      var p = state.players || {};
      var confirm = Promise.resolve(true);
      if (enabled && !listNames(p.whitelist).length) {
        confirm = confirmDialog({
          tone: 'warn', icon: 'i-shield',
          title: 'Turn on an empty whitelist?',
          body: [el('p', null, 'Nobody is on the whitelist yet, so only operators will be able to join. Players who are online now stay connected until they leave.'),
            el('p', null, 'You can add your friends right after.')],
          confirmLabel: 'Turn it on', confirmIcon: 'i-shield',
        });
      }
      confirm.then(function (ok) {
        if (!ok) { sw.checked = !enabled; return; }
        state.wlSaving = true;
        sw.setAttribute('aria-busy', 'true');
        sw.disabled = true;
        api('PUT', '/api/players/whitelist/enabled', { enabled: enabled }).then(function () {
          toast({ tone: 'ok', title: enabled ? 'Whitelist is on' : 'Whitelist is off', text: enabled ? 'Only players on the list can join now.' : 'Anyone with the address can join now.' });
          if (state.players) state.players.whitelist_enabled = enabled;
        }, function (err) {
          sw.checked = !enabled;
          toastError('Couldn\'t change the whitelist', err);
        }).finally(function () {
          state.wlSaving = false;
          sw.removeAttribute('aria-busy');
          sw.disabled = false;
          sw.focus();
          loadPlayers(true);
          pollStatus();
        });
      });
    });
  }

  // A removed player stays in the list for a while with an Undo button, so
  // undoing is one Tab away instead of in a toast at the end of the page.
  var UNDO_MS = 15000;

  function removePlayer(kind, name, button) {
    var path = '/api/players/' + kind + '/' + encodeURIComponent(name);
    runBusy(button, '', function () {
      return api('DELETE', path).then(function () {
        clearRemovedPlayer(kind, true);
        state.removedPlayers[kind] = { name: name, timer: window.setTimeout(function () { expireRemovedPlayer(kind); }, UNDO_MS) };
        announce((kind === 'ops' ? name + ' is no longer an operator.' : name + ' was removed from the whitelist.') + ' Undo is next to the list.');
        return loadPlayers(true).then(function () {
          var undo = $('[data-undo="' + kind + '"]');
          if (undo) undo.focus();
        });
      }, function (err) { toastError('Couldn\'t remove ' + name, err); });
    });
  }

  function expireRemovedPlayer(kind) {
    var removed = state.removedPlayers[kind];
    if (!removed) return;
    var active = document.activeElement;
    if (active && active.getAttribute && active.getAttribute('data-undo') === kind) {
      // Someone is about to press it: wait until focus moves on.
      removed.timer = window.setTimeout(function () { expireRemovedPlayer(kind); }, 4000);
      return;
    }
    clearRemovedPlayer(kind);
  }

  function clearRemovedPlayer(kind, quiet) {
    var removed = state.removedPlayers[kind];
    if (!removed) return;
    window.clearTimeout(removed.timer);
    delete state.removedPlayers[kind];
    if (!quiet && state.players) renderPlayers();
  }

  function undoRemovePlayer(kind, button) {
    var removed = state.removedPlayers[kind];
    if (!removed) return;
    var name = removed.name;
    window.clearTimeout(removed.timer);
    runBusy(button, 'Undoing…', function () {
      return api('POST', '/api/players/' + kind, { name: name }).then(function () {
        clearRemovedPlayer(kind, true);
        announce(kind === 'ops' ? name + ' is an operator again.' : name + ' is back on the whitelist.');
        return loadPlayers(true).then(function () {
          var again = $('[data-remove="' + kind + ':' + cssEscape(name.toLowerCase()) + '"]');
          (again || $('#' + (kind === 'ops' ? 'ops' : 'wl') + '-name')).focus();
        });
      }, function (err) {
        removed.timer = window.setTimeout(function () { expireRemovedPlayer(kind); }, UNDO_MS);
        toastError('Couldn\'t undo', err);
      });
    });
  }

  /* ================================================================ settings */

  var SETTINGS_GROUPS = [
    { title: 'Gameplay', keys: ['difficulty', 'gamemode', 'hardcore', 'pvp', 'allow-flight', 'allow-nether', 'enable-command-block', 'spawn-protection'] },
    { title: 'Players and server list', keys: ['motd', 'max-players', 'online-mode'] },
    { title: 'Performance', keys: ['view-distance', 'simulation-distance'] },
    { title: 'New worlds', keys: ['level-seed', 'level-type'] },
  ];

  var settingsFields = {}; // key -> { scope, input, type, original }

  function loadSettings() {
    if (state.settingsDirty) { renderRestartBanner(); return Promise.resolve(); }
    if (!state.settings) $('#settings-body').replaceChildren(el('section', { class: 'card' }, skeletonRows(4)));
    return api('GET', '/api/settings').then(function (data) {
      if (state.settingsDirty) return;
      state.settings = data || {};
      renderSettings();
      restoreSettingsDraft();
    }, function (err) {
      if (!state.settings) $('#settings-body').replaceChildren(el('section', { class: 'card' }, errorState('Couldn\'t load settings', err, function () { loadSettings(); })));
      else toastError('Couldn\'t refresh settings', err);
    });
  }

  function renderSettings() {
    var data = state.settings || {};
    var props = data.properties || {};
    var schema = data.schema || {};
    var rp = data.railpowered || {};
    settingsFields = {};
    var body = $('#settings-body');

    var rpCard = el('section', { class: 'card', 'aria-labelledby': 'rp-settings-title' },
      el('div', { class: 'card-head' }, el('h2', { id: 'rp-settings-title' }, el('img', { src: '/static/assets/favicon.svg', alt: '', width: '18', height: '18' }), 'Railpowered')),
      el('p', { class: 'card-sub' }, 'How Railpowered looks after the server.'),
      el('div', { class: 'settings-group' },
        el('div', { class: 'settings-grid' },
          settingField('railpowered', 'idle_timeout_minutes', {
            type: 'int', min: 0, max: 1440, label: 'Sleep after (minutes with nobody online)',
            help: 'The server saves and sleeps after this long with nobody online. 0 means it never sleeps on its own.',
          }, rp.idle_timeout_minutes),
          settingField('railpowered', 'auto_start', {
            type: 'bool', label: 'Start Minecraft when Railpowered starts',
            help: 'After a redeploy or restart, start Minecraft right away instead of waiting for the first join.',
          }, rp.auto_start))));

    // Only keys in the schema can be changed for this version. server.properties
    // may hold others (online-mode on pre-1.7 versions, say); the server would
    // refuse them, so they aren't drawn.
    var used = {};
    var groups = [];
    SETTINGS_GROUPS.forEach(function (g) {
      var keys = g.keys.filter(function (k) { return !!schema[k]; });
      keys.forEach(function (k) { used[k] = true; });
      if (keys.length) groups.push({ title: g.title, keys: keys });
    });
    var rest = Object.keys(schema).filter(function (k) { return !used[k]; });
    if (rest.length) groups.push({ title: 'Other', keys: rest });

    var mcCard = el('section', { class: 'card', 'aria-labelledby': 'mc-settings-title' },
      el('div', { class: 'card-head' }, el('h2', { id: 'mc-settings-title' }, icon('i-cube'), 'Minecraft server')),
      el('p', { class: 'card-sub' }, 'Saved in server.properties on the volume. Minecraft reads them when it starts.'),
      groups.length ? groups.map(function (g) {
        return el('div', { class: 'settings-group' },
          el('h3', null, g.title),
          el('div', { class: 'settings-grid' }, g.keys.map(function (k) {
            return settingField('properties', k, schema[k], props[k]);
          })));
      }) : el('div', { class: 'card-body' }, emptyStateCompact('i-settings', 'No settings available', 'This Minecraft version doesn\'t expose any settings here.')));

    body.replaceChildren(rpCard, mcCard);
    setDirty(false);
    renderRestartBanner();
  }

  function toBool(v) { return v === true || v === 'true' || v === 1 || v === '1'; }

  function settingField(scope, key, sch, value) {
    var id = 'set-' + scope + '-' + key.replace(/[^A-Za-z0-9_-]/g, '_');
    var type = sch.type || 'string';
    var label = sch.label || humanize(key);
    var helpId = id + '-help';
    var errId = id + '-error';
    var help = sch.help ? el('p', { class: 'field-help', id: helpId }, sch.help) : null;
    var err = el('p', { class: 'field-error', id: errId, hidden: true });
    var describedBy = [sch.help ? helpId : null, errId].filter(Boolean).join(' ');
    var input, original, field;
    if (type === 'bool') {
      original = toBool(value);
      input = el('input', { type: 'checkbox', role: 'switch', class: 'switch', id: id, 'aria-describedby': describedBy });
      input.checked = original;
      field = el('div', { class: 'field' },
        el('div', { class: 'switch-row' },
          el('div', { class: 'switch-copy' }, el('label', { for: id }, label), help),
          input),
        err);
    } else if (type === 'enum') {
      original = value == null ? '' : String(value);
      var opts = (sch.options || []).map(function (o) {
        var v = typeof o === 'object' && o ? String(o.value) : String(o);
        var l = typeof o === 'object' && o ? String(o.label || o.value) : humanize(o);
        return el('option', { value: v }, l);
      });
      if (original && !opts.some(function (o) { return o.value === original; })) opts.unshift(el('option', { value: original }, original));
      input = el('select', { class: 'select', id: id, 'aria-describedby': describedBy }, opts);
      input.value = original;
      field = el('div', { class: 'field' }, el('label', { class: 'field-label', for: id }, label), input, help, err);
    } else if (type === 'int') {
      original = value == null || value === '' ? '' : String(value);
      input = el('input', {
        class: 'input input-num', id: id, type: 'number', inputmode: 'numeric', step: '1',
        min: sch.min != null ? sch.min : null, max: sch.max != null ? sch.max : null, 'aria-describedby': describedBy,
      });
      input.value = original;
      var range = sch.min != null && sch.max != null ? el('span', { class: 'subtle text-xs' }, sch.min + ' to ' + sch.max) : null;
      field = el('div', { class: 'field' }, el('label', { class: 'field-label', for: id }, label),
        el('div', { class: 'input-with-range' }, input, range), help, err);
    } else {
      original = value == null ? '' : String(value);
      input = el('input', { class: 'input', id: id, type: 'text', autocomplete: 'off', spellcheck: key === 'motd' ? 'true' : 'false', 'aria-describedby': describedBy });
      input.value = original;
      field = el('div', { class: 'field' + (key === 'motd' ? ' wide' : '') }, el('label', { class: 'field-label', for: id }, label), input, help, err);
    }
    var entry = { scope: scope, key: key, input: input, type: type, original: original, schema: sch, error: err };
    settingsFields[scope + ':' + key] = entry;
    input.addEventListener(type === 'bool' || type === 'enum' ? 'change' : 'input', function () {
      validateSetting(entry, true);
      if (key === 'online-mode' && type === 'bool') onlineModeWarning(entry);
      setDirty(isSettingsDirty());
    });
    return field;
  }

  function onlineModeWarning(entry) {
    if (!entry.input.checked) {
      entry.error.replaceChildren(icon('i-alert'), el('span', null, 'With online mode off, anyone can join using any name. Only do this with the whitelist on.'));
      entry.error.hidden = false;
      entry.error.style.color = 'var(--warn-text)';
    } else {
      entry.error.hidden = true;
      entry.error.style.color = '';
    }
  }

  function settingValue(entry) {
    if (entry.type === 'bool') return entry.input.checked;
    if (entry.type === 'int') return entry.input.value.trim() === '' ? '' : Number(entry.input.value);
    return entry.input.value;
  }

  function validateSetting(entry, live) {
    if (entry.type !== 'int') return true;
    var raw = entry.input.value.trim();
    var msg = '';
    var sch = entry.schema || {};
    if (raw === '') msg = live ? '' : 'Enter a number.';
    else if (!/^-?\d+$/.test(raw)) msg = 'Use a whole number.';
    else {
      var n = Number(raw);
      if (sch.min != null && n < sch.min) msg = 'The lowest allowed value is ' + sch.min + '.';
      else if (sch.max != null && n > sch.max) msg = 'The highest allowed value is ' + sch.max + '.';
    }
    showFieldError(entry.input, entry.error, msg);
    return !msg;
  }

  function settingChanged(e) {
    var v = settingValue(e);
    if (e.type === 'bool') return v !== e.original;
    return String(v) !== String(e.original);
  }

  function isSettingsDirty() {
    return Object.keys(settingsFields).some(function (k) { return settingChanged(settingsFields[k]); });
  }

  // Only what the admin changed is sent. Sending everything would also pin
  // values that come from Railway variables (IDLE_TIMEOUT, AUTO_START) and
  // undo changes saved from another tab since this one loaded.
  function changedSettings() {
    var payload = {};
    Object.keys(settingsFields).forEach(function (k) {
      var entry = settingsFields[k];
      if (!settingChanged(entry)) return;
      (payload[entry.scope] = payload[entry.scope] || {})[entry.key] = settingValue(entry);
    });
    return payload;
  }

  // Unsaved edits survive a trip through the sign-in page (same tab only).
  function stashSettingsDraft() {
    if (!state.settingsDirty) return;
    var draft = {};
    Object.keys(settingsFields).forEach(function (k) {
      var entry = settingsFields[k];
      if (settingChanged(entry)) draft[k] = entry.type === 'bool' ? entry.input.checked : entry.input.value;
    });
    sessionStore.set(SESSION_KEYS.settingsDraft, JSON.stringify(draft));
  }

  function restoreSettingsDraft() {
    var raw = sessionStore.get(SESSION_KEYS.settingsDraft);
    if (!raw) return;
    sessionStore.set(SESSION_KEYS.settingsDraft, null);
    var draft;
    try { draft = JSON.parse(raw); } catch (_) { return; }
    var applied = 0;
    Object.keys(draft || {}).forEach(function (k) {
      var entry = settingsFields[k];
      if (!entry) return;
      if (entry.type === 'bool') entry.input.checked = !!draft[k];
      else entry.input.value = String(draft[k]);
      if (entry.type === 'enum' && entry.input.value !== String(draft[k])) return; // that option is gone
      if (entry.key === 'online-mode' && entry.type === 'bool') onlineModeWarning(entry);
      applied++;
    });
    if (!applied) return;
    setDirty(isSettingsDirty());
    if (state.settingsDirty) toast({ tone: 'info', title: 'Your unsaved changes are back', text: 'Save them, or choose Discard.' });
  }

  function setDirty(dirty) {
    state.settingsDirty = dirty;
    $('#savebar').hidden = !dirty;
  }

  function renderRestartBanner() {
    var s = state.status;
    var need = !!(state.settings && state.settings.restart_required);
    $('#restart-banner').hidden = !(need && s && RUNNING[s.phase]);
  }

  function initSettings() {
    var form = $('#settings-form');
    form.addEventListener('submit', function (e) {
      e.preventDefault();
      var ok = true, first = null;
      Object.keys(settingsFields).forEach(function (k) {
        var entry = settingsFields[k];
        if (!validateSetting(entry, false)) { ok = false; first = first || entry.input; }
      });
      if (!ok) { first.focus(); return; }
      var payload = changedSettings();
      if (!Object.keys(payload).length) { setDirty(false); return; }
      runBusy($('#settings-save'), 'Saving…', function () {
        return api('PUT', '/api/settings', payload).then(function (data) {
          setDirty(false);
          var restart = !!(data && data.restart_required);
          var running = !!(state.status && RUNNING[state.status.phase]);
          toast({
            tone: 'ok', title: 'Settings saved',
            text: restart && running ? 'Restart the server to apply them.' : (restart ? 'They apply the next time the server starts.' : 'They\'re in effect.'),
            actions: restart && running ? [{ label: 'Restart now', run: function () { serverAction('restart'); } }] : null,
          });
          return api('GET', '/api/settings').then(function (fresh) {
            state.settings = fresh || {};
            if (restart) state.settings.restart_required = true;
            renderSettings();
          });
        }, function (err) { toastError('Couldn\'t save settings', err); });
      });
    });
    $('#settings-discard').addEventListener('click', function () {
      setDirty(false);
      sessionStore.set(SESSION_KEYS.settingsDraft, null);
      renderSettings();
      toast({ tone: 'info', title: 'Changes discarded' });
    });
    $('#restart-now').addEventListener('click', function () { serverAction('restart'); });
  }

  /* ================================================================ help */

  function renderHelp() {
    var s = state.status;
    var setup = s && s.setup;
    var version = setup ? (exactVersion(setup) || UNKNOWN_LATEST) : '';
    $$('[data-bind="version-plain"]').forEach(function (n) { setText(n, version || 'the version shown on the Overview'); });
    $$('#view-help [data-bind="address"]').forEach(function (n) { setText(n, s && s.join_address ? s.join_address : 'the join address from the Overview'); });
    var timeout = s && s.idle ? s.idle.timeout_seconds : null;
    $$('[data-bind="idle"]').forEach(function (n) {
      setText(n, timeout > 0 ? formatMinutes(Math.round(timeout / 60)) : 'a while');
    });
    $('#help-sleep-off').hidden = timeout !== 0;
  }

  /* ================================================================ boot */

  function boot() {
    initDialog();
    initShell();
    initLogs();
    initSetupForm();
    initWorlds();
    initPlayers();
    initSettings();
    $('#firstrun-dismiss').addEventListener('click', function () {
      store.set(KEYS.firstRun, '1');
      $('#firstrun').hidden = true;
      toast({ tone: 'info', title: 'Getting started hidden', text: 'The same tips are always in Help.' });
    });
    if (store.get(KEYS.firstRun) !== '1') $('#firstrun').hidden = false;
    var returnTo = sessionStore.get(SESSION_KEYS.returnTo);
    sessionStore.set(SESSION_KEYS.returnTo, null);
    if (returnTo && /^#\/[a-z]+(\/[A-Za-z0-9_-]+)?$/.test(returnTo) && !window.location.hash && window.history.replaceState) {
      window.history.replaceState(null, '', window.location.pathname + window.location.search + returnTo);
    }
    renderMiniLog();
    navigate(true);
    pollStatus();
    document.addEventListener('visibilitychange', onVisibility);
    timers.tick = window.setInterval(tickHero, 1000);
    timers.slow = window.setInterval(refreshVisibleData, 20000);
  }
})();
