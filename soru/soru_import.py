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
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from collections import OrderedDict

TAR = os.path.join(os.environ.get('SystemRoot', r'C:\Windows'), 'System32', 'tar.exe')
SD = 'ind_sd_asoru_8.'

# ---------------------------------------------------------------- справочники
# Коды ответов взяты из справочников armsoru.exe (стартовая диагностика, анкета родителей).
YES4 = {'да': 0, 'скорее да': 1, 'скорее нет': 2, 'нет': 3}
BOOL = {'да': 1, 'нет': 0}

SINGLE = {  # номер вопроса в нашей анкете → (показатель СОРУ, {вариант ответа: код})
    '2': ('2.', {'по месту жительства': 0, 'в ту школу, которую вы специально выбрали': 1, 'в другую': 2}),
    '3': ('3.', {'совсем не ходил': 0, 'менее 1 года': 1, '1-2 года': 2, '2-3 года': 3, '3 года и более': 4}),
    '4': ('4.', {'до 6 лет': 0, '6-6,5 лет': 1, '6,5-7 лет': 2, '7-8 лет': 3, 'старше 8 лет': 4}),
    '6': ('6.', {'на русском языке': 0, 'на другом языке': 1}),
    '7': ('7.', BOOL),
    '17': ('17.', {'да': 0, 'стараемся, и часто удается': 1, 'стараемся, но далеко не всегда удается': 2,
                   'нет, мы не придаем этому большого значения': 3}),
    '19': ('19.', {'очень хотел': 0, 'скорее хотел': 1, 'скорее не хотел': 2, 'очень не хотел': 3}),
    '20': ('20.', {'наотрез отказывается идти в школу': 0, 'просит оставить дома': 1, 'капризничает, ворчит': 2,
                   'без особого желания, но спокойно': 3, 'с охотой': 4}),
    '21': ('21.', {'конечно же, на отличные оценки': 0, 'главное - чтоб учился хорошо': 1,
                   'оценки - не главное, главное - знания': 2, 'оценки не главное, главное - чтоб был здоровым': 3}),
    '22': ('22.', BOOL),
    '26': ('27.', {'0-10 книг': 0, '11-25 книг': 1, '26-50 книг': 2, '51-100 книг': 3, 'более 100 книг': 4}),
    '27': ('28.', {'очень плохое': 0, 'плохое': 1, 'среднее': 2, 'хорошее': 3, 'очень хорошее': 4}),
}

# Вопросы-таблицы: строки по порядку → показатели <префикс><n>.
MATRIX = {
    '5': ('5.', 5, YES4),
    '10': ('10.', 15, {'никогда или почти никогда': 0, 'иногда': 1, 'часто': 2}),
    '11': ('11.', 6, {'очень хорошо': 0, 'хорошо': 1, 'не очень хорошо': 2, 'плохо': 3}),
    '12': ('12.', 8, {'очень хорошо': 0, 'хорошо': 1, 'не очень хорошо': 2, 'не умел вообще': 3}),
    '13': ('13.', 7, BOOL),
}

# Вопросы с несколькими ответами: вариант → показатель (1 — отмечен, 0 — нет).
MULTI = {
    '8': {'нет, не проходил': '8.1.', 'да, в подготовительной группе детского сада': '8.2.',
          'да, на подготовительных занятиях в школе, в которой учится ребенок': '8.3.',
          'да, на подготовительных занятиях в другой школе': '8.4.', 'да, на занятиях с логопедом': '8.5.',
          'да, на занятиях с репетитором': '8.6.', 'да, мы готовили его сами': '8.7.', 'другое': '8.8.'},
    # из 11 причин в СОРУ есть только две
    '15': {'в перегрузке детей работой по дому или хозяйству': '15.5.', 'в позднем засыпании': '15.10.'},
}

