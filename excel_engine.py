# excel_engine.py — модуль для работы с локальным файлом Excel с расписанием отслушек.
#
# В этом файле собрана ВСЯ логика чтения и записи расписания.
# Остальной код бота (handlers.py, keyboards.py) не должен напрямую открывать Excel —
# он просто вызывает функции отсюда:
#   1. get_available_days()                — какие дни (листы) есть в расписании и их даты
#   2. get_slots_for_day(sheet)            — все слоты дня: время, свободен ли, кто записан
#   3. book_slot(sheet, time, nickname)    — записать ученика на конкретный слот
#
# ВАЖНО: реальная таблица имеет НЕОБЫЧНУЮ структуру (не плоские строки «Дата/Время/...»):
#   - каждый ЛИСТ — это день недели ("Вторник", "Среда", ...);
#   - дата занятия лежит в ячейке C2 (например "20.10.2026");
#   - внутри листа блоки слотов идут с шагом 15 строк (см. config.SLOT_STEP):
#       * в строке-заголовке блока в колонке C записано ВРЕМЯ (напр. "17:00 — 17:15");
#       * ниже, в колонке B ("Кодовое слово"), перечислены УЧЕНИКИ этого слота;
#       * колонка C у учеников — "Задач", колонка D — "Статус" (бот их НЕ трогает).
#   - слот СВОБОДЕН, если в его блоке нет ни одного ученика.

import datetime   # нужен, чтобы самим вычислять дату занятия (см. _compute_date)
import re         # регулярка — вытаскиваем время из текста формулы
import threading  # нужен для блокировки (Lock): защита от одновременной записи в файл

from openpyxl import load_workbook  # библиотека для чтения/записи .xlsx

# Импортируем настройки из config.py: путь к файлу и параметры структуры таблицы.
from config import (
    DATE_COLUMN,
    DATE_ROW,
    EXCEL_FILE,
    FIRST_SLOT_ROW,
    MAX_TASKS,
    NAME_COLUMN,
    SCHEDULE_CUTOFF_HOUR,
    SLOT_STEP,
    TASKS_COLUMN,
    TIME_COLUMN,
    WEEKDAY_NUMBERS,
)


class ExcelEngineError(Exception):
    """
    Наше собственное исключение — «проблема при работе с файлом Excel».

    Зачем оно нужно:
    - Файл может быть занят другой программой (например, открыт в Excel),
      отсутствовать или быть повреждён. Тогда openpyxl бросит разные исключения.
    - Мы «оборачиваем» все такие ошибки в одно понятное исключение ExcelEngineError,
      чтобы в handlers.py ловить только его и показывать пользователю вежливое сообщение.
    """


# Блокировка (Lock) — «замок» вокруг работы с файлом.
# Если один пользователь в этот момент бронирует слот, второй будет ждать,
# пока первый закончит. Это защищает от ОДНОВРЕМЕННОЙ записи в файл и их конфликта.
_FILE_LOCK = threading.Lock()


def _open_workbook(read_only: bool = True):
    """
    Вспомогательная функция: открывает файл Excel и возвращает книгу (workbook).

    Всегда открываем с data_only=False, то есть ЧИТАЕМ ФОРМУЛЫ, а не их значения.
    Почему так:
      - Лист считается «настоящим днём расписания», если дата в C2 задана формулой
        (data_type == "f"). При data_only=True формула отдаёт строку-значение и
        data_type становится "s" — тогда наши дни просто не находились бы.
      - К тому же openpyxl при сохранении (после брони) СТИРАЕТ кэш вычисленных
        значений формул. Значит, значения из data_only=True были бы ненадёжны, а
        сами формулы всегда на месте.
    Время слота и дату мы извлекаем прямо из текста формул (см. _extract_time_from_formula).

    Параметр read_only оставлен для совместимости вызовов, но на открытие книги
    он больше не влияет (формулы читаются в любом случае).
    """
    try:
        return load_workbook(EXCEL_FILE, data_only=False)
    except FileNotFoundError as error:
        raise ExcelEngineError(
            f"Файл Excel не найден: {EXCEL_FILE}. Проверь путь в config.py."
        ) from error
    except PermissionError as error:
        raise ExcelEngineError(
            f"Нет доступа к файлу {EXCEL_FILE}. Возможно, он открыт в Excel — закрой его."
        ) from error
    except Exception as error:
        raise ExcelEngineError(
            f"Не удалось открыть файл {EXCEL_FILE}: {error}"
        ) from error


