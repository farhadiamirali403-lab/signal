# ثبت ربات در Task Scheduler ویندوز تا بعد از هر ری‌استارت خودکار بالا بیاید.
#
# روی VPS، PowerShell را as Administrator باز کن و بزن:
#     powershell -ExecutionPolicy Bypass -File deploy\install_autostart.ps1
#
# برای حذف:
#     Unregister-ScheduledTask -TaskName "SignalBot" -Confirm:$false

$ErrorActionPreference = "Stop"

$taskName = "SignalBot"
$projectRoot = Split-Path -Parent $PSScriptRoot
$batch = Join-Path $PSScriptRoot "run_bot.bat"

if (-not (Test-Path $batch)) {
    throw "فایل run_bot.bat پیدا نشد: $batch"
}

Write-Host "پوشه‌ی پروژه: $projectRoot"

# ربات باید در نشست تعاملی اجرا شود، چون متاتریدر به دسکتاپ نیاز دارد
$action  = New-ScheduledTaskAction -Execute $batch -WorkingDirectory $projectRoot
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -RestartCount 999 `
    -ExecutionTimeLimit ([TimeSpan]::Zero)

if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
    Write-Host "تسک قبلی پیدا شد، حذف می‌شود..."
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
}

Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -RunLevel Highest `
    -Description "ربات کپی سیگنال تلگرام به متاتریدر ۵" | Out-Null

Write-Host ""
Write-Host "✅ تسک «$taskName» ثبت شد — بعد از هر ورود به ویندوز خودکار اجرا می‌شود."
Write-Host ""
Write-Host "برای اینکه بعد از ری‌استارت ویندوز بدون دخالت تو بالا بیاید، ورود"
Write-Host "خودکار به ویندوز را هم فعال کن (در بخش ۶ راهنمای SETUP_VPS.md)."
Write-Host ""
Write-Host "اجرای فوری برای تست:  Start-ScheduledTask -TaskName $taskName"
