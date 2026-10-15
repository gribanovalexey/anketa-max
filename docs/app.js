(function () {
  'use strict';

  var S = window.SURVEY;
  var WA = window.WebApp || null;            // MAX Bridge
  var initData = (WA && WA.initData) || '';  // подписанная строка для проверки на сервере
  var user = (WA && WA.initDataUnsafe && WA.initDataUnsafe.user) || {};

  var app = document.getElementById('app');
  var answers = {};   // id -> значение (строка | массив | число)
  var others = {};    // id -> текст «Другое»
  var current = 0;

  // ---------- утилиты ----------
  function el(tag, attrs, children) {
    var n = document.createElement(tag);
    for (var k in attrs || {}) {
      if (k === 'class') n.className = attrs[k];
      else if (k === 'text') n.textContent = attrs[k];
      else if (k.slice(0, 2) === 'on') n.addEventListener(k.slice(2), attrs[k]);
      else n.setAttribute(k, attrs[k]);
    }
    (children || []).forEach(function (c) { if (c) n.appendChild(c); });
    return n;
  }
  function render(screen) { app.innerHTML = ''; app.appendChild(screen); }
  function haptic(kind) {
    try {
      if (!WA || !WA.HapticFeedback) return;
      if (kind === 'select') WA.HapticFeedback.selectionChanged();
      else WA.HapticFeedback.notificationOccurred(kind);
    } catch (e) { /* не поддерживается на десктопе */ }
  }
  function isRequired(q) { return q.required !== false; }

  function api(action, extra) {
    var body = Object.assign({ action: action, initData: initData }, extra || {});
    // text/plain — «простой» запрос без CORS preflight, Apps Script его принимает
    return fetch(S.endpoint, {
      method: 'POST',
      headers: { 'Content-Type': 'text/plain;charset=utf-8' },
      body: JSON.stringify(body)
    }).then(function (r) { return r.json(); });
  }

  // ---------- кнопка «Назад» в MAX ----------
  function onBack() { if (current > 0) { current--; showQuestion(); } }
  function setBack(visible) {
    if (!WA || !WA.BackButton) return;
    try {
      WA.BackButton.offClick(onBack);
      if (visible) { WA.BackButton.onClick(onBack); WA.BackButton.show(); }
      else WA.BackButton.hide();
    } catch (e) {}
  }

  // ---------- экраны ----------
  function showMessage(icon, title, text, withClose) {
    setBack(false);
    render(el('div', { class: 'screen center' }, [
      el('div', { class: 'big-icon', text: icon }),
      el('h2', { text: title }),
      el('p', { class: 'muted', text: text || '' }),
      withClose && WA && WA.close ? el('div', { class: 'footer', style: 'width:100%' }, [
        el('button', { class: 'btn', text: 'Закрыть', onclick: function () { WA.close(); } })
      ]) : null
    ]));
  }

  function showIntro() {
    setBack(false);
    var hello = user.first_name ? 'Здравствуйте, ' + user.first_name + '!' : 'Здравствуйте!';
    render(el('div', { class: 'screen' }, [
      el('div', { class: 'body' }, [
        el('h1', { text: S.title }),
        el('p', { class: 'muted', text: hello + ' ' + S.intro }),
        el('p', { class: 'muted', text: 'Вопросов: ' + S.questions.length })
      ]),
      el('div', { class: 'footer' }, [
        el('button', { class: 'btn', text: 'Начать', onclick: function () { current = 0; showQuestion(); } })
      ])
    ]));
  }

  function showQuestion() {
    var q = S.questions[current];
    var total = S.questions.length;
    var isLast = current === total - 1;
    var errorBox = el('div', { class: 'error' });
    var nextBtn = el('button', { class: 'btn', text: isLast ? 'Отправить' : 'Далее' });

    setBack(current > 0);

    var input = buildInput(q, function () { errorBox.textContent = ''; });

    nextBtn.onclick = function () {
      var err = validate(q);
      if (err) { errorBox.textContent = err; haptic('error'); return; }
      if (isLast) submit(); else { current++; showQuestion(); }
    };

    var footer = [nextBtn];
    if (current > 0) {
      footer.unshift(el('button', { class: 'btn secondary', text: '←', 'aria-label': 'Назад', onclick: onBack }));
    }

    render(el('div', { class: 'screen' }, [
      el('div', { class: 'progress' }, [el('div', { style: 'width:' + ((current + 1) / total * 100) + '%' })]),
      el('div', { class: 'step', text: 'Вопрос ' + (current + 1) + ' из ' + total }),
      el('div', { class: 'body' }, [
        el('h2', { text: q.title }),
        el('div', { class: 'hint', text: hintFor(q) }),
        input,
        errorBox
      ]),
      el('div', { class: 'footer' }, footer)
    ]));
  }

  function hintFor(q) {
    var parts = [];
    if (q.type === 'multi') parts.push('Можно выбрать несколько вариантов');
    if (!isRequired(q)) parts.push('Необязательный вопрос');
    if (q.hint) parts.push(q.hint);
    return parts.join(' · ');
  }

  // ---------- поля ввода ----------
  function buildInput(q, onChange) {
    switch (q.type) {
      case 'single':
      case 'multi': return buildChoice(q, onChange);
      case 'scale': return buildScale(q, onChange);
      case 'textarea': {
        var ta = el('textarea', { class: 'field', placeholder: q.placeholder || '' });
        ta.value = answers[q.id] || '';
        ta.oninput = function () { answers[q.id] = ta.value; onChange(); };
        return ta;
      }
      default: {
        var types = { phone: 'tel', email: 'email', number: 'number', date: 'date' };
        var inp = el('input', { class: 'field', type: types[q.type] || 'text', placeholder: q.placeholder || '' });
        if (q.type === 'phone') inp.setAttribute('inputmode', 'tel');
        inp.value = answers[q.id] || '';
        inp.oninput = function () { answers[q.id] = inp.value; onChange(); };
        return inp;
      }
    }
  }

  function buildChoice(q, onChange) {
    var multi = q.type === 'multi';
    var OTHER = '__other__';
    var opts = q.options.slice();
    if (q.other) opts.push(OTHER);
    if (multi && !Array.isArray(answers[q.id])) answers[q.id] = [];

    var wrap = el('div');
    var otherInput = el('input', { class: 'field other-input', placeholder: 'Ваш вариант' });
    otherInput.value = others[q.id] || '';
    otherInput.oninput = function () { others[q.id] = otherInput.value; onChange(); };

    function isSel(v) { return multi ? answers[q.id].indexOf(v) !== -1 : answers[q.id] === v; }

    function draw() {
      wrap.innerHTML = '';
      opts.forEach(function (v) {
        var label = v === OTHER ? 'Другое' : v;
        wrap.appendChild(el('button', {
          type: 'button',
          class: 'option' + (multi ? ' multi' : '') + (isSel(v) ? ' selected' : ''),
          onclick: function () {
            if (multi) {
              var a = answers[q.id], i = a.indexOf(v);
              if (i === -1) a.push(v); else a.splice(i, 1);
            } else {
              answers[q.id] = v;
            }
            haptic('select'); onChange(); draw();
            if (v === OTHER && isSel(v)) otherInput.focus();
          }
        }, [el('span', { class: 'mark' }), el('span', { text: label })]));
        if (v === OTHER && isSel(v)) wrap.appendChild(otherInput);
      });
    }
    draw();
    return wrap;
  }

  function buildScale(q, onChange) {
    var min = q.min != null ? q.min : 1, max = q.max != null ? q.max : 10;
    var grid = el('div', { class: 'scale' });
    function draw() {
      grid.innerHTML = '';
      for (var i = min; i <= max; i++) (function (n) {
        grid.appendChild(el('button', {
          type: 'button', text: String(n), class: answers[q.id] === n ? 'selected' : '',
          onclick: function () { answers[q.id] = n; haptic('select'); onChange(); draw(); }
        }));
      })(i);
    }
    draw();
    return el('div', {}, [grid, el('div', { class: 'scale-labels' }, [
      el('span', { text: q.minLabel || String(min) }), el('span', { text: q.maxLabel || String(max) })
    ])]);
  }

  // ---------- проверка и сбор ответов ----------
  function finalValue(q) {
    var v = answers[q.id];
    var other = (others[q.id] || '').trim();
    if (q.type === 'multi') {
      var arr = (v || []).map(function (x) { return x === '__other__' ? 'Другое: ' + other : x; });
      return arr.join('; ');
    }
    if (v === '__other__') return 'Другое: ' + other;
    return v == null ? '' : String(v).trim();
  }

  function validate(q) {
    var v = answers[q.id];
    var empty = v == null || v === '' || (Array.isArray(v) && !v.length) || (typeof v === 'string' && !v.trim());
    if (empty) return isRequired(q) ? 'Пожалуйста, ответьте на вопрос' : '';
    var picked = Array.isArray(v) ? v : [v];
    if (picked.indexOf('__other__') !== -1 && !(others[q.id] || '').trim()) return 'Укажите ваш вариант';
    var s = String(v).trim();
    if (q.type === 'email' && !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(s)) return 'Похоже, адрес почты указан с ошибкой';
    if (q.type === 'phone' && s.replace(/\D/g, '').length < 10) return 'Укажите номер полностью';
    return '';
  }

  function submit() {
    setBack(false);
    render(el('div', { class: 'screen center' }, [el('div', { class: 'spinner' }), el('p', { class: 'muted', text: 'Отправляем ответы…' })]));
    var payload = S.questions.map(function (q) { return { id: q.id, title: q.title, value: finalValue(q) }; });
    api('submit', { answers: payload, allowResubmit: !!S.allowResubmit })
      .then(function (res) {
        if (res.ok) { haptic('success'); showMessage('✅', S.thanks, '', true); }
        else if (res.error === 'already') showMessage('👌', S.alreadyDone, '', true);
        else showError(res.message || res.error);
      })
      .catch(function () { showError('Нет связи с сервером'); });
  }

  function showError(msg) {
    haptic('error');
    render(el('div', { class: 'screen center' }, [
      el('div', { class: 'big-icon', text: '⚠️' }),
      el('h2', { text: 'Не удалось отправить' }),
      el('p', { class: 'muted', text: msg || 'Попробуйте ещё раз' }),
      el('div', { class: 'footer', style: 'width:100%' }, [
        el('button', { class: 'btn', text: 'Повторить', onclick: submit })
      ])
    ]));
  }

  // ---------- старт ----------
  try { WA && WA.ready && WA.ready(); } catch (e) {}

  if (!initData && !/[?&]debug=1/.test(location.search)) {
    showMessage('📱', 'Откройте анкету в MAX', 'Анкета работает только внутри мессенджера MAX — через кнопку в чате с ботом.');
    return;
  }
  if (S.allowResubmit) { showIntro(); return; }

  api('check')
    .then(function (res) { if (res.ok && res.done) showMessage('👌', S.alreadyDone, '', true); else showIntro(); })
    .catch(showIntro);
})();
