#!/usr/bin/env bash
# گام ۴ (فقط بعد از داشتن دامنه): nginx + گواهی Let's Encrypt برای بازگشت از درگاه‌های پرداخت.
# اجرا: sudo bash deploy/debian/04-https.sh bots.example.com you@example.com
# پیش‌نیاز: رکورد A دامنه به IP همین سرور اشاره کند و پورت‌های ۸۰ و ۴۴۳ باز باشند.
set -euo pipefail
cd "$(dirname "$0")"; . ./lib.sh; need_root
DOMAIN="${1:-}"; EMAIL="${2:-}"
[[ "$DOMAIN" =~ ^[A-Za-z0-9.-]+\.[A-Za-z]{2,}$ ]] || { echo "استفاده: $0 دامنه ایمیل" >&2; exit 1; }
[[ "$EMAIL" =~ ^[^@\ ]+@[^@\ ]+$ ]] || { echo "ایمیل معتبر لازم است." >&2; exit 1; }

SERVER_IP="$(curl -4 -s -m 8 https://api.ipify.org || true)"
DNS_IP="$(getent ahostsv4 "$DOMAIN" | awk 'NR==1{print $1}')"
if [ -n "$SERVER_IP" ] && [ "$DNS_IP" != "$SERVER_IP" ]; then
  echo "✘ دامنه $DOMAIN به «${DNS_IP:-هیچ‌چیز}» اشاره می‌کند ولی IP سرور $SERVER_IP است. رکورد A را اصلاح کنید و کمی صبر کنید." >&2
  exit 1
fi

cat > /etc/nginx/sites-available/botmother <<CONF
server {
    listen 80;
    server_name $DOMAIN;
    client_max_body_size 5m;
    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 60s;
    }
}
CONF
ln -sf /etc/nginx/sites-available/botmother /etc/nginx/sites-enabled/botmother
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl enable --now nginx
systemctl reload nginx
certbot --nginx -d "$DOMAIN" -m "$EMAIL" --agree-tos --no-eff-email --redirect --non-interactive

set_env BASE_URL "https://$DOMAIN"
systemctl restart botmother
sleep 4
CODE="$(curl -s -o /dev/null -m 10 -w '%{http_code}' "https://$DOMAIN/healthz" || true)"
echo "healthz → $CODE   (باید 200 باشد)"
echo "حالا درگاه زرین‌پال/NOWPayments داخل پنل ربات‌ها قابل‌استفاده است."
