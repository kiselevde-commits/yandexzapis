# handlers.py — модуль обработки всех событий бота: команд, состояний (FSM) и нажатий кнопок.
#
# Здесь «мозг» бота: этот файл соединяет postgres_engine.py (данные), keyboards.py (кнопки)
# и aiogram (приём сообщений и ответы).
#
# Поток работы бота:
#   /start → список ДНЕЙ → выбор дня → список СЛОТОВ этого дня →
#   выбор свободного слота → ввод ника → запись в базу PostgreSQL → возврат к списку дней.

import asyncio
import json     # для сохранения/загрузки списка администраторов
import logging
import os       # для проверки существования файла

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

logger = logging.getLogger(__name__)

# Настройки администраторов (кто имеет доступ к админ-меню).
from commands import set_admin_commands_for_chat
from config import ADMIN_ALLOW_CHAT_ADMINS, ADMIN_IDS, ADMIN_IDS_FILE
from db_engine import (
    DatabaseError,
    ExcelEngineError,
    book_slot,
    cancel_booking,
    find_user_bookings,
    get_all_records,
    get_available_days,
    get_slots_for_day,
    get_statistics,
    init_db,
    reset_all_slots,
    reset_slot,
    update_slot_time,
)
from keyboards import (
    ADMIN_ADD,
    ADMIN_CANCEL,
    ADMIN_CLOSE,
    ADMIN_CONFIRM_ALL,
    ADMIN_EDIT_CHOOSE,
    ADMIN_PREFIX,
    ADMIN_RESET_ALL,
    ADMIN_RESET_CHOOSE,
    ADMIN_STATS,
    ADMIN_VIEW_RECORDS,
    BACK_TO_DAYS,
    CANCEL_PREFIX,
    DAY_PREFIX,
    EDIT_TASKS_PREFIX,
    OCCUPIED_DATA,
    SLOT_PREFIX,
    TASKS_CANCEL,
    TASKS_PREFIX,
    build_admin_confirm_reset_keyboard,
    build_admin_days_keyboard,
    build_admin_menu_keyboard,
    build_admin_slots_keyboard,
    build_days_keyboard,
    build_my_bookings_keyboard,
    build_slots_keyboard,
    build_tasks_keyboard,
)

# Создаём роутер — сюда регистрируем все обработчики этого модуля.
router = Router()

# Текст, который показываем пользователю, если не удалось обратиться к Google Таблице.
EXCEL_ERROR_TEXT = (
    "⚠️ Не удалось обратиться к расписанию (проблема с подключением к Google Таблицам).\n"
    "Попробуй чуть позже или напиши администратору."
)


# ---------------------------------------------------------------------------
# Безопасные асинхронные обёртки функций БД.
# db_engine.py уже предоставляет асинхронные функции, поэтому просто вызываем их.
# ---------------------------------------------------------------------------


async def _get_days_safe() -> list[dict]:
    return await get_available_days()


async def _get_slots_safe(sheet: str) -> list[dict]:
    return await get_slots_for_day(sheet)


async def _book_slot_safe(sheet: str, header_row: int, nickname: str, tasks: int = 0) -> bool:
    return await book_slot(sheet, header_row, nickname, tasks)


async def _find_user_bookings_safe(nickname: str) -> list[dict]:
    """Асинхронная обёртка для find_user_bookings."""
    return await find_user_bookings(nickname)


# Асинхронные обёртки админ-функций.
async def _get_records_safe() -> list[dict]:
    return await get_all_records()


async def _reset_all_safe() -> int:
    return await reset_all_slots()


async def _reset_slot_safe(sheet: str, header_row: int) -> int:
    return await reset_slot(sheet, header_row)


async def _update_time_safe(sheet: str, header_row: int, new_time: str) -> bool:
    return await update_slot_time(sheet, header_row, new_time)


async def _cancel_booking_safe(sheet: str, header_row: int, nickname: str) -> bool:
    """Асинхронная обёртка для cancel_booking."""
    return await cancel_booking(sheet, header_row, nickname)


async def _get_statistics_safe():
    """Асинхронная обёртка для get_statistics."""
    return await get_statistics()

# ---------------------------------------------------------------------------
# Проверка прав администратора.
# ---------------------------------------------------------------------------

async def _is_admin(user_id: int, chat_id: int, bot) -> bool:
    """
    Возвращает True, если пользователь — администратор бота.

    Проверяем два условия:
      1) ID пользователя есть в списке ADMIN_IDS из config.py (главные админы);
      2) если включён ADMIN_ALLOW_CHAT_ADMINS — пользователь является
         администратором ТЕКУЩЕГО ЧАТА (актуально для групп).
    """
    if user_id in ADMIN_IDS:
        return True

    if ADMIN_ALLOW_CHAT_ADMINS:
        try:
            member = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
            # Статус "administrator" или "creator" = админ/владелец чата.
            if member.status in ("administrator", "creator"):
                return True
        except Exception:
            # Не удалось проверить (например, бот не в чате) — считаем, что не админ.
            return False

    return False


# ---------------------------------------------------------------------------
# Работа с JSON-файлом динамических администраторов.
# ---------------------------------------------------------------------------

