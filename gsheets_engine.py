# gsheets_engine.py — модуль для работы с Google Таблицей расписания отслушек.
#
# Этот файл является ЗАМЕНОЙ excel_engine.py (который работал с локальным .xlsx).
# Все функции имеют ТЕ ЖЕ САМЫЕ имена и сигнатуры, что и в excel_engine.py,
# чтобы handlers.py не требовал изменений.
#
# ВАЖНО: теперь данные хранятся НЕ в локальном Excel, а в Google Таблице.
# Подключение через Service Account (библиотека gspread).

import datetime
import json
import logging
import os
import re
import threading
import time
from typing import Callable, TypeVar
from zoneinfo import ZoneInfo

import gspread
from google.oauth2.service_account import Credentials

from config import (
    DATE_COLUMN, DATE_ROW, FIRST_SLOT_ROW, MAX_TASKS,
    NAME_COLUMN, SCHEDULE_CUTOFF_HOUR, SERVICE_ACCOUNT_FILE,
    SLOT_STEP, SPREADSHEET_ID, TASKS_COLUMN, TIME_COLUMN, WEEKDAY_NUMBERS,
)

logger = logging.getLogger(__name__)

T = TypeVar('T')

# Максимальное количество повторных попыток для сетевых операций
MAX_RETRIES: int = 3
# Задержка между попытками (в секундах)
RETRY_DELAY: float = 1.0


def _retry_on_network_error(func: Callable[..., T], *args, **kwargs) -> T:
    """
    Выполняет функцию с повторными попытками при сетевых ошибках.

    Перечисленные исключения считаются временными и вызывают повтор:
      - gspread.exceptions.APIError (ошибки API Google)
      - gspread.exceptions.GSpreadException (общие ошибки gspread)
      - TimeoutError (таймаут)
    Другие исключения пробрасываются без повторов.
    """
    last_exception = None

    for attempt in range(MAX_RETRIES):
        try:
            return func(*args, **kwargs)
        except gspread.exceptions.APIError as e:
            last_exception = e
            error_code = getattr(e, 'response', None)
            if error_code is not None:
                status_code = getattr(error_code, 'status_code', None)
                # Ошибки, которые НЕ стоит ретраить (клиентские ошибки)
                if status_code in (400, 401, 403, 404):
                    logger.error(f"Клиентская ошибка API Google (код {status_code}), повтор не поможет: {e}")
                    raise ExcelEngineError(
                        f"Ошибка Google API (код {status_code}): проверьте настройки доступа и ID таблицы. "
                        f"Подробности: {e}"
                    ) from e
            if attempt < MAX_RETRIES - 1:
                logger.warning(f"Попытка {attempt + 1}/{MAX_RETRIES} не удалась (API Error): {e}. Повтор через {RETRY_DELAY} сек...")
                time.sleep(RETRY_DELAY)
            else:
                logger.error(f"Все {MAX_RETRIES} попыток не удались. Последняя ошибка API: {e}")
        except (gspread.exceptions.GSpreadException, TimeoutError) as e:
            last_exception = e
            if attempt < MAX_RETRIES - 1:
                logger.warning(f"Попытка {attempt + 1}/{MAX_RETRIES} не удалась: {e}. Повтор через {RETRY_DELAY} сек...")
                time.sleep(RETRY_DELAY)
            else:
                logger.error(f"Все {MAX_RETRIES} попыток не удались. Последняя ошибка: {e}")
        except Exception as e:
            # Для других исключений не делаем retry
            raise

    raise ExcelEngineError(
        f"Не удалось выполнить операцию с Google Таблицами после {MAX_RETRIES} попыток. "
        f"Проверьте интернет-соединение и доступность Google API. Последняя ошибка: {last_exception}"
    ) from last_exception


class ExcelEngineError(Exception):
    """Наше собственное исключение — проблема при работе с Google Таблицей."""


_client = None
_spreadsheet = None
_FILE_LOCK = threading.Lock()


