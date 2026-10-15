/**
 * Анкета MAX: бот запрашивает контакт, мини-приложение присылает ответы,
 * всё записывается в Google Таблицу.
 *
 * Свойства скрипта (Настройки проекта → Свойства скрипта):
 *   BOT_TOKEN        — токен бота MAX (обязательно)
 *   SHEET_NAME       — имя листа с ответами (по умолчанию «Ответы»)
 *   REQUIRE_CONTACT  — "false", чтобы анкету можно было заполнить без номера телефона
 *   ALLOW_DEBUG      — "true", чтобы принимать ответы без подписи (только для теста в браузере!)
 *
 * Запуск бота (см. README) — один из двух режимов:
 *   setupWebhook — MAX сам присылает события в doPost, бот отвечает сразу;
 *   setupBot     — запасной режим: бот забирает события раз в минуту (pollUpdates по триггеру).
 */

var FIXED_HEADERS = ['Дата и время', 'ID пользователя MAX', 'Имя', 'Фамилия', 'Username', 'Телефон'];
var CONTACT_HEADERS = ['Дата и время', 'ID пользователя MAX', 'Имя', 'Телефон', 'Подтверждён MAX'];
var CONTACTS_SHEET = 'Контакты';
var MAX_AGE_SECONDS = 24 * 60 * 60; // насколько «свежей» должна быть подпись initData

// platform-api2.max.ru подписан сертификатом Минцифры, которому серверы Google не доверяют,
// поэтому используем основной адрес API.
var MAX_API = 'https://platform-api.max.ru';