def _load_dynamic_admins() -> list[int]:
    """
    Загружает список Telegram ID администраторов из JSON-файла.
    Если файла нет — возвращает пустой список.
    """
    if not os.path.exists(ADMIN_IDS_FILE):
        return []
    try:
        with open(ADMIN_IDS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _save_dynamic_admins(admin_ids: list[int]) -> None:
    """Сохраняет список Telegram ID администраторов в JSON-файл."""
    with open(ADMIN_IDS_FILE, "w", encoding="utf-8") as f:
        json.dump(admin_ids, f, ensure_ascii=False, indent=2)


def _is_dynamic_admin(user_id: int) -> bool:
    """Проверяет, есть ли user_id в списке динамических администраторов."""
    return user_id in _load_dynamic_admins()


async def _is_admin_full(user_id: int, chat_id: int, bot) -> bool:
    """
    Расширенная проверка прав администратора.
    Пользователь считается администратором, если он:
      1) в списке ADMIN_IDS из config.py (статические админы);
      2) в динамическом списке (JSON-файл);
      3) администратор чата (если ADMIN_ALLOW_CHAT_ADMINS=True).
    """
    # Статические админы из config.py
    if user_id in ADMIN_IDS:
        return True
    # Динамические админы из JSON-файла
    if _is_dynamic_admin(user_id):
        return True
    # Администраторы чата (группы)
    if ADMIN_ALLOW_CHAT_ADMINS:
        try:
            member = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
            if member.status in ("administrator", "creator"):
                return True
        except Exception:
            return False

    return False


# FSM-состояния: на каком шаге сейчас пользователь.
class BookingStates(StatesGroup):
    # Пользователь выбрал слот, бот ждёт ввода ника.
    waiting_for_nickname = State()
    # Пользователь ввёл ник, бот ждёт выбора количества задач.
    waiting_for_tasks = State()
    # Пользователь ищет свои записи через /mybookings.
    waiting_for_search_nickname = State()


# FSM-состояния админ-меню.
class AdminStates(StatesGroup):
    # Админ выбрал слот для изменения времени и вводит новое время.
    waiting_for_new_time = State()
    # Админ вводит Telegram ID нового администратора.
    waiting_for_new_admin_id = State()


# ---------------------------------------------------------------------------
# Вспомогательные функции.
# ---------------------------------------------------------------------------


def _safe_parse_callback(callback_data: str, expected_parts: int) -> list[str] | None:
    """
    Безопасно парсит callback_data, возвращает части или None при ошибке.

    Параметры:
        callback_data    — строка callback_data из кнопки
        expected_parts   — минимальное количество частей после split(":")

    Возвращает список частей или None при ошибке парсинга.
    """
    try:
        parts = callback_data.split(":")
        if len(parts) < expected_parts:
            return None
        return parts
    except Exception:
        return None


def _validate_time_format(time_str: str) -> bool:
    """
    Проверяет формат времени: ЧЧ:ММ — ЧЧ:ММ

    Параметры:
        time_str — строка с временем

    Возвращает True если формат корректен, иначе False.
    """
    import re

    pattern = r'^\d{1,2}:\d{2}\s*—\s*\d{1,2}:\d{2}$'
    if not re.match(pattern, time_str):
        return False

    # Парсим и проверяем корректность времени
    parts = time_str.split('—')
    for part in parts:
        try:
            hours, minutes = map(int, part.strip().split(':'))
            if hours < 0 or hours > 23 or minutes < 0 or minutes > 59:
                return False
        except (ValueError, IndexError):
            return False

    return True


def _is_not_command(message: Message) -> bool:
    """
    Фильтр для FSM-обработчиков текста: пропускает только НЕ-команды
    (сообщение, не начинающееся с «/»).

    Зачем это нужно: обработчики состояний (ввод ника, ввод нового времени,
    ввод ID админа) зарегистрированы в этом файле. Без такого фильтра команда
    /admin — или /start, /book, /help, /mybookings, — отправленная из состояния,
    перехватывалась бы как обычный текст (например, «/admin» сохранялся как ник
    ученика). Теперь команды «проскакивают» эти обработчики и доходят до своих
    Command-handlers независимо от порядка регистрации.
    """
    return not (message.text or "").startswith("/")


# ---------------------------------------------------------------------------
# Вспомогательная функция: показать список дней.
# ---------------------------------------------------------------------------

async def _send_days(target, edit: bool = False, state: FSMContext | None = None) -> None:
    """
    Показывает список дней (листов-дней недели) с датами.

    target — Message или CallbackQuery.message. edit=True → редактируем существующее сообщение.
    """
    try:
        days = await _get_days_safe()
    except ExcelEngineError as e:
        logger.error(f"Ошибка при получении дней: {e}")
        if edit:
            await target.edit_text(EXCEL_ERROR_TEXT)
        else:
            await target.answer(EXCEL_ERROR_TEXT)
        if state is not None:
            await state.clear()
        return
    except Exception as e:
        logger.critical(f"Неожиданная ошибка при получении дней: {e}", exc_info=True)
        if edit:
            await target.edit_text("⚠️ Произошла непредвиденная ошибка. Попробуй позже.")
        else:
            await target.answer("⚠️ Произошла непредвиденная ошибка. Попробуй позже.")
        if state is not None:
            await state.clear()
        return

    if not days:
        text = "😔 В расписании пока нет доступных дней."
        keyboard = None
    else:
        text = "📅 Выбери день занятия:"
        keyboard = build_days_keyboard(days)

    if edit:
        await target.edit_text(text, reply_markup=keyboard)
    else:
        await target.answer(text, reply_markup=keyboard)

    if state is not None:
        await state.clear()


# ---------------------------------------------------------------------------
# Команды.
# ---------------------------------------------------------------------------

@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext) -> None:
    """Обработчик /start: приветствие и показ списка дней."""
    logger.info(f"Пользователь {message.from_user.id} ({message.from_user.username}) запустил /start")
    await state.clear()
    await message.answer(
        "👋 Привет! Я помогу записаться на отлушку по олимпиадной математике.\n"
        "Выбери день, а затем удобное время из свободных слотов.\n\n"
        "ℹ️ Нажми /help для справки по командам."
    )
    await _send_days(message)


