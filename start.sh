#!/bin/sh
cd "$(dirname "$0")"
python3 -m pip install -q fastapi uvicorn feedparser httpx python-multipart jinja2
( sleep 2; open http://127.0.0.1:8000/admin 2>/dev/null || xdg-open http://127.0.0.1:8000/admin ) &
python3 app.py
