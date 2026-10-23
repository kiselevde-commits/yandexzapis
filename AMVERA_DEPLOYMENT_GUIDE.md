# Инструкция по деплою на Amvera (amvera.tech)

Этот документ описывает пошаговый процесс деплоя Telegram-бота на хостинг Amvera с постоянным хранилищем `/data` и PostgreSQL.

---

## 📋 Что сделано в коде

1. **Healthcheck сервер (Flask)** — добавлен в `bot.py`. Слушает переменную окружения `$PORT` и возвращает `200 OK` по адресу `/`. Amvera требует открытый порт для проверки живости сервиса.
2. **SQLite в постоянном хранилище** — `config.py` использует папку `/data` для файла `schedule.db`. Данные сохраняются между перезапусками.
3. **PostgreSQL поддержка** — создан `postgres_engine.py` с `asyncpg`. Переменная `DATABASE_URL` для подключения к PostgreSQL (если нужен).
4. **Динамические администраторы** — файл `admin_ids.json` хранится в `/data` (переменная `ADMIN_IDS_FILE`).
5. **Зависимости** — обновлён `requirements.txt` (добавлены `asyncpg`, `flask`, `python-dotenv`).
6. **amvera.yml** — конфигурация с переменными окружения и монтом `/data`.

---

## 🚀 Пошаговый план деплоя

### 1. Подготовка репозитория на GitHub

```bash
# Инициализируй git (если ещё не сделано)
git init

# Добавь все файлы
git add .

# Создай коммит
git commit -m "Prepare for Amvera deployment: Flask healthcheck, SQLite in /data, env vars"

# Создай репозиторий на GitHub (через веб-интерфейс или CLI)
# Затем привяжи удалённый репозиторий:
git remote add origin https://github.com/ТВОЙ_ЮЗЕРНЕЙМ/ТВОЙ_РЕПО.git
git branch -M main
git push -u origin main
```

> ⚠️ **Важно:** Файл `.env` **НЕ** должен попасть в git (он в `.gitignore`). Локально создай `.env` на основе `.env.example` для тестов.

---

### 2. Создание сервиса на Amvera

