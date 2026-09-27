#!/usr/bin/env bash
# Push Confer inbox events to your phone via a Telegram bot.
#   confer config notify_cmd "/path/to/notify-telegram.sh"
# Needs TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in the environment of `confer serve`
# (e.g. an EnvironmentFile= in the systemd unit). Event JSON arrives on stdin.
set -euo pipefail
event="$(cat)"
summary="$(printf '%s' "$event" | python3 -c 'import json,sys; e=json.load(sys.stdin); print(("❗ " if e.get("actionable") else "") + e["summary"])')"
curl -fsS -m 10 "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
  --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" \
  --data-urlencode "text=${summary}" >/dev/null
