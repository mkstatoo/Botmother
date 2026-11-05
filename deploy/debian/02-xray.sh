#!/usr/bin/env bash
# گام ۲: نصب Xray و ساخت پروکسی محلی برای تلگرام از روی لینک V2Ray شما.
# لینک را همین‌جا (بدون نمایش روی صفحه) می‌پرسد و در تاریخچه‌ی shell ذخیره نمی‌شود.
# اجرا: sudo bash deploy/debian/02-xray.sh      (اگر پروکسی socks5/http ساده دارید این گام را رد کنید)
set -euo pipefail
cd "$(dirname "$0")"; . ./lib.sh; need_root
CFG_DIR=/usr/local/etc/xray
BIN=/usr/local/bin/xray

if [ ! -x "$BIN" ]; then
  echo "▶ دریافت Xray از GitHub…"
  TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
  if ! curl -fsSL --retry 3 -m 120 -o "$TMP/x.zip" https://github.com/XTLS/Xray-core/releases/latest/download/Xray-linux-64.zip; then
    echo "دریافت Xray ناموفق بود (ممکن است دانلود فایل از GitHub روی این سرور بسته باشد)." >&2
    echo "فایل Xray-linux-64.zip را از جای دیگر بگیرید و در /root قرار دهید، سپس دوباره اجرا کنید." >&2
    [ -f /root/Xray-linux-64.zip ] || exit 1
    cp /root/Xray-linux-64.zip "$TMP/x.zip"
  fi
  unzip -qo "$TMP/x.zip" xray -d "$TMP"
  install -m 755 "$TMP/xray" "$BIN"
fi
"$BIN" version | head -1

echo
echo "لینک پروکسی (vless:// یا vmess:// یا trojan:// یا ss://) را بچسبانید و Enter بزنید."
echo "(هنگام چسباندن چیزی نمایش داده نمی‌شود؛ این طبیعی است)"
read -rs LINK; echo
mkdir -p "$CFG_DIR"
if ! printf '%s' "$LINK" | PYTHONPATH="$APP_DIR" python3 -m botfactory.xray_link > "$CFG_DIR/config.json.new"; then
  rm -f "$CFG_DIR/config.json.new"; echo "تبدیل لینک ناموفق بود." >&2; exit 1
fi
unset LINK
"$BIN" run -test -c "$CFG_DIR/config.json.new" >/dev/null
mv "$CFG_DIR/config.json.new" "$CFG_DIR/config.json"
chown root:nogroup "$CFG_DIR/config.json"; chmod 640 "$CFG_DIR/config.json"

cat > /etc/systemd/system/xray.service <<'UNIT'
[Unit]
Description=Xray client (local proxy for Telegram)
After=network-online.target
Wants=network-online.target

[Service]
User=nobody
Group=nogroup
ExecStart=/usr/local/bin/xray run -c /usr/local/etc/xray/config.json
Restart=always
RestartSec=5
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable xray >/dev/null 2>&1
systemctl restart xray
sleep 3

echo "▶ آزمایش اتصال به تلگرام از طریق پروکسی…"
CODE="$(curl -s -o /dev/null -m 15 -w '%{http_code}' --socks5-hostname 127.0.0.1:1080 https://api.telegram.org/ || true)"
if [ "$CODE" != "000" ] && [ -n "$CODE" ]; then
  echo "✔ تلگرام از طریق پروکسی در دسترس است (کد $CODE)."
  [ -f "$ENV_FILE" ] && set_env PROXY_URL "socks5://127.0.0.1:1080" && echo "  PROXY_URL در .env تنظیم شد."
else
  echo "✘ از طریق پروکسی به تلگرام نرسیدیم. لاگ: journalctl -u xray -n 30 --no-pager" >&2
  echo "  احتمال‌ها: لینک منقضی/غلط است، یا دیتاسنتر این پروتکل را قطع کرده است." >&2
  exit 1
fi
