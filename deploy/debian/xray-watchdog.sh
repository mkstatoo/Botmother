#!/usr/bin/env bash
# نگهبان پروکسی تلگرام: هر چند دقیقه اتصال تلگرام از طریق Xray را می‌سنجد؛
# اگر دو بار پشت‌سرهم قطع بود، همه‌ی سرورهای اشتراک را می‌سنجد، سریع‌ترین سرور سالم را جایگزین می‌کند
# و (اگر بتواند) به ادمین در تلگرام خبر می‌دهد. نصب: sudo bash deploy/debian/05-watchdog.sh
# مسیرها برای تست قابل تغییرند (متغیرهای محیطی).
set -u
APP_DIR="${APP_DIR:-/opt/botmother}"
CFG_DIR="${CFG_DIR:-/usr/local/etc/xray}"
BIN="${BIN:-/usr/local/bin/xray}"
STATE="${STATE:-/var/lib/botmother-watchdog}"
LINK_FILE="${LINK_FILE:-/etc/botmother/proxy-link}"
ENV_FILE="${ENV_FILE:-$APP_DIR/.env}"
PROXY_ADDR="${PROXY_ADDR:-127.0.0.1:1080}"
CHECK_URL="${CHECK_URL:-https://api.telegram.org/}"
API_BASE="${API_BASE:-https://api.telegram.org}"
FAILS_NEEDED="${FAILS_NEEDED:-2}"      # تعداد اجرای ناموفق پیاپی قبل از اقدام
COOLDOWN="${COOLDOWN:-600}"            # حداقل فاصله‌ی دو انتخاب مجدد (ثانیه)
RECHECK_DELAY="${RECHECK_DELAY:-10}"   # سنجش دوباره برای رد کردن قطعی‌های لحظه‌ای
PORT_WAIT="${PORT_WAIT:-12}"

log() { echo "[watchdog] $*"; }
mkdir -p "$STATE"; chmod 700 "$STATE"
exec 9>"$STATE/lock"
flock -n 9 || exit 0   # نمونه‌ی دیگری در حال اجراست

get_env() { grep -E "^$1=" "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2- || true; }

check() {
  local code
  code="$(curl -s -o /dev/null -m 15 --noproxy '' -w '%{http_code}' --socks5-hostname "$PROXY_ADDR" "$CHECK_URL" 2>/dev/null || true)"
  [ -n "$code" ] && [ "$code" != "000" ]
}

notify() {  # notify TEXT → 0 اگر حداقل برای یک ادمین ارسال شد (توکن در آرگومان‌های curl دیده نمی‌شود)
  local text="$1" token ids id out sent=1
  token="$(get_env BOT_TOKEN)"; ids="$(get_env ADMIN_IDS)"
  [ -n "$token" ] && [ -n "$ids" ] || return 2
  for id in ${ids//,/ }; do
    out="$(curl -s -m 20 --noproxy '' --socks5-hostname "$PROXY_ADDR" -K - \
      --data-urlencode "chat_id=$id" --data-urlencode "text=$text" <<< "url = \"$API_BASE/bot$token/sendMessage\"" 2>/dev/null || true)"
    case "$out" in *'"ok":true'*) sent=0 ;; esac
  done
  return $sent
}

minutes_since() { local s; s="$(cat "$1" 2>/dev/null || echo "$(date +%s)")"; echo $(( ( $(date +%s) - s + 59 ) / 60 )); }

wait_port() {
  local i=0
  while [ "$i" -lt "$PORT_WAIT" ]; do
    (exec 3<>"/dev/tcp/${PROXY_ADDR%:*}/${PROXY_ADDR##*:}") 2>/dev/null && return 0
    sleep 1; i=$((i + 1))
  done
  [ "$PORT_WAIT" -eq 0 ]
}

