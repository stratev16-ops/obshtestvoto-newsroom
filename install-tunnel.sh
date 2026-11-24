#!/bin/bash
# Изнася приложението на https://<домейн> през Cloudflare Tunnel (без отворени портове на рутера).
# Пускай на сървърния Mac:  ./install-tunnel.sh news.obshtovzeto.com
# ПРЕДИ ТОВА: Cloudflare Zero Trust → Access → приложение за същия домейн (само екипът), за да не е отворено нито минута.
set -e
cd "$(dirname "$0")"
HOSTNAME="${1:-news.obshtovzeto.com}"; NAME="newsroom"; PORT="${NEWSROOM_PORT:-8000}"
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

command -v cloudflared >/dev/null || brew install cloudflared
[ -f "$HOME/.cloudflared/cert.pem" ] || { echo "→ Отваря се браузър: избери домейна и натисни Authorize"; cloudflared tunnel login; }

ID=$(cloudflared tunnel list -o json | python3 -c "import json,sys; print(next((t['id'] for t in json.load(sys.stdin) if t['name']=='$NAME'), ''))")
[ -n "$ID" ] || { cloudflared tunnel create "$NAME"; ID=$(cloudflared tunnel list -o json | python3 -c "import json,sys; print(next(t['id'] for t in json.load(sys.stdin) if t['name']=='$NAME'))"); }

cat > "$HOME/.cloudflared/config.yml" <<CFG
tunnel: $ID
credentials-file: $HOME/.cloudflared/$ID.json
ingress:
  - hostname: $HOSTNAME
    service: http://127.0.0.1:$PORT
  - service: http_status:404
CFG
cloudflared tunnel route dns "$NAME" "$HOSTNAME" || echo "(DNS записът вероятно вече съществува — продължавам)"

# Приложението трябва да знае публичния си адрес: паролата става задължителна, cookie-то Secure, Canva redirect-ът сочи натам.
grep -v '^NEWSROOM_PUBLIC_URL=' .env 2>/dev/null > .env.tmp || true
echo "NEWSROOM_PUBLIC_URL=https://$HOSTNAME" >> .env.tmp && mv .env.tmp .env
./install-service.sh

# Tunnel-ът като потребителска услуга: тръгва при влизане (включи автоматичния вход в macOS).
launchctl list | grep -q com.cloudflare.cloudflared || cloudflared service install
sleep 5
echo; echo "Проверка: https://$HOSTNAME/healthz"
curl -s -m 15 "https://$HOSTNAME/healthz" -o /dev/null -w "HTTP %{http_code} (302/403 = Access пази сайта ✓, 200 = ОТВОРЕН — провери Access!)\n" || true