@router.message(Command("book"))
async def cmd_book(message: Message) -> None:
    """Обработчик /book: сразу показывает список дней для записи."""
    logger.info(f"Пользователь {message.from_user.id} ({message.from_user.username}) запустил /book")
    await _send_days(message)


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    """Обработчик /help: показывает справку по командам."""
    logger.info(f"Пользователь {message.from_user.id} ({message.from_user.username}) запустил /help")
    help_text = (
        "📚 *Справка по командам:*\n\n"
        "*Для учеников:*\n"
        "/start — начать запись на отлушку\n"
        "/book — быстрая запись на слот\n"
        "/mybookings — показать мои записи\n"
        "/help — эта справка\n\n"
        "*Для администраторов:*\n"
        "/admin — меню администратора\n\n"
        "📝 *Как записаться:*\n"
        "1. Нажми /start\n"
        "2. Выбери день\n"
        "3. Выбери свободное время\n"
        "4. Введи свой ник\n"
        "5. Укажи количество задач (1-4)"
    )
    await message.answer(help_text, parse_mode="Markdown")


@router.message(Command("mybookings"))
async def cmd_mybookings(message: Message, state: FSMContext) -> None:
    """Обработчик /mybookings: показывает записи пользователя."""
    logger.info(f"Пользователь {message.from_user.id} ({message.from_user.username}) запустил /mybookings")
    await state.clear()

    # Просим ввести ник для поиска записей
    await message.answer(
        "📋 Введи свой ник или имя, чтобы найти свои записи:"
    )
    await state.set_state(BookingStates.waiting_for_search_nickname)


@router.message(BookingStates.waiting_for_search_nickname, _is_not_command)
async def on_mybookings_nickname(message: Message, state: FSMContext) -> None:
    """Принимает ник и показывает записи пользователя."""
    nickname = (message.text or "").strip()
    if not nickname:
        await message.answer("Пожалуйста, введи ник текстом (например: @ivan).")
        return

    logger.info(f"Пользователь {message.from_user.id} ищет записи для ника: {nickname}")

    try:
        bookings = await _find_user_bookings_safe(nickname)
    except ExcelEngineError as e:
        logger.error(f"Ошибка при поиске записей для {nickname}: {e}")
        await message.answer(EXCEL_ERROR_TEXT)
        await state.clear()
        return
    except Exception as e:
        logger.critical(f"Неожиданная ошибка при поиске записей для {nickname}: {e}", exc_info=True)
        await message.answer("⚠️ Произошла ошибка. Попробуй позже.")
        await state.clear()
        return

    if not bookings:
        await message.answer(
            f"ℹ️ Для ника «{nickname}» записей не найдено.\n\n"
            "Хочешь записаться? Нажми /start"
        )
        await state.clear()
        return

    # Сохраняем ник для последующей отмены
    await state.update_data(nickname=nickname)

    lines = [
        f"📋 **Записи для {nickname}:**\n",
    ]
    for booking in bookings:
        lines.append(f"  • {booking['sheet']} ({booking['date']}), {booking['time']}")
    lines.append("")

    await message.answer("\n".join(lines), parse_mode="Markdown")
    await message.answer(
        "Выбери действие:",
        reply_markup=build_my_bookings_keyboard(bookings)
    )


