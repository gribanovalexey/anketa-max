"""
Перенос ответов анкеты для родителей (MAX → Google Таблица) в АРМ СОРУ.

Берёт:
  * CSV из Google Таблицы (лист «Ответы»: Файл → Скачать → CSV);
  * шаблон — выгрузку формы «Анкета родителей» из АРМ СОРУ (*.7z) с периодом сентября:
    в ней список класса с кодами учеников;
и собирает *.7z для загрузки в АРМ СОРУ («Стартовая диагностика 1 класс → Анкета родителей»).

Запуск:
  python soru_import.py --csv ответы.csv --template 3.7z --out загрузка.7z
  python soru_import.py ... --match "Петров Петя=std_00001234_000000"   # ручное сопоставление

Ничего не пишет в базу СОРУ — только создаёт файл для загрузки.
"""
import argparse
import csv
import difflib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from collections import OrderedDict

TAR = os.path.join(os.environ.get('SystemRoot', r'C:\Windows'), 'System32', 'tar.exe')
SD = 'ind_sd_asoru_8.'   # показатели стартовой анкеты
A = 'ind_asoru_8.'       # общие показатели анкеты родителей (тоже входят в стартовую форму)

# ---------------------------------------------------------------- справочники
# Состав формы «Стартовая диагностика 1 класс → Анкета родителей» (111 показателей, порядок как в форме)
# и коды ответов взяты из справочников armsoru.exe.
YES4 = {'да': 0, 'скорее да': 1, 'скорее нет': 2, 'нет': 3}
BOOL = {'да': 1, 'нет': 0}

SINGLE = {  # номер вопроса в нашей анкете → (показатель СОРУ, {вариант ответа: код})
    '1': (A + '20.', {'мать': 0, 'отец': 1, 'другие родственники': 2}),
    '2': (SD + '2.', {'по месту жительства': 0, 'в ту школу, которую вы специально выбрали': 1, 'в другую': 2}),
    '3': (SD + '3.', {'совсем не ходил': 0, 'менее 1 года': 1, '1-2 года': 2, '2-3 года': 3, '3 года и более': 4}),
    '4': (SD + '4.', {'до 6 лет': 0, '6-6,5 лет': 1, '6,5-7 лет': 2, '7-8 лет': 3, 'старше 8 лет': 4}),
    '6': (SD + '6.', {'на русском языке': 0, 'на другом языке': 1}),
    '7': (SD + '7.', BOOL),
    '9': (A + '1.', {'низкий': 0, 'средний': 1, 'высокий': 2}),
    '17': (SD + '17.', {'да': 0, 'стараемся, и часто удается': 1, 'стараемся, но далеко не всегда удается': 2,
                        'нет, мы не придаем этому большого значения': 3}),
    '19': (SD + '19.', {'очень хотел': 0, 'скорее хотел': 1, 'скорее не хотел': 2, 'очень не хотел': 3}),
    '20': (SD + '20.', {'наотрез отказывается идти в школу': 0, 'просит оставить дома': 1, 'капризничает, ворчит': 2,
                        'без особого желания, но спокойно': 3, 'с охотой': 4}),
    '21': (SD + '21.', {'конечно же, на отличные оценки': 0, 'главное - чтоб учился хорошо': 1,
                        'оценки - не главное, главное - знания': 2, 'оценки не главное, главное - чтоб был здоровым': 3}),
    '22': (SD + '22.', BOOL),
    '26': (SD + '27.', {'0-10 книг': 0, '11-25 книг': 1, '26-50 книг': 2, '51-100 книг': 3, 'более 100 книг': 4}),
    '27': (SD + '28.', {'очень плохое': 0, 'плохое': 1, 'среднее': 2, 'хорошее': 3, 'очень хорошее': 4}),
}

