#!/usr/bin/env bash
# Quick Tunnel を起動し、URLを ./data/tunnel_url に書き込む
# Discord bot が on_ready でそのファイルを読んで通知する

CLOUDFLARED="${HOME}/.local/bin/cloudflared"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
URL_FILE="${SCRIPT_DIR}/data/tunnel_url"
LOG_FILE="/tmp/cf_tunnel.log"

if [ ! -x "$CLOUDFLARED" ]; then
  echo "cloudflared not found at $CLOUDFLARED"
  exit 1
fi

mkdir -p "${SCRIPT_DIR}/data"

# 既存プロセスを停止
pkill -f "cloudflared tunnel" 2>/dev/null
sleep 1

>"$URL_FILE"
>"$LOG_FILE"

echo "Starting Cloudflare Quick Tunnel..."
"$CLOUDFLARED" tunnel --url http://localhost:8080 >"$LOG_FILE" 2>&1 &

# URLが出るまで最大30秒待つ
for i in $(seq 1 30); do
  URL=$(grep -o 'https://[^ ]*\.trycloudflare\.com' "$LOG_FILE" 2>/dev/null | head -1)
  if [ -n "$URL" ]; then
    echo "$URL" > "$URL_FILE"
    echo "Tunnel URL: $URL"
    echo "Saved to $URL_FILE"
    exit 0
  fi
  sleep 1
done

echo "Timed out waiting for tunnel URL"
exit 1
