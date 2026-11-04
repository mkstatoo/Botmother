#!/usr/bin/env bash
# پشتیبان‌گیری از دیتابیس SQLite و کلید رمزنگاری (data/fernet.key).
# استفاده:  ./scripts/backup.sh [پوشه‌ی مقصد، پیش‌فرض ./backups]
# پیشنهاد cron (هر شب ساعت ۳):  0 3 * * *  cd /opt/botmother && ./scripts/backup.sh
# ⚠️ بدون fernet.key، توکن‌های رمزنگاری‌شده‌ی داخل دیتابیس قابل بازیابی نیستند.
set -euo pipefail

cd "$(dirname "$0")/.."
DATA_DIR="${DATA_DIR:-data}"
OUT_DIR="${1:-backups}"
KEEP="${KEEP:-14}"
STAMP="$(date +%Y%m%d-%H%M%S)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

mkdir -p "$OUT_DIR"

if [ -f "$DATA_DIR/botfactory.db" ]; then
  # کپی امن حتی وقتی ربات در حال کار است
  python3 - "$DATA_DIR/botfactory.db" "$TMP/botfactory.db" <<'PY'
import sqlite3, sys
src = sqlite3.connect(sys.argv[1])
dst = sqlite3.connect(sys.argv[2])
with dst:
    src.backup(dst)
dst.close(); src.close()
PY
else
  echo "هشدار: $DATA_DIR/botfactory.db پیدا نشد (اگر از PostgreSQL استفاده می‌کنید از pg_dump بگیرید)." >&2
fi

if [ -f "$DATA_DIR/fernet.key" ]; then
  cp "$DATA_DIR/fernet.key" "$TMP/fernet.key"
else
  echo "هشدار: $DATA_DIR/fernet.key پیدا نشد (اگر FERNET_KEY در .env است، آن را جداگانه نگه دارید)." >&2
fi

[ -n "$(ls -A "$TMP")" ] || { echo "چیزی برای پشتیبان‌گیری نبود." >&2; exit 1; }

FILE="$OUT_DIR/botmother-$STAMP.tar.gz"
umask 077
tar -czf "$FILE" -C "$TMP" .
echo "ساخته شد: $FILE"

# نگه‌داری فقط آخرین $KEEP نسخه
ls -1t "$OUT_DIR"/botmother-*.tar.gz 2>/dev/null | tail -n +"$((KEEP + 1))" | xargs -r rm -f --
