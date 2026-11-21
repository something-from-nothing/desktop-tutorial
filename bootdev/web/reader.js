// Boot.dev Dashboard reader.
// Runs inside the boot.dev page (Windows: injected through the browser's debugging port,
// Android: injected into the WebView). It only watches: it never clicks Run or Submit.
// It passes what it sees to the app through window.__bdHost (Windows) or BootHost.push (Android).
(function () {
  if (window.__bdReader) return;
  window.__bdReader = 1;

  var UUID = /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/;

  function send(o) {
    try {
      o.at = Date.now();
      var s = JSON.stringify(o);
      if (window.BootHost && window.BootHost.push) window.BootHost.push(s);
      else if (typeof window.__bdHost === 'function') window.__bdHost(s);
      else window.postMessage({ __bdDash: 1, s: s }, location.origin); // Chrome extension relay
    } catch (e) {}
  }

  // Never pass boot.dev's own solution files on: the dashboard teaches, it doesn't copy answers.
  function stripSolutions(o, depth) {
    if (!o || typeof o !== 'object' || depth > 6) return o;
    if (Array.isArray(o)) { for (var i = 0; i < o.length; i++) stripSolutions(o[i], depth + 1); return o; }
    for (var k in o) {
      if (/solution/i.test(k)) delete o[k];
      else stripSolutions(o[k], depth + 1);
    }
    return o;
  }

  function onApi(url, method, status, text) {
    if (!url || url.indexOf('api.boot.dev') < 0) return;
    var path = (url.split('/v1/')[1] || '').split('?')[0];
    if (/^a\//.test(path)) return; // analytics pings
    var j = null;
    try { j = JSON.parse(text); } catch (e) {}
    var id = (path.match(UUID) || [])[0] || '';
    method = (method || 'GET').toUpperCase();
    if (method === 'GET' && /^static\/lessons\//.test(path) && j && j.lesson) {
      send({ type: 'lesson_data', id: j.lesson.uuid || id, data: stripSolutions(j, 0) });
    } else if (method === 'GET' && /^users\/lessons\//.test(path) && j) {
      send({ type: 'lesson_user', id: id, data: { hintViewedAt: j.hintViewedAt, solutionViewedAt: j.solutionViewedAt, successCount: j.successCount, armorUsedAt: j.armorUsedAt } });
    } else if (method !== 'GET') {
      send({ type: 'action', method: method, path: path.replace(UUID, '<id>'), id: id, status: status, body: stripBody(j, text) });
    }
  }

  function stripBody(j, text) {
    if (j) { stripSolutions(j, 0); try { return JSON.stringify(j).slice(0, 6000); } catch (e) {} }
    return String(text || '').slice(0, 6000);
  }

  // Watch boot.dev's own requests (fetch and XHR). Responses are read from a copy.
  var origFetch = window.fetch;
  if (origFetch) {
    window.fetch = function (input, init) {
      var p = origFetch.apply(this, arguments);
      try {
        var url = typeof input === 'string' ? input : (input && input.url) || String(input);
        var method = (init && init.method) || (input && input.method) || 'GET';
        if (url.indexOf('api.boot.dev') >= 0) {
          p.then(function (r) {
            try { r.clone().text().then(function (t) { onApi(url, method, r.status, t); }, function () {}); } catch (e) {}
          }, function () {});
        }
      } catch (e) {}
      return p;
    };
  }
  var origOpen = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function (method, url) {
    try {
      var x = this;
      if (String(url).indexOf('api.boot.dev') >= 0) {
        x.addEventListener('load', function () {
          var t = '';
          try { t = x.responseType === '' || x.responseType === 'text' ? x.responseText : JSON.stringify(x.response); } catch (e) {}
          onApi(String(url), method, x.status, t);
        });
      }
    } catch (e) {}
    return origOpen.apply(this, arguments);
  };

  // Page snapshot: level, XP bar, streak, chapter progress, current lesson and code.
  function leafMatching(re, root) {
    var w = document.createTreeWalker(root || document.body, NodeFilter.SHOW_TEXT);
    var n;
    while ((n = w.nextNode())) {
      var t = n.nodeValue.trim();
      if (t && re.test(t)) return n.parentElement;
    }
    return null;
  }

  function chapters() {
    var out = {}, list = [];
    var els = document.querySelectorAll('a, button, li, div');
    for (var i = 0; i < els.length && list.length < 60; i++) {
      var e = els[i];
      if (e.childElementCount > 8) continue;
      var t = (e.innerText || '').replace(/Reset (Chapter|Course) Progress/g, '').trim();
      if (t.length > 90) continue;
      var m = t.match(/^(\d+)\.\s*([^\n]+?)\s*(?:\n\s*)*(\d+)\s*\/\s*(\d+)\s*$/);
      if (!m || out[m[1]]) continue;
      out[m[1]] = 1;
      list.push({ n: +m[1], title: m[2].trim(), done: +m[3], total: +m[4] });
    }
    if (!list.length) return null;
    var course = '';
    var ch = leafMatching(/^Chapters$/);
    // The course name is the nearest heading above the word "Chapters".
    for (var el = ch, k = 0; el && k < 5 && !course; k++, el = el.parentElement) {
      for (var sib = el.previousElementSibling; sib && !course; sib = sib.previousElementSibling) {
        var first = (sib.innerText || '').replace(/Reset (Chapter|Course) Progress/g, '').trim().split('\n')[0].trim();
        if (first && first !== 'Chapters' && first.length < 80) course = first;
      }
    }
    list.sort(function (a, b) { return a.n - b.n; });
    return { course: course, list: list };
  }

  function editorCode() {
    var ed = document.querySelector('.cm-content');
    if (!ed) return '';
    var lines = ed.querySelectorAll('.cm-line');
    var a = [];
    for (var i = 0; i < lines.length; i++) a.push(lines[i].innerText.replace(/​/g, ''));
    return a.join('\n').slice(0, 20000);
  }

  function snapshot() {
    var o = { type: 'page', url: location.href, path: location.pathname, title: document.title, visible: document.visibilityState !== 'hidden' };
    var lv = leafMatching(/^Level \d+$/);
    if (lv) o.level = +lv.textContent.replace(/\D+/g, '');
    var bar = document.querySelector('header [style*="width"], nav [style*="width"]');
    if (bar) {
      var w = (bar.getAttribute('style') || '').match(/width:\s*([\d.]+)%/);
      if (w) o.xpPct = +w[1];
    }
    var st = leafMatching(/^Current streak$/);
    if (st && st.parentElement) {
      var sm = (st.parentElement.innerText || '').match(/Current streak\s*(\d+)/);
      if (sm) o.streak = +sm[1];
    }
    var lm = location.pathname.match(/^\/lessons\/([0-9a-f-]{36})/);
    if (lm) {
      o.lessonId = lm[1]; o.code = editorCode();
      var out = document.querySelector('.lesson-code-output');
      if (out) o.output = (out.innerText || '').slice(0, 4000);
    }
    var c = chapters();
    if (c) o.chapters = c;
    o.login = !!document.querySelector('input[type=password]') || /\/(login|signin|sign-in)/.test(location.pathname)
      || (o.level == null && !!loginLink());
    return o;
  }

  var last = '', lastSent = 0, fetched = {};
  function tick() {
    try {
      if (!document.body) return;
      var o = snapshot();
      var key = JSON.stringify(o);
      if (key !== last || Date.now() - lastSent > 30000) {
        last = key; lastSent = Date.now();
        send(o);
      }
      // Lesson data loaded before the reader started? Ask the public lesson endpoint once.
      if (o.lessonId && !fetched[o.lessonId]) {
        fetched[o.lessonId] = 1;
        setTimeout(function () {
          fetch('https://api.boot.dev/v1/static/lessons/' + o.lessonId).catch(function () {});
        }, 1500);
      }
    } catch (e) {}
  }
  setInterval(tick, 1500);
  document.addEventListener('visibilitychange', tick);
  window.addEventListener('focus', tick);
  tick();

  // A "Log in" link or button, shown when signed out.
  function loginLink() {
    var els = document.querySelectorAll('a, button');
    for (var i = 0; i < els.length; i++) {
      var t = (els[i].innerText || '').trim();
      if (/^(log\s*in|sign\s*in|login)$/i.test(t)) return els[i];
    }
    return null;
  }

  // Called by the app to sign in with the saved account. Works for one step or two step forms.
  function setValue(el, v) {
    if (!el) return;
    var proto = Object.getPrototypeOf(el);
    var desc = Object.getOwnPropertyDescriptor(proto, 'value') || Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value');
    desc.set.call(el, v);
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
  }
  window.__bdFill = function (email, pass) {
    var pw = document.querySelector('input[type=password]');
    var em = document.querySelector('input[type=email], input[autocomplete=email], input[autocomplete=username], input[name*=email i]');
    if (!em) {
      var ins = document.querySelectorAll('input[type=text], input:not([type])');
      if (ins.length) em = ins[0];
    }
    if (!pw && !em) {
      var link = loginLink();
      if (link) { link.click(); return 'opened login'; }
      return 'nothing';
    }
    if (em && !em.value) setValue(em, email);
    if (pw) setValue(pw, pass);
    var scope = (pw && pw.form) || (em && em.form) || document;
    var btn = scope.querySelector('button[type=submit]');
    if (!btn) {
      var bs = scope.querySelectorAll('button');
      for (var i = 0; i < bs.length; i++) {
        if (/log\s*in|sign\s*in|continue|next/i.test(bs[i].innerText)) { btn = bs[i]; break; }
      }
    }
    setTimeout(function () { if (btn) btn.click(); }, 400);
    return pw ? 'filled' : 'email';
  };
})();