def _get_client():
    global _client
    if _client is None:
        def _create_client():
            # На Amvera ключ сервисного аккаунта передаётся через переменную
            # окружения GOOGLE_CREDENTIALS_JSON (JSON-строка целиком).
            # Локально (переменной нет) по-прежнему читаем файл
            # SERVICE_ACCOUNT_FILE — локальная работа не ломается.
            credentials_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")
            if credentials_json:
                creds = Credentials.from_service_account_info(
                    json.loads(credentials_json),
                    scopes=["https://www.googleapis.com/auth/spreadsheets"],
                )
            else:
                creds = Credentials.from_service_account_file(
                    SERVICE_ACCOUNT_FILE,
                    scopes=["https://www.googleapis.com/auth/spreadsheets"],
                )
            return gspread.authorize(creds)

        try:
            _client = _retry_on_network_error(_create_client)
        except ExcelEngineError:
            raise
        except Exception as error:
            raise ExcelEngineError(
                f"Ошибка авторизации в Google Таблицах: проверь файл ключа сервисного аккаунта "
                f"или переменную GOOGLE_CREDENTIALS_JSON ({error})"
            ) from error
    return _client


def _get_spreadsheet():
    global _spreadsheet
    if _spreadsheet is None:
        def _open_spreadsheet():
            client = _get_client()
            return client.open_by_key(SPREADSHEET_ID)

        try:
            _spreadsheet = _retry_on_network_error(_open_spreadsheet)
        except ExcelEngineError:
            raise
        except Exception as error:
            error_str = str(error).lower()
            if "not found" in error_str or "404" in error_str:
                raise ExcelEngineError(
                    f"Google Таблица с ID '{SPREADSHEET_ID}' не найдена. "
                    f"Проверь SPREADSHEET_ID в config.py и доступ сервисного аккаунта к таблице."
                ) from error
            elif "permission" in error_str or "403" in error_str:
                raise ExcelEngineError(
                    f"Нет доступа к Google Таблице. Добавь email сервисного аккаунта "
                    f"в настройки доступа таблицы (кнопка «Поделиться» → «Редактор»)."
                ) from error
            else:
                raise ExcelEngineError(
                    f"Не удалось открыть Google Таблицу: {error}"
                ) from error
    return _spreadsheet


def _open_worksheet(sheet_name):
    def _get_worksheet():
        spreadsheet = _get_spreadsheet()
        logger.debug(f"Открытие листа: {sheet_name}")
        return spreadsheet.worksheet(sheet_name)

    try:
        return _retry_on_network_error(_get_worksheet)
    except gspread.WorksheetNotFound:
        logger.error(f"Лист '{sheet_name}' не найден в Google Таблице. Доступные листы будут логированы ниже.")
        # Попытка получить список доступных листов для диагностики
        try:
            spreadsheet = _get_spreadsheet()
            available_sheets = [ws.title for ws in spreadsheet.worksheets()]
            logger.info(f"Доступные листы в таблице: {available_sheets}")
        except Exception:
            pass
        raise ExcelEngineError(f"Лист '{sheet_name}' не найден в Google Таблице.")
    except ExcelEngineError:
        raise
    except Exception as error:
        logger.error(f"Ошибка при открытии листа '{sheet_name}': {error}", exc_info=True)
        raise ExcelEngineError(
            f"Ошибка при открытии листа '{sheet_name}': {error}"
        ) from error


def _invalidate_cache():
    global _spreadsheet
    _spreadsheet = None


def _extract_time_from_formula(formula):
    if not isinstance(formula, str):
        return None
    match = re.search(r'"(\d{1,2}:\d{2}[^"]*)"', formula)
    if match:
        return match.group(1).strip()
    return None


def _get_slot_time(worksheet, row):
    try:
        value = worksheet.cell(row, TIME_COLUMN).value
    except Exception:
        return None
    if value is None:
        return None
    if isinstance(value, str):
        if value.startswith("="):
            return _extract_time_from_formula(value)
        if ":" in value:
            return value.strip()
    return None


def _is_schedule_sheet(worksheet):
    try:
        # ВАЖНО: читаем ячейку с датой в режиме FORMULA — gspread по умолчанию
        # возвращает отформатированное значение (например "20.10.2026"), а нам
        # нужно проверить, что в ячейке лежит ФОРМУЛА (начинается с "=").
        # Так мы отличаем листы-дни расписания от служебных листов.
        value = worksheet.cell(
            DATE_ROW, DATE_COLUMN, value_render_option="FORMULA"
        ).value
        return isinstance(value, str) and value.startswith("=")
    except Exception:
        return False