# Вопросы-таблицы: строки по порядку → показатели.
rows_ = lambda prefix, nums: [f'{prefix}{n}.' for n in nums]
MATRIX = {
    '5': (rows_(SD + '5.', range(1, 6)), YES4),
    '10': (rows_(SD + '10.', range(1, 16)), {'никогда или почти никогда': 0, 'иногда': 1, 'часто': 2}),
    '11': (rows_(SD + '11.', range(1, 7)), {'очень хорошо': 0, 'хорошо': 1, 'не очень хорошо': 2, 'плохо': 3}),
    '12': (rows_(SD + '12.', range(1, 9)), {'очень хорошо': 0, 'хорошо': 1, 'не очень хорошо': 2, 'не умел вообще': 3}),
    '13': (rows_(SD + '13.', range(1, 8)), BOOL),
    '16': (rows_(A + '14.', [1, 3, 4, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16]), BOOL),
    '25': (rows_(A + '16.', [1, 2]), BOOL),
    '28': (rows_(A + '21.', [1, 2]), {'до 25 лет': 0, '25-30 лет': 1, '31-35 лет': 2, '36-45 лет': 3,
                                      '46-60 лет': 4, 'старше 60 лет': 5}),
}
OTHER_16 = A + '14.17.'  # «16. Другие изменения в поведении» (текст) → «другое»: да/нет

# Вопросы с несколькими ответами: вариант → показатель (1 — отмечен, 0 — нет).
MULTI = {
    '8': {'нет, не проходил': SD + '8.1.', 'да, в подготовительной группе детского сада': SD + '8.2.',
          'да, на подготовительных занятиях в школе, в которой учится ребенок': SD + '8.3.',
          'да, на подготовительных занятиях в другой школе': SD + '8.4.', 'да, на занятиях с логопедом': SD + '8.5.',
          'да, на занятиях с репетитором': SD + '8.6.', 'да, мы готовили его сами': SD + '8.7.', 'другое': SD + '8.8.'},
    '14': {'мама': A + '4.1.', 'папа': A + '4.2.', 'бабушка': A + '4.3.', 'дедушка': A + '4.4.', 'няня': A + '4.5.',
           'другие родственники': A + '4.6.', 'ребенок остается на продленке': A + '4.7.'},
    '15': {'в усложненных программах обучения': A + '13.1.',
           'в недостаточном уровне готовности детей к школе': A + '13.2.',
           'в перегрузке детей в школе': A + '13.3.',
           'в перегрузке детей дополнительными занятиями (спорт, музыка и др.)': A + '13.4.',
           'в перегрузке детей работой по дому или хозяйству': SD + '15.5.',
           'в ухудшении здоровья детей': A + '13.5.',
           'в недостаточной поддержке семьей школьного обучения': A + '13.6.',
           'в трудностях в отношениях с учителями': A + '13.7.',
           'в трудностях в отношениях со сверстниками': A + '13.8.',
           'в позднем засыпании': SD + '15.10.',
           'в перегрузке детей домашними заданиями': A + '13.9.',
           'другое': A + '13.10.'},
    '18': {'мама': A + '18.1.', 'папа': A + '18.2.', 'другие родственники': A + '18.3.',
           'никто не будет помогать. в помощи не нуждается': A + '18.4.',
           'никто не будет помогать. должна помогать школа': A + '18.5.',
           'если будет надо, наймем репетитора': A + '18.6.', 'другое': A + '18.7.'},
}

NUMBERS = {'23. сколько всего человек живут у вас дома?': SD + '23.', '23. сколько из них детей?': SD + '24.',
           '24. сколько у вас комнат?': SD + '25.'}

