#!/usr/bin/env bash
# گام ۲: نصب Xray و ساخت پروکسی محلی برای تلگرام از روی لینک V2Ray شما.
# لینک را همین‌جا (بدون نمایش روی صفحه) می‌پرسد و در تاریخچه‌ی shell ذخیره نمی‌شود.
# اجرا: sudo bash deploy/debian/02-xray.sh      (اگر پروکسی socks5/http ساده دارید این گام را رد کنید)
set -euo pipefail
cd "$(dirname "$0")"; . ./lib.sh; need_root
CFG_DIR=/usr/local/etc/xray
BIN=/usr/local/bin/xray

if [ ! -x "$BIN" ]; then
  ZIP=/root/Xray-linux-64.zip
  URL=https://github.com/XTLS/Xray-core/releases/latest/download/Xray-linux-64.zip
  TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
  # دانلود قابل‌ازسرگیری: اگر اینترنت کند باشد یا قطع شود، از همان‌جا ادامه می‌دهد
  for attempt in 1 2 3 4 5 6 7 8; do
    if unzip -tq "$ZIP" >/dev/null 2>&1; then break; fi
    echo "▶ دریافت Xray از GitHub (تلاش $attempt از ۸)…"
    curl -fL -C - --retry 5 --retry-all-errors --retry-delay 2 --connect-timeout 20 -m 300 -o "$ZIP" "$URL" || true
  done
  if ! unzip -tq "$ZIP" >/dev/null 2>&1; then
    echo "دریافت Xray کامل نشد. فایل $ZIP را از جای دیگری بگیرید و دوباره اجرا کنید." >&2
    exit 1
  fi
  unzip -qo "$ZIP" xray -d "$TMP"
  install -m 755 "$TMP/xray" "$BIN"
fi
"$BIN" version 2>&1 | head -1 || true

echo
echo "لینک پروکسی را بچسبانید و Enter بزنید. این‌ها قبول است:"
echo "  • لینک اشتراک (https://…/sub/…)   • vless:// vmess:// trojan:// ss://"
echo "(هنگام چسباندن چیزی نمایش داده نمی‌شود؛ این طبیعی است)"
read -rs LINK; echo
mkdir -p "$CFG_DIR"

# لینک‌ها یک‌بار گرفته و در فایل موقت (دسترسی ۶۰۰) ذخیره می‌شوند؛ سایت اشتراک ممکن است درخواست تکراری را رد کند
LF="$(mktemp)"; chmod 600 "$LF"; rm -f "$LF"
cleanup() { rm -f "$LF"; rm -rf "${TMP:-}"; }
trap cleanup EXIT
xl() { PYTHONPATH="$APP_DIR" python3 -m botfactory.xray_link --links-file "$LF" --xray "$BIN" "$@"; }

echo "▶ دریافت و بررسی لینک…"
printf '%s' "$LINK" | xl --list > /tmp/xray-list.$$ || { echo "تبدیل/دریافت لینک ناموفق بود." >&2; exit 1; }
unset LINK
cat /tmp/xray-list.$$; COUNT="$(grep -c . /tmp/xray-list.$$)"; rm -f /tmp/xray-list.$$

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

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable xray >/dev/null 2>&1
}

try_pick() {  # try_pick N → 0 اگر تلگرام از طریق این سرور واقعاً در دسترس بود
  local n="$1" code
  xl --pick "$n" > "$CFG_DIR/config.json.new" </dev/null || { rm -f "$CFG_DIR/config.json.new"; return 2; }
  "$BIN" run -test -c "$CFG_DIR/config.json.new" >/dev/null || { rm -f "$CFG_DIR/config.json.new"; return 2; }
  mv "$CFG_DIR/config.json.new" "$CFG_DIR/config.json"
  chown root:nogroup "$CFG_DIR/config.json"; chmod 640 "$CFG_DIR/config.json"
  install_unit
  systemctl restart xray
  local i
  for i in $(seq 1 12); do
    (exec 3<>/dev/tcp/127.0.0.1/1080) 2>/dev/null && break
    sleep 1
  done
  if ! (exec 3<>/dev/tcp/127.0.0.1/1080) 2>/dev/null; then
    echo "  ✘ سرویس xray روی پورت 1080 بالا نیامد. وضعیت:" >&2
    systemctl status xray --no-pager -l 2>&1 | head -12 >&2 || true
    journalctl -u xray -n 12 --no-pager 2>&1 >&2 || true
    return 3
  fi
  code="$(curl -s -o /dev/null -m 15 --noproxy '' -w '%{http_code}' --socks5-hostname 127.0.0.1:1080 https://api.telegram.org/ || true)"
  if [ -n "$code" ] && [ "$code" != "000" ]; then return 0; fi
  echo "  ✘ سرویس بالا آمد ولی تلگرام جواب نداد (کد ${code:-خالی})." >&2
  journalctl -u xray -n 8 --no-pager 2>&1 >&2 || true
  return 1
}

ANS=a
if [ "$COUNT" -gt 1 ]; then
  read -rp "Enter = انتخاب خودکار سریع‌ترین سرور (پیشنهادی)  |  یا شماره‌ی یک سرور: " ANS
  ANS="${ANS:-a}"
fi

OK=0; N=1
if [ "$ANS" = "a" ] || [ "$ANS" = "A" ]; then
  echo "▶ آزمایش تأخیر همه‌ی سرورها تا تلگرام (حدود ۱ تا ۳ دقیقه)…"
  RANK="$(xl --bench --test-url "${XRAY_TEST_URL:-https://api.telegram.org/}" </dev/null || true)"
  if [ -z "$RANK" ]; then
    echo "هیچ‌کدام از سرورها از این شبکه به تلگرام نرسیدند." >&2
  else
    echo "سریع‌ترین‌ها (شماره | میلی‌ثانیه | مشخصات):"
    printf '%s\n' "$RANK" | head -5
    while IFS=$'\t' read -r N MS _; do
      echo "▶ امتحان نهایی سرور $N (${MS}ms)…"
      if try_pick "$N"; then OK=1; break; fi
    done <<< "$RANK"
  fi
else
  [[ "$ANS" =~ ^[0-9]+$ ]] || { echo "شماره معتبر نیست." >&2; exit 1; }
  N="$ANS"
  try_pick "$N" && OK=1 || true
fi

if [ "$OK" = 1 ]; then
  echo "✔ تلگرام از طریق پروکسی در دسترس است (سرور شماره‌ی $N)."
  [ -f "$ENV_FILE" ] && set_env PROXY_URL "socks5://127.0.0.1:1080" && echo "  PROXY_URL در .env تنظیم شد."
  exit 0
fi
echo "✘ از طریق پروکسی به تلگرام نرسیدیم. لاگ: journalctl -u xray -n 30 --no-pager" >&2
echo "  احتمال‌ها: سرورها خراب/منقضی‌اند یا دیتاسنتر این پروتکل‌ها را قطع کرده است." >&2
exit 1
