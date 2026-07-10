#!/usr/bin/env bash
# Установка hhautoapply в один клик (Ubuntu/Debian).
#   bash <(curl -fsSL https://raw.githubusercontent.com/s1rry/hhautoapply/install-docs/scripts/install.sh)
set -e

REPO="https://github.com/s1rry/hhautoapply.git"
DIR="${HHAUTOAPPLY_DIR:-/opt/hhautoapply}"

echo "=== hhautoapply — установка ==="

# Python 3.12 (нужен >=3.12)
if ! command -v python3.12 >/dev/null 2>&1; then
    echo ">>> ставлю Python 3.12"
    apt-get update
    apt-get install -y software-properties-common
    add-apt-repository -y ppa:deadsnakes/ppa
    apt-get update
    apt-get install -y python3.12 python3.12-venv python3.12-dev git curl
fi

# Клонирование
if [ ! -d "$DIR/.git" ]; then
    echo ">>> клонирую в $DIR"
    git clone "$REPO" "$DIR"
fi
cd "$DIR"

# venv + зависимости
echo ">>> зависимости"
python3.12 -m venv .venv
.venv/bin/pip install -U pip wheel >/dev/null
.venv/bin/pip install -e .

# Браузер для входа на hh по коду
echo ">>> Playwright"
.venv/bin/playwright install chromium
.venv/bin/playwright install-deps chromium || true

# Конфиг и папки
[ -f .env ] || cp .env.example .env
[ -f configs/resume.txt ] || cp configs/resume.example.txt configs/resume.txt 2>/dev/null || true
mkdir -p data

echo ""
echo "=== Готово! Осталось 3 шага ==="
echo "1. Заполни $DIR/.env:"
echo "   TG_BOT_TOKEN     — токен бота из @BotFather"
echo "   TG_ADMIN_CHAT_ID — твой id из @userinfobot"
echo "   AI_API_KEY       — бесплатный ключ Cerebras: https://cloud.cerebras.ai"
echo "   (и впиши резюме в configs/resume.txt)"
echo "2. Запусти: cd $DIR && .venv/bin/python -m app.main"
echo "3. В боте команда /login — вход на hh по коду (телефон -> код, пароль не нужен)"
