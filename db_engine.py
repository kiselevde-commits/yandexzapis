# db_engine.py — единая точка входа для работы с базой данных.
#
# Автоматически выбирает движок:
#   - Если задана DATABASE_URL → использует postgres_engine (асинхронный, для Amvera/Render)
#   - Иначе → использует gsheets_engine (Google Таблицы) для работы с онлайн-таблицей
#
# ВНИМАНИЕ: все публичные функции этого модуля — АСИНХРОННЫЕ (async def).
# handlers.py должен вызывать их через await БЕЗ asyncio.to_thread().

import asyncio
import os
from config import DATABASE_URL

# Проверяем, какой движок использовать
# DATABASE_URL может быть установлен как пустая строка или строка с пробелами — считаем это "не задано"
_USE_POSTGRES = bool(DATABASE_URL and DATABASE_URL.strip())

if _USE_POSTGRES and DATABASE_URL.strip():
    # PostgreSQL — нативные асинхронные функции
    from postgres_engine import (
        DatabaseError,
        init_db,
        get_available_days,
        get_slots_for_day,
        book_slot,
        cancel_booking,
        find_user_bookings,
        get_all_records,
        reset_all_slots,
        reset_slot,
        update_slot_time,
        get_statistics,
        close_pool,
    )

    ExcelEngineError = DatabaseError

    # Реэкспортируем как есть (они уже async)
    __all__ = [
        "DatabaseError",
        "ExcelEngineError",
        "init_db",
        "get_available_days",
        "get_slots_for_day",
        "book_slot",
        "cancel_booking",
        "find_user_bookings",
        "get_all_records",
        "reset_all_slots",
        "reset_slot",
        "update_slot_time",
        "get_statistics",
        "close_pool",
    ]

else:
    # Google Таблицы — оборачиваем синхронные функции gsheets_engine в asyncio.to_thread
    from gsheets_engine import (
        ExcelEngineError,
        get_available_days as _get_available_days,
        get_slots_for_day as _get_slots_for_day,
        book_slot as _book_slot,
        cancel_booking as _cancel_booking,
        find_user_bookings as _find_user_bookings,
        get_all_records as _get_all_records,
        reset_all_slots as _reset_all_slots,
        reset_slot as _reset_slot,
        update_slot_time as _update_slot_time,
        get_statistics as _get_statistics,
    )

    ExcelEngineError = ExcelEngineError
    DatabaseError = ExcelEngineError

    async def init_db(force: bool = False) -> None:
        """Асинхронная обёртка для инициализации (Google Таблицы не требуют инициализации)."""
        pass

    async def get_available_days() -> list[dict[str, str]]:
        """Асинхронная обёртка для синхронного get_available_days."""
        return await asyncio.to_thread(_get_available_days)

    async def get_slots_for_day(sheet: str) -> list[dict[str, any]]:
        """Асинхронная обёртка для синхронного get_slots_for_day."""
        return await asyncio.to_thread(_get_slots_for_day, sheet)

    async def book_slot(sheet: str, header_row: int, nickname: str, tasks: int = 0) -> bool:
        """Асинхронная обёртка для синхронного book_slot."""
        return await asyncio.to_thread(_book_slot, sheet, header_row, nickname, tasks)

    async def cancel_booking(sheet: str, header_row: int, nickname: str) -> bool:
        """Асинхронная обёртка для синхронного cancel_booking."""
        return await asyncio.to_thread(_cancel_booking, sheet, header_row, nickname)

    async def find_user_bookings(nickname: str) -> list[dict[str, any]]:
        """Асинхронная обёртка для синхронного find_user_bookings."""
        return await asyncio.to_thread(_find_user_bookings, nickname)

    async def get_all_records() -> list[dict[str, any]]:
        """Асинхронная обёртка для синхронного get_all_records."""
        return await asyncio.to_thread(_get_all_records)

    async def reset_all_slots() -> int:
        """Асинхронная обёртка для синхронного reset_all_slots."""
        return await asyncio.to_thread(_reset_all_slots)

    async def reset_slot(sheet: str, header_row: int) -> int:
        """Асинхронная обёртка для синхронного reset_slot."""
        return await asyncio.to_thread(_reset_slot, sheet, header_row)

    async def update_slot_time(sheet: str, header_row: int, new_time: str) -> bool:
        """Асинхронная обёртка для синхронного update_slot_time."""
        return await asyncio.to_thread(_update_slot_time, sheet, header_row, new_time)

    async def get_statistics() -> dict[str, any]:
        """Асинхронная обёртка для синхронного get_statistics."""
        return await asyncio.to_thread(_get_statistics)

    async def close_pool() -> None:
        """Заглушка для совместимости."""
        pass

    __all__ = [
        "DatabaseError",
        "ExcelEngineError",
        "init_db",
        "get_available_days",
        "get_slots_for_day",
        "book_slot",
        "cancel_booking",
        "find_user_bookings",
        "get_all_records",
        "reset_all_slots",
        "reset_slot",
        "update_slot_time",
        "get_statistics",
        "close_pool",
    ]


# Логируем выбранный движок при импорте
import logging
logger = logging.getLogger(__name__)
if _USE_POSTGRES:
    logger.info("db_engine: используем PostgreSQL (DATABASE_URL задан)")
else:
    logger.info("db_engine: используем Google Таблицы (gsheets_engine)")