def _compute_date(worksheet, sheet_name):
    if not _is_schedule_sheet(worksheet):
        return None
    weekday_number = WEEKDAY_NUMBERS.get(sheet_name.strip())
    if weekday_number is None:
        return None
    # ВАЖНО: сервер Amvera работает в UTC, а расписание — в часовом поясе
    # Екатеринбурга (UTC+5). Считаем «текущее время» в явной таймзоне,
    # чтобы день и SCHEDULE_CUTOFF_HOUR определялись по местному времени.
    # Python 3.10+: ZoneInfo из стандартной библиотеки (zoneinfo).
    now = datetime.datetime.now(ZoneInfo("Asia/Yekaterinburg"))
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    days_until = (weekday_number - 1 - today.weekday()) % 7
    base_date = today + datetime.timedelta(days=days_until)
    if base_date < today:
        base_date += datetime.timedelta(days=7)
    if now.hour >= SCHEDULE_CUTOFF_HOUR and base_date <= today:
        base_date += datetime.timedelta(days=7)
    return base_date.strftime("%d.%m.%Y")


def _parse_slots(worksheet):
    """Парсит лист и возвращает список слотов (та же структура, что в excel_engine.py)."""
    slots = []
    row = FIRST_SLOT_ROW

    def _get_values():
        return worksheet.get_all_values(value_render_option="FORMULA")

    try:
        all_values = _retry_on_network_error(_get_values)
    except ExcelEngineError:
        raise
    except Exception as error:
        raise ExcelEngineError(f"Не удалось прочитать лист: {error}") from error

    max_row = len(all_values)
    while row <= max_row:
        time_text = None
        if row - 1 < len(all_values):
            row_data = all_values[row - 1]
            if len(row_data) > TIME_COLUMN - 1:
                cell = row_data[TIME_COLUMN - 1]
                if isinstance(cell, str) and ":" in cell:
                    if cell.startswith("="):
                        time_text = _extract_time_from_formula(cell)
                    else:
                        time_text = cell.strip()
                elif isinstance(cell, str) and cell.startswith("="):
                    time_text = _extract_time_from_formula(cell)

        if time_text is None:
            break

        students = []
        for offset in range(1, SLOT_STEP):
            idx = row - 1 + offset
            if idx >= len(all_values):
                break
            r = all_values[idx]
            name_val = r[NAME_COLUMN - 1] if len(r) > NAME_COLUMN - 1 else ""
            if name_val and str(name_val).strip():
                students.append(str(name_val).strip())

        slots.append({
            "time": time_text,
            "header_row": row,
            "students": students,
            "free": len(students) == 0,
        })
        row += SLOT_STEP

    return slots


# ---------------------------------------------------------------------------
# Основные функции (публичный API).
# ---------------------------------------------------------------------------


def get_available_days():
    """Возвращает список всех дней (листов) с датами из Google Таблицы."""
    try:
        logger.debug("Получение списка доступных дней из Google Таблицы")
        worksheets = _get_spreadsheet().worksheets()
        days = []
        for ws in worksheets:
            date_text = _compute_date(ws, ws.title)
            if date_text is not None:
                days.append({"sheet": ws.title, "date": date_text})
        logger.info(f"Найдено дней расписания: {len(days)}")
        return days
    except ExcelEngineError:
        raise
    except Exception as error:
        logger.error(f"Ошибка при получении списка дней: {error}", exc_info=True)
        raise ExcelEngineError(f"Ошибка при получении списка дней: {error}") from error


def get_slots_for_day(sheet):
    """Возвращает все слоты указанного дня (и свободные, и занятые)."""
    with _FILE_LOCK:
        try:
            logger.debug(f"Получение слотов для дня: {sheet}")
            worksheet = _open_worksheet(sheet)
            slots = _parse_slots(worksheet)
            logger.info(f"Найдено слотов для '{sheet}': {len(slots)}")
            return slots
        except ExcelEngineError:
            raise
        except Exception as error:
            logger.error(f"Ошибка при получении слотов для '{sheet}': {error}", exc_info=True)
            raise ExcelEngineError(
                f"Ошибка при получении слотов для '{sheet}': {error}"
            ) from error


