/**
 * Приём ответов анкеты из мини-приложения MAX и запись в Google Таблицу.
 *
 * Свойства скрипта (Настройки проекта → Свойства скрипта):
 *   BOT_TOKEN    — токен бота MAX (обязательно, нужен для проверки подписи initData)
 *   SHEET_NAME   — имя листа (необязательно, по умолчанию «Ответы»)
 *   ALLOW_DEBUG  — "true", чтобы принимать ответы без подписи (только для теста в браузере!)
 */

var FIXED_HEADERS = ['Дата и время', 'ID пользователя MAX', 'Имя', 'Фамилия', 'Username'];
var MAX_AGE_SECONDS = 24 * 60 * 60; // насколько «свежей» должна быть подпись initData

function doPost(e) {
  try {
    var req = JSON.parse(e.postData.contents);
    var auth = authenticate_(req.initData);
    if (!auth.ok) return json_({ ok: false, error: 'auth', message: auth.message });

    if (req.action === 'check') {
      return json_({ ok: true, done: auth.user.id ? hasAnswered_(auth.user.id) : false });
    }
    if (req.action === 'submit') {
      return json_(saveAnswers_(auth.user, req.answers || [], !!req.allowResubmit));
    }
    return json_({ ok: false, error: 'unknown_action' });
  } catch (err) {
    console.error(err);
    return json_({ ok: false, error: 'server', message: String(err) });
  }
}

function doGet() {
  return json_({ ok: true, message: 'Сервис анкеты работает' });
}

// ---------------- запись в таблицу ----------------

function saveAnswers_(user, answers, allowResubmit) {
  var lock = LockService.getScriptLock();
  lock.waitLock(20000);
  try {
    if (!allowResubmit && user.id && hasAnswered_(user.id)) return { ok: false, error: 'already' };

    var sheet = getSheet_();
    var headers = ensureHeaders_(sheet, answers.map(function (a) { return a.title; }));

    var row = new Array(headers.length).fill('');
    row[0] = new Date();
    row[1] = user.id ? String(user.id) : '';
    row[2] = user.first_name || '';
    row[3] = user.last_name || '';
    row[4] = user.username ? '@' + user.username : '';
    answers.forEach(function (a) {
      var col = headers.indexOf(String(a.title));
      if (col !== -1) row[col] = safeCell_(a.value);
    });

    sheet.appendRow(row);
    return { ok: true };
  } finally {
    lock.releaseLock();
  }
}

function getSheet_() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var name = PropertiesService.getScriptProperties().getProperty('SHEET_NAME') || 'Ответы';
  return ss.getSheetByName(name) || ss.insertSheet(name);
}

/** Создаёт шапку и добавляет колонки для новых вопросов. Возвращает массив заголовков. */
function ensureHeaders_(sheet, questionTitles) {
  var lastCol = sheet.getLastColumn();
  var headers = lastCol ? sheet.getRange(1, 1, 1, lastCol).getValues()[0].map(String) : [];
  if (!headers.length) headers = FIXED_HEADERS.slice();

  questionTitles.forEach(function (t) {
    if (headers.indexOf(String(t)) === -1) headers.push(String(t));
  });

  if (headers.length !== lastCol) {
    sheet.getRange(1, 1, 1, headers.length).setValues([headers]).setFontWeight('bold');
    sheet.setFrozenRows(1);
  }
  return headers;
}

function hasAnswered_(userId) {
  var sheet = getSheet_();
  var last = sheet.getLastRow();
  if (last < 2) return false;
  var found = sheet.getRange(2, 2, last - 1, 1)
    .createTextFinder(String(userId)).matchEntireCell(true).findNext();
  return !!found;
}

/** Защита от формул в ячейках (=, +, -, @ в начале строки). */
function safeCell_(v) {
  var s = v == null ? '' : String(v);
  return /^[=+\-@]/.test(s) ? "'" + s : s;
}

// ---------------- проверка подписи MAX ----------------
// Алгоритм: https://dev.max.ru/docs/webapps/validation
//   secret = HMAC_SHA256(key="WebAppData", msg=BOT_TOKEN)
//   hash   = hex(HMAC_SHA256(key=secret, msg="k1=v1\nk2=v2…" — пары без hash, сортировка по ключу))

function authenticate_(initData) {
  var props = PropertiesService.getScriptProperties();

  if (!initData) {
    if (props.getProperty('ALLOW_DEBUG') === 'true') return { ok: true, user: { first_name: 'DEBUG' } };
    return { ok: false, message: 'Откройте анкету из MAX' };
  }

  var token = props.getProperty('BOT_TOKEN');
  if (!token) return { ok: false, message: 'На сервере не задан BOT_TOKEN' };

  var params = {}, hashes = [];
  String(initData).split('&').forEach(function (pair) {
    if (!pair) return;
    var i = pair.indexOf('=');
    var k = decode_(i === -1 ? pair : pair.slice(0, i));
    var v = i === -1 ? '' : decode_(pair.slice(i + 1));
    if (k === 'hash') hashes.push(v); else params[k] = v;
  });
  if (hashes.length !== 1) return { ok: false, message: 'Некорректная подпись' };

  var checkString = Object.keys(params).sort().map(function (k) { return k + '=' + params[k]; }).join('\n');
  var secret = Utilities.computeHmacSha256Signature(
    Utilities.newBlob(token).getBytes(), Utilities.newBlob('WebAppData').getBytes());
  var sign = Utilities.computeHmacSha256Signature(Utilities.newBlob(checkString).getBytes(), secret);

  if (toHex_(sign) !== String(hashes[0]).toLowerCase()) return { ok: false, message: 'Подпись не совпала' };

  var authDate = Number(params.auth_date || 0);
  // auth_date может прийти в секундах или миллисекундах
  if (authDate > 1e12) authDate = Math.floor(authDate / 1000);
  if (authDate && Date.now() / 1000 - authDate > MAX_AGE_SECONDS) {
    return { ok: false, message: 'Сессия устарела, откройте анкету заново' };
  }

  var user = {};
  try { user = JSON.parse(params.user || '{}'); } catch (e) {}
  return { ok: true, user: user };
}

function decode_(s) {
  return decodeURIComponent(String(s).replace(/\+/g, ' '));
}

function toHex_(bytes) {
  return bytes.map(function (b) { return ('0' + (b & 0xff).toString(16)).slice(-2); }).join('');
}

function json_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(ContentService.MimeType.JSON);
}

// ---------------- для ручной проверки из редактора ----------------
function testWrite() {
  console.log(saveAnswers_({ id: 0, first_name: 'Тест' },
    [{ title: 'Тестовый вопрос', value: 'Тестовый ответ' }], true));
}
