"""تست کلید Gemini و توانایی خواندن عکس سیگنال.

    python tools/check_vision.py                  # کلید و مدل‌های در دسترس
    python tools/check_vision.py path\\to\\signal.jpg   # یک عکس واقعی را بخوان

هیچ معامله‌ای باز نمی‌کند.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from signalbot.config import load_settings
from signalbot.vision import VisionError, VisionReader

MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
        ".webp": "image/webp", ".gif": "image/gif"}


ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models"


def diagnose(api_key: str, proxy: str) -> None:
    """تشخیص اینکه مشکل از کلید است، از شبکه، یا از مسدودیت منطقه‌ای."""
    import urllib.error
    import urllib.request

    print("--- عیب‌یابی اتصال ---")
    if proxy:
        print(f"از پراکسی استفاده می‌شود: {proxy}")
        handler = urllib.request.ProxyHandler({"https": proxy, "http": proxy})
    else:
        handler = urllib.request.ProxyHandler({})
    opener = urllib.request.build_opener(handler)

    try:
        with opener.open(f"{ENDPOINT}?key={api_key or 'none'}", timeout=25) as response:
            print(f"✅ سرویس پاسخ داد (HTTP {response.status}) — اتصال سالم است.")
            return
    except urllib.error.HTTPError as exc:
        ctype = (exc.headers.get("Content-Type") or "").lower()
        body = exc.read()[:300].decode("utf-8", errors="replace")
        if "html" in ctype:
            # پاسخ HTML دو علت کاملاً متفاوت دارد: کلید بدشکل، یا مسدودیت منطقه‌ای
            if not api_key.startswith(("AIza", "AQ.")) or len(api_key) < 30:
                print("🔑 کلیدی که دادی فرمت کلید Gemini را ندارد.")
                print("   کلیدهای معتبر با AIza یا AQ. شروع می‌شوند.")
                print("   از https://aistudio.google.com/apikey بگیر و در .env بگذار.")
            else:
                print(f"⛔ سرویس درخواست را در لبه رد کرد (HTTP {exc.code}، پاسخ HTML).")
                print("   با کلیدی که فرمتش درست است، این معمولاً یعنی مسدودیت منطقه‌ای.")
                print("\n   راه‌ها:")
                print("   ۱) پراکسی: در .env مقدار GEMINI_PROXY را بگذار،")
                print("      مثلا  GEMINI_PROXY=http://127.0.0.1:10809")
                print("   ۲) اجرای ربات روی یک VPS ویندوزی خارج از منطقه")
                print("      (برای ۲۴ ساعته بودن ربات هم لازمش داری)")
        elif "json" in ctype:
            print(f"🔑 سرویس در دسترس است ولی درخواست را نپذیرفت (HTTP {exc.code}):")
            print(f"   {body[:200]}")
            print("\n   معمولاً یعنی کلید اشتباه یا فعال‌نشده است.")
        else:
            print(f"❌ خطای HTTP {exc.code}: {body[:200]}")
    except urllib.error.URLError as exc:
        print(f"🌐 اصلاً به سرویس نرسیدیم: {exc.reason}")
        print("   اینترنت، فایروال یا پراکسی را چک کن.")


def main() -> None:
    settings = load_settings(require=("vision",))
    if not settings.vision.enabled:
        print("vision.enabled در config.yaml خاموش است.")
        return

    reader = VisionReader(
        api_key=settings.vision.api_key,
        model=settings.vision.model,
        aliases=settings.symbols.aliases,
        whitelist=settings.symbols.whitelist,
        min_confidence=settings.vision.min_confidence,
        proxy=settings.vision.proxy,
    )
    print(f"مدل تنظیم‌شده: {settings.vision.model}")

    print("\n--- مدل‌های در دسترس کلید تو ---")
    try:
        names = []
        for model in reader.client.models.list():
            actions = getattr(model, "supported_actions", None) or []
            if not actions or "generateContent" in actions:
                names.append(model.name.replace("models/", ""))
        for name in sorted(n for n in names if "gemini" in n):
            mark = "  <-- تنظیم‌شده" if name == settings.vision.model else ""
            print(f"  {name}{mark}")
        if settings.vision.model not in names:
            print(f"\n⚠️ مدل «{settings.vision.model}» در لیست بالا نیست. "
                  f"یکی از موارد بالا را در config.yaml بگذار.")
    except Exception as exc:  # noqa: BLE001
        print(f"❌ لیست مدل‌ها گرفته نشد: {str(exc)[:160]}\n")
        diagnose(settings.vision.api_key, settings.vision.proxy)
        return

    if len(sys.argv) < 2:
        print("\nبرای تست روی یک عکس واقعی، مسیر عکس را بده:")
        print("  python tools/check_vision.py images/12345.jpg")
        return

    path = Path(sys.argv[1])
    if not path.exists():
        print(f"\n❌ فایل پیدا نشد: {path}")
        return

    print(f"\n--- خواندن {path.name} ---")
    try:
        data = reader.extract(path.read_bytes(), MIME.get(path.suffix.lower(), "image/jpeg"))
    except VisionError as exc:
        print(f"❌ {exc}")
        return

    print(VisionReader.describe(data))
    result = reader.to_result(data)
    print("\n--- نتیجه‌ی نهایی برای ربات ---")
    if result.signal is not None:
        print(result.signal.summary())
        if result.rejected_reason:
            print(f"⚠️ ولی رد می‌شود: {result.rejected_reason}")
        else:
            print("✅ این سیگنال اجرا می‌شد")
    elif result.update is not None:
        print(f"🔧 دستور مدیریتی: {result.update.kind.value}")
    elif result.rejected_reason:
        print(f"⚠️ اجرا نمی‌شود: {result.rejected_reason}")
    else:
        print("ℹ️ بدون اقدام (گزارش یا پیام غیرسیگنالی)")


if __name__ == "__main__":
    main()