def book_slot(sheet, header_row, nickname, tasks=0):
    """Записывает ученика на конкретный слот. True при успехе, False — если слот занят."""
    with _FILE_LOCK:
        try:
            logger.debug(f"Попытка бронирования: sheet={sheet}, row={header_row}, nickname={nickname}, tasks={tasks}")
            worksheet = _open_worksheet(sheet)

            # Проверяем корректность номера строки блока.
            if not isinstance(header_row, int) or header_row < FIRST_SLOT_ROW:
                logger.warning(f"Некорректный header_row: {header_row}")
                return False
            if (header_row - FIRST_SLOT_ROW) % SLOT_STEP != 0:
                logger.warning(f"header_row не кратен SLOT_STEP: {header_row}")
                return False
            if _get_slot_time(worksheet, header_row) is None:
                logger.warning(f"Нет времени в строке {header_row}")
                return False

            # Читаем колонку B блока (строки header_row+1 … header_row+SLOT_STEP-1).
            # ВАЖНО: используем range(), а не get()/batch_get() — Google API
            # возвращает ПУСТОЙ список для диапазона, где все ячейки пустые,
            # а range() всегда возвращает все ячейки диапазона.
            body_range = f"B{header_row + 1}:B{header_row + SLOT_STEP - 1}"
            try:
                body_cells = worksheet.range(body_range)
            except Exception as e:
                logger.error(f"Ошибка чтения диапазона {body_range}: {e}")
                body_cells = []

            # Ищем первую пустую строку и одновременно проверяем, свободен ли слот.
            first_empty_row = None
            for idx, cell in enumerate(body_cells):
                student_value = cell.value
                if student_value is None or not str(student_value).strip():
                    if first_empty_row is None:
                        first_empty_row = header_row + 1 + idx
                else:
                    # В блоке уже есть ученик — слот занят, бронь невозможна.
                    logger.info(f"Слот занят: {sheet} row={header_row}, уже записан: {student_value}")
                    return False

            # Если пустой строки нет — весь блок занят (или сломан).
            if first_empty_row is None:
                logger.warning(f"Нет пустых строк в блоке: {sheet} row={header_row}")
                return False

            # Слот свободен: пишем ник в первую пустую строку блока (колонка B).
            def _update_nickname():
                worksheet.update_cell(first_empty_row, NAME_COLUMN, nickname)
            _retry_on_network_error(_update_nickname)

            # Если указано количество задач — пишем его в колонку C (TASKS_COLUMN).
            if 1 <= tasks <= MAX_TASKS:
                def _update_tasks():
                    worksheet.update_cell(first_empty_row, TASKS_COLUMN, tasks)
                _retry_on_network_error(_update_tasks)

            logger.info(f"Успешная бронь: {sheet} row={header_row}, nickname={nickname}, tasks={tasks}")
            return True
        except ExcelEngineError:
            raise
        except Exception as error:
            logger.error(f"Ошибка при бронировании слота '{sheet}': {error}", exc_info=True)
            raise ExcelEngineError(
                f"Ошибка при бронировании слота '{sheet}': {error}"
            ) from error


# ---------------------------------------------------------------------------
# ФУНКЦИИ ДЛЯ АДМИНИСТРАТОРА.
# Всё, что касается управления расписанием (сброс, изменение времени, просмотр
# записей), собрано здесь. Логика работы с таблицей остаётся в этом модуле —
# handlers.py только вызывает эти функции (как и в случае с бронированием).
# ---------------------------------------------------------------------------


