@echo off
cd /d %~dp0
python -m pip install -q fastapi uvicorn feedparser httpx python-multipart jinja2
start "" http://127.0.0.1:8000/admin
python app.py
