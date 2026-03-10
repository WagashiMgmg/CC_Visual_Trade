#!/usr/bin/env bash
# Quick Tunnel を起動し、URLを ./data/tunnel_url に書き込む
# URL確定後にDiscordへ直接通知する

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

    # .env から Discord 設定を読み込んで通知
    if [ -f "${SCRIPT_DIR}/.env" ]; then
      BOT_TOKEN=$(grep '^DISCORD_BOT_TOKEN=' "${SCRIPT_DIR}/.env" | cut -d= -f2-)
      CHANNEL_ID=$(grep '^DISCORD_CHANNEL_ID=' "${SCRIPT_DIR}/.env" | cut -d= -f2-)
    fi
    if [ -n "$BOT_TOKEN" ] && [ -n "$CHANNEL_ID" ]; then
      NOW=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
      curl -s -X POST "https://discord.com/api/v10/channels/${CHANNEL_ID}/messages" \
        -H "Authorization: Bot ${BOT_TOKEN}" \
        -H "Content-Type: application/json" \
        -d "{\"embeds\":[{\"title\":\"Cloudflare Tunnel 起動\",\"description\":\"Dashboard: ${URL}\",\"color\":7471056,\"footer\":{\"text\":\"CC Visual Trade\"},\"timestamp\":\"${NOW}\"}]}" \
        > /dev/null && echo "Discord notified."
    fi

    exit 0
  fi
  sleep 1
done

echo "Timed out waiting for tunnel URL"
exit 1