@router.callback_query(F.data.startswith(f"{CANCEL_PREFIX}:"))
async def on_cancel_booking(callback: CallbackQuery, state: FSMContext) -> None:
    """Обрабатывает отмену записи."""
    payload = callback.data.split(":", 1)[1]
    sheet, header_row_text, time = payload.split("|", 2)
    header_row = int(header_row_text)

    data = await state.get_data()
    nickname = data.get("nickname")

    if not nickname:
        await callback.answer("⚠️ Сессия истекла. Начни заново с /mybookings", show_alert=True)
        return

    logger.info(f"Пользователь {callback.from_user.id} отменяет запись: {sheet} {time}")

    try:
        success = await _cancel_booking_safe(sheet, header_row, nickname)
    except ExcelEngineError as e:
        logger.error(f"Ошибка при отмене записи: {e}")
        await callback.answer(EXCEL_ERROR_TEXT, show_alert=True)
        return
    except Exception as e:
        logger.critical(f"Неожиданная ошибка при отмене записи: {e}", exc_info=True)
        await callback.answer("⚠️ Произошла ошибка. Попробуй позже.", show_alert=True)
        return

    if success:
        await callback.message.edit_text(
            f"✅ Запись на «{sheet}», {time} отменена.\n\n"
            "Мои записи:"
        )
        # Показать обновлённый список
        try:
            bookings = await _find_user_bookings_safe(nickname)
            if bookings:
                lines = [f"📋 **Записи для {nickname}:**\n"]
                for booking in bookings:
                    lines.append(f"  • {booking['sheet']} ({booking['date']}), {booking['time']}")
                lines.append("")
                await callback.message.answer("\n".join(lines), parse_mode="Markdown")
                await callback.message.answer(
                    "Выбери действие:",
                    reply_markup=build_my_bookings_keyboard(bookings)
                )
            else:
                await callback.message.answer(
                    f"ℹ️ Все записи для {nickname} отменены.\n\n"
                    "Хочешь записаться? Нажми /start"
                )
        except Exception as e:
            logger.error(f"Ошибка при обновлении списка записей: {e}")
            await callback.message.answer("⚠️ Не удалось обновить список записей.")
    else:
        await callback.answer("⚠️ Не удалось отменить запись. Возможно, запись уже была удалена.", show_alert=True)

    await callback.answer()


# ---------------------------------------------------------------------------
# Обработка нажатий на инлайн-кнопки.
# ---------------------------------------------------------------------------

@router.callback_query(F.data == BACK_TO_DAYS)
async def on_back_to_days(callback: CallbackQuery, state: FSMContext) -> None:
    """Кнопка «⬅️ Назад»: возвращаемся к списку дней."""
    await state.clear()
    await _send_days(callback.message, edit=True)
    await callback.answer()


@router.callback_query(F.data == OCCUPIED_DATA)
async def on_occupied(callback: CallbackQuery) -> None:
    """Нажата кнопка ЗАНЯТОГО слота: объясняем, что место занято."""
    await callback.answer(
        "🔒 Этот слот уже занят. Выбери другой свободный слот.",
        show_alert=True,
    )


@router.callback_query(F.data.startswith(f"{DAY_PREFIX}:"))
async def on_day_chosen(callback: CallbackQuery) -> None:
    """
    Нажата кнопка дня. Показываем все слоты этого дня.
    callback_data имеет вид "day:Вторник".
    """
    parts = _safe_parse_callback(callback.data, 2)
    if not parts:
        await callback.answer("⚠️ Сообщение устарело. Начните заново.", show_alert=True)
        return

    sheet = parts[1]  # имя листа-дня
    logger.info(f"Пользователь {callback.from_user.id} выбрал день: {sheet}")

    try:
        slots = await _get_slots_safe(sheet)
    except ExcelEngineError as e:
        logger.error(f"Ошибка при получении слотов для {sheet}: {e}")
        await callback.answer(EXCEL_ERROR_TEXT, show_alert=True)
        return
    except Exception as e:
        logger.critical(f"Неожиданная ошибка при получении слотов для {sheet}: {e}", exc_info=True)
        await callback.answer("⚠️ Произошла ошибка. Попробуй позже.", show_alert=True)
        return

    if not slots:
        await callback.answer("В этот день нет слотов.", show_alert=True)
        return

    await callback.message.edit_text(
        f"🕒 Слоты на день «{sheet}»:\n"
        "✅ — свободно (можно записаться), 🔒 — занято.\nВыбери удобное время:",
        reply_markup=build_slots_keyboard(sheet, slots),
    )
    await callback.answer()


@router.callback_query(F.data.startswith(f"{SLOT_PREFIX}:"))
async def on_slot_chosen(callback: CallbackQuery, state: FSMContext) -> None:
    """
    Нажата кнопка свободного слота. Запоминаем выбор и просим ввести ник.
    callback_data имеет вид "slot:Вторник|6|17:00 — 17:15"
    (лист | номер строки блока | текст времени для показа).
    """
    parts = _safe_parse_callback(callback.data, 2)
    if not parts:
        await callback.answer("⚠️ Сообщение устарело. Начните заново.", show_alert=True)
        return

    payload = parts[1]  # "Вторник|6|17:00 — 17:15"
    try:
        sheet, header_row_text, time = payload.split("|", 2)
        header_row = int(header_row_text)
    except (ValueError, IndexError):
        await callback.answer("⚠️ Некорректные данные кнопки. Начните заново.", show_alert=True)
        return

    # Сохраняем выбор в FSM, чтобы использовать после ввода ника.
    await state.update_data(sheet=sheet, header_row=header_row, time=time)
    await state.set_state(BookingStates.waiting_for_nickname)

    await callback.message.edit_text(
        f"✍️ Ты выбрал день «{sheet}», время {time}.\n"
        "Теперь отправь сообщением свой ник или имя (например: @ivan)."
    )
    await callback.answer()


# ---------------------------------------------------------------------------
# Ввод ника (текстовое сообщение в состоянии waiting_for_nickname).
# ---------------------------------------------------------------------------

