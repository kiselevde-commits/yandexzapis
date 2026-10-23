# sqlite_engine.py — модуль для работы с базой данных SQLite расписания отслушек.
#
# Этот файл является современной, надёжной и быстрой заменой gsheets_engine.py
# (работа с Google Таблицами) и excel_engine.py (работа с локальным .xlsx).
#
# Все публичные функции имеют ТЕ ЖЕ САМЫЕ имена и сигнатуры, что и в gsheets_engine.py,
# поэтому handlers.py переключается на SQLite бесшовно и без поломки логики.

import datetime
import logging
import os
import sqlite3
import threading
from contextlib import contextmanager
from typing import Any, Generator
from zoneinfo import ZoneInfo

from config import (
    DB_PATH,
    MAX_TASKS,
    SCHEDULE_CUTOFF_HOUR,
    WEEKDAY_NUMBERS,
)

logger = logging.getLogger(__name__)

# Исключение для совместимости с кодом handlers.py (где перехватывается ExcelEngineError)
class DatabaseError(Exception):
    """Базовое исключение при работе с базой данных расписания."""

ExcelEngineError = DatabaseError

_LOCK = threading.Lock()
_DB_INITIALIZED = False


def _get_connection() -> sqlite3.Connection:
    """Создаёт и возвращает соединение с SQLite."""
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
    return conn


@contextmanager
def get_db_connection() -> Generator[sqlite3.Connection, None, None]:
    """Контекстный менеджер для безопасной работы с БД в транзакции."""
    conn = _get_connection()
    try:
        yield conn
        conn.commit()
    except Exception as error:
        conn.rollback()
        raise DatabaseError(f"Ошибка базы данных: {error}") from error
    finally:
        conn.close()


def init_db(force: bool = False) -> None:
    """
    Создаёт таблицы и индексы в SQLite, если они ещё не созданы.
    Потокобезопасная инициализация.
    """
    global _DB_INITIALIZED
    if _DB_INITIALIZED and not force:
        return

    with _LOCK:
        if _DB_INITIALIZED and not force:
            return

        db_dir = os.path.dirname(DB_PATH)
        if db_dir and not os.path.exists(db_dir):
            os.makedirs(db_dir, exist_ok=True)

        with get_db_connection() as conn:
            cursor = conn.cursor()

            # Таблица дней недели (расписания)
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS days (
                    name TEXT PRIMARY KEY,
                    order_index INTEGER NOT NULL DEFAULT 0,
                    is_active INTEGER NOT NULL DEFAULT 1
                );
                """
            )

            # Таблица слотов времени
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS slots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    day_name TEXT NOT NULL REFERENCES days(name) ON DELETE CASCADE,
                    header_row INTEGER NOT NULL,
                    time TEXT NOT NULL,
                    order_index INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(day_name, header_row)
                );
                """
            )

            # Таблица бронирований учеников
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS bookings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    slot_id INTEGER NOT NULL UNIQUE REFERENCES slots(id) ON DELETE CASCADE,
                    nickname TEXT NOT NULL,
                    tasks INTEGER DEFAULT 0,
                    created_at TEXT DEFAULT (datetime('now', 'localtime'))
                );
                """
            )

            # Индексы для быстрого поиска
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_slots_day ON slots(day_name);")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_bookings_slot ON bookings(slot_id);")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_bookings_nickname ON bookings(nickname COLLATE NOCASE);")

        _DB_INITIALIZED = True
        logger.info("База данных SQLite успешно инициализирована: %s", DB_PATH)


def _compute_date(day_name: str) -> str | None:
    """
    Вычисляет ближайшую дату занятия для указанного дня недели с учётом
    часового пояса Екатеринбурга (UTC+5) и часа переключения (SCHEDULE_CUTOFF_HOUR).
    Полностью идентично логике из gsheets_engine.py.
    """
    weekday_number = WEEKDAY_NUMBERS.get(day_name.strip())
    if weekday_number is None:
        return None

    now = datetime.datetime.now(ZoneInfo("Asia/Yekaterinburg"))
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    days_until = (weekday_number - 1 - today.weekday()) % 7
    base_date = today + datetime.timedelta(days=days_until)
    if base_date < today:
        base_date += datetime.timedelta(days=7)
    if now.hour >= SCHEDULE_CUTOFF_HOUR and base_date <= today:
        base_date += datetime.timedelta(days=7)
    return base_date.strftime("%d.%m.%Y")


# ---------------------------------------------------------------------------
# Основные функции (публичный API).
# Совпадают 1-в-1 с gsheets_engine.py.
# ---------------------------------------------------------------------------


def get_available_days() -> list[dict[str, str]]:
    """Возвращает список всех активных дней с датами."""
    init_db()
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT name FROM days WHERE is_active = 1 ORDER BY order_index, rowid"
            )
            rows = cursor.fetchall()
            days = []
            for r in rows:
                day_name = r["name"]
                date_text = _compute_date(day_name)
                if date_text is not None:
                    days.append({"sheet": day_name, "date": date_text})
            return days
    except Exception as error:
        raise DatabaseError(f"Ошибка при получении списка дней: {error}") from error


def get_slots_for_day(sheet: str) -> list[dict[str, Any]]:
    """Возвращает все слоты указанного дня (и свободные, и занятые)."""
    init_db()
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT s.header_row, s.time, b.nickname, b.tasks
                FROM slots s
                LEFT JOIN bookings b ON s.id = b.slot_id
                WHERE s.day_name = ?
                ORDER BY s.order_index, s.header_row
                """,
                (sheet,)
            )
            rows = cursor.fetchall()
            slots = []
            for r in rows:
                students = [r["nickname"]] if r["nickname"] else []
                slots.append({
                    "time": r["time"],
                    "header_row": r["header_row"],
                    "students": students,
                    "free": len(students) == 0,
                })
            return slots
    except Exception as error:
        raise DatabaseError(f"Ошибка при получении слотов для '{sheet}': {error}") from error