# Порядок показателей в форме СОРУ — в этом порядке строки пишутся в файл
ORDER = ([A + '20.'] + [SD + x for x in ('2.', '3.', '4.')] + MATRIX['5'][0] + [SD + '6.', SD + '7.']
         + list(MULTI['8'].values()) + [A + '1.'] + MATRIX['10'][0] + MATRIX['11'][0] + MATRIX['12'][0]
         + MATRIX['13'][0] + list(MULTI['14'].values())
         + [A + '13.1.', A + '13.2.', A + '13.3.', A + '13.4.', SD + '15.5.', A + '13.5.', A + '13.6.',
            A + '13.7.', A + '13.8.', SD + '15.10.', A + '13.9.', A + '13.10.']
         + MATRIX['16'][0] + [OTHER_16, SD + '17.'] + list(MULTI['18'].values())
         + [SD + x for x in ('19.', '20.', '21.', '22.', '23.', '24.', '25.')] + MATRIX['25'][0]
         + [SD + '27.', SD + '28.'] + MATRIX['28'][0])

# ---------------------------------------------------------------- итоговая анкета (конец года)
# Форма «Итоговая диагностика 1 класс → Анкета родителей»: 84 показателя ind_asoru_8.*
F_TIME3 = {'нисколько': 0, 'менее 1 часа': 1, '1-2 часа': 2, 'от 2 до 3 часов': 3, 'более 3 часов': 4}
F_TIME5 = {'нисколько': 0, 'менее 1 часа': 1, '1-3 часа': 2, 'от 3 до 5 часов': 3, 'более 5 часов': 4}
F_NONE = {'ничего из перечисленного': None}  # вариант «ничего» — все пункты = 0

