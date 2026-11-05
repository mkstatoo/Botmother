#!/usr/bin/env bash
# گام ۳: نصب برنامه (venv)، ساخت .env، سرویس systemd و پشتیبان شبانه.
# اجرا: sudo bash deploy/debian/03-app.sh
set -euo pipefail
cd "$(dirname "$0")"; . ./lib.sh; need_root
[ -f "$APP_DIR/requirements.txt" ] || { echo "کد باید در $APP_DIR باشد (git clone)." >&2; exit 1; }

echo "▶ ساخت محیط پایتون و نصب وابستگی‌ها…"
[ -d "$APP_DIR/venv" ] || python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install -q --upgrade pip
"$APP_DIR/venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"
chown -R "$APP_USER":"$APP_USER" "$APP_DIR/data"

if [ ! -f "$ENV_FILE" ]; then
  cp "$APP_DIR/.env.example" "$ENV_FILE"; chmod 600 "$ENV_FILE"
fi
if [ -z "$(get_env BOT_TOKEN)" ]; then
  echo "توکن ربات مادر (از BotFather) را بچسبانید و Enter بزنید (نمایش داده نمی‌شود):"
  read -rs TOKEN; echo
  [[ "$TOKEN" =~ ^[0-9]{5,}:[A-Za-z0-9_-]{30,}$ ]] || { echo "فرمت توکن درست نیست." >&2; exit 1; }
  set_env BOT_TOKEN "$TOKEN"; unset TOKEN
fi
if [ -z "$(get_env ADMIN_IDS)" ]; then
  read -rp "آیدی عددی تلگرام شما (ادمین پلتفرم): " AID
  [[ "$AID" =~ ^[0-9]+(,[0-9]+)*$ ]] || { echo "آیدی باید عدد باشد." >&2; exit 1; }
  set_env ADMIN_IDS "$AID"
fi
set_env MODE polling
set_env WEB_HOST 127.0.0.1
set_env WEB_PORT 8080
if [ -z "$(get_env PROXY_URL)" ] && systemctl is-active --quiet xray 2>/dev/null; then
  set_env PROXY_URL "socks5://127.0.0.1:1080"
fi
chown "$APP_USER":"$APP_USER" "$ENV_FILE"

cat > /etc/systemd/system/botmother.service <<UNIT
[Unit]
Description=Telegram Bot Factory
After=network-online.target xray.service
Wants=network-online.target

[Service]
User=$APP_USER
Group=$APP_USER
WorkingDirectory=$APP_DIR
EnvironmentFile=$ENV_FILE
ExecStart=$APP_DIR/venv/bin/python -m botfactory
Restart=always
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=$APP_DIR/data

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable botmother >/dev/null 2>&1
systemctl restart botmother
sleep 6
systemctl --no-pager --lines=15 status botmother || true

# پشتیبان شبانه ساعت ۳
CRON=/etc/cron.d/botmother-backup
echo "0 3 * * * root cd $APP_DIR && DATA_DIR=$APP_DIR/data ./scripts/backup.sh /var/backups/botmother >/dev/null 2>&1" > "$CRON"
chmod 644 "$CRON"

echo
echo "لاگ زنده:  journalctl -u botmother -f"
echo "اگر خط «mother bot: @…» را دیدید، ربات مادر را در تلگرام /start کنید."