def _extract_time_from_formula(formula: str) -> str | None:
    """
    Извлекает текст времени из СТРОКИ ФОРМУЛЫ ячейки.

    Почему так: в реальной таблице время слота хранится НЕ значением, а формулой,
    внутри которой резервное значение времени записано в кавычках. Пример
    (тройные кавычки формулы заменены на одинарные для читаемости):
        =IFERROR(__xludf.DUMMYFUNCTION('COMPUTED_VALUE'),"17:00 – 17:15")
    Нужное время записано прямо в тексте формулы в кавычках (это «резервное»
    значение на случай, если формула не вычислится). Просто достаём его регуляркой.
    """
    if not isinstance(formula, str):
        return None
    # Ищем фрагмент вида ЧЧ:ММ в кавычках: "17:00 – 17:15".
    match = re.search(r'"(\d{1,2}:\d{2}[^"]*)"', formula)
    if match:
        return match.group(1).strip()
    return None


def _get_slot_time(worksheet, row: int) -> str | None:
    """
    Возвращает текст времени слота из строки-заголовка блока (колонка C).

    Хитрость: ячейка времени в реальной таблице — это ФОРМУЛА (например
    =IFERROR(__xludf.DUMMYFUNCTION(...),"17:00 — 17:15")), внутри текста которой
    записано само время. Поэтому в зависимости от того, КАК открыта книга,
    значение ячейки будет разным:
      - книга открыта с data_only=False → value — строка формулы (начинается с "=");
      - книга открыта с data_only=True  → value — вычисленное значение ("17:00 — 17:15").

    Различаем эти два случая:
      1. Если строка начинается с "=" — это формула, вытаскиваем время из её текста.
      2. Иначе если это обычный текст со ":" — это уже готовое значение времени.

    ВАЖНО: проверять `":" in value` ДО проверки на формулу нельзя — в тексте формулы
    тоже есть двоеточие (внутри "17:00 …"), и тогда функция вернула бы всю формулу.
    """
    value = worksheet.cell(row=row, column=TIME_COLUMN).value
    if isinstance(value, str):
        # Формула — достаём время из её текста регуляркой (см. _extract_time_from_formula).
        if value.startswith("="):
            return _extract_time_from_formula(value)
        # Обычный текст времени.
        if ":" in value:
            return value.strip()
    # Ни формула, ни текст (например пусто или число) — времени в строке нет.
    return None


def _is_schedule_sheet(worksheet) -> bool:
    """
    Проверяет, является ли лист «настоящим» днём расписания.

    В исходной таблице у рабочих листов дата в C2 задана ФОРМУЛОЙ (data_type == "f").
    А у служебных листов-заготовок (например "Пятница") в C2 лежит обычный текст
    ("Кружок"). Такие листы нам не нужны — по ним нельзя показать расписание.
    """
    return worksheet.cell(row=DATE_ROW, column=DATE_COLUMN).data_type == "f"


