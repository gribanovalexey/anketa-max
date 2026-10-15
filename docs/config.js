// ============================================================
//  НАСТРОЙКИ АНКЕТЫ — меняйте только этот файл
// ============================================================
window.SURVEY = {
  // URL веб-приложения Google Apps Script (см. README, шаг 2)
  endpoint: 'https://script.google.com/macros/s/ВСТАВЬТЕ_ID/exec',

  title: 'Анкета участника',
  intro: 'Ответьте, пожалуйста, на несколько вопросов. Это займёт 2–3 минуты.',
  thanks: 'Спасибо! Ваши ответы сохранены.',
  alreadyDone: 'Вы уже заполнили эту анкету. Спасибо!',

  // Разрешить заполнять анкету повторно одному пользователю
  allowResubmit: false,

  // Типы вопросов:
  //   single — один вариант        multi — несколько вариантов
  //   text   — строка              textarea — развёрнутый ответ
  //   scale  — шкала (min..max)    phone / email / number / date
  // other: true — добавляет вариант «Другое» со своим ответом (single/multi)
  // required: false — вопрос можно пропустить
  questions: [
    { id: 'name', type: 'text', title: 'Как вас зовут?', placeholder: 'Имя и фамилия' },
    { id: 'age', type: 'single', title: 'Ваш возраст',
      options: ['До 18', '18–24', '25–34', '35–44', '45–54', '55 и старше'] },
    { id: 'source', type: 'multi', title: 'Откуда вы о нас узнали?', other: true,
      options: ['Друзья и знакомые', 'Социальные сети', 'Поиск в интернете', 'Реклама'] },
    { id: 'rating', type: 'scale', title: 'Оцените качество нашей работы', min: 1, max: 10,
      minLabel: 'Плохо', maxLabel: 'Отлично' },
    { id: 'phone', type: 'phone', title: 'Телефон для связи', required: false },
    { id: 'comment', type: 'textarea', title: 'Что нам стоит улучшить?', required: false,
      placeholder: 'Ваш комментарий' }
  ]
};
