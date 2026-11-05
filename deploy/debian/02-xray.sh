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
echo "لینک پروکسی را بچسبانید و Enter بزنید. این‌ها قبول است:"
echo "  • لینک اشتراک (https://…/sub/…)   • vless:// vmess:// trojan:// ss://"
echo "(هنگام چسباندن چیزی نمایش داده نمی‌شود؛ این طبیعی است)"
read -rs LINK; echo
mkdir -p "$CFG_DIR"
conv() { printf '%s' "$LINK" | PYTHONPATH="$APP_DIR" python3 -m botfactory.xray_link "$@"; }

COUNT=1
if [[ "$LINK" =~ ^https?:// ]]; then
  echo "▶ دریافت فهرست سرورها از لینک اشتراک…"
  LIST="$(conv --list)" || { echo "$LIST" >&2; exit 1; }
  echo "$LIST"
  COUNT="$(printf '%s\n' "$LIST" | grep -c .)"
fi

install_unit() {
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
}

try_pick() {  # try_pick N → 0 اگر تلگرام از طریق این سرور در دسترس بود
  local n="$1"
  conv --pick "$n" > "$CFG_DIR/config.json.new" || { rm -f "$CFG_DIR/config.json.new"; return 2; }
  "$BIN" run -test -c "$CFG_DIR/config.json.new" >/dev/null || { rm -f "$CFG_DIR/config.json.new"; return 2; }
  mv "$CFG_DIR/config.json.new" "$CFG_DIR/config.json"
  chown root:nogroup "$CFG_DIR/config.json"; chmod 640 "$CFG_DIR/config.json"
  install_unit
  systemctl restart xray
  sleep 3
  local code
  code="$(curl -s -o /dev/null -m 15 -w '%{http_code}' --socks5-hostname 127.0.0.1:1080 https://api.telegram.org/ || true)"
  [ -n "$code" ] && [ "$code" != "000" ]
}

N=1
if [ "$COUNT" -gt 1 ]; then
  read -rp "شماره‌ی سروری که امتحان شود (پیش‌فرض 1، یا a برای امتحان خودکار همه): " ANS
  ANS="${ANS:-1}"
else
  ANS=1
fi

OK=0
if [ "$ANS" = "a" ] || [ "$ANS" = "A" ]; then
  for ((N=1; N<=COUNT; N++)); do
    echo "▶ سرور $N از $COUNT …"
    if try_pick "$N"; then OK=1; break; fi
  done
else
  [[ "$ANS" =~ ^[0-9]+$ ]] || { echo "شماره معتبر نیست." >&2; exit 1; }
  N="$ANS"
  try_pick "$N" && OK=1 || true
fi
unset LINK

if [ "$OK" = 1 ]; then
  echo "✔ تلگرام از طریق پروکسی در دسترس است (سرور شماره‌ی $N)."
  [ -f "$ENV_FILE" ] && set_env PROXY_URL "socks5://127.0.0.1:1080" && echo "  PROXY_URL در .env تنظیم شد."
  exit 0
fi
echo "✘ از طریق پروکسی به تلگرام نرسیدیم. لاگ: journalctl -u xray -n 30 --no-pager" >&2
echo "  احتمال‌ها: آن سرور خراب/منقضی است، یا دیتاسنتر این پروتکل را قطع کرده است. اسکریپت را دوباره اجرا و سرور دیگری انتخاب کنید." >&2
exit 1
