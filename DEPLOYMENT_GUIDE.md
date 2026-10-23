# Инструкция по деплою на Render (render.com)

Этот документ описывает пошаговый процесс деплоя Telegram-бота на бесплатный хостинг Render с PostgreSQL.

---

## 📋 Что сделано в коде

1. **Healthcheck сервер (Flask)** — добавлен в `bot.py`. Слушает переменную окружения `$PORT` и возвращает `200 OK` по адресу `/`.
2. **PostgreSQL вместо SQLite** — создан `postgres_engine.py` с асинхронным драйвером `asyncpg`. Все функции имеют тот же API, что и в `sqlite_engine.py`.
3. **Зависимости** — обновлён `requirements.txt` (добавлены `asyncpg`, `flask`).
4. **Конфигурация** — в `config.py` добавлена переменная `DATABASE_URL`.
5. **Игнорирование файлов** — обновлён `.gitignore` (добавлены `.env`, виртуальные окружения, кэш Python, файлы SQLite).

---

## 🚀 Пошаговый план деплоя

### 1. Подготовка репозитория на GitHub

```bash
# Инициализируй git (если ещё не сделано)
git init

# Добавь все файлы
git add .

# Создай коммит
git commit -m "Prepare for Render deployment: Flask healthcheck, PostgreSQL, updated deps"

# Создай репозиторий на GitHub (через веб-интерфейс или CLI)
# Затем привяжи удалённый репозиторий:
git remote add origin https://github.com/ТВОЙ_ЮЗЕРНЕЙМ/ТВОЙ_РЕПО.git
git branch -M main
git push -u origin main
```

> ⚠️ **Важно:** Файл `.env` **НЕ** должен попасть в git (он в `.gitignore`). Локально создай `.env` на основе `.env.example` для тестов.

---

### 2. Создание Web Service на Render

