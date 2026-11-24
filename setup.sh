#!/bin/bash
# Първоначална настройка на нов Mac: виртуална среда + пакети. Пуска се веднъж (и след git pull, ако има нови пакети).
set -e
cd "$(dirname "$0")"
command -v python3 >/dev/null || { echo "Няма python3. Пусни: xcode-select --install"; exit 1; }
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt
mkdir -p static/exports logs backups
echo "Готово. Пусни ./install-service.sh, за да работи постоянно."
