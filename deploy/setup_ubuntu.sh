#!/usr/bin/env bash
# نصب کامل ربات روی سرور اوبونتو — فقط همین یک دستور:
#
#     bash deploy/setup_ubuntu.sh
#
# سه مقدار می‌پرسد (توکن ربات، api_id، api_hash)، ربات را به عنوان سرویس
# همیشه‌روشن نصب می‌کند، و بقیه‌ی راه‌اندازی داخل تلگرام با دکمه انجام می‌شود.

set -eu
cd "$(dirname "$0")/.."

step()  { printf '\n\033[36m=== %s ===\033[0m\n' "$1"; }

step "۱) نصب پایتون و کتابخانه‌ها (چند دقیقه)"
apt-get update -qq
apt-get install -y -qq python3 python3-pip python3-venv >/dev/null
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install --upgrade pip --quiet
.venv/bin/pip install -r requirements.txt --quiet
echo "  [OK] نصب شد"

step "۲) سه مقدار پایه"
.venv/bin/python -c "from signalbot.panel import ensure_env; ensure_env()"

step "۳) روشن کردن ربات به عنوان سرویس همیشگی"
bash deploy/install_service.sh >/dev/null
sleep 5
if systemctl is-active --quiet signalbot; then
    echo "  [OK] ربات روشن است و بعد از ری‌استارت سرور هم خودش بالا می‌آید"
else
    echo "  [X] ربات بالا نیامد. آخر لاگ:"
    tail -n 20 logs/service.log || true
    exit 1
fi

cat <<'EOF'

  ✅ تمام شد.

  حالا در تلگرام رباتی را که با BotFather ساختی باز کن و  /start  بزن.
  بقیه‌ی کارها (اکانت تلگرام، کانال، کلید Gemini، حساب معاملاتی) با
  دکمه‌ها همان‌جا انجام می‌شود.

  اولین نفری که /start بزند صاحب ربات می‌شود — پس همین الان بزن.

  دیدن لاگ:   tail -f logs/signalbot.log
EOF