def _validate_header_row(worksheet, header_row):
    """Проверяет, что header_row — корректный номер строки-заголовка блока слота.

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


def _clear_slot_students(worksheet, header_row):
    """Очищает всех учеников одного блока слота (колонка B от header_row+1 до
    header_row+SLOT_STEP-1). Возвращает количество удалённых записей.

    Функция НЕ сохраняет ничего отдельно — запись в Google Таблицу происходит
    сразу (каждая ячейка обновляется на сервере), поэтому вызывающий код
    просто получает количество удалённых записей.
    """
    removed = 0
    body_range = f"B{header_row + 1}:B{header_row + SLOT_STEP - 1}"
    try:
        body_cells = worksheet.range(body_range)
    except Exception:
        body_cells = []

    cells_to_clear = []
    for idx, cell in enumerate(body_cells):
        student_value = cell.value
        if student_value is not None and str(student_value).strip():
            cells_to_clear.append(header_row + 1 + idx)
            removed += 1

    if cells_to_clear:
        # Очищаем найденные ячейки (пишем пустую строку вместо ника).
        # ВАЖНО: в gspread 6.x batch_update принимает список СЛОВАРЕЙ
        # {"range": ..., "values": ...}, а не кортежей (как в 5.x).
        def _clear_cells():
            worksheet.batch_update(
                [{"range": f"B{row}", "values": [[""]]} for row in cells_to_clear]
            )
        _retry_on_network_error(_clear_cells)
    return removed


def get_all_records():
    """Админ-функция: возвращает ВСЕ записи (кто на какой слот записан).

    Формат результата — список словарей:
        [
            {"sheet": "Вторник", "date": "20.10.2026",
             "students": [{"time": "17:00 — 17:15", "name": "@ivan"}, ...]},
            ...
        ]
    Дни без единой записи в список не попадают.
    """
    with _FILE_LOCK:
        try:
            records = []
            for worksheet in _get_spreadsheet().worksheets():
                date_text = _compute_date(worksheet, worksheet.title)
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
                            "sheet": worksheet.title,
                            "date": date_text,
                            "students": booked,
                        }
                    )
            return records
        except ExcelEngineError:
            raise
        except Exception as error:
            raise ExcelEngineError(f"Ошибка при получении всех записей: {error}") from error


def reset_all_slots():
    """Админ-функция: ПОЛНЫЙ СБРОС — стирает всех учеников во ВСЕХ днях расписания.

    Возвращает общее количество удалённых записей.
    """
    with _FILE_LOCK:
        try:
            total_removed = 0
            for worksheet in _get_spreadsheet().worksheets():
                if _compute_date(worksheet, worksheet.title) is None:
                    continue  # служебный лист — не трогаем
                for slot in _parse_slots(worksheet):
                    total_removed += _clear_slot_students(
                        worksheet, slot["header_row"]
                    )
            return total_removed
        except ExcelEngineError:
            raise
        except Exception as error:
            raise ExcelEngineError(f"Ошибка при полном сбросе: {error}") from error


def reset_slot(sheet, header_row):
    """Админ-функция: сбрасывает ОДИН конкретный слот (освобождает его).

    Параметры: sheet — имя листа-дня, header_row — номер строки блока слота.
    Возвращает количество удалённых записей (0 — если слот и так был пуст,
    -1 — если блок некорректен).
    """
    with _FILE_LOCK:
        try:
            worksheet = _open_worksheet(sheet)

            if not _validate_header_row(worksheet, header_row):
                return -1  # некорректный номер блока

            return _clear_slot_students(worksheet, header_row)
        except ExcelEngineError:
            raise
        except Exception as error:
            raise ExcelEngineError(
                f"Ошибка при сбросе слота '{sheet}': {error}"
            ) from error


def update_slot_time(sheet, header_row, new_time):
    """Админ-функция: изменяет ВРЕМЯ слота (текст в колонке C строки-заголовка).

    Параметры:
        sheet      — имя листа-дня,
        header_row — номер строки блока слота,
        new_time   — новый текст времени (например "17:00 — 17:15").

    Возвращает True при успехе, False — если блок некорректен.
    ВАЖНО: в таблице время хранится формулой. Мы перезаписываем ячейку обычным
    текстом — это осознанно: так новое время гарантированно сохранится и будет
    читаться ботом и в Google Таблице.
    """
    with _FILE_LOCK:
        try:
            worksheet = _open_worksheet(sheet)

            if not _validate_header_row(worksheet, header_row):
                return False

            # Пишем новое время обычным текстом в колонку C строки-заголовка.
            def _update_time():
                worksheet.update_cell(header_row, TIME_COLUMN, new_time)
            _retry_on_network_error(_update_time)
            return True
        except ExcelEngineError:
            raise
        except Exception as error:
            raise ExcelEngineError(
                f"Ошибка при изменении времени слота '{sheet}': {error}"
            ) from error


# ---------------------------------------------------------------------------
# Поиск существующих записей пользователя (защита от двойной записи).
# ---------------------------------------------------------------------------


def find_user_bookings(nickname):
    """Ищет все слоты, в которых записан пользователь с указанным ником.

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
        try:
            results = []
            nickname_lower = nickname.strip().lower()

            for worksheet in _get_spreadsheet().worksheets():
                date_text = _compute_date(worksheet, worksheet.title)
                if date_text is None:
                    continue  # не день расписания

                for slot in _parse_slots(worksheet):
                    if isinstance(slot["students"], list):
                        for student in slot["students"]:
                            if student.strip().lower() == nickname_lower:
                                results.append({
                                    "sheet": worksheet.title,
                                    "date": date_text,
                                    "time": slot["time"],
                                    "header_row": slot["header_row"],
                                })
                                # Продолжаем сканировать — вдруг тот же ник
                                # попал в несколько слотов (ошибка админа).
            return results
        except ExcelEngineError:
            raise
        except Exception as error:
            raise ExcelEngineError(
                f"Ошибка при поиске записей пользователя: {error}"
            ) from error


