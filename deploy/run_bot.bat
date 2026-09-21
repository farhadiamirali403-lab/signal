@echo off
REM اجرای ربات با ری‌استارت خودکار.
REM اگر ربات به هر دلیلی بسته شود (قطعی شبکه، خطای غیرمنتظره)، ۱۵ ثانیه بعد
REM دوباره بالا می‌آید. برای توقف کامل، همین پنجره را ببند.

cd /d "%~dp0.."
if not exist logs mkdir logs

:loop
echo [%date% %time%] شروع ربات >> logs\restarts.log
python run.py
echo [%date% %time%] ربات بسته شد (کد %errorlevel%) - ۱۵ ثانیه دیگر دوباره >> logs\restarts.log
timeout /t 15 /nobreak >nul
goto loop
