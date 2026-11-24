#!/bin/bash
# Взима новия код от GitHub и рестартира услугата. Базата не се пипа.
set -e; cd "$(dirname "$0")"
./backup.sh
git pull --ff-only
.venv/bin/pip install -q -r requirements.txt
launchctl kickstart -k "gui/$(id -u)/bg.obshtestvoto.newsroom" && echo "Рестартирано с новия код."
