# راه‌اندازی یک‌مرحله‌ای روی سرور مجازی.
#
# روی VPS، PowerShell را as Administrator باز کن، برو داخل پوشه‌ی پروژه و بزن:
#     powershell -ExecutionPolicy Bypass -File deploy\setup.ps1
#
# این اسکریپت چیزی را خراب نمی‌کند: فقط نصب و بررسی می‌کند و می‌گوید چه مانده.

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $projectRoot

function Step($number, $title) {
    Write-Host ""
    Write-Host "=== $number) $title ===" -ForegroundColor Cyan
}
function Ok($msg)   { Write-Host "  [OK] $msg" -ForegroundColor Green }
function Warn($msg) { Write-Host "  [!]  $msg" -ForegroundColor Yellow }
function Bad($msg)  { Write-Host "  [X]  $msg" -ForegroundColor Red }

$problems = @()

Step 1 "بررسی پایتون"
try {
    $pyVersion = (python --version 2>&1) -join ""
    Ok $pyVersion
} catch {
    Bad "پایتون پیدا نشد. از python.org نصبش کن و تیک 'Add python.exe to PATH' را بزن."
    exit 1
}

Step 2 "نصب کتابخانه‌ها"
python -m pip install --upgrade pip --quiet
python -m pip install -r requirements.txt --quiet
if ($LASTEXITCODE -ne 0) { Bad "نصب کتابخانه‌ها ناموفق بود"; exit 1 }
Ok "کتابخانه‌ها نصب شدند"

Step 3 "فایل تنظیمات"
if (-not (Test-Path "config.yaml")) {
    Copy-Item "config.example.yaml" "config.yaml"
    Ok "config.yaml از روی نمونه ساخته شد"
} else {
    Ok "config.yaml موجود است"
}

Step 4 "بررسی فایل .env"
if (-not (Test-Path ".env")) {
    if (Test-Path ".env.example") {
        Copy-Item ".env.example" ".env"
        Warn ".env ساخته شد ولی خالی است — مقادیر واقعی را داخلش بگذار و دوباره اجرا کن."
        notepad ".env"
        exit 1
    }
    Bad ".env و .env.example هیچ‌کدام نیستند"
    exit 1
}

$envText = Get-Content ".env" -Raw
$broker = (Get-Content "config.yaml" | Where-Object { $_ -match "^\s*broker\s*:" }) -replace ".*:\s*", ""
$broker = $broker.Trim()
Ok "بروکر: $broker"
$required = @("TG_API_ID", "TG_API_HASH", "TG_BOT_TOKEN", "GEMINI_API_KEY")
if ($broker -eq "ctrader") {
    $required += @("CTRADER_CLIENT_ID", "CTRADER_CLIENT_SECRET")
} else {
    $required += @("MT5_LOGIN", "MT5_PASSWORD", "MT5_SERVER")
}
$placeholders = @("1234567", "your_api_hash_here", "123456:AAA...", "AIza...",
                  "12345678", "your_password")
foreach ($key in $required) {
    $line = ($envText -split "`n" | Where-Object { $_ -match "^\s*$key\s*=" }) | Select-Object -First 1
    if (-not $line) {
        $problems += "$key در .env نیست"
        Bad "$key تنظیم نشده"
        continue
    }
    $value = ($line -split "=", 2)[1].Trim()
    if (-not $value -or $placeholders -contains $value) {
        $problems += "$key هنوز مقدار نمونه دارد"
        Bad "$key هنوز پر نشده"
    } else {
        Ok $key
    }
}

Step 5 "پراکسی Gemini"
if ($envText -match "(?m)^\s*GEMINI_PROXY\s*=\s*\S") {
    Warn "GEMINI_PROXY فعال است. اگر این سرور خارج از ایران است، آن خط را کامنت کن (# اولش)."
} else {
    Ok "پراکسی خاموش است — درست، اگر سرور خارج از ایران باشد"
}

if ($problems.Count -gt 0) {
    Write-Host ""
    Bad "اول این‌ها را در .env درست کن، بعد دوباره همین اسکریپت را بزن:"
    $problems | ForEach-Object { Write-Host "     - $_" }
    notepad ".env"
    exit 1
}

Step 6 "تست اتصال به بروکر"
if ($broker -eq "ctrader") {
    $token = ($envText -split "`n" | Where-Object { $_ -match "^\s*CTRADER_ACCESS_TOKEN\s*=\s*\S" })
    if (-not $token) {
        Warn "هنوز توکن cTrader نگرفته‌ای. الان اجرا می‌شود:"
        python tools\ctrader_auth.py
    }
}
python tools\check_broker.py
if ($LASTEXITCODE -ne 0) {
    Warn "اتصال بروکر برقرار نشد — خروجی بالا را ببین."
}

Step 7 "تست خواندن عکس (Gemini)"
python tools\check_vision.py
if ($LASTEXITCODE -ne 0) {
    Warn "Gemini جواب نداد — خروجی بالا می‌گوید مشکل از کلید است یا از شبکه."
}

Step 8 "ورود تلگرام"
if (Test-Path "sessions\user.session") {
    Ok "نشست تلگرام از قبل روی این سرور هست"
} else {
    Warn "هنوز وارد تلگرام نشده‌ای. الان اجرا می‌شود — شماره و کدی که تلگرام می‌فرستد را وارد کن."
    Write-Host ""
    python tools\list_chats.py
    Write-Host ""
    Warn "آیدی کانال سیگنال را از لیست بالا بردار و در config.yaml بگذار."
}

Write-Host ""
Write-Host "=== قدم‌های بعدی ===" -ForegroundColor Cyan
Write-Host ""
Write-Host "  1) آیدی کانال و آیدی خودت در config.yaml درست باشد"
Write-Host "  2) تست روی کانال واقعی، بدون باز کردن معامله:"
Write-Host "       python tools\dry_run.py 200 --images 20"
Write-Host "  3) اجرای خودکار بعد از هر ری‌استارت ویندوز:"
Write-Host "       powershell -ExecutionPolicy Bypass -File deploy\install_autostart.ps1"
Write-Host "  4) اجرای دستی برای تست:"
Write-Host "       deploy\run_bot.bat"
Write-Host ""
Write-Host "  یادآوری: اول با حساب دمو. حالت اجرا الان 'auto' است یعنی بدون" -ForegroundColor Yellow
Write-Host "  تایید تو معامله باز می‌کند." -ForegroundColor Yellow
Write-Host ""
