# توابع مشترک اسکریپت‌های نصب (با source بارگذاری می‌شود)
APP_DIR="${APP_DIR:-/opt/botmother}"
APP_USER="${APP_USER:-botmother}"
ENV_FILE="$APP_DIR/.env"

need_root() { [ "$(id -u)" -eq 0 ] || { echo "این اسکریپت باید با root اجرا شود." >&2; exit 1; }; }

# set_env KEY VALUE  → در .env جایگزین یا اضافه می‌کند
set_env() {
  local key="$1" value="$2"
  touch "$ENV_FILE"; chmod 600 "$ENV_FILE"
  if grep -q "^${key}=" "$ENV_FILE"; then
    python3 - "$ENV_FILE" "$key" "$value" <<'PY'
import sys
path, key, value = sys.argv[1:4]
lines = open(path, encoding="utf-8").read().splitlines()
lines = [f"{key}={value}" if l.startswith(key + "=") else l for l in lines]
open(path, "w", encoding="utf-8").write("\n".join(lines) + "\n")
PY
  else
    printf '%s=%s\n' "$key" "$value" >> "$ENV_FILE"
  fi
  chown "$APP_USER":"$APP_USER" "$ENV_FILE" 2>/dev/null || true
}

get_env() { grep -E "^$1=" "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2- || true; }