def get_statistics():
    """Возвращает статистику по расписанию для администратора.

    Возвращает словарь:
        {
            "total_records": int,  # всего записей
            "total_slots": int,    # всего слотов
            "free_slots": int,     # свободных слотов
            "occupied_slots": int, # занятых слотов
            "days": [              # статистика по дням
                {
                    "sheet": "Вторник",
                    "date": "20.10.2026",
                    "total_slots": 8,
                    "free_slots": 3,
                    "occupied_slots": 5,
                    "total_students": 5
                },
                ...
            ]
        }
    """
    with _FILE_LOCK:
        try:
            total_records = 0
            total_slots = 0
            free_slots = 0
            occupied_slots = 0
            day_stats = []

            for worksheet in _get_spreadsheet().worksheets():
                date_text = _compute_date(worksheet, worksheet.title)
                if date_text is None:
                    continue  # не день расписания

                slots = _parse_slots(worksheet)
                day_free = sum(1 for s in slots if s["free"])
                day_occupied = sum(1 for s in slots if not s["free"])
                day_students = sum(len(s["students"]) for s in slots)

                day_stats.append({
                    "sheet": worksheet.title,
                    "date": date_text,
                    "total_slots": len(slots),
                    "free_slots": day_free,
                    "occupied_slots": day_occupied,
                    "total_students": day_students
                })

                total_records += day_students
                total_slots += len(slots)
                free_slots += day_free
                occupied_slots += day_occupied

            return {
                "total_records": total_records,
                "total_slots": total_slots,
                "free_slots": free_slots,
                "occupied_slots": occupied_slots,
                "days": day_stats
            }
        except ExcelEngineError:
            raise
        except Exception as error:
            raise ExcelEngineError(
                f"Ошибка при получении статистики: {error}"
            ) from error


def cancel_booking(sheet: str, header_row: int, nickname: str) -> bool:
    """Отменяет запись пользователя на конкретный слот.

    Параметры:
        sheet      — имя листа-дня,
        header_row — номер строки блока слота,
        nickname   — ник пользователя для удаления.

    Возвращает True при успехе, False если запись не найдена или некорректный слот.
    """
    with _FILE_LOCK:
        try:
            worksheet = _open_worksheet(sheet)

            if not _validate_header_row(worksheet, header_row):
                return False

            # Читаем блок слота
            body_range = f"B{header_row + 1}:B{header_row + SLOT_STEP - 1}"
            try:
                body_cells = worksheet.range(body_range)
            except Exception:
                body_cells = []

            nickname_lower = nickname.strip().lower()
            found = False
            row_to_clear = None

            for idx, cell in enumerate(body_cells):
                student_value = cell.value
                if student_value and str(student_value).strip().lower() == nickname_lower:
                    found = True
                    row_to_clear = header_row + 1 + idx
                    break

            if not found:
                return False

            # Очищаем ячейку
            def _clear_cell():
                worksheet.update_cell(row_to_clear, NAME_COLUMN, "")
            _retry_on_network_error(_clear_cell)

            # Инвалидация кэша (если будет добавлен)
            return True
        except ExcelEngineError:
            raise
        except Exception as error:
            raise ExcelEngineError(
                f"Ошибка при отмене записи '{sheet}': {error}"
            ) from error


# Блок ниже выполняется ТОЛЬКО если запустить этот файл напрямую:
#     python gsheets_engine.py
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