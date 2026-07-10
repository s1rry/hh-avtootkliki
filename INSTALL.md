# Установка HH Автоотклики (job-hunter)

Инструкция по установке на десктоп (macOS, Windows, Linux) и на VPS.

Самый простой путь это Docker, он одинаково работает везде и сам поднимает базу и Redis. Если Docker не хочешь, ниже есть ручная установка.

Self-host это одиночный режим (`MODE=single`): полный функционал бесплатно, свой бот, свой ИИ-ключ, свой аккаунт hh. Платить ничего не нужно.

## 1. Что подготовить заранее (ключи)

| Ключ в `.env` | Что это и где взять |
|---|---|
| `MODE` | Оставь `single` (одиночный режим для себя) |
| `TG_BOT_TOKEN` | Токен Telegram-бота. В @BotFather команда /newbot, скопировать токен |
| `TG_ADMIN_CHAT_ID` | Твой числовой Telegram ID. Узнать в @userinfobot |
| `AI_API_KEY` | Ключ ИИ. По умолчанию Cerebras (бесплатно): https://cloud.cerebras.ai. Любой OpenAI-совместимый — тогда поменяй ещё `AI_BASE_URL` и `AI_MODEL` |
| `CONTACTS` | Контакты для подписи в письмах, напр. `email@mail.ru, tg @nick` |
| `RESUME_TEXT_PATH` | Оставь `configs/resume.txt`, положи туда резюме: `cp configs/resume.example.txt configs/resume.txt` |
| `DESIRED_SALARY_MIN`, `DESIRED_SALARY_MAX` | Вилка зарплаты |
| `MAX_APPLIES_PER_DAY`, `MIN_DELAY_SEC`, `MAX_DELAY_SEC` | Антибан, оставь как в примере |
| `DATABASE_URL`, `REDIS_URL` | Если ставишь через Docker, не трогай |

hh.ru **не требует логина/пароля** — вход по одноразовому коду прямо из бота (раздел 4). Хабр и второй Telegram-аккаунт для чтения сообщений опциональны (см. `.env.example`).

## 2. Установка через Docker (рекомендуется)

Подходит для macOS, Windows, Linux и VPS.

1. Поставь Docker:
   - macOS и Windows: установи Docker Desktop с docker.com.
   - Linux и VPS: установи Docker Engine (`curl -fsSL https://get.docker.com | sh`).
2. Склонируй проект и подготовь конфиг:
   ```bash
   git clone https://github.com/s1rry/hhautoapply.git
   cd hhautoapply
   cp .env.example .env
   ```
3. Открой `.env` и заполни ключи из таблицы выше. Положи текст резюме в `configs/resume.txt`.
4. Запусти:
   ```bash
   docker compose up -d --build
   docker compose logs -f app
   ```
   Postgres и Redis поднимутся автоматически, их в `.env` менять не нужно.

После запуска один раз выполни вход на hh, смотри раздел 4.

## 3. Ручная установка (без Docker)

Нужен Python 3.12. Postgres и Redis поставь отдельно, либо подними только их через Docker.

### macOS
```bash
brew install python@3.12 postgresql@16 redis
brew services start postgresql@16
brew services start redis
git clone https://github.com/s1rry/hhautoapply.git
cd hhautoapply
python3.12 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/playwright install chromium
cp .env.example .env   # заполни ключи
.venv/bin/python -m app.main
```

### Windows
Проще всего поставить через Docker Desktop (раздел 2). Если без Docker:
1. Установи Python 3.12 с python.org (галочка Add Python to PATH).
2. Установи Postgres и Redis (или Memurai вместо Redis).
3. В PowerShell:
   ```powershell
   git clone https://github.com/s1rry/hhautoapply.git
   cd hhautoapply
   py -3.12 -m venv .venv
   .venv\Scripts\pip install -e .
   .venv\Scripts\playwright install chromium
   copy .env.example .env   # заполни ключи
   .venv\Scripts\python -m app.main
   ```
Либо используй WSL2 с Ubuntu и иди по инструкции для Linux.

### Linux и VPS (Ubuntu 22.04+)
В репозитории есть готовый скрипт `deploy/setup_vps.sh`, либо вручную:
```bash
sudo apt update
sudo apt install -y python3.12 python3.12-venv git postgresql redis-server
git clone https://github.com/s1rry/hhautoapply.git
cd hhautoapply
python3.12 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/playwright install chromium
.venv/bin/playwright install-deps chromium
cp .env.example .env   # заполни ключи
.venv/bin/python -m app.main
```
На VPS без экрана первый вход на hh делается через Xvfb и x11vnc.

## 4. Первый вход на hh (один раз)

Проще всего прямо из бота, работает и на VPS без экрана:

1. Напиши своему боту в Telegram `/login`.
2. Пришли номер телефона, привязанный к hh. hh отправит одноразовый код.
3. Пришли код боту. Всё — токен сохранится, автоотклики заработают.

Пароль вводить не нужно. Токен сам обновляется. Для Хабра (если включаешь) — вход через `manual_login_habr.py` под Xvfb/VNC.

## 5. Обновление

```bash
cd hhautoapply
git pull
docker compose up -d --build   # для Docker
# или перезапусти процесс python для ручной установки
```

## Важно

Авто-отклики нарушают правила hh, аккаунт могут ограничить. Держи `MAX_APPLIES_PER_DAY` небольшим и задержки как в примере. Используй на свой риск.
