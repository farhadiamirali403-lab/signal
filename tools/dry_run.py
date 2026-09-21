"""پارسر را روی پیام‌های واقعی کانال امتحان می‌کند — بدون باز کردن هیچ معامله‌ای.

    python tools/dry_run.py                 # ۵۰ پیام آخر، حداکثر ۱۵ عکس
    python tools/dry_run.py 200 --images 40 # بازه‌ی بزرگ‌تر
    python tools/dry_run.py --text "BUY GOLD 3345 SL 3338 TP 3360"
    python tools/dry_run.py --image path\\to\\signal.jpg

هر عکس یک فراخوانی به Gemini است، پس تعدادش با --images محدود شده تا نه هزینه
بالا برود و نه به سقف نرخ رایگان بخوری. عکس‌ها در images/ ذخیره می‌شوند تا بعداً
بتوانی تک‌تک با tools/check_vision.py رویشان کار کنی.

با --mt5 نقشه‌ی اجرا (لات، نوع اردر) هم حساب می‌شود.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from telethon import TelegramClient

from signalbot.config import ROOT, load_settings
from signalbot.executor import Executor
from signalbot.broker import BrokerError, make_broker
from signalbot.parser import SignalParser
from signalbot.store import Store
from signalbot.vision import VisionError, VisionReader

SEP = "─" * 68
IMAGE_DIR = ROOT / "images"
# فاصله بین فراخوانی‌ها تا به سقف نرخ رایگان Gemini نخوریم
THROTTLE_SECONDS = 4.0


def build_executor(settings):
    client = make_broker(settings)
    client.connect()
    store = Store(ROOT / "dryrun.db")
    return Executor(client, store, settings.trading), client


def build_vision(settings):
    if not settings.vision.enabled:
        return None
    return VisionReader(
        api_key=settings.vision.api_key,
        model=settings.vision.model,
        aliases=settings.symbols.aliases,
        whitelist=settings.symbols.whitelist,
        min_confidence=settings.vision.min_confidence,
        proxy=settings.vision.proxy,
    )


def report(result, executor=None, prefix: str = "") -> str:
    """نتیجه‌ی پارس را چاپ و یک برچسب برای آمار برمی‌گرداند."""
    if result.signal is not None and not result.rejected_reason:
        print(f"✅ سیگنال {prefix}")
        print("   " + result.signal.summary().replace("\n", "\n   "))
        if executor is not None:
            try:
                plan = executor.build_plan(result.signal)
                print("   ── نقشه‌ی اجرا ──")
                print("   " + plan.describe().replace("\n", "\n   "))
            except BrokerError as exc:
                print(f"   ⚠️ قابل اجرا نیست: {exc}")
        return "signal"

    if result.signal is not None:
        print(f"⚠️ رد شد {prefix}\n   دلیل: {result.rejected_reason}")
        print("   " + result.signal.summary().replace("\n", "\n   "))
        return "rejected"

    if result.update is not None:
        price = f" @ {result.update.price:g}" if result.update.price else ""
        print(f"🔧 دستور مدیریتی: {result.update.kind.value}{price} {prefix}")
        return "update"

    if result.rejected_reason:
        print(f"⚠️ نادیده {prefix}\n   {result.rejected_reason}")
        return "ignored"
    return "ignored"


def show_image(reader: VisionReader, data: bytes, prefix: str, executor=None,
               caption: str = "") -> str:
    try:
        extraction = reader.extract(data, "image/jpeg", caption)
    except VisionError as exc:
        print(f"❌ خواندن عکس ناموفق {prefix}: {exc}")
        return "error"
    print(f"🖼 {prefix}")
    print("   " + VisionReader.describe(extraction).replace("\n", "\n   "))
    if extraction.kind in ("info", "none"):
        return "info"
    return report(reader.to_result(extraction), executor, prefix="(از عکس)")


async def from_channel(count: int, max_images: int, use_mt5: bool) -> None:
    need = ("telegram", "vision") + (("broker",) if use_mt5 else ())
    settings = load_settings(require=need)
    parser = SignalParser(settings.symbols.aliases, settings.symbols.whitelist)
    reader = build_vision(settings)
    executor, mt5 = (None, None)
    if use_mt5:
        executor, mt5 = build_executor(settings)

    session_dir = ROOT / "sessions"
    session_dir.mkdir(exist_ok=True)
    tg = TelegramClient(str(session_dir / "user"),
                        settings.telegram.api_id, settings.telegram.api_hash)
    await tg.start()
    entity = await tg.get_entity(settings.telegram.source_channel)
    print(f"کانال: {getattr(entity, 'title', settings.telegram.source_channel)}")
    print(f"بررسی {count} پیام آخر (حداکثر {max_images} عکس)\n{SEP}")

    IMAGE_DIR.mkdir(exist_ok=True)
    stats: dict[str, int] = {}
    images_done = 0
    messages = [m async for m in tg.iter_messages(entity, limit=count)]

    for message in reversed(messages):
        caption = message.message or ""
        is_image = bool(message.photo) or (
            getattr(getattr(message, "document", None), "mime_type", "") or ""
        ).startswith("image/")
        label = f"| پیام {message.id}" + (" | ریپلای" if message.reply_to_msg_id else "")

        if is_image:
            if reader is None:
                print(f"🖼 عکس (خواندن عکس خاموش است) {label}")
                stats["image_skipped"] = stats.get("image_skipped", 0) + 1
                continue
            if images_done >= max_images:
                stats["image_over_limit"] = stats.get("image_over_limit", 0) + 1
                continue
            data = await message.download_media(file=bytes)
            (IMAGE_DIR / f"{message.id}.jpg").write_bytes(data)
            if images_done:
                time.sleep(THROTTLE_SECONDS)
            images_done += 1
            verdict = show_image(reader, data, label, executor, caption)
        elif caption.strip():
            preview = " / ".join(x for x in caption.strip().splitlines() if x.strip())[:80]
            verdict = report(
                parser.parse(caption, is_reply=message.reply_to_msg_id is not None),
                executor,
                prefix=f"{label} | {preview}",
            )
        else:
            continue
        stats[verdict] = stats.get(verdict, 0) + 1

    print(SEP)
    print("خلاصه: " + " | ".join(
        f"{k}: {v}" for k, v in sorted(stats.items(), key=lambda kv: -kv[1])
    ))
    print(f"\nعکس‌ها در {IMAGE_DIR} ذخیره شدند.")
    print("اگر سیگنالی اشتباه خوانده شده، همین خروجی را نشانم بده تا تنظیمش کنم.")
    await tg.disconnect()
    if mt5:
        mt5.shutdown()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("count", nargs="?", type=int, default=50)
    ap.add_argument("--images", type=int, default=15, help="حداکثر تعداد عکسی که خوانده شود")
    ap.add_argument("--text", help="یک متن مشخص را پارس کن، بدون تلگرام")
    ap.add_argument("--image", help="یک فایل عکس مشخص را بخوان، بدون تلگرام")
    ap.add_argument("--mt5", action="store_true", help="نقشه‌ی اجرا را هم حساب کن")
    args = ap.parse_args()

    if args.text:
        settings = load_settings(require=("broker",) if args.mt5 else ())
        parser = SignalParser(settings.symbols.aliases, settings.symbols.whitelist)
        executor = build_executor(settings)[0] if args.mt5 else None
        report(parser.parse(args.text), executor)
        return

    if args.image:
        settings = load_settings(require=("vision",) + (("broker",) if args.mt5 else ()))
        reader = build_vision(settings)
        if reader is None:
            print("vision.enabled خاموش است.")
            return
        executor = build_executor(settings)[0] if args.mt5 else None
        show_image(reader, Path(args.image).read_bytes(), Path(args.image).name, executor)
        return

    asyncio.run(from_channel(args.count, args.images, args.mt5))


if __name__ == "__main__":
    main()