1. Зайди на [render.com](https://render.com) → **New +** → **Web Service**.
2. Подключи свой GitHub-репозиторий.
3. Настрой параметры:

| Поле | Значение |
|------|----------|
| **Name** | `math-bot` (или любое имя) |
| **Region** | `Frankfurt (EU Central)` или ближайший к тебе |
| **Branch** | `main` |
| **Runtime** | `Python 3` |
| **Build Command** | `pip install -r requirements.txt` |
| **Start Command** | `python bot.py` |

4. Нажми **Advanced** → **Add Environment Variable**:

| Key | Value |
|-----|-------|
| `BOT_TOKEN` | Твой токен от @BotFather |
| `DATABASE_URL` | *(заполнится автоматически после шага 3)* |
| `ADMIN_IDS_FILE` | `/data/admin_ids.json` |
| `DATA_DIR` | `/data` |

5. Нажми **Create Web Service**.

---

### 3. Создание бесплатной PostgreSQL на Render

1. В дашборде Render: **New +** → **PostgreSQL**.
2. Настрой:

| Поле | Значение |
|------|----------|
| **Name** | `math-bot-db` |
| **Database** | `bot_db` |
| **User** | `bot_user` |
| **Region** | Тот же, что у Web Service |
| **Plan** | **Free** (бесплатный тир) |

3. Нажми **Create Database**.
4. После создания открой базу → скопируй **Internal Database URL** (вида `postgresql://user:pass@host:port/db`).
5. Вернись в свой **Web Service** → **Environment** → добавь/обнови переменную:
   - `DATABASE_URL` = скопированный Internal Database URL

> 💡 Render автоматически подставит `DATABASE_URL` в переменные окружения Web Service, если обе службы в одном аккаунте. Но лучше прописать вручную для надёжности.

---

### 4. Деплой и проверка

1. Нажми **Manual Deploy** → **Deploy latest commit**.
2. Дождись завершения билда (логи покажут установку зависимостей и запуск).
3. Проверь healthcheck: открой `https://твой-сервис.onrender.com/` — должен вернуть `OK`.
4. Напиши боту `/start` в Telegram — должен ответить.

---

## ⚙️ Точная команда запуска (Start Command)

```
python bot.py
```

> **Не используй `gunicorn`** — бот работает на Long Polling (aiogram), а не как веб-приложение. Flask сервер запускается внутри `bot.py` в отдельном потоке.

---

## 🔄 Что произойдёт после деплоя

1. **Render поднимет контейнер** → выполнит `pip install -r requirements.txt`.
2. **Запустит `python bot.py`**:
   - Flask сервер стартует в фоновом потоке на порту `$PORT` (healthcheck для Render).
   - aiogram бот подключается к Telegram через Long Polling.
   - При первом запуске `postgres_engine.py` создаст таблицы в PostgreSQL (`init_db`).
3. **Бесплатный тир Render**:
   - Web Service «засыпает» после 15 мин неактивности (холодный старт ~30 сек).
   - PostgreSQL Free тоже «засыпает», но пробудится при первом запросе бота.
   - Диск эфемерный — **база в PostgreSQL сохраняется**, а файлы (Excel, SQLite) — нет.
4. **Логи** доступны в дашборде Render (Log stream).

---

## 🛠 Полезные команды для отладки

```bash
# Локальный запуск (нужен PostgreSQL локально или через Docker)
cp .env.example .env
# Отредактируй .env
python bot.py

# Проверка миграции БД (таблицы создадутся автоматически при первом запуске)
# Можно подключиться к БД через psql или DBeaver и проверить:
# SELECT * FROM days;
# SELECT * FROM slots;
# SELECT * FROM bookings;
```

---

## ❗ Известные ограничения бесплатного тира Render

| Ресурс | Лимит |
|--------|-------|
| **Web Service** | Засыпает через 15 мин без запросов. Cold start ~30 сек. |
| **PostgreSQL** | 90 дней бесплатно, затем требует подтверждения. 1 GB хранилища. |
| **Build** | 500 минут в месяц. |
| **Bandwidth** | 100 GB/мес. |

Для продакшена рассмотри платные тарифы или VPS (Timeweb, Aeza, DigitalOcean и т.д.).

---

## 📁 Структура файлов после изменений

```
Tgbotyandex/
├── bot.py              # Точка входа + Flask healthcheck
├── config.py           # Конфигурация (BOT_TOKEN, DATABASE_URL, ...)
├── postgres_engine.py  # Работа с PostgreSQL (asyncpg)
├── sqlite_engine.py    # Старый движок (оставлен для истории)
├── handlers.py         # Обработчики (теперь используют postgres_engine)
├── keyboards.py        # Клавиатуры
├── commands.py         # Команды меню
├── rate_limiter.py     # Rate limiting
├── requirements.txt    # Зависимости (aiogram, asyncpg, flask, ...)
├── .gitignore          # Игнорируемые файлы
├── .env.example        # Пример переменных окружения
├── DEPLOYMENT_GUIDE.md # Этот файл
└── ...остальные файлы
```

---

## ✅ Чек-лист перед деплоем

- [ ] Код загружен на GitHub (main branch)
- [ ] `.env` НЕ в коммите (проверь `git status`)
- [ ] `requirements.txt` содержит `asyncpg` и `flask`
- [ ] В `config.py` есть `DATABASE_URL = os.environ.get("DATABASE_URL", "")`
- [ ] На Render создан Web Service с **Start Command: `python bot.py`**
- [ ] На Render создана бесплатная PostgreSQL
- [ ] В Web Service задана переменная `DATABASE_URL` (Internal Database URL)
- [ ] В Web Service задана переменная `BOT_TOKEN`
- [ ] Healthcheck `https://твой-сервис.onrender.com/` возвращает `OK`
- [ ] Бот отвечает на `/start` в Telegram

---

**Готово!** 🎉 Твой бот теперь работает на Render с PostgreSQL.