def book_slot(sheet: str, header_row: int, nickname: str, tasks: int = 0) -> bool:
    """Записывает ученика на конкретный слот. True при успехе, False — если слот занят или не найден."""
    init_db()
    with _LOCK:
        try:
            with get_db_connection() as conn:
                cursor = conn.cursor()

                # Проверяем наличие слота
                cursor.execute(
                    "SELECT id FROM slots WHERE day_name = ? AND header_row = ?",
                    (sheet, header_row)
                )
                slot_row = cursor.fetchone()
                if not slot_row:
                    return False
                slot_id = slot_row["id"]

                # Проверяем, свободен ли слот
                cursor.execute("SELECT id FROM bookings WHERE slot_id = ?", (slot_id,))
                if cursor.fetchone():
                    return False

                # Записываем
                valid_tasks = tasks if 1 <= tasks <= MAX_TASKS else 0
                try:
                    cursor.execute(
                        "INSERT INTO bookings (slot_id, nickname, tasks) VALUES (?, ?, ?)",
                        (slot_id, nickname.strip(), valid_tasks)
                    )
                    return True
                except sqlite3.IntegrityError:
                    return False
        except Exception as error:
            raise DatabaseError(f"Ошибка при бронировании слота '{sheet}': {error}") from error


def cancel_booking(sheet: str, header_row: int, nickname: str) -> bool:
    """Отменяет запись пользователя на конкретный слот."""
    init_db()
    with _LOCK:
        try:
            with get_db_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    SELECT b.id, b.nickname
                    FROM bookings b
                    JOIN slots s ON b.slot_id = s.id
                    WHERE s.day_name = ? AND s.header_row = ?
                    """,
                    (sheet, header_row)
                )
                booking = cursor.fetchone()
                if not booking:
                    return False

                if booking["nickname"].strip().lower() != nickname.strip().lower():
                    return False

                cursor.execute("DELETE FROM bookings WHERE id = ?", (booking["id"],))
                return True
        except Exception as error:
            raise DatabaseError(f"Ошибка при отмене записи '{sheet}': {error}") from error


def find_user_bookings(nickname: str) -> list[dict[str, Any]]:
    """Ищет все слоты, в которых записан пользователь с указанным ником."""
    init_db()
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT s.day_name, s.header_row, s.time
                FROM bookings b
                JOIN slots s ON b.slot_id = s.id
                WHERE LOWER(TRIM(b.nickname)) = LOWER(TRIM(?))
                ORDER BY s.day_name, s.order_index, s.header_row
                """,
                (nickname,)
            )
            rows = cursor.fetchall()
            results = []
            for r in rows:
                day_name = r["day_name"]
                date_text = _compute_date(day_name)
                results.append({
                    "sheet": day_name,
                    "date": date_text or "",
                    "time": r["time"],
                    "header_row": r["header_row"],
                })
            return results
    except Exception as error:
        raise DatabaseError(f"Ошибка при поиске записей пользователя: {error}") from error


