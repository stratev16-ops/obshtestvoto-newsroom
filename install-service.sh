#!/bin/bash
# Пуска приложението като услуга на macOS (launchd): тръгва при влизане, рестартира се само при срив,
# плюс всеки ден в 04:00 копие на базата в backups/ (пазят се последните 14).
# Мрежа: NEWSROOM_HOST=0.0.0.0 ./install-service.sh  → достъпно от други устройства (паролата се включва сама).
set -e
cd "$(dirname "$0")"; DIR="$(pwd)"
[ -x .venv/bin/python ] || ./setup.sh
HOST="${NEWSROOM_HOST:-127.0.0.1}"; PORT="${NEWSROOM_PORT:-8000}"; PUBLIC="${NEWSROOM_PUBLIC_URL:-http://127.0.0.1:$PORT}"
LA="$HOME/Library/LaunchAgents"; mkdir -p "$LA" logs backups
P="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
cat > "$LA/bg.obshtestvoto.newsroom.plist" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>bg.obshtestvoto.newsroom</string>
  <key>ProgramArguments</key><array><string>$DIR/.venv/bin/python</string><string>$DIR/app.py</string></array>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>EnvironmentVariables</key><dict>
    <key>PATH</key><string>$P</string>
    <key>NEWSROOM_HOST</key><string>$HOST</string>
    <key>NEWSROOM_PORT</key><string>$PORT</string>
    <key>NEWSROOM_PUBLIC_URL</key><string>$PUBLIC</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>15</integer>
  <key>StandardOutPath</key><string>$DIR/logs/app.log</string>
  <key>StandardErrorPath</key><string>$DIR/logs/app.log</string>
</dict></plist>
PL
cat > "$LA/bg.obshtestvoto.newsroom.backup.plist" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>bg.obshtestvoto.newsroom.backup</string>
  <key>ProgramArguments</key><array><string>/bin/bash</string><string>$DIR/backup.sh</string></array>
  <key>StartCalendarInterval</key><dict><key>Hour</key><integer>4</integer><key>Minute</key><integer>0</integer></dict>
  <key>StandardOutPath</key><string>$DIR/logs/backup.log</string>
  <key>StandardErrorPath</key><string>$DIR/logs/backup.log</string>
</dict></plist>
PL
# спира стар процес на порта (напр. пуснат ръчно със start.command)
OLD=$(lsof -ti tcp:$PORT || true); [ -n "$OLD" ] && kill $OLD && sleep 2
for n in newsroom newsroom.backup; do
  launchctl bootout "gui/$(id -u)/bg.obshtestvoto.$n" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$LA/bg.obshtestvoto.$n.plist"
done
sleep 4
curl -s -o /dev/null -w "Приложението отговаря: HTTP %{http_code}\n" "http://127.0.0.1:$PORT/" || echo "Още не отговаря — виж logs/app.log"
echo "Адрес: $PUBLIC/admin   ·   Лог: $DIR/logs/app.log   ·   Спиране: ./uninstall-service.sh"
