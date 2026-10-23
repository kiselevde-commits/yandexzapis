# postgres_engine.py — модуль для работы с базой данных PostgreSQL расписания отслушек.
#
# Этот файл является заменой sqlite_engine.py для работы с PostgreSQL.
# Все публичные функции имеют ТЕ ЖЕ САМЫЕ имена и сигнатуры, что и в sqlite_engine.py,
# поэтому handlers.py переключается на PostgreSQL бесшовно и без поломки логики.

import asyncio
import datetime
import logging
import os
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator, Optional

import asyncpg
from zoneinfo import ZoneInfo

from config import (
    DATABASE_URL,
    MAX_TASKS,
    SCHEDULE_CUTOFF_HOUR,
    WEEKDAY_NUMBERS,
)

logger = logging.getLogger(__name__)

# Исключение для совместимости с кодом handlers.py (где перехватывается ExcelEngineError)
class DatabaseError(Exception):
    """Базовое исключение при работе с базой данных расписания."""

ExcelEngineError = DatabaseError

# Глобальный пул соединений
_pool: Optional[asyncpg.Pool] = None


async def get_pool() -> asyncpg.Pool:
    """Создаёт и возвращает пул соединений к PostgreSQL."""
    global _pool
    if _pool is None:
        database_url = DATABASE_URL
        if not database_url:
            raise DatabaseError("Переменная окружения DATABASE_URL не задана")
        
        # Render использует postgres:// а asyncpg требует postgresql://
        if database_url.startswith("postgres://"):
            database_url = database_url.replace("postgres://", "postgresql://", 1)
        
        _pool = await asyncpg.create_pool(
            database_url,
            min_size=1,
            max_size=10,
            command_timeout=60,
        )
        logger.info("Пул соединений PostgreSQL создан")
    return _pool


async def close_pool() -> None:
    """Закрывает пул соединений."""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
        logger.info("Пул соединений PostgreSQL закрыт")


@asynccontextmanager
async def get_db_connection() -> AsyncGenerator[asyncpg.Connection, None]:
    """Контекстный менеджер для безопасной работы с БД в транзакции."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            yield conn


async def init_db(force: bool = False) -> None:
    """
    Создаёт таблицы и индексы в PostgreSQL, если они ещё не созданы.
    """
    try:
        async with get_db_connection() as conn:
            # Таблица дней недели (расписания)
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS days (
                    name TEXT PRIMARY KEY,
                    order_index INTEGER NOT NULL DEFAULT 0,
                    is_active INTEGER NOT NULL DEFAULT 1
                );
                """
            )

            # Таблица слотов времени
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS slots (
                    id SERIAL PRIMARY KEY,
                    day_name TEXT NOT NULL REFERENCES days(name) ON DELETE CASCADE,
                    header_row INTEGER NOT NULL,
                    time TEXT NOT NULL,
                    order_index INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(day_name, header_row)
                );
                """
            )

            # Таблица бронирований учеников
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS bookings (
                    id SERIAL PRIMARY KEY,
                    slot_id INTEGER NOT NULL UNIQUE REFERENCES slots(id) ON DELETE CASCADE,
                    nickname TEXT NOT NULL,
                    tasks INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                """
            )

            # Индексы для быстрого поиска
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_slots_day ON slots(day_name);")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_bookings_slot ON bookings(slot_id);")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_bookings_nickname ON bookings(nickname);")

        logger.info("База данных PostgreSQL успешно инициализирована")
    except Exception as error:
        raise DatabaseError(f"Ошибка при инициализации базы данных: {error}") from error


