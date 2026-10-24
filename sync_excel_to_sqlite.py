#!/usr/bin/env python3
# sync_excel_to_sqlite.py — скрипт синхронизации Excel файла в SQLite.
# Запуск: python sync_excel_to_sqlite.py
# Что делает:
#   - читает Excel-файл через excel_engine (дни и слоты)
#   - инициализирует SQLite (создаёт таблицы если нет)
#   - заполняет таблицы days и slots актуальными данными из Excel
#   - существующие бронирования в SQLite НЕ удаляются (только слоты/дни обновляются)

import logging
import sys
from pathlib import Path

# Добавляем корень проекта в путь для импортов
sys.path.insert(0, str(Path(__file__).parent))

from excel_engine import get_available_days, get_slots_for_day, ExcelEngineError
from sqlite_engine import get_db_connection, init_db, DatabaseError

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s]: %(message)s")
logger = logging.getLogger(__name__)


def sync_excel_to_sqlite():
    """Синхронизирует данные из Excel в SQLite."""
    
    logger.info("Начинаем синхронизацию Excel -> SQLite...")
    
    # 1. Читаем дни и слоты из Excel
    try:
        days_data = get_available_days()
        logger.info(f"Найдено дней в Excel: {len(days_data)}")
        for d in days_data:
            logger.info(f"  - {d['sheet']}: {d['date']}")
    except ExcelEngineError as e:
        logger.error(f"Ошибка чтения Excel: {e}")
        return False
    
    if not days_data:
        logger.warning("В Excel не найдено ни одного дня расписания!")
        return False
    
    # 2. Инициализируем SQLite
    init_db(force=True)
    
    # 3. Заполняем таблицы в SQLite
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            
            # Обновляем таблицу days (INSERT OR REPLACE - сохраняем order_index)
            for order_idx, day_info in enumerate(days_data, start=1):
                sheet = day_info["sheet"]
                cursor.execute(
                    """
                    INSERT OR REPLACE INTO days (name, order_index, is_active) 
                    VALUES (?, ?, 1)
                    """,
                    (sheet, order_idx),
                )
            
            # Получаем существующие слоты для сравнения
            cursor.execute("SELECT day_name, header_row FROM slots")
            existing_slots = {(row["day_name"], row["header_row"]) for row in cursor.fetchall()}
            
            new_slots = set()
            slots_count = 0
            
            for day_info in days_data:
                sheet = day_info["sheet"]
                try:
                    slots = get_slots_for_day(sheet)
                    logger.info(f"  День {sheet}: {len(slots)} слотов")
                    
                    for slot_order, slot in enumerate(slots, start=1):
                        header_row = slot["header_row"]
                        slot_time = slot["time"]
                        
                        new_slots.add((sheet, header_row))
                        
                        # INSERT OR REPLACE для слотов (обновляем время если изменилось)
                        cursor.execute(
                            """
                            INSERT OR REPLACE INTO slots (day_name, header_row, time, order_index)
                            VALUES (?, ?, ?, ?)
                            """,
                            (sheet, header_row, slot_time, slot_order),
                        )
                        slots_count += 1
                        
                except ExcelEngineError as e:
                    logger.error(f"Ошибка чтения слотов для дня {sheet}: {e}")
                    continue
            
            # Удаляем слоты, которых больше нет в Excel (очистка)
            slots_to_delete = existing_slots - new_slots
            if slots_to_delete:
                for day_name, header_row in slots_to_delete:
                    cursor.execute(
                        "DELETE FROM slots WHERE day_name = ? AND header_row = ?",
                        (day_name, header_row)
                    )
                logger.info(f"Удалено устаревших слотов: {len(slots_to_delete)}")
            
            logger.info(f"Синхронизация завершена успешно!")
            logger.info(f" - Дней: {len(days_data)}")
            logger.info(f" - Слотов всего: {slots_count}")
            
            return True
            
    except DatabaseError as e:
        logger.error(f"Ошибка базы данных SQLite: {e}")
        return False
    except Exception as e:
        logger.error(f"Неожиданная ошибка: {e}")
        return False


if __name__ == "__main__":
    success = sync_excel_to_sqlite()
    sys.exit(0 if success else 1)