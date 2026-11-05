#!/usr/bin/env bash
# گام ۱: پکیج‌های پایه، کاربر سرویس، swap و tmux.   اجرا: sudo bash deploy/debian/01-base.sh
set -euo pipefail
cd "$(dirname "$0")"; . ./lib.sh; need_root

echo "▶ نصب پکیج‌ها…"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y python3 python3-venv python3-pip git curl unzip ca-certificates tmux nginx certbot python3-certbot-nginx

PYV="$(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])')"
python3 - <<'PY' || { echo "پایتون ۳٫۱۱ یا بالاتر لازم است." >&2; exit 1; }
import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)
PY
echo "  پایتون $PYV ✔"

if ! id "$APP_USER" >/dev/null 2>&1; then
  echo "▶ ساخت کاربر سرویس $APP_USER"
  useradd --system --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"
fi
mkdir -p "$APP_DIR/data"; chown -R "$APP_USER":"$APP_USER" "$APP_DIR/data"

if ! swapon --show | grep -q .; then
  echo "▶ ساخت swap دو گیگی (رم سرور کم است)"
  fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile >/dev/null && swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

# nginx تا گام HTTPS لازم نیست؛ پیش‌فرضش را غیرفعال نگه می‌داریم
systemctl disable --now nginx >/dev/null 2>&1 || true
echo "✔ گام ۱ تمام شد."
