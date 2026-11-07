#!/usr/bin/env bash
# گام ۵ (اختیاری ولی توصیه‌شده): نگهبان خودکار پروکسی تلگرام.
# هر ۳ دقیقه اتصال را می‌سنجد و در صورت قطع‌شدن، سرور سالم‌تری از اشتراک شما انتخاب می‌کند.
# اجرا: sudo bash deploy/debian/05-watchdog.sh
set -euo pipefail
cd "$(dirname "$0")"; . ./lib.sh; need_root
DIR=/etc/botmother
LINK_FILE="$DIR/proxy-link"
mkdir -p "$DIR"; chmod 700 "$DIR"

echo "برای انتخاب خودکار سرور، نگهبان باید لینک اشتراک (یا لینک‌های سرور) را بداند."
echo "لینک فقط روی همین سرور و در فایلی با دسترسی ۶۰۰ (فقط root) ذخیره می‌شود: $LINK_FILE"
if [ -s "$LINK_FILE" ]; then
  echo "یک لینک از قبل ذخیره شده است. Enter = همان را نگه دار | یا لینک جدید را بچسبانید"
else
  echo "لینک را بچسبانید و Enter بزنید (هنگام چسباندن چیزی نمایش داده نمی‌شود):"
fi
read -rs LINK; echo
if [ -z "$LINK" ]; then
  [ -s "$LINK_FILE" ] || { echo "لینکی وارد نشد." >&2; exit 1; }
else
  TMP="$(mktemp -u)"; trap 'rm -f "$TMP"' EXIT
  printf '%s' "$LINK" | PYTHONPATH="$APP_DIR" python3 -m botfactory.xray_link --links-file "$TMP" --list > /tmp/wd-list.$$ \
    || { echo "لینک معتبر نبود یا دریافت نشد." >&2; rm -f /tmp/wd-list.$$; exit 1; }
  echo "✔ $(grep -c . /tmp/wd-list.$$) سرور شناسایی شد."; rm -f /tmp/wd-list.$$
  ( umask 077; printf '%s\n' "$LINK" > "$LINK_FILE" ); chmod 600 "$LINK_FILE"
fi
unset LINK

install -m 755 xray-watchdog.sh /usr/local/sbin/botmother-xray-watchdog
cat > /etc/systemd/system/botmother-watchdog.service <<'UNIT'
[Unit]
Description=BotMother Telegram proxy watchdog
After=xray.service

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/botmother-xray-watchdog
TimeoutStartSec=900
UNIT
cat > /etc/systemd/system/botmother-watchdog.timer <<'UNIT'
[Unit]
Description=Run the BotMother proxy watchdog every 3 minutes

[Timer]
OnBootSec=2min
OnUnitInactiveSec=3min
AccuracySec=15s

[Install]
WantedBy=timers.target
UNIT
systemctl daemon-reload
systemctl enable --now botmother-watchdog.timer >/dev/null 2>&1
systemctl start botmother-watchdog.service || true
echo "✔ نگهبان نصب و فعال شد (هر ۳ دقیقه)."
echo "  گزارش: journalctl -u botmother-watchdog -n 20 --no-pager"
echo "  حذف لینک ذخیره‌شده: rm $LINK_FILE   |  توقف: systemctl disable --now botmother-watchdog.timer"
