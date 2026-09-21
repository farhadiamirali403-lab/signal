"""تست منطق ایمنیِ خواندن عکس — بدون تماس با Gemini.

این تست‌ها چیزی را می‌سنجند که واقعاً خطرناک است: اینکه خروجی مدل بدون بررسی
تبدیل به معامله نشود.

اجرا:  python tests/test_vision.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from signalbot.models import Side, UpdateKind
from signalbot.vision import VisionExtraction, VisionReader

ALIASES = {"GOLD": "XAUUSD", "XAUUSD": "XAUUSD", "طلا": "XAUUSD", "EURUSD": "EURUSD"}

reader = VisionReader(api_key="", model="test", aliases=ALIASES,
                      whitelist=["XAUUSD"], min_confidence=0.8)


def run_checks() -> list[str]:
    errs: list[str] = []

    def check(name: str, condition: bool, detail: str = "") -> None:
        if not condition:
            errs.append(f"{name}{': ' + detail if detail else ''}")

    # سیگنال سالم باید عبور کند
    good = VisionExtraction(kind="signal", symbol="GOLD", side="BUY", entry=3345.0,
                            sl=3338.0, tps=[3352.0, 3360.0], confidence=0.95)
    res = reader.to_result(good)
    check("سیگنال سالم", res.signal is not None and not res.rejected_reason,
          str(res.rejected_reason))
    if res.signal:
        check("نماد نگاشت شد", res.signal.symbol == "XAUUSD", res.signal.symbol)
        check("جهت درست", res.signal.side is Side.BUY)

    # اطمینان پایین = اجرا نشود، حتی اگر اعداد منطقی باشند
    unsure = good.model_copy(update={"confidence": 0.5})
    res = reader.to_result(unsure)
    check("اطمینان پایین رد شود", res.signal is None and res.rejected_reason is not None)

    # «TP1 خورد» هرگز نباید معامله باز یا بسته کند
    info = VisionExtraction(kind="info", symbol="GOLD", side="BUY", entry=3345.0,
                            confidence=0.99, notes="TP1 hit")
    res = reader.to_result(info)
    check("گزارش TP بدون اقدام", res.is_empty, str(res))

    # اعداد متناقض باید بگیرند: حد ضرر بالای ورود در خرید
    broken = VisionExtraction(kind="signal", symbol="GOLD", side="BUY", entry=3345.0,
                              sl=3350.0, tps=[3360.0], confidence=0.99)
    res = reader.to_result(broken)
    check("حد ضرر معکوس رد شود", res.rejected_reason is not None)

    # نماد خارج از whitelist
    other = VisionExtraction(kind="signal", symbol="EURUSD", side="SELL", entry=1.09,
                             sl=1.095, tps=[1.08], confidence=0.99)
    res = reader.to_result(other)
    check("whitelist رعایت شود", res.rejected_reason is not None, str(res.rejected_reason))

    # نماد ناشناخته نباید به معامله تبدیل شود
    unknown = VisionExtraction(kind="signal", symbol="DOGECOIN", side="BUY", entry=1.0,
                               sl=0.9, tps=[1.2], confidence=0.99)
    res = reader.to_result(unknown)
    check("نماد ناشناخته رد شود", res.signal is None and res.rejected_reason is not None)

    # محدوده‌ی ورود
    zone = VisionExtraction(kind="signal", symbol="طلا", side="BUY", entry_low=3344.0,
                            entry_high=3340.0, sl=3332.0, tps=[3360.0], confidence=0.9)
    res = reader.to_result(zone)
    check("محدوده‌ی ورود مرتب شود",
          res.signal is not None and res.signal.entry_zone == (3340.0, 3344.0),
          str(res.signal.entry_zone if res.signal else None))

    # دستور مدیریتی سالم
    close = VisionExtraction(kind="update", update_kind="CLOSE_ALL", confidence=0.95)
    res = reader.to_result(close)
    check("دستور بستن", res.update is not None and res.update.kind is UpdateKind.CLOSE_ALL)

    # جابه‌جایی حد ضرر بدون قیمت = خطرناک، باید رد شود
    no_price = VisionExtraction(kind="update", update_kind="MOVE_SL", confidence=0.95)
    res = reader.to_result(no_price)
    check("MOVE_SL بدون قیمت رد شود", res.update is None and res.rejected_reason is not None)

    # بستن جزئی با درصد
    partial = VisionExtraction(kind="update", update_kind="CLOSE_PARTIAL",
                               update_fraction=0.3, confidence=0.9)
    res = reader.to_result(partial)
    check("بستن جزئی با نسبت درست",
          res.update is not None and abs(res.update.fraction - 0.3) < 1e-9)

    # تصویر بی‌ربط
    res = reader.to_result(VisionExtraction(kind="none", confidence=0.9))
    check("تصویر بی‌ربط نادیده", res.is_empty)

    # فروش: تارگت‌ها باید نزولی مرتب شوند تا تقسیم حجم درست انجام شود
    sell = VisionExtraction(kind="signal", symbol="GOLD", side="SELL", entry=3350.0,
                            sl=3358.0, tps=[3330.0, 3340.0], confidence=0.9)
    res = reader.to_result(sell)
    check("ترتیب تارگت فروش",
          res.signal is not None and res.signal.tps == [3340.0, 3330.0],
          str(res.signal.tps if res.signal else None))

    return errs


def test_vision():
    assert not run_checks()


if __name__ == "__main__":
    problems = run_checks()
    if problems:
        print(f"❌ {len(problems)} خطا:\n")
        for problem in problems:
            print(" -", problem)
        sys.exit(1)
    print("✅ همه‌ی بررسی‌های ایمنیِ خواندن عکس پاس شد.")