@router.message(BookingStates.waiting_for_nickname, _is_not_command)
async def on_nickname_received(message: Message, state: FSMContext) -> None:
    """Принимает ник, проверяет двойную запись и просит указать количество задач."""
    data = await state.get_data()
    sheet = data.get("sheet")
    header_row = data.get("header_row")
    time = data.get("time")

    nickname = (message.text or "").strip()
    if not nickname:
        logger.warning(f"Пользователь {message.from_user.id} отправил пустой ник")
        await message.answer("Пожалуйста, введи ник текстом (например: @ivan).")
        return

    if len(nickname) > 50:
        logger.warning(f"Пользователь {message.from_user.id} отправил слишком длинный ник: {len(nickname)} символов")
        await message.answer("⚠️ Ник слишком длинный (максимум 50 символов). Попробуй короче.")
        return

    # Сохраняем ник в состоянии.
    await state.update_data(nickname=nickname)
    logger.info(f"Пользователь {message.from_user.id} ввёл ник: {nickname}")

    # Сразу просим указать количество задач.
    # (Проверка «уже записан» намеренно убрана по требованию заказчика:
    #  после ввода ника бот не должен возвращать пользователя к выбору дня.)
    await state.set_state(BookingStates.waiting_for_tasks)

    await message.answer(
        f"✅ Ник «{nickname}» принят.\n\n"
        f"Сколько задач ты готов(а) готов сдать на отслушке "
        f"«{sheet}», {time}?",
        reply_markup=build_tasks_keyboard(),
    )


# ---------------------------------------------------------------------------
# Выбор количества задач (нажатие кнопки 1..4).
# ---------------------------------------------------------------------------

@router.callback_query(F.data.startswith(f"{TASKS_PREFIX}:"))
async def on_tasks_chosen(callback: CallbackQuery, state: FSMContext) -> None:
    """
    Нажата кнопка выбора количества задач. Завершаем бронирование.
    callback_data: "tasks:3" (количество задач).
    """
    tasks_text = callback.data.split(":", 1)[1]

    if tasks_text == "cancel":
        await state.clear()
        await callback.message.edit_text("❌ Выбор отменён. Возврат к дням:")
        await _send_days(callback.message, edit=True)
        await callback.answer()
        return

    try:
        tasks = int(tasks_text)
    except (ValueError, TypeError):
        await callback.answer("Некорректное значение.", show_alert=True)
        return

    if tasks < 1 or tasks > 4:
        await callback.answer("Количество задач должно быть от 1 до 4.", show_alert=True)
        return

    data = await state.get_data()
    sheet = data.get("sheet")
    header_row = data.get("header_row")
    time = data.get("time")
    nickname = data.get("nickname")

    # Бронируем слот с указанием количества задач.
    try:
        success = await _book_slot_safe(sheet, header_row, nickname, tasks)
    except ExcelEngineError:
        await callback.message.edit_text(EXCEL_ERROR_TEXT)
        await state.clear()
        await callback.answer()
        return

    if success:
        # Запись успешно создана: показываем итог и НЕ возвращаем пользователя
        # к экрану «Выбери день занятия» (раньше здесь был вызов _send_days,
        # из-за которого бот снова запрашивал выбор дня).
        await callback.message.edit_text(
            f"✅ Готово! Ты записан(а) на «{sheet}», {time}.\n"
            f"   Указано задач: {tasks}.\n\n"
            "Если захочешь записаться ещё — нажми /start.\n"
            "Если что-то изменится — напиши администратору."
        )
    else:
        # Слот заняли, пока пользователь вводил данные. Сообщаем об этом и
        # предлагаем вернуться к выбору через /start (без показа дней сразу).
        await callback.message.edit_text(
            "⚠️ Увы, этот слот только что заняли.\n\n"
            "Чтобы выбрать другой свободный слот, нажми /start."
        )

    await state.clear()
    await callback.answer()



# ===========================================================================
# АДМИН-МЕНЮ.
# Все обработчики ниже проверяют права администратора (см. _is_admin).
# ===========================================================================


@router.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext) -> None:
    """
    Обработчик /admin: открывает меню администратора.

    Доступ только для администраторов (ID из config.ADMIN_IDS или админ чата).
    """
    is_admin = await _is_admin_full(
        message.from_user.id, message.chat.id, message.bot
    )
    if not is_admin:
        await message.answer("⛔ У тебя нет доступа к админ-меню.")
        return

    await state.clear()
    await message.answer(
        "🛠 Меню администратора.\nВыбери действие:",
        reply_markup=build_admin_menu_keyboard(),
    )


@router.callback_query(F.data == ADMIN_CLOSE)
async def on_admin_close(callback: CallbackQuery, state: FSMContext) -> None:
    """Кнопка «❌ Закрыть»: убираем админ-меню."""
    await state.clear()
    await callback.message.edit_text("Меню администратора закрыто.")
    await callback.answer()


@router.callback_query(F.data == ADMIN_CANCEL)
async def on_admin_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    """Кнопка «⬅️ Отмена/Назад»: возвращаемся в главное меню администратора."""
    await state.clear()
    await callback.message.edit_text(
        "🛠 Меню администратора.\nВыбери действие:",
        reply_markup=build_admin_menu_keyboard(),
    )
    await callback.answer()


# ---------------------------------------------------------------------------
# 1) Статистика.
# ---------------------------------------------------------------------------