def _compute_date(worksheet, sheet_name: str) -> str | None:
    """
    Вычисляет дату занятия (формат "дд.мм.гггг") по имени листа-дня недели.

    Зачем вычислять самим, если дата есть в таблице: в C2 она задана формулой
    с now(), а openpyxl при сохранении СТИРАЕТ кэш формул (после брони значение
    станет пустым). Поэтому мы повторяем ту же логику на Python — она стабильна.

    Формула из таблицы:
        =text(if(now() <= trunc(now()) + (A1 - weekday(now(),2)) + time(20,0,0),
                 trunc(now()) + (A1 - weekday(now(),2)),
                 trunc(now()) + (A1 - weekday(now(),2)) + 7),
              "dd.mm.yyyy")
    где A1 — номер дня недели (Вторник=2, Среда=3, ...). Смысл: берём дату
    текущего дня недели; если он уже прошёл «по расписанию» (после 20:00),
    сдвигаем на следующую неделю.

    Возвращает строку даты или None, если лист — не настоящий день расписания.
    """
    if not _is_schedule_sheet(worksheet):
        return None  # лист-заготовка (например "Пятница") — пропускаем

    weekday_number = WEEKDAY_NUMBERS.get(sheet_name.strip())
    if weekday_number is None:
        return None  # название листа не похоже на день недели

    now = datetime.datetime.now()
    today = datetime.date.today()
    current_weekday = today.isoweekday()  # 1=Пн ... 7=Вс (как в Excel weekday(...,2))

    # Дата нужного дня недели в пределах текущей недели.
    target_date = today + datetime.timedelta(days=weekday_number - current_weekday)

    # Момент «переключения»: target_date в SCHEDULE_CUTOFF_HOUR:00.
    cutoff = datetime.datetime.combine(
        target_date, datetime.time(SCHEDULE_CUTOFF_HOUR, 0, 0)
    )
    # Если этот день/время уже прошли — берём тот же день на следующей неделе.
    if now > cutoff:
        target_date += datetime.timedelta(days=7)

    return target_date.strftime("%d.%m.%Y")


def _parse_slots(worksheet) -> list[dict]:
    """
    Разбирает ВСЕ слоты одного листа (дня).

    Возвращает список словарей вида:
        {
            "time": "17:00 — 17:15",   # время слота (из колонки C строки-заголовка)
            "header_row": 6,            # номер строки, где записано время
            "students": ["кто14", ...], # список учеников этого слота
            "free": False,              # True, если учеников нет
        }

    Как определяем блоки: начинаем со строки FIRST_SLOT_ROW и идём вниз с шагом SLOT_STEP.
    Как только в строке-заголовке больше нет времени — считаем, что слоты кончились.
    """
    slots = []
    row = FIRST_SLOT_ROW

    # Идём, пока не выйдем за пределы листа.
    while row <= worksheet.max_row:
        time_text = _get_slot_time(worksheet, row)

        # Нет времени в заголовке блока — слоты закончились.
        if time_text is None:
            break

        # Ученики лежат в строках от (row+1) до (row+SLOT_STEP-1) в колонке B.
        students = []
        for student_row in range(row + 1, row + SLOT_STEP):
            name_value = worksheet.cell(row=student_row, column=NAME_COLUMN).value
            if name_value is not None and str(name_value).strip():
                students.append(str(name_value).strip())

        slots.append(
            {
                "time": time_text,
                "header_row": row,
                "students": students,
                "free": len(students) == 0,
            }
        )

        row += SLOT_STEP  # переходим к следующему блоку

    return slots


def get_available_days() -> list[dict]:
    """
    Возвращает список ВСЕХ дней (листов) с датами из таблицы.

    Пример результата:
        [
            {"sheet": "Вторник", "date": "20.10.2026"},
            {"sheet": "Среда",   "date": "21.10.2026"},
            ...
        ]

    Дату считаем сами (см. _compute_date) — так она не «пропадёт» после того,
    как бот сохранит файл и openpyxl сотрёт кэш формул. Служебные листы-заготовки
    (например "Пятница") пропускаются.
    """
    with _FILE_LOCK:
        workbook = _open_workbook(read_only=True)
        try:
            days = []
            for sheet_name in workbook.sheetnames:
                worksheet = workbook[sheet_name]
                date_text = _compute_date(worksheet, sheet_name)
                if date_text is None:
                    continue  # не день расписания — пропускаем
                days.append({"sheet": sheet_name, "date": date_text})
            return days
        finally:
            workbook.close()


