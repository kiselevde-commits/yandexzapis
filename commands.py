# commands.py — настройка нативной кнопки меню Telegram.
#
# Telegram показывает «кнопку меню» слева от поля ввода. По нажатию на неё
# открывается список команд. Этот список задаётся программно через метод
# API setMyCommands. Здесь собраны функции, которые:
#   1) регистрируют глобальный набор команд для ВСЕХ пользователей;
#   2) для каждого администратора дополнительно показывают команду /admin
#      (через набор команд только для его chat_id);
#   3) включают кнопку меню, открывающую список команд (MenuButtonCommands).
#
# Модуль вынесен отдельно (а не в bot.py), чтобы handlers.py тоже мог
# переустанавливать команды новому админу без циклических импортов.

import logging

from aiogram import Bot
from aiogram.types import BotCommand, BotCommandScopeChat, BotCommandScopeDefault

# Импортируем описания команд и список админов из config.py.
from config import ADMIN_COMMAND, USER_COMMANDS, get_all_admin_ids

logger = logging.getLogger(__name__)


def _build_bot_commands(pairs: list[tuple[str, str]]) -> list[BotCommand]:
    """
    Преобразует список кортежей (команда, описание) в объекты BotCommand,
    которые понимает Telegram API.
    """
    return [BotCommand(command=cmd, description=desc) for cmd, desc in pairs]


async def setup_bot_commands(bot: Bot) -> None:
    """
    Настраивает кнопку меню Telegram при запуске бота.

    Делает три вещи:
      1) ставит ГЛОБАЛЬНЫЙ набор команд (USER_COMMANDS) для всех пользователей;
      2) для каждого администратора ставит ПЕРСОНАЛЬНЫЙ набор
         (USER_COMMANDS + ADMIN_COMMAND), чтобы /admin был виден только ему;
      3) включает MenuButtonCommands — кнопка меню открывает список команд.

    Ошибки по отдельным админам не роняют запуск: например, пользователь мог
    ещё ни разу не запустить бота (Telegram вернёт ошибку) — просто пропускаем.
    """
    # 1) Глобальный набор команд. scope=default => видят все пользователи.
    try:
        user_commands = _build_bot_commands(USER_COMMANDS)
        await bot.set_my_commands(
            commands=user_commands,
            scope=BotCommandScopeDefault(),
        )
        logger.info("Глобальные команды меню установлены: %s", [c for c, _ in USER_COMMANDS])
    except Exception as e:
        logger.warning("Не удалось установить глобальные команды меню: %s", e)

    # 2) Персональный набор для каждого администратора (добавляем /admin).
    admin_commands = _build_bot_commands(USER_COMMANDS + [ADMIN_COMMAND])
    for admin_id in get_all_admin_ids():
        try:
            await bot.set_my_commands(
                commands=admin_commands,
                scope=BotCommandScopeChat(chat_id=admin_id),
            )
            logger.info("Команды администратора установлены для ID %s", admin_id)
        except Exception as e:
            # Наиболее частая причина — админ ни разу не писал боту.
            logger.warning("Не удалось установить команды для админа %s: %s", admin_id, e)

    # 3) Включаем кнопку меню, которая открывает список команд.
    #    Если не сделать этого, у бота может отображаться другая кнопка меню.
    try:
        from aiogram.types import MenuButtonCommands

        await bot.set_chat_menu_button(menu_button=MenuButtonCommands())
        logger.info("Кнопка меню (MenuButtonCommands) включена.")
    except Exception as e:
        logger.warning("Не удалось установить кнопку меню: %s", e)


async def set_admin_commands_for_chat(bot: Bot, admin_id: int) -> None:
    """
    Устанавливает персональный набор команд (с /admin) для ОДНОГО администратора.

    Нужна, когда нового админа добавляют уже во время работы бота (команда
    «Назначить администратора»). Так /admin появляется в меню сразу,
    без перезапуска бота.
    """
    admin_commands = _build_bot_commands(USER_COMMANDS + [ADMIN_COMMAND])
    try:
        await bot.set_my_commands(
            commands=admin_commands,
            scope=BotCommandScopeChat(chat_id=admin_id),
        )
        logger.info("Команды администратора установлены для нового ID %s", admin_id)
    except Exception as e:
        logger.warning("Не удалось установить команды для нового админа %s: %s", admin_id, e)