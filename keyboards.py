# keyboards.py — модуль генерации инлайн-клавиатур (кнопок под сообщением).
#
# Здесь НЕТ логики работы с Excel и НЕТ обработки нажатий. Задача файла —
# просто «собрать» клавиатуру из готовых данных и вернуть её.
# Данные приходят сюда из handlers.py, который берёт их из excel_engine.py.
#
# В aiogram 3.x для сборки инлайн-клавиатур удобно использовать InlineKeyboardBuilder.

from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import MAX_TASKS

# Префиксы callback_data — короткие строки, которые Telegram присылает боту при нажатии.
# По ним в handlers.py мы понимаем, что именно нажали.
DAY_PREFIX = "day"          # нажата кнопка выбора дня
SLOT_PREFIX = "slot"        # нажата кнопка выбора времени
OCCUPIED_DATA = "occupied"  # нажата кнопка ЗАНЯТОГО слота (просто покажем подсказку)
BACK_TO_DAYS = "back_to_days"  # кнопка «назад к выбору дня»

# --- Префиксы для АДМИН-МЕНЮ -------------------------------------------------
ADMIN_PREFIX = "adm"            # общий префикс админ-кнопок "adm:действие"
ADMIN_VIEW_RECORDS = "adm:view"      # просмотр — кто записался
ADMIN_RESET_ALL = "adm:reset_all"    # полный сброс всех слотов (запрос подтверждения)
ADMIN_RESET_CHOOSE = "adm:reset"     # выбрать слот для сброса
ADMIN_EDIT_CHOOSE = "adm:edit"       # выбрать слот для изменения времени
ADMIN_CONFIRM_ALL = "adm:confirm_all"  # подтвердить полный сброс
ADMIN_CANCEL = "adm:cancel"          # отмена действия
ADMIN_CLOSE = "adm:close"            # закрыть админ-меню
ADMIN_ADD = "adm:add_admin"         # назначить нового администратора

# --- Префикс для выбора количества задач ------------------------------------
TASKS_PREFIX = "tasks"              # нажата кнопка выбора количества задач
TASKS_CANCEL = "tasks:cancel"       # отмена выбора задач (возврат к дням)

DAYS_PER_ROW = 2   # сколько кнопок дней в одной строке
SLOTS_PER_ROW = 2  # сколько кнопок слотов в одной строке


def build_days_keyboard(days: list[dict]) -> InlineKeyboardMarkup:
    """
    Строит клавиатуру с кнопками дней недели.

    Параметр days: список словарей вида {"sheet": "Вторник", "date": "20.10.2026"}
    (как их вернул excel_engine.get_available_days()).

    Текст кнопки: "Вторник — 20.10.2026". callback_data: "day:Вторник".
    """
    builder = InlineKeyboardBuilder()

    for day in days:
        text = f"{day['sheet']} — {day['date']}"
        builder.button(text=text, callback_data=f"{DAY_PREFIX}:{day['sheet']}")

    builder.adjust(DAYS_PER_ROW)
    return builder.as_markup()


def build_slots_keyboard(sheet: str, slots: list[dict]) -> InlineKeyboardMarkup:
    """
    Строит клавиатуру со ВСЕМИ слотами дня.

    Параметры:
        sheet — имя листа-дня (нужно для callback и кнопки «назад»).
        slots — список слотов из excel_engine.get_slots_for_day().

    Свободный слот   → "✅ 17:00 — 17:15" (callback_data содержит номер строки блока).
    Занятый слот     → "🔒 17:15 — 17:30" (callback_data "occupied" — бронь невозможна,
                        просто покажем пользователю подсказку).

    В callback_data свободного слота кладём НОМЕР СТРОКИ блока (header_row), а не время:
    время в таблице может быть формулой, а номер строки — надёжный идентификатор.
    Формат: "slot:Вторник|6|17:00 — 17:15" (лист | номер строки | текст времени).

    В конце добавляем кнопку «⬅️ Назад» для возврата к списку дней.
    """
    builder = InlineKeyboardBuilder()

    for slot in slots:
        if slot["free"]:
            builder.button(
                text=f"✅ {slot['time']}",
                callback_data=f"{SLOT_PREFIX}:{sheet}|{slot['header_row']}|{slot['time']}",
            )
        else:
            # Занятый слот: показываем, но помечаем замком и делаем некликабельным
            # (нажатие обрабатывается отдельно и выдаёт подсказку).
            count = len(slot["students"])
            builder.button(
                text=f"🔒 {slot['time']} ({count})",
                callback_data=OCCUPIED_DATA,
            )

    # Кнопка «Назад» — возврат к списку дней.
    builder.button(text="⬅️ Назад", callback_data=BACK_TO_DAYS)

    builder.adjust(SLOTS_PER_ROW)
    return builder.as_markup()


