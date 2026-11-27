#!/usr/bin/env bash
# Установка DS-BOT на Linux-сервер (Ubuntu/Debian, в т.ч. Oracle Cloud Free).
# Запуск:  bash setup-linux.sh
# Бот ставится как служба systemd: сам запускается после перезагрузки и при падении.
set -e
cd "$(dirname "$0")"
DIR="$(pwd)"
USER_NAME="$(whoami)"

echo "=== Пакеты ==="
sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip ffmpeg curl unzip git

if ! command -v deno >/dev/null; then
  echo "=== Deno (нужен yt-dlp для YouTube) ==="
  curl -fsSL https://deno.land/install.sh | sudo DENO_INSTALL=/usr/local sh -s -- -y
fi

echo "=== Python-библиотеки ==="
python3 -m venv .venv
.venv/bin/pip install -U pip
.venv/bin/pip install -r requirements.txt

if [ ! -f .env ]; then
  cp .env.example .env
  echo
  echo "!!! Создан файл .env — впиши в него DISCORD_TOKEN:  nano $DIR/.env"
fi

echo "=== Служба systemd ==="
sudo tee /etc/systemd/system/ds-bot.service >/dev/null <<UNIT
[Unit]
Description=DS-BOT Discord music bot
After=network-online.target
Wants=network-online.target

[Service]
User=$USER_NAME
WorkingDirectory=$DIR
ExecStartPre=$DIR/.venv/bin/pip install -q -U yt-dlp[default]
ExecStart=$DIR/.venv/bin/python bot.py
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload
sudo systemctl enable ds-bot

echo
echo "Готово! Дальше:"
echo "  nano .env                      — вписать DISCORD_TOKEN (если ещё не)"
echo "  sudo systemctl start ds-bot    — запустить"
echo "  journalctl -u ds-bot -f        — смотреть лог"
echo "  git pull && sudo systemctl restart ds-bot   — обновить бота"
