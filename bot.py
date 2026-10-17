# bot.py — точка входа. Этот файл мы запускаем командой: python bot.py
#
# Здесь только «каркас»: создаём бота, диспетчер, подключаем роутер с обработчиками
# и запускаем длительный опрос (long polling). Вся логика лежит в других файлах:
#   - config.py        — настройки (токен, путь к Google Таблице),
#   - gsheets_engine.py — работа с Google Таблицей расписания,
#   - keyboards.py     — генерация кнопок,
#   - handlers.py      — обработка команд и нажатий.

import asyncio
import logging
import time

from aiogram import Bot, Dispatcher

# Импортируем настройки и роутер с обработчиками.
from config import BOT_TOKEN
from handlers import router

# Включаем логирование: в консоли будет видно, что происходит с ботом.
logging.basicConfig(level=logging.INFO)


async def main() -> None:
    """Создаёт бота и запускает его в режиме длительного опроса."""
    # Небольшая пауза при старте: на Amvera при перезапуске возможен конфликт
    # двух инстансов бота (ошибка aiogram «TerminatedByOtherGetUpdates»).
    # Ожидание даёт старому процессу завершиться и освободить getUpdates.
    time.sleep(3)

    # Создаём объект бота с токеном из config.py.
    bot = Bot(token=BOT_TOKEN)

    # Создаём диспетчер — он направляет сообщения нужным обработчикам.
    dp = Dispatcher()

    # Подключаем роутер с обработчиками из handlers.py.
    # Именно после этой строки бот «узнаёт» про /start, кнопки и т.д.
    dp.include_router(router)

    print("Бот запущен. Нажми Ctrl+C для остановки.")

    # Удаляем возможные «застрявшие» обновления, накопившиеся пока бот был выключен,
    # чтобы он не отвечал на старые сообщения при старте.
    await bot.delete_webhook(drop_pending_updates=True)

    # Запускаем длительный опрос Telegram (бот ждёт сообщения и обрабатывает их).
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Бот остановлен.")