def get_slots_for_day(sheet: str) -> list[dict]:
    """
    Возвращает ВСЕ слоты указанного дня (листа) — и свободные, и занятые.
    Формат описан в _parse_slots().

    Параметр sheet: имя листа (например "Вторник").
    """
    with _FILE_LOCK:
        workbook = _open_workbook(read_only=True)
        try:
            if sheet not in workbook.sheetnames:
                raise ExcelEngineError(f"Лист '{sheet}' не найден в файле {EXCEL_FILE}.")
            worksheet = workbook[sheet]
            return _parse_slots(worksheet)
        finally:
            workbook.close()


def book_slot(sheet: str, header_row: int, nickname: str, tasks: int = 0) -> bool:
    """
    Записывает ученика на конкретный слот.

    Параметры:
        sheet      — имя листа-дня (например "Вторник").
        header_row — НОМЕР СТРОКИ блока (строки, где записано время слота).
                     Используем номер строки, а НЕ текст времени: время в таблице
                     может быть формулой, и при открытии на запись (data_only=False)
                     оно читается как формула, а не значение. Номер строки надёжен.
        nickname   — ник/имя ученика, которое запишем в колонку B.
        tasks      — количество задач (0 = не указано, 1..MAX_TASKS).

    Возвращает:
        True  — если слот был свободен и мы успешно его забронировали.
        False — если слот уже занят (в блоке есть ученики) или блок некорректен.

    Логика: проверяем строки блока (header_row+1 … header_row+SLOT_STEP-1);
    если все пусты — слот свободен, пишем ник в самую верхнюю пустую строку.
    """
    with _FILE_LOCK:
        workbook = _open_workbook(read_only=False)
        try:
            if sheet not in workbook.sheetnames:
                raise ExcelEngineError(f"Лист '{sheet}' не найден в файле {EXCEL_FILE}.")
            worksheet = workbook[sheet]

            # Проверяем корректность номера строки блока.
            # Строка должна быть целым числом не меньше первой строки слотов,
            # отстоять от неё на целое число шагов и в этой строке должно быть
            # время слота (защита от «угаданных» некорректных номеров строк).
            if not isinstance(header_row, int) or header_row < FIRST_SLOT_ROW:
                return False
            if (header_row - FIRST_SLOT_ROW) % SLOT_STEP != 0:
                return False
            if _get_slot_time(worksheet, header_row) is None:
                return False

            # Ищем первую пустую строку блока и одновременно проверяем, свободен ли слот.
            first_empty_row = None
            for student_row in range(header_row + 1, header_row + SLOT_STEP):
                value = worksheet.cell(row=student_row, column=NAME_COLUMN).value
                if value is None or not str(value).strip():
                    # Пустая строка. Запоминаем только первую.
                    if first_empty_row is None:
                        first_empty_row = student_row
                else:
                    # В блоке уже есть ученик — слот занят, бронь невозможна.
                    return False

            # Если пустой строки нет — весь блок занят (или сломан).
            if first_empty_row is None:
                return False

            # Слот свободен: пишем ник в первую пустую строку блока (колонка B).
            worksheet.cell(row=first_empty_row, column=NAME_COLUMN, value=nickname)

            # Если указано количество задач — пишем его в колонку C (TASKS_COLUMN).
            if 1 <= tasks <= MAX_TASKS:
                worksheet.cell(row=first_empty_row, column=TASKS_COLUMN, value=tasks)

            # Сохраняем весь файл на диск.
            try:
                workbook.save(EXCEL_FILE)
            except PermissionError as error:
                raise ExcelEngineError(
                    f"Не удалось сохранить файл {EXCEL_FILE}: он занят (открыт в Excel?)."
                ) from error
            return True
        finally:
            workbook.close()


# ---------------------------------------------------------------------------
# ФУНКЦИИ ДЛЯ АДМИНИСТРАТОРА.
# Всё, что касается управления расписанием (сброс, изменение времени, просмотр
# записей), собрано здесь. Логика работы с файлом остаётся в этом модуле —
# handlers.py только вызывает эти функции (как и в случае с бронированием).
# ---------------------------------------------------------------------------


