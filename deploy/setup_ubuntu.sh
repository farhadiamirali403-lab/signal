#!/usr/bin/env bash
# راه‌اندازی ربات روی سرور اوبونتو.
#
# روی سرور اجرا کن:
#     bash deploy/setup_ubuntu.sh
#
# چیزی را خراب نمی‌کند: نصب و بررسی می‌کند و می‌گوید چه مانده.

set -u
cd "$(dirname "$0")/.."
PROJECT="$(pwd)"

green() { printf '\033[32m  [OK] %s\033[0m\n' "$1"; }
warn()  { printf '\033[33m  [!]  %s\033[0m\n' "$1"; }
bad()   { printf '\033[31m  [X]  %s\033[0m\n' "$1"; }
step()  { printf '\n\033[36m=== %s ===\033[0m\n' "$1"; }

step "۱) بسته‌های سیستمی"
if ! command -v python3 >/dev/null; then
    apt-get update -qq && apt-get install -y -qq python3 python3-pip python3-venv
fi
python3 --version
apt-get install -y -qq python3-venv >/dev/null 2>&1 || true
green "پایتون آماده است"

step "۲) محیط مجازی و کتابخانه‌ها"
if [ ! -d .venv ]; then
    python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
pip install --upgrade pip --quiet
pip install -r requirements.txt --quiet
if [ $? -ne 0 ]; then bad "نصب کتابخانه‌ها ناموفق بود"; exit 1; fi
green "کتابخانه‌ها نصب شدند (MetaTrader5 روی لینوکس رد می‌شود، طبیعی است)"

step "۳) فایل .env"
if [ ! -f .env ]; then
    cp .env.example .env
    warn ".env ساخته شد ولی خالی است."
    warn "با  nano .env  مقادیر را پر کن و دوباره این اسکریپت را بزن."
    exit 1
fi

missing=0
for key in TG_API_ID TG_API_HASH TG_BOT_TOKEN GEMINI_API_KEY \
           CTRADER_CLIENT_ID CTRADER_CLIENT_SECRET; do
    value=$(grep -E "^\s*${key}\s*=" .env | head -1 | cut -d= -f2- | xargs)
    case "$value" in
        ""|1234567|your_api_hash_here|"123456:AAA..."|"AIza..."|12345678|your_password)
            bad "$key پر نشده"; missing=1 ;;
        *) green "$key" ;;
    esac
done
[ "$missing" -eq 1 ] && { warn "اول این‌ها را در .env پر کن:  nano .env"; exit 1; }

step "۴) پراکسی Gemini"
if grep -qE "^\s*GEMINI_PROXY\s*=\s*\S" .env; then
    warn "GEMINI_PROXY فعال است. این سرور خارج از ایران است، پس احتمالاً لازم نیست."
    warn "اگر check_vision خطا داد، آن خط را با # کامنت کن."
else
    green "پراکسی خاموش است"
fi

step "۵) توکن cTrader"
token=$(grep -E "^\s*CTRADER_ACCESS_TOKEN\s*=" .env | head -1 | cut -d= -f2- | xargs)
if [ -z "$token" ]; then
    warn "هنوز توکن نگرفته‌ای. الان اجرا می‌شود:"
    echo
    python tools/ctrader_auth.py
else
    green "توکن موجود است"
fi

step "۶) تست اتصال به بروکر"
python tools/check_broker.py || warn "اتصال بروکر برقرار نشد — خروجی بالا را ببین"

step "۷) تست خواندن عکس"
python tools/check_vision.py || warn "Gemini جواب نداد — خروجی بالا را ببین"

step "۸) ورود تلگرام"
if [ -f sessions/user.session ]; then
    green "نشست تلگرام از قبل روی این سرور هست"
else
    warn "هنوز وارد تلگرام نشده‌ای. شماره و کد را وارد کن:"
    echo
    python tools/list_chats.py
fi

step "قدم‌های بعدی"
cat <<EOF

  1) آیدی کانال و آیدی خودت را در config.yaml بگذار:
       nano config.yaml

  2) تست روی کانال واقعی، بدون باز کردن معامله:
       source .venv/bin/activate && python tools/dry_run.py 200 --images 20

  3) اجرای دائمی به عنوان سرویس:
       bash deploy/install_service.sh

  یادآوری: ctrader.live در config.yaml روی false بماند تا با حساب دمو کار کنی.
  حالت اجرا 'auto' است، یعنی بدون تایید تو معامله باز می‌کند.

EOF