# ---------------------------------------------------------------------------
# Клавиатуры АДМИН-МЕНЮ.
# ---------------------------------------------------------------------------

def build_admin_menu_keyboard() -> InlineKeyboardMarkup:
    """
    Главное меню администратора.

    Кнопки:
        👥 Кто записался — просмотр всех записей,
        🗑 Сбросить слот — освободить один слот,
        ✏️ Изменить время — поменять время одного слота,
        ♻️ Полный сброс — освободить ВСЕ слоты (с подтверждением),
        ❌ Закрыть — убрать меню.
    """
    builder = InlineKeyboardBuilder()

    builder.button(text="👥 Кто записался", callback_data=ADMIN_VIEW_RECORDS)
    builder.button(text="🗑 Сбросить слот", callback_data=ADMIN_RESET_CHOOSE)
    builder.button(text="✏️ Изменить время", callback_data=ADMIN_EDIT_CHOOSE)
    builder.button(text="♻️ Полный сброс", callback_data=ADMIN_RESET_ALL)
    builder.button(text="👑 Назначить администратора", callback_data=ADMIN_ADD)
    builder.button(text="❌ Закрыть", callback_data=ADMIN_CLOSE)

    # По 1–2 кнопки в ряд для аккуратного вида.
    builder.adjust(1, 1, 1, 1, 1, 1)
    return builder.as_markup()


def build_admin_confirm_reset_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура подтверждения ПОЛНОГО сброса (защита от случайного нажатия)."""
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Да, сбросить всё", callback_data=ADMIN_CONFIRM_ALL)
    builder.button(text="⬅️ Отмена", callback_data=ADMIN_CANCEL)
    builder.adjust(1, 1)
    return builder.as_markup()


def build_admin_days_keyboard(days: list[dict], action: str) -> InlineKeyboardMarkup:
    """
    Клавиатура выбора дня для админ-действия.

    Параметры:
        days   — список дней из excel_engine.get_available_days().
        action — "reset" (сбросить слот) или "edit" (изменить время).

    Формат callback_data: "adm:reset:Вторник" или "adm:edit:Вторник".
    """
    builder = InlineKeyboardBuilder()

    for day in days:
        text = f"{day['sheet']} — {day['date']}"
        builder.button(text=text, callback_data=f"{ADMIN_PREFIX}:{action}:{day['sheet']}")

    builder.button(text="⬅️ Назад в меню", callback_data=ADMIN_CANCEL)
    builder.adjust(DAYS_PER_ROW)
    return builder.as_markup()


def build_admin_slots_keyboard(sheet: str, slots: list[dict], action: str) -> InlineKeyboardMarkup:
    """
    Клавиатура выбора конкретного слота для админ-действия.

    Параметры:
        sheet  — имя листа-дня,
        slots  — слоты из excel_engine.get_slots_for_day(),
        action — "reset" (сбросить) или "edit" (изменить время).

    Формат callback_data: "adm:reset:Вторник|6|17:00 — 17:15"
    (действие, лист, номер строки блока, время). Показываем у занятого
    слота количество учеников — админу важно видеть, что он сбрасывает.
    """
    builder = InlineKeyboardBuilder()

    for slot in slots:
        count = len(slot["students"])
        mark = "🔒" if not slot["free"] else "✅"
        caption = f"{mark} {slot['time']}"
        if count:
            caption += f" ({count})"
        builder.button(
            text=caption,
            callback_data=f"{ADMIN_PREFIX}:{action}:{sheet}|{slot['header_row']}|{slot['time']}",
        )

    builder.button(text="⬅️ Назад в меню", callback_data=ADMIN_CANCEL)
    builder.adjust(SLOTS_PER_ROW)
    return builder.as_markup()


# ---------------------------------------------------------------------------
# Клавиатура выбора количества задач.
# ---------------------------------------------------------------------------


def build_tasks_keyboard() -> InlineKeyboardMarkup:
    """
    Строит клавиатуру с кнопками 1, 2, 3, 4 и «⬅️ Отмена».

    Ученик после выбора слота указывает, сколько задач он готов сдать.
    Количество кнопок динамическое — от 1 до MAX_TASKS (из config.py).
    """
    builder = InlineKeyboardBuilder()

    for t in range(1, MAX_TASKS + 1):
        builder.button(text=f"{t} задачи" if t > 1 else "1 задача",
                       callback_data=f"{TASKS_PREFIX}:{t}")

    builder.button(text="⬅️ Отмена", callback_data=TASKS_CANCEL)
    builder.adjust(2, 2)
    return builder.as_markup()