FINAL = dict(
    SINGLE={
        '1': (A + '1.', {'низкий': 0, 'средний': 1, 'высокий': 2}),
        '5': (A + '5.', {'конечно же, на отличные оценки': 0, 'главное - чтоб учился хорошо': 1,
                         'оценки - не главное, главное - знания': 2, 'оценки - не главное, главное - чтоб был здоровым': 3}),
        '6': (A + '6.', {'очень не хочет': 0, 'скорее не хочет': 1, 'скорее хочет': 2, 'очень хочет': 3}),
        '7': (A + '7.', {'наотрез отказывается идти в школу': 0, 'просит оставить дома': 1, 'капризничает, ворчит': 2,
                         'без особого желания, но спокойно': 3, 'с охотой': 4}),
        '8': (A + '8.', {'заниженная': 0, 'адекватная': 1, 'завышенная': 2}),
        '15': (A + '15.', BOOL),
        '17': (A + '17.', BOOL),
        '19': (A + '19.', {'нет': 0, 'да': 1, 'не обращались': 2}),
        '20': (A + '20.', {'мать': 0, 'отец': 1, 'другие родственники': 2}),
    },
    MATRIX={
        '2': (rows_(A + '2.', range(1, 6)), F_TIME3),
        '3': (rows_(A + '3.', range(1, 3)), F_TIME5),
        '10': (rows_(A + '10.', range(1, 7)), {'никогда или почти никогда': 0, 'иногда': 1, 'часто': 2}),
        '21': (rows_(A + '21.', [1, 2]), {'до 25 лет': 0, '25-30 лет': 1, '31-35 лет': 2, '36-45 лет': 3,
                                          '46-60 лет': 4, 'старше 60 лет': 5}),
    },
    MULTI={
        '4': {'мама': A + '4.1.', 'папа': A + '4.2.', 'бабушка': A + '4.3.', 'дедушка': A + '4.4.', 'няня': A + '4.5.',
              'другие родственники': A + '4.6.', 'ребенок остается на "продленке"': A + '4.7.'},
        '9': {'с увлечением': A + '9.1.', 'легко, без особых усилий': A + '9.2.',
              'требуются усилия, чтобы заставить ребенка учиться': A + '9.3.'},
        '11': {'общительный': A + '11.1.', 'часто меняет друзей': A + '11.2.',
               'не складываются дружеские отношения': A + '11.3.', 'ходит к друзьям домой': A + '11.4.',
               'друзья приходят в гости': A + '11.5.'},
        '12': dict({'трудно усидеть на месте': A + '12.1.', 'часто тревожится': A + '12.2.',
                    'плохо развита мелкая моторика': A + '12.3.', 'боится сделать ошибку': A + '12.4.',
                    '"задирают" другие дети': A + '12.5.', 'часто жалуется, "хнычет"': A + '12.6.',
                    'трудно переключить на другие занятия': A + '12.7.', 'огорчается до слез': A + '12.8.',
                    'проблемы с концентрацией внимания': A + '12.9.'}, **F_NONE),
        '13': {'в усложненных программах обучения': A + '13.1.',
               'в недостаточном уровне готовности ребенка к школе': A + '13.2.',
               'из-за перегрузки ребенка в школе': A + '13.3.',
               'из-за перегрузки ребенка дополнительным образованием': A + '13.4.',
               'в ухудшении здоровья ребенка': A + '13.5.',
               'в недостаточной помощи семьи при обучении ребенка': A + '13.6.',
               'из-за трудностей в отношениях ребенка с учителями': A + '13.7.',
               'из-за трудностей в отношениях ребенка со сверстниками': A + '13.8.',
               'из-за перегрузки ребенка домашней работой': A + '13.9.', 'другое': A + '13.10.'},
        '14': dict({'после школы выглядит утомленным, нуждается в дополнительном отдыхе': A + '14.1.',
                    'стало наблюдаться пониженное настроение': A + '14.2.',
                    'стал засыпать с трудом вечером': A + '14.3.', 'стал спать беспокойно': A + '14.4.',
                    'стал вскакивать во сне, разговаривать, жаловаться на страшные сны': A + '14.5.',
                    'стал просыпаться утром с трудом': A + '14.6.',
                    'стал просыпаться утром в плохом настроении': A + '14.7.',
                    'после уроков приходит и сразу ложится спать': A + '14.8.', 'изменился аппетит': A + '14.9.',
                    'после школы стал более возбужденным': A + '14.10.',
                    'вечером стал с трудом успокаиваться': A + '14.11.',
                    'стали заметны тики, навязчивые движения': A + '14.12.',
                    'стал тревожиться о школьных делах': A + '14.13.',
                    'боится опоздать в школу и что-либо не сделать': A + '14.14.',
                    'появились жалобы на здоровье': A + '14.15.', 'стал капризничать': A + '14.16.',
                    'другое': A + '14.17.'}, **F_NONE),
        '16': {'своя комната': A + '16.1.', 'часть комнаты, обустроенная для занятий': A + '16.2.', 'нет': None},
        '18': {'мама': A + '18.1.', 'папа': A + '18.2.', 'другие родственники': A + '18.3.',
               'никто не помогает. в помощи не нуждается': A + '18.4.',
               'никто не помогает. должна помогать школа': A + '18.5.',
               'если будет надо, наймем репетитора': A + '18.6.', 'другое': A + '18.7.'},
    },
    NUMBERS={},
    OTHER_16=None,
)
_vals = lambda q: [v for v in FINAL['MULTI'][q].values() if v]
FINAL['ORDER'] = ([A + '1.'] + FINAL['MATRIX']['2'][0] + FINAL['MATRIX']['3'][0] + _vals('4')
                  + [A + '5.', A + '6.', A + '7.', A + '8.'] + _vals('9') + FINAL['MATRIX']['10'][0] + _vals('11')
                  + _vals('12') + _vals('13') + _vals('14') + [A + '15.'] + _vals('16') + [A + '17.'] + _vals('18')
                  + [A + '19.', A + '20.'] + FINAL['MATRIX']['21'][0])

START = dict(SINGLE=SINGLE, MATRIX=MATRIX, MULTI=MULTI, NUMBERS=NUMBERS, OTHER_16=OTHER_16, ORDER=ORDER)
SURVEYS = {'start': START, 'final': FINAL}


