#!/usr/bin/env bash
# ربات را به عنوان سرویس systemd نصب می‌کند تا همیشه بالا بماند
# و بعد از ری‌استارت سرور خودکار اجرا شود.
#
#     bash deploy/install_service.sh

set -eu
cd "$(dirname "$0")/.."
PROJECT="$(pwd)"
SERVICE=/etc/systemd/system/signalbot.service

if [ ! -x "$PROJECT/.venv/bin/python" ]; then
    echo "اول deploy/setup_ubuntu.sh را اجرا کن." >&2
    exit 1
fi

cat > "$SERVICE" <<EOF
[Unit]
Description=Telegram signal copier -> cTrader
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$PROJECT
ExecStart=$PROJECT/.venv/bin/python $PROJECT/run.py
Restart=always
RestartSec=15
StandardOutput=append:$PROJECT/logs/service.log
StandardError=append:$PROJECT/logs/service.log

[Install]
WantedBy=multi-user.target
EOF

mkdir -p "$PROJECT/logs"
systemctl daemon-reload
systemctl enable signalbot
systemctl restart signalbot
sleep 2

echo
echo "✅ سرویس نصب و اجرا شد."
echo
echo "دستورهای مفید:"
echo "  وضعیت:      systemctl status signalbot"
echo "  لاگ زنده:    journalctl -u signalbot -f"
echo "  توقف:        systemctl stop signalbot"
echo "  شروع دوباره: systemctl restart signalbot"
echo
systemctl status signalbot --no-pager | head -12
