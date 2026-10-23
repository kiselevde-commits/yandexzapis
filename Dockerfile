# Dockerfile для деплоя Telegram-бота на Amvera
# Используем официальный Python 3.10 slim образ
FROM python:3.10-slim

# Устанавливаем рабочую директорию
WORKDIR /app

# Копируем файл зависимостей и устанавливаем их
# Используем --no-cache-dir для уменьшения размера образа
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем весь код проекта
COPY . .

# Создаём директорию для постоянного хранилища (будет примонтирована Amvera к /data)
# SQLite БД и admin_ids.json будут храниться там
RUN mkdir -p /data

# Переменные окружения (значения задаются в панели Amvera)
ENV BOT_TOKEN=""
ENV DATABASE_URL=""
ENV ADMIN_IDS_FILE="/data/admin_ids.json"
ENV DATA_DIR="/data"
ENV PORT="10000"
ENV PYTHONUNBUFFERED=1

# Открываем порт для healthcheck (Amvera проверяет живость сервиса)
EXPOSE 10000

# Запускаем бота
# Flask healthcheck запускается внутри bot.py в отдельном потоке
CMD ["python", "bot.py"]