def use_survey(name):
    """Переключает таблицы соответствия на нужную анкету (start — стартовая, final — итоговая)."""
    global SINGLE, MATRIX, MULTI, NUMBERS, OTHER_16, ORDER
    m = SURVEYS[name]
    SINGLE, MATRIX, MULTI, NUMBERS, OTHER_16, ORDER = (m[k] for k in ('SINGLE', 'MATRIX', 'MULTI', 'NUMBERS', 'OTHER_16', 'ORDER'))


def detect_survey(headers):
    return 'final' if any(norm(h).startswith('2. дорогу в школу') for h in headers) else 'start'


CHILD_COL = 'фамилия и имя учащегося'
DATE_COL = 'дата и время'


def norm(s):
    s = str(s or '').strip().lower().replace('ё', 'е')
    s = re.sub(r'[–—−]', '-', s)                          # тире → дефис
    s = re.sub(r'(?<=\d)\s*-\s*(?=\d)', '-', s)          # «1 – 2» → «1-2»
    s = s.replace('«', '"').replace('»', '"')
    return re.sub(r'\s+', ' ', s)


def code_of(mapping, answer):
    a = norm(answer)
    if a in mapping: return mapping[a]
    a2 = a.replace(' - ', ' ').replace('-', ' ')
    for k, v in mapping.items():
        if k.replace(' - ', ' ').replace('-', ' ') == a2: return v
    raise ValueError(f'неизвестный вариант ответа: «{answer}»')


# ---------------------------------------------------------------- шаблон из СОРУ
def read_7z(path):
    names = subprocess.run([TAR, '-tf', path], capture_output=True, text=True, check=True).stdout.split()
    out = {}
    for n in names:
        out[n] = subprocess.run([TAR, '-xOf', path, n], capture_output=True, check=True,
                                stdin=subprocess.DEVNULL).stdout
    return out


def load_template(path):
    """Все файлы выгрузки (по порядку) + имя и текст файла формы «Анкета родителей»."""
    entries = read_7z(path)
    for name, raw in entries.items():
        text = raw.decode('cp1251')
        if 'formid="soru_08"' in text[:400]:
            return entries, name, text
    sys.exit('В шаблоне нет формы «Анкета родителей» (formid="soru_08"). Выгрузите её из АРМ СОРУ.')


def students_of(text):
    root = ET.fromstring(text.encode('cp1251'))
    res = OrderedDict()
    for it in root.iter('item'):
        if it.get('class') != 'objects.EduStudent': continue
        attrs = {a.get('name'): a.get('value') for a in it.findall('attribute')}
        res[it.get('name')] = attrs.get('caption', '')
    return res, root.get('periodid')


# ---------------------------------------------------------------- сопоставление учеников
def name_key(s):
    return ' '.join(sorted(norm(s).replace('.', ' ').split()))


def match_students(rows, students, manual):
    by_key = {name_key(cap): code for code, cap in students.items()}
    tokens = {code: set(name_key(cap).split()) for code, cap in students.items()}
    result, problems = {}, []
    for r in rows:
        child = r.get(CHILD_COL, '')
        if norm(child) in manual:
            result[id(r)] = manual[norm(child)]; continue
        code = by_key.get(name_key(child))
        if not code:
            # «Фамилия Имя» из анкеты при «Фамилия Имя Отчество» в СОРУ — достаточно совпадения всех слов анкеты
            want = set(name_key(child).split())
            hits = [c for c, t in tokens.items() if len(want) >= 2 and want <= t]
            code = hits[0] if len(hits) == 1 else None
        if code:
            result[id(r)] = code; continue
        best = difflib.get_close_matches(name_key(child), list(by_key), n=1, cutoff=0.75)
        problems.append((child, by_key[best[0]] if best else None, students.get(by_key[best[0]]) if best else None))
    return result, problems