repick() {
  if [ ! -s "$LINK_FILE" ]; then
    log "لینک اشتراک ذخیره نشده؛ فقط xray ری‌استارت می‌شود."
    systemctl restart xray || true
    return 1
  fi
  local LF; LF="$(mktemp -u)"
  trap 'rm -f "$LF" "$CFG_DIR/config.new.json"' RETURN
  xl() { PYTHONPATH="$APP_DIR" python3 -m botfactory.xray_link --links-file "$LF" --xray "$BIN" "$@"; }
  log "آزمایش تأخیر همه‌ی سرورها…"
  local RANK; RANK="$(xl --bench --test-url "$CHECK_URL" < "$LINK_FILE" || true)"
  if [ -z "$RANK" ]; then log "هیچ سروری از این شبکه به تلگرام نرسید."; return 1; fi
  local N MS DESC
  while IFS=$'\t' read -r N MS DESC; do
    xl --pick "$N" > "$CFG_DIR/config.new.json" < /dev/null || continue
    "$BIN" run -test -c "$CFG_DIR/config.new.json" > /dev/null 2>&1 || continue
    mv "$CFG_DIR/config.new.json" "$CFG_DIR/config.json"
    chown root:nogroup "$CFG_DIR/config.json" 2>/dev/null || true
    chmod 640 "$CFG_DIR/config.json"
    systemctl restart xray || true
    wait_port || continue
    if check; then
      log "سرور $N انتخاب شد (${MS}ms)."
      echo "$N|$MS" > "$STATE/last_server"
      return 0
    fi
  done <<< "$RANK"
  return 1
}

# ------------------------------------------------------------------ main
if check; then
  rm -f "$STATE/fails" "$STATE/last_repick"
  if [ -f "$STATE/outage_since" ]; then
    mins="$(minutes_since "$STATE/outage_since")"
    srv=""; [ -f "$STATE/last_server" ] && srv=" (سرور شماره‌ی $(cut -d'|' -f1 "$STATE/last_server")، حدود $(cut -d'|' -f2 "$STATE/last_server" | cut -d. -f1) میلی‌ثانیه)"
    if notify "🔁 نگهبان پروکسی: اتصال تلگرام حدود ${mins} دقیقه قطع بود و دوباره برقرار شد${srv}." || [ $? -eq 2 ]; then
      rm -f "$STATE/outage_since" "$STATE/last_server"
    fi
  fi
  exit 0
fi

sleep "$RECHECK_DELAY"
if check; then rm -f "$STATE/fails"; exit 0; fi

fails=$(( $(cat "$STATE/fails" 2>/dev/null || echo 0) + 1 )); echo "$fails" > "$STATE/fails"
[ -f "$STATE/outage_since" ] || date +%s > "$STATE/outage_since"
log "اتصال تلگرام قطع است (بار $fails)."
[ "$fails" -ge "$FAILS_NEEDED" ] || exit 0

last="$(cat "$STATE/last_repick" 2>/dev/null || echo 0)"; now="$(date +%s)"
if [ $((now - last)) -lt "$COOLDOWN" ]; then log "هنوز در فاصله‌ی مجاز بین دو انتخاب مجدد هستیم."; exit 0; fi
echo "$now" > "$STATE/last_repick"

if repick; then
  rm -f "$STATE/fails" "$STATE/last_repick"
  mins="$(minutes_since "$STATE/outage_since")"; N="$(cut -d'|' -f1 "$STATE/last_server")"; MS="$(cut -d'|' -f2 "$STATE/last_server" | cut -d. -f1)"
  if notify "🔁 نگهبان پروکسی: اتصال تلگرام حدود ${mins} دقیقه قطع بود؛ به سرور شماره‌ی ${N} (${MS} میلی‌ثانیه) منتقل شد."; then
    rm -f "$STATE/outage_since" "$STATE/last_server"
  fi
  exit 0
fi
log "هنوز سرور سالمی پیدا نشد؛ در اجرای بعدی دوباره تلاش می‌شود."
exit 1