function doPost(e) {
  try {
    var req = JSON.parse(e.postData.contents);
    if (req.update_type) return handleWebhook_(e, req);

    var auth = authenticate_(req.initData);
    if (!auth.ok) return json_({ ok: false, error: 'auth', message: auth.message });

    if (req.action === 'check') {
      var id = auth.user.id;
      return json_({
        ok: true,
        done: id ? hasAnswered_(id) : false,
        needContact: id ? contactRequired_() && !getContact_(id) : false
      });
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

// ---------------- запись ответов ----------------

function saveAnswers_(user, answers, allowResubmit) {
  var lock = LockService.getScriptLock();
  lock.waitLock(20000);
  try {
    if (!allowResubmit && user.id && hasAnswered_(user.id)) return { ok: false, error: 'already' };

    var contact = user.id ? getContact_(user.id) : null;
    if (user.id && !contact && contactRequired_()) return { ok: false, error: 'need_contact' };

    var sheet = getSheet_();
    var headers = ensureHeaders_(sheet, FIXED_HEADERS.concat(answers.map(function (a) { return a.title; })));

    var row = new Array(headers.length).fill('');
    function put(title, value) {
      var col = headers.indexOf(String(title));
      if (col !== -1) row[col] = value;
    }
    put('Дата и время', new Date());
    put('ID пользователя MAX', user.id ? String(user.id) : '');
    put('Имя', user.first_name || '');
    put('Фамилия', user.last_name || '');
    put('Username', user.username ? '@' + user.username : '');
    put('Телефон', contact ? contact.phone : '');
    answers.forEach(function (a) { put(a.title, safeCell_(a.value)); });

    sheet.appendRow(row);
    return { ok: true };
  } finally {
    lock.releaseLock();
  }
}

function getSheet_() {
  var name = PropertiesService.getScriptProperties().getProperty('SHEET_NAME') || 'Ответы';
  return getOrCreateSheet_(name);
}

function getOrCreateSheet_(name) {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  return ss.getSheetByName(name) || ss.insertSheet(name);
}

/** Создаёт шапку и добавляет недостающие колонки в конец. Возвращает массив заголовков. */
function ensureHeaders_(sheet, titles) {
  var lastCol = sheet.getLastColumn();
  var headers = lastCol ? sheet.getRange(1, 1, 1, lastCol).getValues()[0].map(String) : [];

  titles.forEach(function (t) {
    if (headers.indexOf(String(t)) === -1) headers.push(String(t));
  });

  if (headers.length !== lastCol) {
    sheet.getRange(1, 1, 1, headers.length).setValues([headers]).setFontWeight('bold');
    sheet.setFrozenRows(1);
  }
  return headers;
}

/** Номер строки (≥2), где во 2-й колонке стоит userId, или 0. */
function findUserRow_(sheet, userId) {
  var last = sheet.getLastRow();
  if (last < 2) return 0;
  var found = sheet.getRange(2, 2, last - 1, 1)
    .createTextFinder(String(userId)).matchEntireCell(true).findNext();
  return found ? found.getRow() : 0;
}

function hasAnswered_(userId) {
  return findUserRow_(getSheet_(), userId) > 0;
}

/** Защита от формул в ячейках (=, +, -, @ в начале строки). */
function safeCell_(v) {
  var s = v == null ? '' : String(v);
  return /^[=+\-@]/.test(s) ? "'" + s : s;
}

// ---------------- контакты ----------------

function contactRequired_() {
  return PropertiesService.getScriptProperties().getProperty('REQUIRE_CONTACT') !== 'false';
}

function getContact_(userId) {
  var sheet = getOrCreateSheet_(CONTACTS_SHEET);
  var row = findUserRow_(sheet, userId);
  if (!row) return null;
  var v = sheet.getRange(row, 1, 1, CONTACT_HEADERS.length).getValues()[0];
  return v[3] ? { phone: String(v[3]), verified: v[4] === 'да' } : null;
}

function saveContact_(user, phone, verified) {
  var sheet = getOrCreateSheet_(CONTACTS_SHEET);
  ensureHeaders_(sheet, CONTACT_HEADERS);
  var name = [user.first_name, user.last_name].filter(Boolean).join(' ') || user.name || '';
  var values = [[new Date(), String(user.user_id), safeCell_(name), "'" + phone, verified ? 'да' : 'нет']];
  var row = findUserRow_(sheet, user.user_id);
  if (row) sheet.getRange(row, 1, 1, values[0].length).setValues(values);
  else sheet.appendRow(values[0]);
}

// ---------------- бот: события из MAX ----------------

var UPDATE_TYPES = ['bot_started', 'message_created'];

/**
 * Режим webhook: MAX сам присылает события в doPost этого веб-приложения.
 * Запускать из редактора ПОСЛЕ развертывания новой версии.
 */
function setupWebhook() {
  var props = PropertiesService.getScriptProperties();
  loadBotInfo_();

  var url = props.getProperty('WEBAPP_URL') || ScriptApp.getService().getUrl();
  if (!url || !/\/exec$/.test(url)) {
    throw new Error('Не удалось определить адрес веб-приложения (' + url + '). ' +
      'Добавьте свойство скрипта WEBAPP_URL = https://script.google.com/macros/s/…/exec');
  }

  // Apps Script не видит заголовки запроса, поэтому секрет передаём параметром в адресе
  var secret = Utilities.getUuid().replace(/-/g, '');
  props.setProperty('WEBHOOK_SECRET', secret);
  props.setProperty('WEBHOOK_DUPLICATES', '0');

  stopBot();
  deleteWebhooks();
  var res = maxApi_('post', '/subscriptions', null,
    { url: url + '?secret=' + secret, update_types: UPDATE_TYPES, secret: secret });
  console.log('Ответ MAX: ' + JSON.stringify(res));
  console.log('Готово: MAX присылает события на ' + url + '. Проверить состояние — функция webhookStatus.');
}

/**
 * Режим опроса (запасной): бот забирает события раз в минуту.
 * Webhook при этом отключается — MAX не отдаёт события опросом, пока он активен.
 */
function setupBot() {
  loadBotInfo_();
  deleteWebhooks();
  stopBot();
  ScriptApp.newTrigger('pollUpdates').timeBased().everyMinutes(1).create();
  pollUpdates();
  console.log('Готово: бот проверяет новые сообщения раз в минуту.');
}

/** Показывает в журнале активные подписки и сколько раз MAX присылал одно событие повторно. */
function webhookStatus() {
  var subs = maxApi_('get', '/subscriptions').subscriptions || [];
  console.log(subs.length
    ? 'Подписки: ' + subs.map(function (s) { return s.url.replace(/secret=\w+/, 'secret=…'); }).join(', ')
    : 'Подписок нет — webhook отключён (MAX мог отключить его сам после ошибок доставки).');
  console.log('Повторных доставок (MAX не принял ответ с первого раза): ' +
    (PropertiesService.getScriptProperties().getProperty('WEBHOOK_DUPLICATES') || 0));
  var polling = ScriptApp.getProjectTriggers().some(function (t) { return t.getHandlerFunction() === 'pollUpdates'; });
  console.log('Режим опроса раз в минуту: ' + (polling ? 'включён' : 'выключен'));
}

/** Отключить опрос событий (удалить триггер pollUpdates). */
function stopBot() {
  ScriptApp.getProjectTriggers().forEach(function (t) {
    if (t.getHandlerFunction() === 'pollUpdates') ScriptApp.deleteTrigger(t);
  });
}

/** Удалить все webhook-подписки бота. */
function deleteWebhooks() {
  (maxApi_('get', '/subscriptions').subscriptions || []).forEach(function (s) {
    maxApi_('delete', '/subscriptions', { url: s.url });
    console.log('Удалена подписка: ' + s.url.replace(/secret=\w+/, 'secret=…'));
  });
}

function loadBotInfo_() {
  var me = maxApi_('get', '/me');
  var props = PropertiesService.getScriptProperties();
  props.setProperty('BOT_USERNAME', me.username || '');
  props.deleteProperty('APP_BUTTON');
  console.log('Бот: ' + (me.name || me.first_name) + ' (@' + me.username + ')');
}

/** Событие от MAX по webhook. Повторные доставки того же события пропускаются. */
function handleWebhook_(e, u) {
  var props = PropertiesService.getScriptProperties();
  var secret = props.getProperty('WEBHOOK_SECRET');
  if (!secret || (e.parameter || {}).secret !== secret) return json_({ ok: false });

  var lock = LockService.getScriptLock();
  lock.waitLock(20000);
  try {
    var cache = CacheService.getScriptCache();
    var key = updateKey_(u);
    if (cache.get(key)) {
      props.setProperty('WEBHOOK_DUPLICATES', String(Number(props.getProperty('WEBHOOK_DUPLICATES') || 0) + 1));
      return json_({ ok: true, duplicate: true });
    }
    cache.put(key, '1', 21600); // 6 часов
  } finally {
    lock.releaseLock();
  }

  try { handleUpdate_(u); } catch (err) { console.error('Ошибка обработки события', err, JSON.stringify(u)); }
  return json_({ ok: true });
}

function updateKey_(u) {
  var m = u.message || {};
  var raw = [u.update_type, u.timestamp, (m.body && m.body.mid) || '', (u.user && u.user.user_id) || ''].join('|');
  return 'upd_' + toHex_(Utilities.computeDigest(Utilities.DigestAlgorithm.MD5, raw));
}

/** Забирает новые события из MAX (вызывается триггером раз в минуту). */
function pollUpdates() {
  var lock = LockService.getScriptLock();
  if (!lock.tryLock(5000)) return;
  try {
    var props = PropertiesService.getScriptProperties();
    var query = { limit: 100, timeout: 0, types: UPDATE_TYPES.join(',') };
    var marker = props.getProperty('MAX_MARKER');
    if (marker) query.marker = marker;

    var res = maxApi_('get', '/updates', query);
    (res.updates || []).forEach(function (u) {
      try { handleUpdate_(u); } catch (err) { console.error('Ошибка обработки события', err, JSON.stringify(u)); }
    });
    if (res.marker != null) props.setProperty('MAX_MARKER', String(res.marker));
  } finally {
    lock.releaseLock();
  }
}

function handleUpdate_(u) {
  if (u.update_type === 'bot_started') {
    var user = u.user || {};
    if (getContact_(user.user_id)) sendAppInvite_(user.user_id, 'Здравствуйте! Номер у нас уже есть — можно заполнять анкету.');
    else askContact_(user.user_id, 'Здравствуйте! Чтобы пройти анкету, поделитесь, пожалуйста, номером телефона — нажмите кнопку ниже.');
    return;
  }

  if (u.update_type === 'message_created') {
    var m = u.message || {};
    var sender = m.sender;
    if (!sender || sender.is_bot) return;
    if (m.recipient && m.recipient.chat_type && m.recipient.chat_type !== 'dialog') return;

    var contact = ((m.body && m.body.attachments) || []).filter(function (a) { return a.type === 'contact'; })[0];
    if (contact) return processContact_(sender, contact.payload || {});

    if (getContact_(sender.user_id)) sendAppInvite_(sender.user_id, 'Анкета открывается кнопкой ниже.');
    else askContact_(sender.user_id, 'Чтобы пройти анкету, сначала поделитесь номером телефона — кнопка ниже.');
  }
}

function processContact_(sender, payload) {
  var info = payload.max_info || {};
  if (info.user_id && String(info.user_id) !== String(sender.user_id)) {
    return askContact_(sender.user_id, 'Нужен ваш собственный номер. Нажмите, пожалуйста, кнопку «Поделиться контактом».');
  }
  var phone = phoneFromVcf_(payload.vcf_info);
  if (!phone) return askContact_(sender.user_id, 'Не удалось прочитать номер. Нажмите, пожалуйста, кнопку ещё раз.');

  saveContact_(sender, phone, verifyContact_(payload));
  sendAppInvite_(sender.user_id, 'Спасибо, номер сохранён! Теперь заполните анкету.');
}

function phoneFromVcf_(vcf) {
  var text = String(vcf || '').replace(/\\r\\n|\\n/g, '\n');
  var m = text.match(/^TEL[^:\n]*:(.+)$/im);
  if (!m) return '';
  var digits = m[1].replace(/[^\d+]/g, '');
  return digits && digits.charAt(0) !== '+' ? '+' + digits : digits;
}

/**
 * Проверка, что номер привязан к аккаунту MAX (а не введён вручную):
 * hash = HMAC-SHA256(key = токен бота, msg = vcf_info). Документация не уточняет,
 * как именно передаются переводы строк, поэтому пробуем несколько вариантов.
 */
function verifyContact_(payload) {
  if (!payload.hash || !payload.vcf_info) return false;
  var token = PropertiesService.getScriptProperties().getProperty('BOT_TOKEN');
  var raw = String(payload.vcf_info);
  var variants = [raw, raw.replace(/\\r\\n/g, '\r\n'), raw.replace(/\r\n/g, '\n'), raw.replace(/\\r\\n/g, '\n')];
  var expected = String(payload.hash).toLowerCase();
  return variants.some(function (v) {
    var sign = Utilities.computeHmacSha256Signature(Utilities.newBlob(v).getBytes(), Utilities.newBlob(token).getBytes());
    return toHex_(sign) === expected || Utilities.base64Encode(sign).toLowerCase() === expected;
  });
}

function askContact_(userId, text) {
  sendMessage_(userId, text, [[{ type: 'request_contact', text: '📱 Поделиться контактом' }]]);
}

/** Сообщение с кнопкой открытия анкеты. Если кнопка не поддерживается — просто текст. */
function sendAppInvite_(userId, text) {
  var props = PropertiesService.getScriptProperties();
  var username = props.getProperty('BOT_USERNAME');
  if (username && props.getProperty('APP_BUTTON') !== 'fail') {
    try {
      sendMessage_(userId, text, [[{ type: 'open_app', text: '📝 Заполнить анкету', web_app: username }]]);
      return;
    } catch (err) {
      console.warn('Кнопка open_app не принята, дальше шлём без неё', err);
      props.setProperty('APP_BUTTON', 'fail');
    }
  }
  sendMessage_(userId, text + '\n\nНажмите кнопку «Открыть» внизу чата.');
}

function sendMessage_(userId, text, buttons) {
  var body = { text: text };
  if (buttons) body.attachments = [{ type: 'inline_keyboard', payload: { buttons: buttons } }];
  return maxApi_('post', '/messages', { user_id: userId }, body);
}

function maxApi_(method, path, query, body) {
  var token = PropertiesService.getScriptProperties().getProperty('BOT_TOKEN');
  if (!token) throw new Error('Не задано свойство BOT_TOKEN');

  var qs = Object.keys(query || {}).map(function (k) {
    return encodeURIComponent(k) + '=' + encodeURIComponent(query[k]);
  }).join('&');
  var options = { method: method, headers: { Authorization: token }, muteHttpExceptions: true };
  if (body) { options.contentType = 'application/json'; options.payload = JSON.stringify(body); }

  var resp = UrlFetchApp.fetch(MAX_API + path + (qs ? '?' + qs : ''), options);
  var code = resp.getResponseCode();
  var text = resp.getContentText();
  if (code >= 300) throw new Error('MAX API ' + method.toUpperCase() + ' ' + path + ' → ' + code + ': ' + text);
  return text ? JSON.parse(text) : {};
}

// ---------------- проверка подписи мини-приложения ----------------
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