# ---------------------------------------------------------------- перевод ответов в коды
def convert(row, cols):
    """cols — {нормализованный заголовок: исходный заголовок}. Возвращает ({показатель: код}, [предупреждения])."""
    vals, warn = OrderedDict(), []
    get = lambda title: row.get(cols.get(norm(title), ''), '')

    def put(ind, v):
        vals[ind] = v

    by_num = {}
    for n_title, title in cols.items():
        m = re.match(r'^(\d+)\.\s', n_title)
        if m: by_num.setdefault(m.group(1), []).append(title)

    for num, (ind, mapping) in SINGLE.items():
        titles = [t for t in by_num.get(num, []) if norm(t) not in NUMBERS]
        if not titles: continue
        ans = row.get(titles[0], '').strip()
        if not ans: continue
        try: put(ind, code_of(mapping, ans))
        except ValueError as e: warn.append(f'вопрос {num}: {e}')

    for num, (inds, mapping) in MATRIX.items():
        titles = by_num.get(num, [])[:len(inds)]
        for k, (title, ind) in enumerate(zip(titles, inds), 1):
            ans = row.get(title, '').strip()
            if not ans: continue
            try: put(ind, code_of(mapping, ans))
            except ValueError as e: warn.append(f'вопрос {num}, строка {k}: {e}')
    # вопрос 16: строка «другое» = есть ли текст в поле «Другие изменения в поведении»
    if OTHER_16 and any(i in vals for i in MATRIX['16'][0]):
        other = [t for t in by_num.get('16', []) if 'друг' in norm(t)]
        put(OTHER_16, 1 if other and row.get(other[0], '').strip() else 0)

    for num, mapping in MULTI.items():
        titles = by_num.get(num, [])
        if not titles: continue
        ans = row.get(titles[0], '').strip()
        if not ans: continue  # необязательный вопрос пропущен — ничего не передаём
        picked = set()
        for part in ans.split(';'):
            p = norm(part)
            if p.startswith('другое'): p = 'другое'
            if p in mapping:
                if mapping[p]: picked.add(mapping[p])
            elif p: warn.append(f'вопрос {num}: неизвестный вариант «{part.strip()}»')
        for ind in dict.fromkeys(v for v in mapping.values() if v):
            put(ind, 1 if ind in picked else 0)

    for title, ind in NUMBERS.items():
        ans = get(title).strip()
        if not ans: continue
        if re.fullmatch(r'\d+', ans): put(ind, int(ans))
        else: warn.append(f'«{title}»: не число «{ans}»')
    return vals, warn


# ---------------------------------------------------------------- сборка файла
def build(template_text, period, data, pid=None):
    text = template_text
    if pid:  # по умолчанию заголовок остаётся как в выгрузке программы
        root_tag = re.search(r'<root[^>]*>', text).group(0)
        text = text.replace(root_tag, re.sub(r'pid="[^"]*"', f'pid="{pid}"', root_tag), 1)
    pos = {ind: k for k, ind in enumerate(ORDER)}
    items = ''.join(f'\t\t\t<item i="{ind}" o="{obj}" p="{period}" dk="1" v="{v}"/>\n'
                    for obj, vals in data.items()
                    for ind, v in sorted(vals.items(), key=lambda kv: pos[kv[0]]))
    text, n = re.subn(r'\t<data>\s*</data>', '\t<data>\n' + items + '\t</data>', text, count=1)
    if n != 1: sys.exit('В шаблоне не найден пустой блок <data>. Выгрузите из СОРУ пустую форму.')
    return text


