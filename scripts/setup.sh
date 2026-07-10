#!/bin/bash
set -e

echo "=== Job Hunter Setup ==="

# Копируем env если нет
if [ ! -f .env ]; then
    cp .env.example .env
    echo "✓ Создан .env — заполни его перед запуском (MODE=single, TG_BOT_TOKEN, AI_API_KEY)"
fi

# Копируем шаблон резюме если нет
if [ ! -f configs/resume.txt ]; then
    mkdir -p configs
    cp configs/resume.example.txt configs/resume.txt 2>/dev/null || true
    echo "✓ Создан configs/resume.txt из шаблона — впиши своё резюме"
fi

# Устанавливаем зависимости
pip install -e ".[dev]"
echo "✓ Зависимости установлены"

# Устанавливаем Playwright
playwright install chromium
echo "✓ Браузер Chromium установлен"

# Создаём директории
mkdir -p data/browser_sessions logs configs
echo "✓ Директории созданы"

echo ""
echo "=== Готово! ==="
echo "1. Заполни .env (MODE=single, TG_BOT_TOKEN, AI_API_KEY — ключ Cerebras бесплатно на cloud.cerebras.ai)"
echo "2. Впиши своё резюме в configs/resume.txt"
echo "3. Запуск: python -m app.main   (или Docker: docker compose up -d)"
echo "4. В боте: /login — вход на hh по коду (телефон -> код), пароль не нужен"