@router.callback_query(F.data == ADMIN_STATS)
async def on_admin_stats(callback: CallbackQuery) -> None:
    """Кнопка «📊 Статистика»: показываем общую статистику."""
    try:
        stats = await _get_statistics_safe()
    except ExcelEngineError:
        await callback.answer(EXCEL_ERROR_TEXT, show_alert=True)
        return

    lines = [
        "📊 **Статистика по расписанию:**\n\n",
        f"📈 Всего записей: {stats['total_records']}\n",
        f"🔢 Всего слотов: {stats['total_slots']}\n",
        f"✅ Свободных слотов: {stats['free_slots']}\n",
        f"🔒 Занятых слотов: {stats['occupied_slots']}\n\n",
        "**По дням:**\n",
    ]

    for day in stats['days']:
        lines.append(f"📅 {day['sheet']} ({day['date']}):")
        lines.append(f"   • Слотов: {day['total_slots']} (свободно: {day['free_slots']}, занято: {day['occupied_slots']})")
        lines.append(f"   • Учеников: {day['total_students']}")
        lines.append("")

    text = "\n".join(lines)

    await callback.message.edit_text(
        text, reply_markup=build_admin_menu_keyboard(), parse_mode="Markdown"
    )
    await callback.answer()


# ---------------------------------------------------------------------------
# 2) Просмотр — кто записался.
# ---------------------------------------------------------------------------

@router.callback_query(F.data == ADMIN_VIEW_RECORDS)
async def on_admin_view_records(callback: CallbackQuery) -> None:
    """Кнопка «👥 Кто записался»: показываем все записи по дням."""
    try:
        records = await _get_records_safe()
    except ExcelEngineError:
        await callback.answer(EXCEL_ERROR_TEXT, show_alert=True)
        return

    if not records:
        text = "📭 Пока никто никуда не записан."
    else:
        lines = ["👥 Все записи по дням:\n"]
        total = 0
        for day in records:
            lines.append(f"📅 {day['sheet']} ({day['date']}):")
            for student in day["students"]:
                lines.append(f"   • {student['time']} — {student['name']}")
                total += 1
            lines.append("")  # пустая строка между днями
        lines.append(f"Всего записей: {total}")
        text = "\n".join(lines)

    # Меню остаётся под сообщением, чтобы админ мог продолжить работу.
    await callback.message.edit_text(
        text, reply_markup=build_admin_menu_keyboard()
    )
    await callback.answer()


# ---------------------------------------------------------------------------
# 2) Полный сброс всех слотов (с подтверждением).
# ---------------------------------------------------------------------------

@router.callback_query(F.data == ADMIN_RESET_ALL)
async def on_admin_reset_all(callback: CallbackQuery) -> None:
    """Кнопка «♻️ Полный сброс»: просим подтверждение (действие опасное)."""
    await callback.message.edit_text(
        "⚠️ ВНИМАНИЕ! Ты собираешься удалить ВСЕ записи во ВСЕХ днях.\n"
        "Это действие необратимо. Подтвердить?",
        reply_markup=build_admin_confirm_reset_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data == ADMIN_CONFIRM_ALL)
async def on_admin_confirm_reset_all(callback: CallbackQuery, state: FSMContext) -> None:
    """Подтверждение полного сброса: очищаем все слоты."""
    try:
        removed = await _reset_all_safe()
    except ExcelEngineError:
        await callback.answer(EXCEL_ERROR_TEXT, show_alert=True)
        return

    await state.clear()
    await callback.message.edit_text(
        f"✅ Полный сброс выполнен. Удалено записей: {removed}.\n\n"
        "🛠 Меню администратора.",
        reply_markup=build_admin_menu_keyboard(),
    )
    await callback.answer()


# ---------------------------------------------------------------------------
# 3) Сброс одного слота: выбор дня, затем слота.
# ---------------------------------------------------------------------------

@router.callback_query(F.data == ADMIN_RESET_CHOOSE)
async def on_admin_reset_choose(callback: CallbackQuery) -> None:
    """Кнопка «🗑 Сбросить слот»: предлагаем выбрать день."""
    try:
        days = await _get_days_safe()
    except ExcelEngineError:
        await callback.answer(EXCEL_ERROR_TEXT, show_alert=True)
        return

    if not days:
        await callback.answer("В расписании нет дней.", show_alert=True)
        return

    await callback.message.edit_text(
        "🗑 Выбери день, в котором нужно сбросить слот:",
        reply_markup=build_admin_days_keyboard(days, "reset"),
    )
    await callback.answer()


# ---------------------------------------------------------------------------
# 4) Изменение времени слота: выбор дня, затем слота, затем ввод времени.
# ---------------------------------------------------------------------------

@router.callback_query(F.data == ADMIN_EDIT_CHOOSE)
async def on_admin_edit_choose(callback: CallbackQuery) -> None:
    """Кнопка «✏️ Изменить время»: предлагаем выбрать день."""
    try:
        days = await _get_days_safe()
    except ExcelEngineError:
        await callback.answer(EXCEL_ERROR_TEXT, show_alert=True)
        return

    if not days:
        await callback.answer("В расписании нет дней.", show_alert=True)
        return

    await callback.message.edit_text(
        "✏️ Выбери день, в котором нужно изменить время слота:",
        reply_markup=build_admin_days_keyboard(days, "edit"),
    )
    await callback.answer()