def write_7z(entries, out):
    """entries — {имя в архиве: байты}; имена как в выгрузке программы («../Stream0.xml», …)."""
    with tempfile.TemporaryDirectory() as tmp:
        work = os.path.join(tmp, 'w'); os.makedirs(work)
        for name, raw in entries.items():
            with open(os.path.normpath(os.path.join(work, name)), 'wb') as f: f.write(raw)
        tmp_out = os.path.join(tmp, 'out.7z')
        subprocess.run([TAR, '--format=7zip', '-P', '-cf', tmp_out] + list(entries), cwd=work, check=True)
        if os.path.exists(out): os.remove(out)
        shutil.move(tmp_out, out)  # out может быть на другом диске


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--csv', required=True, help='CSV с листа «Ответы»')
    ap.add_argument('--template', required=True, help='выгрузка формы «Анкета родителей» из АРМ СОРУ (*.7z)')
    ap.add_argument('--out', required=True, help='куда сохранить файл для загрузки (*.7z)')
    ap.add_argument('--pid', help='заменить процесс в заголовке (по умолчанию — как в шаблоне)')
    ap.add_argument('--period', help='период, например month_2025_9 (по умолчанию — из шаблона)')
    ap.add_argument('--match', action='append', default=[], help='«ФИО как в анкете=код ученика», можно несколько раз')
    ap.add_argument('--survey', choices=['start', 'final'],
                    help='анкета: start — стартовая, final — итоговая (по умолчанию определяется по колонкам CSV)')
    a = ap.parse_args()

    entries, entry, ttext = load_template(a.template)
    students, tperiod = students_of(ttext)
    period = a.period or tperiod
    print(f'Шаблон: файлов {len(entries)}, учеников {len(students)}, период {period}')

    with open(a.csv, encoding='utf-8-sig', newline='') as f:
        rows = list(csv.DictReader(f))
    cols = {norm(c): c for c in rows[0].keys()} if rows else {}
    if CHILD_COL not in cols: sys.exit(f'В CSV нет колонки «Фамилия и имя учащегося». Это точно лист «Ответы»?')
    survey = a.survey or detect_survey(cols.values())
    use_survey(survey)
    name = 'итоговая (конец года)' if survey == 'final' else 'стартовая (сентябрь)'
    print(f'Анкета: {name}, показателей в форме СОРУ: {len(ORDER)}')
    month = re.search(r'_(\d+)$', period or '')
    if month and (month.group(1) == '9') != (survey == 'start'):
        print(f'ВНИМАНИЕ: период шаблона {period} не похож на {name} анкету. '
              'Шаблон нужно выгрузить из той же процедуры диагностики, что и анкета.')
    for r in rows:  # приводим ключи к исходным заголовкам
        r[CHILD_COL] = r.get(cols[CHILD_COL], '')

    manual = {}
    for m in a.match:
        k, _, v = m.partition('=')
        if v.strip() not in students: sys.exit(f'Нет ученика с кодом {v.strip()} в шаблоне')
        manual[norm(k)] = v.strip()

    matched, problems = match_students(rows, students, manual)
    data, warnings, dup = OrderedDict(), [], 0
    # берём последнюю анкету по каждому ученику (CSV упорядочен по времени)
    for r in rows:
        obj = matched.get(id(r))
        if not obj: continue
        vals, warn = convert(r, cols)
        if obj in data: dup += 1
        data[obj] = vals
        warnings += [f'{obj}: {w}' for w in warn]

    print(f'Анкет в CSV: {len(rows)}; сопоставлено с учениками: {len(data)}'
          + (f' (повторных анкет на одного ребёнка: {dup}, взята последняя)' if dup else ''))
    if problems:
        print('\nНЕ СОПОСТАВЛЕНЫ (добавьте --match "ФИО как в анкете=код"):')
        for child, code, cap in problems:
            hint = f'  → похоже на {cap} ({code})' if code else ''
            print(f'  «{child}»{hint}')
    if warnings:
        print('\nПредупреждения:'); [print('  ' + w) for w in warnings]
    missing = [cap for code, cap in students.items() if code not in data]
    if missing:
        print(f'\nБез анкеты: {len(missing)} уч.')

    if not data: sys.exit('\nНечего загружать.')
    # архив повторяет выгрузку-шаблон: те же файлы в том же порядке, меняется только анкета родителей
    entries[entry] = build(ttext, period, data, a.pid).encode('cp1251', errors='replace')
    write_7z(entries, a.out)
    print(f'\nГотово: {a.out} — {sum(len(v) for v in data.values())} значений по {len(data)} ученикам.')


if __name__ == '__main__':
    main()