def _validate_header_row(worksheet, header_row) -> bool:
    """
    Проверяет, что header_row — корректный номер строки-заголовка блока слота.

    Правила (те же, что и при бронировании):
      - это целое число не меньше FIRST_SLOT_ROW;
      - отстоит от FIRST_SLOT_ROW на целое число шагов SLOT_STEP;
      - в этой строке реально записано время слота.
    Такая защита не даёт «сбросить» или «переименовать» что-то не то.
    """
    if not isinstance(header_row, int) or header_row < FIRST_SLOT_ROW:
        return False
    if (header_row - FIRST_SLOT_ROW) % SLOT_STEP != 0:
        return False
    return _get_slot_time(worksheet, header_row) is not None


def _clear_slot_students(worksheet, header_row: int) -> int:
    """
    Очищает всех учеников одного блока слота (колонка B от header_row+1 до
    header_row+SLOT_STEP-1). Возвращает количество удалённых записей.

    Функция НЕ сохраняет файл — это делает вызывающий код (book/reset),
    чтобы за одно сохранение выполнить сразу несколько операций.
    """
    removed = 0
    for student_row in range(header_row + 1, header_row + SLOT_STEP):
        cell = worksheet.cell(row=student_row, column=NAME_COLUMN)
        if cell.value is not None and str(cell.value).strip():
            cell.value = None  # стираем ник ученика
            removed += 1
    return removed


def get_all_records() -> list[dict]:
    """
    Админ-функция: возвращает ВСЕ записи (кто на какой слот записан).

    Формат результата — список словарей:
        [
            {"sheet": "Вторник", "date": "20.10.2026",
             "students": [{"time": "17:00 — 17:15", "name": "@ivan"}, ...]},
            ...
        ]
    Дни без единой записи в список не попадают.
    """
    with _FILE_LOCK:
        workbook = _open_workbook(read_only=True)
        try:
            records = []
            for sheet_name in workbook.sheetnames:
                worksheet = workbook[sheet_name]
                date_text = _compute_date(worksheet, sheet_name)
                if date_text is None:
                    continue  # не день расписания

                # Собираем построчно: время слота + ник ученика.
                booked = []
                for slot in _parse_slots(worksheet):
                    for student in slot["students"]:
                        booked.append({"time": slot["time"], "name": student})

                if booked:
                    records.append(
                        {
                            "sheet": sheet_name,
                            "date": date_text,
                            "students": booked,
                        }
                    )
            return records
        finally:
            workbook.close()


def reset_all_slots() -> int:
    """
    Админ-функция: ПОЛНЫЙ СБРОС — стирает всех учеников во ВСЕХ днях расписания.

    Возвращает общее количество удалённых записей. Файл сохраняется один раз
    в конце (быстрее и надёжнее, чем сохранять после каждого дня).
    """
    with _FILE_LOCK:
        workbook = _open_workbook(read_only=False)
        try:
            total_removed = 0
            for sheet_name in workbook.sheetnames:
                worksheet = workbook[sheet_name]
                if _compute_date(worksheet, sheet_name) is None:
                    continue  # служебный лист — не трогаем
                for slot in _parse_slots(worksheet):
                    total_removed += _clear_slot_students(
                        worksheet, slot["header_row"]
                    )

            try:
                workbook.save(EXCEL_FILE)
            except PermissionError as error:
                raise ExcelEngineError(
                    f"Не удалось сохранить файл {EXCEL_FILE}: он занят (открыт в Excel?)."
                ) from error
            return total_removed
        finally:
            workbook.close()


def reset_slot(sheet: str, header_row: int) -> int:
    """
    Админ-функция: сбрасывает ОДИН конкретный слот (освобождает его).

    Параметры: sheet — имя листа-дня, header_row — номер строки блока слота.
    Возвращает количество удалённых записей (0 — если слот и так был пуст,
    -1 — если блок некорректен).
    """
    with _FILE_LOCK:
        workbook = _open_workbook(read_only=False)
        try:
            if sheet not in workbook.sheetnames:
                raise ExcelEngineError(
                    f"Лист '{sheet}' не найден в файле {EXCEL_FILE}."
                )
            worksheet = workbook[sheet]

            if not _validate_header_row(worksheet, header_row):
                return -1  # некорректный номер блока

            removed = _clear_slot_students(worksheet, header_row)

            try:
                workbook.save(EXCEL_FILE)
            except PermissionError as error:
                raise ExcelEngineError(
                    f"Не удалось сохранить файл {EXCEL_FILE}: он занят (открыт в Excel?)."
                ) from error
            return removed
        finally:
            workbook.close()