# ---------------------------------------------------------------------------
# Обработка шага выбора дня: "adm:reset:Вторник" или "adm:edit:Вторник".
# ---------------------------------------------------------------------------

@router.callback_query(F.data.startswith(f"{ADMIN_PREFIX}:reset:"))
@router.callback_query(F.data.startswith(f"{ADMIN_PREFIX}:edit:"))
async def on_admin_day_chosen(callback: CallbackQuery) -> None:
    """
    Админ выбрал день для действия. Показываем слоты этого дня.

    callback_data: "adm:reset:Вторник" или "adm:edit:Вторник".
    """
    # Разбираем: "adm", действие, имя листа.
    _, action, sheet = callback.data.split(":", 2)

    try:
        slots = await _get_slots_safe(sheet)
    except ExcelEngineError:
        await callback.answer(EXCEL_ERROR_TEXT, show_alert=True)
        return

    if not slots:
        await callback.answer("В этот день нет слотов.", show_alert=True)
        return

    if action == "reset":
        header = f"🗑 День «{sheet}». Выбери слот для сброса:"
    else:
        header = f"✏️ День «{sheet}». Выбери слот для изменения времени:"

    await callback.message.edit_text(
        header,
        reply_markup=build_admin_slots_keyboard(sheet, slots, action),
    )
    await callback.answer()


# ---------------------------------------------------------------------------
# Обработка шага выбора слота для СБРОСА: "adm:reset:Вторник|6|17:00 — 17:15".
# ---------------------------------------------------------------------------

@router.callback_query(F.data.startswith(f"{ADMIN_PREFIX}:reset:"))
async def on_admin_slot_reset(callback: CallbackQuery) -> None:
    """Админ выбрал конкретный слот для сброса — освобождаем его."""
    # payload = "reset:Вторник|6|17:00 — 17:15"
    payload = callback.data.split(":", 1)[1]
    action, rest = payload.split(":", 1)  # action="reset", rest="Вторник|6|17:00..."
    sheet, header_row_text, time = rest.split("|", 2)
    header_row = int(header_row_text)

    try:
        removed = await _reset_slot_safe(sheet, header_row)
    except ExcelEngineError:
        await callback.answer(EXCEL_ERROR_TEXT, show_alert=True)
        return

    if removed < 0:
        await callback.answer("Некорректный слот.", show_alert=True)
        return

    if removed == 0:
        text = f"ℹ️ Слот «{sheet}» {time} и так был пуст."
    else:
        text = f"✅ Слот «{sheet}» {time} освобождён (удалено записей: {removed})."

    await callback.message.edit_text(
        text + "\n\n🛠 Меню администратора.",
        reply_markup=build_admin_menu_keyboard(),
    )
    await callback.answer()


# ---------------------------------------------------------------------------
# Обработка шага выбора слота для ИЗМЕНЕНИЯ ВРЕМЕНИ.
# ---------------------------------------------------------------------------

@router.callback_query(F.data.startswith(f"{ADMIN_PREFIX}:edit:"))
async def on_admin_slot_edit(callback: CallbackQuery, state: FSMContext) -> None:
    """Админ выбрал слот — просим ввести новое время."""
    payload = callback.data.split(":", 1)[1]  # "edit:Вторник|6|17:00 — 17:15"
    action, rest = payload.split(":", 1)
    sheet, header_row_text, time = rest.split("|", 2)
    header_row = int(header_row_text)

    # Запоминаем выбор, ждём новое время.
    await state.update_data(sheet=sheet, header_row=header_row, old_time=time)
    await state.set_state(AdminStates.waiting_for_new_time)

    await callback.message.edit_text(
        f"✏️ Слот «{sheet}», текущее время: {time}.\n\n"
        "Отправь новое время сообщением в формате: 17:00 — 17:15"
    )
    await callback.answer()


@router.message(AdminStates.waiting_for_new_time, _is_not_command)
async def on_admin_new_time_received(message: Message, state: FSMContext) -> None:
    """Принимает новое время слота и сохраняет его в Excel."""
    # Проверяем, что отправитель — всё ещё администратор (защита от подмены).
    if not await _is_admin_full(message.from_user.id, message.chat.id, message.bot):
        await state.clear()
        return

    new_time = (message.text or "").strip()
    if not new_time:
        await message.answer(
            "Пожалуйста, введи время текстом (например: 17:00 — 17:15)."
        )
        return

    if not _validate_time_format(new_time):
        await message.answer(
            "⚠️ Некорректный формат времени.\n"
            "Пример: 17:00 — 17:15\n"
            "Часы: 0-23, минуты: 0-59"
        )
        return

    data = await state.get_data()
    sheet = data.get("sheet")
    header_row = data.get("header_row")

    try:
        success = await _update_time_safe(sheet, header_row, new_time)
    except ExcelEngineError:
        await message.answer(EXCEL_ERROR_TEXT)
        return

    if success:
        await message.answer(
            f"✅ Время слота обновлено: {new_time}.\n\n🛠 Меню администратора.",
            reply_markup=build_admin_menu_keyboard(),
        )
    else:
        await message.answer(
            "⚠️ Не удалось изменить время: некорректный слот.",
            reply_markup=build_admin_menu_keyboard(),
        )
    await state.clear()
