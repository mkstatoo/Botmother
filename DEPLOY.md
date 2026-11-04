# 🚀 راهنمای استقرار (VPS + Docker)

مسیر پیشنهادی: یک سرور لینوکسی (Ubuntu 22.04/24.04)، Docker، یک دامنه. Caddy گواهی HTTPS را خودکار می‌گیرد.

> **اول مکان سرور را تصمیم بگیرید** (مهم‌ترین انتخاب):
> | گزینه | تلگرام | زرین‌پال | توضیح |
> |---|---|---|---|
> | سرور داخل ایران | ❌ فیلتر؛ باید `PROXY_URL` بدهید | ✅ | برای بازار ایران مناسب‌تر؛ به یک پروکسی پایدار نیاز دارد |
> | سرور خارج ایران | ✅ | ⚠️ ممکن است IP خارجی رد شود؛ `ZARINPAL_BASE` را روی یک رله‌ی داخل ایران بگذارید | برای NOWPayments و تلگرام بدون دردسر است |
>
> در هر دو حالت قبل از انتشار، یک پرداخت کوچک واقعی آزمایش کنید.

## ۱) پیش‌نیازها
1. ربات مادر: در @BotFather یک ربات بسازید و توکنش را نگه دارید (`BOT_TOKEN`).
2. آیدی عددی خودتان (مثلاً از @userinfobot) برای `ADMIN_IDS`.
3. دامنه‌ای که رکورد **A** آن به IP سرور اشاره کند (مثلاً `bots.example.com`). پورت‌های ۸۰ و ۴۴۳ باز باشند.
4. نصب Docker روی سرور: `curl -fsSL https://get.docker.com | sh`

## ۲) دریافت کد و تنظیم
```bash
git clone https://github.com/mkstatoo/Botmother.git /opt/botmother
cd /opt/botmother
cp .env.example .env
nano .env
```
حداقل این‌ها را پر کنید:
```
BOT_TOKEN=توکن ربات مادر
ADMIN_IDS=آیدی عددی شما
DOMAIN=bots.example.com
BASE_URL=https://bots.example.com
# فقط اگر سرور به تلگرام دسترسی مستقیم ندارد:
# PROXY_URL=socks5://127.0.0.1:1080
```
`MODE` را لازم نیست بگذارید؛ در فایل prod روی `webhook` تنظیم است. `FERNET_KEY` را خالی بگذارید تا خودکار در `data/fernet.key` ساخته شود (**حتماً پشتیبان بگیرید**، بخش ۵).

## ۳) اجرا
دو روش برای گرفتن ایمیج:

**الف) ساخت از روی کد (ساده‌ترین، بدون نیاز به ایمیج آماده):**
```bash
sed -i 's|^    image: ghcr.io/.*|    build: .|' docker-compose.prod.yml
docker compose -f docker-compose.prod.yml up -d --build
```

**ب) ایمیج آماده‌ی GitHub** (پس از اولین اجرای موفق CI): ایمیج در GHCR به‌طور پیش‌فرض **private** است؛ یا در GitHub بخش Packages آن را Public کنید، یا روی سرور لاگین کنید:
```bash
echo <GITHUB_TOKEN_با_دسترسی_read:packages> | docker login ghcr.io -u mkstatoo --password-stdin
docker compose -f docker-compose.prod.yml up -d
```

بررسی:
```bash
docker compose -f docker-compose.prod.yml ps
docker compose -f docker-compose.prod.yml logs -f botmother
curl https://bots.example.com/healthz     # {"ok": true, ...}
```
سپس ربات مادر را در تلگرام `/start` کنید.

## ۴) به‌روزرسانی
```bash
cd /opt/botmother
./scripts/backup.sh                       # قبل از هر به‌روزرسانی
git pull
docker compose -f docker-compose.prod.yml up -d --build   # یا: pull && up -d
```
> ساختار جدول‌ها فقط با `create_all` ساخته می‌شود و ابزار migration ندارد. اگر نسخه‌ای ستون جدید اضافه کند، در همان نسخه توضیح داده می‌شود؛ پشتیبان قبل از به‌روزرسانی را فراموش نکنید.

## ۵) پشتیبان‌گیری و بازیابی
```bash
./scripts/backup.sh            # خروجی: backups/botmother-تاریخ.tar.gz (۱۴ نسخه‌ی آخر نگه داشته می‌شود)
```
شبانه با cron: `crontab -e` ← `0 3 * * * cd /opt/botmother && ./scripts/backup.sh`
نسخه‌ها را **خارج از همان سرور** هم کپی کنید (مثلاً `rsync`/`scp`).

بازیابی:
```bash
docker compose -f docker-compose.prod.yml down
mkdir -p data && tar -xzf backups/botmother-XXXX.tar.gz -C data/
docker compose -f docker-compose.prod.yml up -d
```
⚠️ دیتابیس و `fernet.key` باید **هم‌زمان** برگردانده شوند، وگرنه توکن ربات‌ها خوانده نمی‌شود.
اگر از PostgreSQL استفاده می‌کنید، دیتابیس را با `pg_dump` بگیرید و فقط `fernet.key` را از این اسکریپت بردارید.

## ۶) اجرای ساده بدون دامنه (آزمایشی)
بدون دامنه و HTTPS هم ربات‌ها کار می‌کنند (`MODE=polling` و `BASE_URL` خالی) اما **درگاه زرین‌پال و NOWPayments کار نمی‌کند** (بازگشت از درگاه به آدرس عمومی نیاز دارد). واریز دستی کارت‌به‌کارت بدون دامنه کار می‌کند:
```bash
docker compose up -d --build        # فایل docker-compose.yml، پورت ۸۰۸۰
```

## ۷) پلتفرم‌های آماده (Railway / Render / Fly.io)
از همین `Dockerfile` استفاده می‌شود. نکات:
- یک **Volume دائمی** روی `/app/data` وصل کنید (وگرنه با هر deploy دیتابیس و کلید پاک می‌شود)، یا `DATABASE_URL` را روی PostgreSQL مدیریت‌شده و `FERNET_KEY` را در متغیرهای محیطی بگذارید.
- متغیرهای `.env.example` را در تنظیمات پلتفرم وارد کنید و `MODE=webhook`، `BASE_URL=https://…` (آدرس عمومی سرویس)، `WEB_PORT` برابر پورت پلتفرم.
- فقط **یک نمونه** از برنامه اجرا شود (چند نمونه با هم، polling تلگرام را دچار تداخل می‌کند).

## ۸) عیب‌یابی
| نشانه | علت محتمل |
|---|---|
| لاگ: ارتباط با تلگرام برقرار نشد | سرور به تلگرام نمی‌رسد ← `PROXY_URL` |
| گواهی HTTPS صادر نمی‌شود | رکورد A دامنه یا پورت‌های ۸۰/۴۴۳ |
| بازگشت از درگاه خطا می‌دهد | `BASE_URL` اشتباه یا بدون https |
| زرین‌پال خطای IP می‌دهد | سرور خارج از ایران ← `ZARINPAL_BASE` با رله‌ی داخلی |
| ربات سازنده جواب نمی‌دهد | توکن باطل شده؛ در «ربات‌های من» وضعیت ⚠️ دارد ← تغییر توکن |
| به سازنده پیام فروش نمی‌رسد | سازنده باید ربات خودش را یک‌بار `/start` کرده باشد |