def update_slot_time(sheet: str, header_row: int, new_time: str) -> bool:
    """
    Админ-функция: изменяет ВРЕМЯ слота (текст в колонке C строки-заголовка).

    Параметры:
        sheet      — имя листа-дня,
        header_row — номер строки блока слота,
        new_time   — новый текст времени (например "17:00 — 17:15").

    Возвращает True при успехе, False — если блок некорректен.
    ВАЖНО: в таблице время хранится формулой. Мы перезаписываем ячейку обычным
    текстом — это осознанно: так новое время гарантированно сохранится и будет
    читаться ботом и в Excel.
    """
    with _FILE_LOCK:
        workbook = _open_workbook(read_only=False)
        try:
            if sheet not in workbook.sheetnames:
                raise ExcelEngineError(
                    f"Лист '{sheet}' не найден в файле {EXCEL_FILE}."
                )
            worksheet = workbook[sheet]

            if not _validate_header_row(worksheet, header_row):
                return False

            # Пишем новое время обычным текстом в колонку C строки-заголовка.
            worksheet.cell(row=header_row, column=TIME_COLUMN, value=new_time)

            try:
                workbook.save(EXCEL_FILE)
            except PermissionError as error:
                raise ExcelEngineError(
                    f"Не удалось сохранить файл {EXCEL_FILE}: он занят (открыт в Excel?)."
                ) from error
            return True
        finally:
            workbook.close()


# ---------------------------------------------------------------------------
# Поиск существующих записей пользователя (защита от двойной записи).
# ---------------------------------------------------------------------------


def find_user_bookings(nickname: str) -> list[dict]:
    """
    Ищет все слоты, в которых записан пользователь с указанным ником.

    Проходит по всем листам-дням и проверяет всех учеников в каждом слоте.
    Если ник найден — возвращает список словарей:
        [
            {"sheet": "Вторник", "date": "20.10.2026",
             "time": "17:00 — 17:15", "header_row": 6},
            ...
        ]
    Если записей нет — возвращает пустой список [].

    Используется в handlers.py перед бронированием, чтобы запретить
    повторную запись одного и того же ученика.
    """
    with _FILE_LOCK:
        workbook = _open_workbook(read_only=True)
        try:
            results: list[dict] = []
            nickname_lower = nickname.strip().lower()

            for sheet_name in workbook.sheetnames:
                worksheet = workbook[sheet_name]
                date_text = _compute_date(worksheet, sheet_name)
                if date_text is None:
                    continue  # не день расписания

                for slot in _parse_slots(worksheet):
                    if isinstance(slot["students"], list):
                        for student in slot["students"]:
                            if student.strip().lower() == nickname_lower:
                                results.append({
                                    "sheet": sheet_name,
                                    "date": date_text,
                                    "time": slot["time"],
                                    "header_row": slot["header_row"],
                                })
                                # Продолжаем сканировать — вдруг тот же ник
                                # попал в несколько слотов (ошибка админа).
            return results
        finally:
            workbook.close()


# Блок ниже выполняется ТОЛЬКО если запустить этот файл напрямую:
#     python excel_engine.py
# Он не срабатывает при обычном импорте. Удобно для быстрой проверки.
if __name__ == "__main__":
    days = get_available_days()
    print("Дни в расписании:")
    for day in days:
        print(f"  - {day['sheet']} ({day['date']})")

    if days:
        first = days[0]
        print(f"\nСлоты дня '{first['sheet']}':")
        for slot in get_slots_for_day(first["sheet"]):
            status = "свободен" if slot["free"] else f"занят ({len(slot['students'])} уч.)"
            print(f"  - {slot['time']}: {status}")