1. Зайди на [amvera.tech](https://amvera.tech) → авторизуйся → **Создать проект** → **Приложение**.
2. Выбери **GitHub** как источник кода.
3. Подключи свой репозиторий (branch `main`).
4. Настрой параметры сборки (обычно определяются автоматически из `amvera.yml`):

| Поле | Значение |
|------|----------|
| **Окружение** | Python |
| **Версия Python** | 3.10 |
| **Менеджер пакетов** | pip |
| **Файл зависимостей** | requirements.txt |
| **Команда запуска** | `python bot.py` |
| **Порт** | 10000 (или переменная `$PORT`) |

5. **Переменные окружения** — нажми **Настройки** → **Переменные окружения** и добавь:

| Key | Value | Описание |
|-----|-------|----------|
| `BOT_TOKEN` | Твой токен от @BotFather | **Обязательно** |
| `DATABASE_URL` | `postgresql://user:pass@host:port/db` | *Опционально* — для PostgreSQL. Если пусто — используется SQLite в `/data/schedule.db` |
| `ADMIN_IDS_FILE` | `/data/admin_ids.json` | Путь к JSON с динамическими админами |
| `DATA_DIR` | `/data` | Папка постоянного хранилища |
| `PORT` | `10000` | Порт для healthcheck (Amvera сам подставит `$PORT`) |

> 💡 **BOT_TOKEN** — обязателен. Остальные имеют дефолты в коде, но лучше задать явно.

6. **Постоянное хранилище** — в настройках проекта включи **Persistent Storage** (обычно 1 Гб бесплатно). Точка монтирования: `/data`. Это сохранит `schedule.db` и `admin_ids.json` между деплоями.

7. Нажми **Деплой**.

---

### 3. (Опционально) PostgreSQL на Amvera

Если хочешь использовать PostgreSQL вместо SQLite:

1. В проекте Amvera: **Создать сервис** → **База данных** → **PostgreSQL**.
2. Настрой: имя БД, пользователь, пароль.
3. После создания скопируй **Внутренний URL подключения** (Internal URL).
4. Вернись в настройки своего **Приложения** → **Переменные окружения** → обнови `DATABASE_URL` = скопированный URL.
5. Передеплой приложение.

> 💡 При наличии `DATABASE_URL` бот автоматически использует PostgreSQL через `postgres_engine.py`. При отсутствии — падает на SQLite (`sqlite_engine.py`).

---

### 4. Деплой и проверка

1. Нажми **Деплой** (или он запустится автоматически после пуша в `main`).
2. Дождись завершения билда (логи покажут установку зависимостей и запуск).
3. Проверь healthcheck: открой `https://твой-проект.amvera.io/` — должен вернуть `OK`.
4. Напиши боту `/start` в Telegram — должен ответить.

---

## ⚙️ Точная команда запуска (Start Command)

```
python bot.py
```

> **Не используй `gunicorn`** — бот работает на Long Polling (aiogram), а не как веб-приложение. Flask сервер запускается внутри `bot.py` в отдельном потоке.

---

## 🔄 Что произойдёт после деплоя

1. **Amvera поднимет контейнер** → выполнит `pip install -r requirements.txt`.
2. **Запустит `python bot.py`**:
   - Flask сервер стартует в фоновом потоке на порту `$PORT` (healthcheck для Amvera).
   - aiogram бот подключается к Telegram через Long Polling.
   - При первом запуске создастся таблица в SQLite (`/data/schedule.db`) или PostgreSQL.
3. **Постоянное хранилище `/data`**:
   - `schedule.db` — база расписания и записей (сохраняется между деплоями).
   - `admin_ids.json` — динамические администраторы (сохраняется между деплоями).
4. **Логи** доступны в дашборде Amvera (вкладка **Логи**).

---

## 🛠 Полезные команды для отладки

```bash
# Локальный запуск (нужен Python 3.10+)
cp .env.example .env
# Отредактируй .env (добавь BOT_TOKEN)
python bot.py

# Проверка базы данных (SQLite)
sqlite3 /data/schedule.db ".schema"
sqlite3 /data/schedule.db "SELECT * FROM days;"
sqlite3 /data/schedule.db "SELECT * FROM slots;"
sqlite3 /data/schedule.db "SELECT * FROM bookings;"

# Проверка admin_ids.json
cat /data/admin_ids.json
```

---

## ❗ Известные особенности Amvera

| Ресурс | Детали |
|--------|--------|
| **Бесплатный тариф** | 1 Гб RAM, 1 vCPU, 1 Гб диск (persistent storage) |
| **Cold start** | Нет (в отличие от Render сервис не засыпает) |
| **Healthcheck** | Обязателен — сервис должен отвечать на HTTP порт `$PORT` |
| **Переменные окружения** | Задаются в панели Amvera, не коммитть в git |
| **Автодеплой** | При пуше в `main` (настраивается в настройках проекта) |
| **Логи** | В реальном времени в панели |

---

## 📁 Структура файлов после изменений

```
Tgbotyandex/
├── amvera.yml              # Конфигурация Amvera (этот файл учитывается при деплое)
├── bot.py                  # Точка входа + Flask healthcheck
├── config.py               # Конфигурация (BOT_TOKEN, DATABASE_URL, DB_PATH, ...)
├── postgres_engine.py      # Работа с PostgreSQL (asyncpg) — используется если есть DATABASE_URL
├── sqlite_engine.py        # Работа с SQLite — фолбэк, используется если нет DATABASE_URL
├── handlers.py             # Обработчики (используют движок из config.py)
├── keyboards.py            # Клавиатуры
├── commands.py             # Команды меню
├── rate_limiter.py         # Rate limiting
├── requirements.txt        # Зависимости (aiogram, asyncpg, flask, python-dotenv, openpyxl, gspread, google-auth)
├── .gitignore              # Игнорируемые файлы
├── .env.example            # Пример переменных окружения
├── DEPLOYMENT_GUIDE.md     # Инструкция для Render
├── AMVERA_DEPLOYMENT_GUIDE.md # Этот файл
└── ...остальные файлы
```

---

## ✅ Чек-лист перед деплоем

- [ ] Код загружен на GitHub (main branch)
- [ ] `.env` НЕ в коммите (проверь `git status`)
- [ ] `requirements.txt` содержит `asyncpg`, `flask`, `python-dotenv`
- [ ] `amvera.yml` настроен (env vars, persistenceMount: /data)
- [ ] На Amvera создано Приложение из GitHub репозитория
- [ ] На Amvera включено **Persistent Storage** с точкой монтирования `/data`
- [ ] Задана переменная `BOT_TOKEN` в панели Amvera
- [ ] (Опционально) Задана `DATABASE_URL` для PostgreSQL
- [ ] Healthcheck `https://твой-проект.amvera.io/` возвращает `OK`
- [ ] Бот отвечает на `/start` в Telegram
- [ ] Админ-меню `/admin` работает (твой ID в `config.py` → `ADMIN_IDS`)

---

## 🎉 Готово!

Твой бот теперь работает на Amvera с постоянным хранилищем и возможностью использовать PostgreSQL.

**Плюсы Amvera для этого проекта:**
- ✅ Бесплатный тариф без "засыпания" (нет cold start как на Render)
- ✅ Постоянное хранилище `/data` в бесплатном тарифе
- ✅ Простая настройка через `amvera.yml`
- ✅ Встроенный PostgreSQL (опционально)
- ✅ Автодеплой из GitHub