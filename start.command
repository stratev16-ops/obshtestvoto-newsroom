#!/bin/bash
cd "$(dirname "$0")"
echo "== Newsroom: инсталирам пакетите (само първия път е бавно) =="
python3 -m pip install --user -q fastapi uvicorn feedparser httpx python-multipart jinja2 2>&1 | tail -2
( sleep 3; open "http://127.0.0.1:8000/admin" ) &
# спира стария процес на порт 8000, иначе новият код не тръгва
OLD=$(lsof -ti tcp:8000); [ -n "$OLD" ] && echo "== Спирам стария процес ($OLD) ==" && kill $OLD && sleep 2
python3 app.py
