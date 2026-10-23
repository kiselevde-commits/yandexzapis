# bot.py — точка входа. Этот файл мы запускаем командой: python bot.py
#
# Здесь только «каркас»: создаём бота, диспетчер, подключаем роутер с обработчиками
# и запускаем длительный опрос (long polling). Вся логика лежит в других файлах:
#   - config.py        — настройки (токен, путь к базе данных SQLite),
#   - sqlite_engine.py — работа с базой данных расписания SQLite,
#   - keyboards.py     — генерация кнопок,
#   - handlers.py      — обработка команд и нажатий.

import asyncio
import logging
import os
import random
import sys
import time
import threading

from aiogram import BaseMiddleware, Bot, Dispatcher
from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter, TelegramServerError
from aiogram.types import Message, CallbackQuery
from flask import Flask

# Импортируем настройки и роутер с обработчиками.
from commands import setup_bot_commands
from config import BOT_TOKEN
from handlers import router
from rate_limiter import rate_limiter

# Настраиваем подробное логирование
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout
)
logger = logging.getLogger(__name__)

# Flask приложение для healthcheck (Render требует прослушивание $PORT)
app = Flask(__name__)

@app.route("/")
def healthcheck():
    """Healthcheck endpoint для Render."""
    return "OK", 200

def run_flask():
    """Запускает Flask сервер в отдельном потоке."""
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)


class RateLimitMiddleware(BaseMiddleware):
    """Middleware для ограничения частоты запросов."""

    async def __call__(self, handler, event, data):
        user_id = event.from_user.id

        if not rate_limiter.is_allowed(user_id):
            logger.warning(f"Rate limit exceeded for user {user_id}")
            if isinstance(event, Message):
                await event.answer("⚠️ Слишком много запросов. Подожди немного.")
            elif isinstance(event, CallbackQuery):
                await event.answer("⚠️ Слишком много запросов. Подожди немного.", show_alert=True)
            return

        return await handler(event, data)


async def run_bot_with_retry() -> None:
    """
    Запускает бота с автоматическим переподключением при потере связи с Telegram.

    Использует экспоненциальный бэкофф (exponential backoff) для повторных попыток:
    - Начинает с 5 секунд, удваивает задержку после каждой неудачи
    - Максимальная задержка — 5 минут (300 секунд)
    - Добавляет случайную дрожь (jitter) для избежания thundering herd
    """
    # Настройки ретраев
    base_delay = 5      # начальная задержка в секундах
    max_delay = 300     # максимальная задержка (5 минут)
    delay = base_delay

    while True:
        try:
            await main_once()
            # Если main_once завершился без исключения — это нормальный выход (Ctrl+C)
            break
        except KeyboardInterrupt:
            logger.info("Бот остановлен пользователем (Ctrl+C).")
            break
        except (TelegramNetworkError, TelegramServerError) as e:
            # Сетевые ошибки и ошибки сервера Telegram — пробуем переподключиться
            jitter = random.uniform(0, delay * 0.1)  # до 10% дрожи
            actual_delay = min(delay + jitter, max_delay)

            logger.warning(
                f"Потеряно соединение с Telegram: {e}. "
                f"Повторная попытка через {actual_delay:.1f} сек..."
            )
            await asyncio.sleep(actual_delay)

            # Увеличиваем задержку для следующей попытки (экспоненциальный бэкофф)
            delay = min(delay * 2, max_delay)
        except TelegramRetryAfter as e:
            # Telegram просит подождать (rate limit на уровне API)
            retry_after = e.retry_after + random.uniform(0, 1)
            logger.warning(f"Telegram rate limit: ждём {retry_after:.1f} сек...")
            await asyncio.sleep(retry_after)
            # Сбрасываем задержку, так как это не ошибка сети
            delay = base_delay
        except Exception as e:
            # Неожиданная ошибка — логируем и пробуем перезапустить
            logger.error(f"Неожиданная ошибка: {e}", exc_info=True)
            jitter = random.uniform(0, delay * 0.1)
            actual_delay = min(delay + jitter, max_delay)
            logger.warning(f"Перезапуск через {actual_delay:.1f} сек...")
            await asyncio.sleep(actual_delay)
            delay = min(delay * 2, max_delay)


async def main_once() -> None:
    """Одна итерация запуска бота (внутренняя логика)."""
    logger.info("Запуск бота...")

    # Небольшая пауза при старте: на Amvera при перезапуске возможен конфликт
    # двух инстансов бота (ошибка aiogram «TerminatedByOtherGetUpdates»).
    # Ожидание даёт старому процессу завершиться и освободить getUpdates.
    logger.info("Ожидание 3 секунд для освобождения getUpdates...")
    time.sleep(3)

    # Создаём объект бота с токеном из config.py.
    logger.info("Создание экземпляра бота...")
    bot = Bot(token=BOT_TOKEN)

    # Создаём диспетчер — он направляет сообщения нужным обработчикам.
    dp = Dispatcher()

    # Подключаем middleware для rate limiting
    dp.message.middleware(RateLimitMiddleware())
    dp.callback_query.middleware(RateLimitMiddleware())

    # Подключаем роутер с обработчиками из handlers.py.
    # Именно после этой строки бот «узнаёт» про /start, кнопки и т.д.
    logger.info("Подключение роутера с обработчиками...")
    dp.include_router(router)

    logger.info("Бот запущен. Нажми Ctrl+C для остановки.")

    # Удаляем возможные «застрявшие» обновления, накопившиеся пока бот был выключен,
    # чтобы он не отвечал на старые сообщения при старте.
    logger.info("Удаление webhook и отложенных обновлений...")
    await bot.delete_webhook(drop_pending_updates=True)

    # Настраиваем нативную кнопку меню Telegram: список команд для пользователей
    # и персональный список с /admin для администраторов.
    logger.info("Настройка команд меню (set_my_commands)...")
    await setup_bot_commands(bot)

    # Запускаем длительный опрос Telegram (бот ждёт сообщения и обрабатывает их).
    logger.info("Запуск polling...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    # Запускаем Flask healthcheck сервер в отдельном потоке (даемон)
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    logger.info("Flask healthcheck сервер запущен в фоновом потоке")

    try:
        asyncio.run(run_bot_with_retry())
    except KeyboardInterrupt:
        logger.info("Бот остановлен пользователем (Ctrl+C).")
    except Exception as e:
        logger.critical(f"Критическая ошибка при работе бота: {e}", exc_info=True)
        raise