def _compute_date(day_name: str) -> str | None:
    """
    Вычисляет ближайшую дату занятия для указанного дня недели с учётом
    часового пояса Екатеринбурга (UTC+5) и часа переключения (SCHEDULE_CUTOFF_HOUR).
    Полностью идентично логике из sqlite_engine.py.
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
# Совпадают 1-в-1 с sqlite_engine.py.
# ---------------------------------------------------------------------------


async def get_available_days() -> list[dict[str, str]]:
    """Возвращает список всех активных дней с датами."""
    await init_db()
    try:
        async with get_db_connection() as conn:
            rows = await conn.fetch(
                "SELECT name FROM days WHERE is_active = 1 ORDER BY order_index, name"
            )
            days = []
            for r in rows:
                day_name = r["name"]
                date_text = _compute_date(day_name)
                if date_text is not None:
                    days.append({"sheet": day_name, "date": date_text})
            return days
    except Exception as error:
        raise DatabaseError(f"Ошибка при получении списка дней: {error}") from error


async def get_slots_for_day(sheet: str) -> list[dict[str, Any]]:
    """Возвращает все слоты указанного дня (и свободные, и занятые)."""
    await init_db()
    try:
        async with get_db_connection() as conn:
            rows = await conn.fetch(
                """
                SELECT s.header_row, s.time, b.nickname, b.tasks
                FROM slots s
                LEFT JOIN bookings b ON s.id = b.slot_id
                WHERE s.day_name = $1
                ORDER BY s.order_index, s.header_row
                """,
                sheet
            )
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


async def book_slot(sheet: str, header_row: int, nickname: str, tasks: int = 0) -> bool:
    """Записывает ученика на конкретный слот. True при успехе, False — если слот занят или не найден."""
    await init_db()
    try:
        async with get_db_connection() as conn:
            # Проверяем наличие слота
            slot_row = await conn.fetchrow(
                "SELECT id FROM slots WHERE day_name = $1 AND header_row = $2",
                sheet, header_row
            )
            if not slot_row:
                return False
            slot_id = slot_row["id"]

            # Проверяем, свободен ли слот
            existing = await conn.fetchrow("SELECT id FROM bookings WHERE slot_id = $1", slot_id)
            if existing:
                return False

            # Записываем
            valid_tasks = tasks if 1 <= tasks <= MAX_TASKS else 0
            try:
                await conn.execute(
                    "INSERT INTO bookings (slot_id, nickname, tasks) VALUES ($1, $2, $3)",
                    slot_id, nickname.strip(), valid_tasks
                )
                return True
            except asyncpg.exceptions.UniqueViolationError:
                return False
    except Exception as error:
        raise DatabaseError(f"Ошибка при бронировании слота '{sheet}': {error}") from error


async def cancel_booking(sheet: str, header_row: int, nickname: str) -> bool:
    """Отменяет запись пользователя на конкретный слот."""
    await init_db()
    try:
        async with get_db_connection() as conn:
            booking = await conn.fetchrow(
                """
                SELECT b.id, b.nickname
                FROM bookings b
                JOIN slots s ON b.slot_id = s.id
                WHERE s.day_name = $1 AND s.header_row = $2
                """,
                sheet, header_row
            )
            if not booking:
                return False

            if booking["nickname"].strip().lower() != nickname.strip().lower():
                return False

            await conn.execute("DELETE FROM bookings WHERE id = $1", booking["id"])
            return True
    except Exception as error:
        raise DatabaseError(f"Ошибка при отмене записи '{sheet}': {error}") from error


async def find_user_bookings(nickname: str) -> list[dict[str, Any]]:
    """Ищет все слоты, в которых записан пользователь с указанным ником."""
    await init_db()
    try:
        async with get_db_connection() as conn:
            rows = await conn.fetch(
                """
                SELECT s.day_name, s.header_row, s.time
                FROM bookings b
                JOIN slots s ON b.slot_id = s.id
                WHERE LOWER(TRIM(b.nickname)) = LOWER(TRIM($1))
                ORDER BY s.day_name, s.order_index, s.header_row
                """,
                nickname
            )
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


async def get_all_records() -> list[dict[str, Any]]:
    """Админ-функция: возвращает ВСЕ записи (кто на какой слот записан)."""
    await init_db()
    try:
        async with get_db_connection() as conn:
            days = await conn.fetch(
                "SELECT name FROM days WHERE is_active = 1 ORDER BY order_index, name"
            )
            records = []
            for d in days:
                day_name = d["name"]
                date_text = _compute_date(day_name)
                if not date_text:
                    continue

                booked_rows = await conn.fetch(
                    """
                    SELECT s.time, b.nickname
                    FROM bookings b
                    JOIN slots s ON b.slot_id = s.id
                    WHERE s.day_name = $1
                    ORDER BY s.order_index, s.header_row
                    """,
                    day_name
                )
                booked = [
                    {"time": r["time"], "name": r["nickname"]}
                    for r in booked_rows
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


async def reset_all_slots() -> int:
    """Админ-функция: стирает ВСЕ записи во ВСЕХ слотах."""
    await init_db()
    try:
        async with get_db_connection() as conn:
            total = await conn.fetchval("SELECT COUNT(*) FROM bookings")
            await conn.execute("DELETE FROM bookings")
            return total
    except Exception as error:
        raise DatabaseError(f"Ошибка при полном сбросе: {error}") from error


async def reset_slot(sheet: str, header_row: int) -> int:
    """Админ-функция: сбрасывает ОДИН конкретный слот."""
    await init_db()
    try:
        async with get_db_connection() as conn:
            slot = await conn.fetchrow(
                "SELECT id FROM slots WHERE day_name = $1 AND header_row = $2",
                sheet, header_row
            )
            if not slot:
                return -1

            slot_id = slot["id"]
            await conn.execute("DELETE FROM bookings WHERE slot_id = $1", slot_id)
            # asyncpg не возвращает rowcount для DELETE, используем альтернативу
            return 1  # возвращаем 1 если слот найден (для совместимости)
    except Exception as error:
        raise DatabaseError(f"Ошибка при сбросе слота '{sheet}': {error}") from error


async def update_slot_time(sheet: str, header_row: int, new_time: str) -> bool:
    """Админ-функция: изменяет ВРЕМЯ слота."""
    await init_db()
    try:
        async with get_db_connection() as conn:
            result = await conn.execute(
                "UPDATE slots SET time = $1 WHERE day_name = $2 AND header_row = $3",
                new_time.strip(), sheet, header_row
            )
            # result имеет формат "UPDATE N", извлекаем число
            return "UPDATE 1" in result
    except Exception as error:
        raise DatabaseError(f"Ошибка при изменении времени слота '{sheet}': {error}") from error


async def get_statistics() -> dict[str, Any]:
    """Возвращает статистику по расписанию для администратора."""
    await init_db()
    try:
        async with get_db_connection() as conn:
            days = await conn.fetch(
                "SELECT name FROM days WHERE is_active = 1 ORDER BY order_index, name"
            )
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

                row = await conn.fetchrow(
                    """
                    SELECT
                        COUNT(s.id) as total_s,
                        COUNT(b.id) as occ_s
                    FROM slots s
                    LEFT JOIN bookings b ON s.id = b.slot_id
                    WHERE s.day_name = $1
                    """,
                    day_name
                )
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