def get_all_records() -> list[dict[str, Any]]:
    """Админ-функция: возвращает ВСЕ записи (кто на какой слот записан)."""
    init_db()
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT name FROM days WHERE is_active = 1 ORDER BY order_index, rowid"
            )
            days = cursor.fetchall()
            records = []
            for d in days:
                day_name = d["name"]
                date_text = _compute_date(day_name)
                if not date_text:
                    continue

                cursor.execute(
                    """
                    SELECT s.time, b.nickname
                    FROM bookings b
                    JOIN slots s ON b.slot_id = s.id
                    WHERE s.day_name = ?
                    ORDER BY s.order_index, s.header_row
                    """,
                    (day_name,)
                )
                booked = [
                    {"time": r["time"], "name": r["nickname"]}
                    for r in cursor.fetchall()
                ]
                if booked:
                    records.append({
                        "sheet": day_name,
                        "date": date_text,
                        "students": booked,
                    })
            return records
    except Exception as error:
        raise DatabaseError(f"Ошибка при получении всех записей: {error}") from error


def reset_all_slots() -> int:
    """Админ-функция: стирает ВСЕ записи во ВСЕХ слотах."""
    init_db()
    with _LOCK:
        try:
            with get_db_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT COUNT(*) as cnt FROM bookings")
                total = cursor.fetchone()["cnt"]
                cursor.execute("DELETE FROM bookings")
                return total
        except Exception as error:
            raise DatabaseError(f"Ошибка при полном сбросе: {error}") from error


def reset_slot(sheet: str, header_row: int) -> int:
    """Админ-функция: сбрасывает ОДИН конкретный слот."""
    init_db()
    with _LOCK:
        try:
            with get_db_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT id FROM slots WHERE day_name = ? AND header_row = ?",
                    (sheet, header_row)
                )
                slot = cursor.fetchone()
                if not slot:
                    return -1

                slot_id = slot["id"]
                cursor.execute("DELETE FROM bookings WHERE slot_id = ?", (slot_id,))
                return cursor.rowcount
        except Exception as error:
            raise DatabaseError(f"Ошибка при сбросе слота '{sheet}': {error}") from error


def update_slot_time(sheet: str, header_row: int, new_time: str) -> bool:
    """Админ-функция: изменяет ВРЕМЯ слота."""
    init_db()
    with _LOCK:
        try:
            with get_db_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "UPDATE slots SET time = ? WHERE day_name = ? AND header_row = ?",
                    (new_time.strip(), sheet, header_row)
                )
                return cursor.rowcount > 0
        except Exception as error:
            raise DatabaseError(f"Ошибка при изменении времени слота '{sheet}': {error}") from error


def get_statistics() -> dict[str, Any]:
    """Возвращает статистику по расписанию для администратора."""
    init_db()
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT name FROM days WHERE is_active = 1 ORDER BY order_index, rowid"
            )
            days = cursor.fetchall()
            day_stats = []
            total_records = 0
            total_slots = 0
            free_slots = 0
            occupied_slots = 0

            for d in days:
                day_name = d["name"]
                date_text = _compute_date(day_name)
                if not date_text:
                    continue

                cursor.execute(
                    """
                    SELECT
                        COUNT(s.id) as total_s,
                        COUNT(b.id) as occ_s
                    FROM slots s
                    LEFT JOIN bookings b ON s.id = b.slot_id
                    WHERE s.day_name = ?
                    """,
                    (day_name,)
                )
                row = cursor.fetchone()
                d_total = row["total_s"]
                d_occupied = row["occ_s"]
                d_free = d_total - d_occupied

                day_stats.append({
                    "sheet": day_name,
                    "date": date_text,
                    "total_slots": d_total,
                    "free_slots": d_free,
                    "occupied_slots": d_occupied,
                    "total_students": d_occupied,
                })

                total_records += d_occupied
                total_slots += d_total
                free_slots += d_free
                occupied_slots += d_occupied

            return {
                "total_records": total_records,
                "total_slots": total_slots,
                "free_slots": free_slots,
                "occupied_slots": occupied_slots,
                "days": day_stats,
            }
    except Exception as error:
        raise DatabaseError(f"Ошибка при получении статистики: {error}") from error
