# migrate_gsheets_to_sqlite.py — скрипт миграции данных из Google Таблиц в SQLite.
#
# Запуск: python migrate_gsheets_to_sqlite.py
#
# Скрипт переносит все дни, слоты и существующие записи учеников
# в локальную базу данных SQLite (schedule.db).

import json
import logging
import os
import sys

from config import DB_PATH
from sqlite_engine import get_db_connection, init_db

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s]: %(message)s")
logger = logging.getLogger(__name__)

# Эталонная структура расписания (резервная копия на случай отсутствия Google API/сети)
FALLBACK_SCHEDULE = {
    "Вторник": [
        {"header_row": 6, "time": "17:00 — 17:15", "students": []},
        {"header_row": 21, "time": "17:15 — 17:30", "students": []},
        {"header_row": 36, "time": "17:30 — 17:45", "students": ["123"]},
        {"header_row": 51, "time": "17:45 — 18:00", "students": ["сепинуа"]},
        {"header_row": 66, "time": "19:00 — 19:15", "students": ["Апаа"]},
        {"header_row": 81, "time": "19:15 — 19:30", "students": []},
        {"header_row": 96, "time": "19:30 — 19:45", "students": []},
    ],
    "Среда": [
        {"header_row": 6, "time": "17:00 — 17:15", "students": ["Аоаоао"]},
        {"header_row": 21, "time": "17:15 — 17:30", "students": ["впвапв"]},
        {"header_row": 36, "time": "19:00 — 19:15", "students": ["@zhabitov"]},
        {"header_row": 51, "time": "19:15 — 19:30", "students": ["цуаврк"]},
    ],
    "Четверг": [
        {"header_row": 6, "time": "15:00 — 15:20", "students": []},
        {"header_row": 21, "time": "15:20 — 15:40", "students": []},
        {"header_row": 36, "time": "15:40 — 16:00", "students": []},
        {"header_row": 51, "time": "16:00 — 16:20", "students": []},
        {"header_row": 66, "time": "16:20 — 16:40", "students": []},
        {"header_row": 81, "time": "16:40 — 17:00", "students": []},
        {"header_row": 96, "time": "17:00 — 17:20", "students": ["ывпывапвап"]},
        {"header_row": 111, "time": "17:20 — 17:40", "students": []},
        {"header_row": 126, "time": "17:40 — 18:00", "students": []},
        {"header_row": 141, "time": "18:00 — 18:20", "students": []},
        {"header_row": 156, "time": "18:20 — 18:40", "students": []},
        {"header_row": 171, "time": "18:40 — 19:00", "students": []},
        {"header_row": 186, "time": "19:00 — 19:20", "students": []},
        {"header_row": 201, "time": "19:20 — 19:40", "students": []},
        {"header_row": 216, "time": "19:40 — 20:00", "students": []},
    ],
    "Воскресенье": [
        {"header_row": 6, "time": "16:00 — 16:20", "students": []},
        {"header_row": 21, "time": "16:20 — 16:40", "students": []},
        {"header_row": 36, "time": "16:40 — 17:00", "students": []},
        {"header_row": 51, "time": "17:00 — 17:20", "students": []},
        {"header_row": 66, "time": "17:20 — 17:40", "students": []},
        {"header_row": 81, "time": "17:40 — 18:00", "students": []},
        {"header_row": 96, "time": "18:00 — 18:20", "students": []},
        {"header_row": 111, "time": "18:20 — 18:40", "students": []},
        {"header_row": 126, "time": "18:40 — 19:00", "students": []},
    ],
}

KNOWN_TASKS = {
    ("Вторник", 36): 4,
    ("Вторник", 51): 3,
    ("Вторник", 66): 3,
    ("Среда", 6): 3,
    ("Среда", 21): 2,
    ("Среда", 36): 3,
    ("Среда", 51): 2,
    ("Четверг", 96): 2,
}


def load_from_gsheets():
    """Пытается прочитать актуальные данные напрямую из Google Таблицы."""
    import gsheets_engine
    logger.info("Попытка загрузки данных напрямую из Google Таблицы...")
    days_data = gsheets_engine.get_available_days()
    all_slots = {}
    for idx, d in enumerate(days_data, start=1):
        sheet = d["sheet"]
        all_slots[sheet] = gsheets_engine.get_slots_for_day(sheet)
    return days_data, all_slots


def migrate():
    # 1. Получаем данные
    try:
        days_data, all_slots = load_from_gsheets()
        logger.info("Успешно получены свежие данные из Google Sheets!")
    except Exception as e:
        logger.warning("Не удалось получить данные из Google Sheets (%s). Используем резервную копию расписания.", e)
        all_slots = FALLBACK_SCHEDULE
        days_data = [{"sheet": sheet} for sheet in FALLBACK_SCHEDULE.keys()]

    # 2. Инициализируем базу данных
    init_db(force=True)

    # 3. Заполняем таблицы
    with get_db_connection() as conn:
        cursor = conn.cursor()

        # Очищаем существующие данные перед миграцией
        cursor.execute("DELETE FROM bookings")
        cursor.execute("DELETE FROM slots")
        cursor.execute("DELETE FROM days")

        days_count = 0
        slots_count = 0
        bookings_count = 0

        for order_idx, day_info in enumerate(days_data, start=1):
            sheet = day_info["sheet"] if isinstance(day_info, dict) else day_info
            cursor.execute(
                "INSERT INTO days (name, order_index, is_active) VALUES (?, ?, 1)",
                (sheet, order_idx),
            )
            days_count += 1

            slots = all_slots.get(sheet, [])
            for slot_order, slot in enumerate(slots, start=1):
                header_row = slot["header_row"]
                slot_time = slot["time"]
                students = slot.get("students", [])

                cursor.execute(
                    """
                    INSERT INTO slots (day_name, header_row, time, order_index)
                    VALUES (?, ?, ?, ?)
                    """,
                    (sheet, header_row, slot_time, slot_order),
                )
                slot_id = cursor.lastrowid
                slots_count += 1

                if students:
                    student_nickname = students[0]
                    tasks = KNOWN_TASKS.get((sheet, header_row), 0)
                    cursor.execute(
                        """
                        INSERT INTO bookings (slot_id, nickname, tasks)
                        VALUES (?, ?, ?)
                        """,
                        (slot_id, student_nickname, tasks),
                    )
                    bookings_count += 1

    logger.info("Миграция успешно завершена!")
    logger.info(" - Дней: %d", days_count)
    logger.info(" - Слотов: %d", slots_count)
    logger.info(" - Активных бронирований: %d", bookings_count)


if __name__ == "__main__":
    migrate()