# ---------------------------------------------------------------------------
# Назначение нового администратора (добавление через бота).
# ---------------------------------------------------------------------------

@router.callback_query(F.data == ADMIN_ADD)
async def on_admin_add(callback: CallbackQuery, state: FSMContext) -> None:
    """Кнопка «👑 Назначить администратора»: просим ввести Telegram ID."""
    # Проверяем, что вызывающий — администратор.
    if not await _is_admin_full(
        callback.from_user.id, callback.message.chat.id, callback.bot
    ):
        await callback.answer("⛔ Нет доступа.", show_alert=True)
        return

    # Показываем текущий список администраторов.
    dynamic = _load_dynamic_admins()
    static_ids = ADMIN_IDS.copy()
    all_admin_ids = list(set(static_ids + dynamic))

    lines = ["👑 **Назначение администратора**\n"]
    if all_admin_ids:
        lines.append("Текущие администраторы бота:")
        for uid in all_admin_ids:
            lines.append(f"  • `{uid}`")
    else:
        lines.append("Пока нет ни одного администратора.")
    lines.append(
        "\nОтправь числовой Telegram ID нового администратора "
        "(можно узнать у бота @userinfobot).\n"
        "Или нажми «⬅️ Отмена» для возврата в меню."
    )

    await state.set_state(AdminStates.waiting_for_new_admin_id)
    await callback.message.edit_text("\n".join(lines))
    await callback.answer()


@router.message(AdminStates.waiting_for_new_admin_id, _is_not_command)
async def on_new_admin_id_received(message: Message, state: FSMContext) -> None:
    """Принимает Telegram ID нового администратора и добавляет его."""
    # Проверяем права — только админ может добавлять админов.
    if not await _is_admin_full(
        message.from_user.id, message.chat.id, message.bot
    ):
        await state.clear()
        return

    text = (message.text or "").strip()
    if not text:
        await message.answer("Пожалуйста, введи числовой Telegram ID.")
        return

    # Пробуем распарсить ID (может быть в формате @username или просто число).
    # Если это @username — пытаемся получить ID через getChatMember.
    target_user_id = None

    if text.startswith("@"):
        # Пользователь ввёл username — пытаемся найти ID.
        try:
            # Пробуем получить информацию о пользователе по username.
            # В Telegram API нет прямого метода username→id, но можно
            # попробовать через get_chat (если username — публичный).
            chat = await message.bot.get_chat(text)
            target_user_id = chat.id
        except Exception:
            await message.answer(
                "⚠️ Не удалось найти пользователя по этому username.\n"
                "Пожалуйста, узнай его числовой ID через @userinfobot "
                "и отправь число.",
                reply_markup=build_admin_menu_keyboard(),
            )
            await state.clear()
            return
    else:
        # Пробуем распарсить как число.
        try:
            target_user_id = int(text)
        except ValueError:
            await message.answer(
                "⚠️ ID должен быть числом (например: 123456789).\n"
                "Либо введи @username пользователя.",
                reply_markup=build_admin_menu_keyboard(),
            )
            await state.clear()
            return

    if target_user_id is None or target_user_id <= 0:
        await message.answer(
            "⚠️ Некорректный ID. Попробуй ещё раз.",
            reply_markup=build_admin_menu_keyboard(),
        )
        await state.clear()
        return

    # Проверяем, не является ли он уже администратором.
    if target_user_id in ADMIN_IDS:
        await message.answer(
            "✅ Этот пользователь уже есть в списке статических "
            "администраторов (config.py).\n\n🛠 Меню администратора.",
            reply_markup=build_admin_menu_keyboard(),
        )
        await state.clear()
        return

    if _is_dynamic_admin(target_user_id):
        await message.answer(
            "ℹ️ Этот пользователь уже является администратором.\n\n"
            "🛠 Меню администратора.",
            reply_markup=build_admin_menu_keyboard(),
        )
        await state.clear()
        return

    # Добавляем нового администратора.
    dynamic = _load_dynamic_admins()
    dynamic.append(target_user_id)
    _save_dynamic_admins(dynamic)

    # Сразу показываем новому админу команду /admin в кнопке меню Telegram,
    # чтобы не приходилось перезапускать бота.
    await set_admin_commands_for_chat(message.bot, target_user_id)

    # Пытаемся узнать имя пользователя для красивого ответа.
    user_name = str(target_user_id)
    try:
        chat = await message.bot.get_chat(target_user_id)
        user_name = chat.full_name or chat.username or str(target_user_id)
    except Exception:
        pass

    await message.answer(
        f"✅ Пользователь «{user_name}» (`{target_user_id}`) "
        f"добавлен в администраторы.\n\n"
        f"Теперь он может открыть админ-меню командой /admin.\n\n"
        f"🛠 Меню администратора.",
        reply_markup=build_admin_menu_keyboard(),
    )
    await state.clear()


# ---------------------------------------------------------------------------
# Конец обработчиков админ-меню.
# ---------------------------------------------------------------------------