NUMBERS = {'23. сколько всего человек живут у вас дома?': '23.', '23. сколько из них детей?': '24.',
           '24. сколько у вас комнат?': '25.'}

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
    for name, raw in read_7z(path).items():
        text = raw.decode('cp1251')
        if 'formid="soru_08"' in text[:400]:
            return name, text
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
        vals[SD + ind] = v

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

    for num, (prefix, nrows, mapping) in MATRIX.items():
        titles = by_num.get(num, [])[:nrows]
        for k, title in enumerate(titles, 1):
            ans = row.get(title, '').strip()
            if not ans: continue
            try: put(f'{prefix}{k}.', code_of(mapping, ans))
            except ValueError as e: warn.append(f'вопрос {num}, строка {k}: {e}')

    for num, mapping in MULTI.items():
        titles = by_num.get(num, [])
        if not titles: continue
        ans = row.get(titles[0], '').strip()
        if not ans: continue  # необязательный вопрос пропущен — ничего не передаём
        picked = set()
        for part in ans.split(';'):
            p = norm(part)
            if p.startswith('другое'): p = 'другое'
            if p in mapping: picked.add(mapping[p])
        for opt, ind in mapping.items():
            put(ind, 1 if ind in picked else 0)

    for title, ind in NUMBERS.items():
        ans = get(title).strip()
        if not ans: continue
        if re.fullmatch(r'\d+', ans): put(ind, int(ans))
        else: warn.append(f'«{title}»: не число «{ans}»')
    return vals, warn


# ---------------------------------------------------------------- сборка файла
def build(template_text, period, data, pid):
    root_tag = re.search(r'<root[^>]*>', template_text).group(0)
    new_root = re.sub(r'pid="[^"]*"', f'pid="{pid}"', root_tag)
    text = template_text.replace(root_tag, new_root, 1)
    items = ''.join(f'\t\t\t<item i="{ind}" o="{obj}" p="{period}" dk="1" v="{v}"/>\n'
                    for obj, vals in data.items() for ind, v in vals.items())
    text, n = re.subn(r'\t<data>\s*</data>', '\t<data>\n' + items + '\t</data>', text, count=1)
    if n != 1: sys.exit('В шаблоне не найден пустой блок <data>. Выгрузите из СОРУ пустую форму.')
    return text


def write_7z(entry_name, text, out):
    with tempfile.TemporaryDirectory() as tmp:
        work = os.path.join(tmp, 'w'); os.makedirs(work)
        target = os.path.normpath(os.path.join(work, entry_name))
        with open(target, 'wb') as f: f.write(text.encode('cp1251', errors='replace'))
        tmp_out = os.path.join(tmp, 'out.7z')
        subprocess.run([TAR, '--format=7zip', '-P', '-cf', tmp_out, entry_name], cwd=work, check=True)
        if os.path.exists(out): os.remove(out)
        os.replace(tmp_out, out)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--csv', required=True, help='CSV с листа «Ответы»')
    ap.add_argument('--template', required=True, help='выгрузка формы «Анкета родителей» из АРМ СОРУ (*.7z)')
    ap.add_argument('--out', required=True, help='куда сохранить файл для загрузки (*.7z)')
    ap.add_argument('--pid', default='p_sd1', help='процесс СОРУ (по умолчанию p_sd1 — стартовая диагностика 1 класс)')
    ap.add_argument('--period', help='период, например month_2025_9 (по умолчанию — из шаблона)')
    ap.add_argument('--match', action='append', default=[], help='«ФИО как в анкете=код ученика», можно несколько раз')
    a = ap.parse_args()

    entry, ttext = load_template(a.template)
    students, tperiod = students_of(ttext)
    period = a.period or tperiod
    print(f'Шаблон: учеников {len(students)}, период {period}, процесс → {a.pid}')

    with open(a.csv, encoding='utf-8-sig', newline='') as f:
        rows = list(csv.DictReader(f))
    cols = {norm(c): c for c in rows[0].keys()} if rows else {}
    if CHILD_COL not in cols: sys.exit(f'В CSV нет колонки «Фамилия и имя учащегося». Это точно лист «Ответы»?')
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
    write_7z(entry, build(ttext, period, data, a.pid), a.out)
    print(f'\nГотово: {a.out} — {sum(len(v) for v in data.values())} значений по {len(data)} ученикам.')


if __name__ == '__main__':
    main()
