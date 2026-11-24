#!/bin/bash
# Копие на базата (безопасно и докато приложението работи). Пази последните 14.
cd "$(dirname "$0")"; mkdir -p backups
F="backups/newsroom-$(date +%Y%m%d-%H%M).db"
.venv/bin/python -c "import sqlite3,sys;s=sqlite3.connect('newsroom.db');d=sqlite3.connect(sys.argv[1]);s.backup(d);d.close()" "$F" && echo "$(date) копие: $F"
ls -1t backups/newsroom-*.db 2>/dev/null | tail -n +15 | xargs rm -f
