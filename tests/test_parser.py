"""نمونه‌های واقعیِ سیگنال برای اطمینان از درست کار کردن پارسر.

اجرا:  python -m pytest tests/ -q      یا      python tests/test_parser.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from signalbot.models import Side, UpdateKind
from signalbot.parser import SignalParser

ALIASES = {
    "GOLD": "XAUUSD", "XAUUSD": "XAUUSD", "XAU": "XAUUSD", "طلا": "XAUUSD",
    "EURUSD": "EURUSD", "GBPJPY": "GBPJPY",
}
P = SignalParser(ALIASES)

CASES = [
    (
        "🟢 BUY GOLD @ 3345.50\nSL: 3338.00\nTP1: 3352\nTP2: 3360\nTP3: 3375",
        dict(symbol="XAUUSD", side=Side.BUY, entry=3345.5, sl=3338.0, tps=[3352, 3360, 3375]),
    ),
    (
        "SELL XAUUSD 3350\nStop Loss 3358\nTake Profit 3335",
        dict(symbol="XAUUSD", side=Side.SELL, entry=3350, sl=3358, tps=[3335]),
    ),
    (
        "XAUUSD BUY LIMIT\nEntry: 3340 - 3344\nS/L 3332\nT/P 3355 / 3365",
        dict(symbol="XAUUSD", side=Side.BUY, entry=None, sl=3332, tps=[3355, 3365]),
    ),
    (
        "طلا خرید در ۳۳۴۵\nحد ضرر ۳۳۳۸\nحد سود ۳۳۶۰",
        dict(symbol="XAUUSD", side=Side.BUY, entry=3345, sl=3338, tps=[3360]),
    ),
    (
        "فروش طلا همین الان\nحد ضرر: ۳۳۶۰\nتارگت ۱: ۳۳۳۰",
        dict(symbol="XAUUSD", side=Side.SELL, entry=None, sl=3360, tps=[3330]),
    ),
    (
        "EURUSD sell now  sl 1.0920  tp 1.0850",
        dict(symbol="EURUSD", side=Side.SELL, entry=None, sl=1.0920, tps=[1.0850]),
    ),
    (
        "Buy GBPJPY @ 195.40 SL 194.80 TP 196.50",
        dict(symbol="GBPJPY", side=Side.BUY, entry=195.40, sl=194.80, tps=[196.50]),
    ),
]

UPDATE_CASES = [
    ("Close half now", UpdateKind.CLOSE_PARTIAL),
    ("نصف پوزیشن رو ببند", UpdateKind.CLOSE_PARTIAL),
    ("move sl to be", UpdateKind.SL_TO_BE),
    ("ریسک فری کنید", UpdateKind.SL_TO_BE),
    ("حد ضرر را به نقطه ورود منتقل کنید", UpdateKind.SL_TO_BE),
    ("SL to 3348", UpdateKind.MOVE_SL),
    ("حد ضرر به ۳۳۴۸", UpdateKind.MOVE_SL),
    ("close the trade", UpdateKind.CLOSE_ALL),
    ("معامله رو ببندید", UpdateKind.CLOSE_ALL),
    ("cancel the order", UpdateKind.CANCEL),
]

REJECT_CASES = [
    "BUY GOLD @ 3345\nSL: 3350\nTP: 3360",          # SL بالای ورود در خرید
    "SELL XAUUSD 3350\nSL 3340\nTP 3360",           # منطق معکوس
]

IGNORE_CASES = [
    "Good morning traders, market looks choppy today",
    "TP1 hit ✅ +70 pips",
    "چارت طلا در تایم ۴ ساعته داره الگوی سر و شانه می‌سازه",
]


def _check_signals() -> list[str]:
    errs = []
    for text, want in CASES:
        res = P.parse(text)
        if res.signal is None:
            errs.append(f"سیگنال تشخیص داده نشد:\n{text}\n  -> {res.rejected_reason}")
            continue
        if res.rejected_reason:
            errs.append(f"نباید رد می‌شد ({res.rejected_reason}):\n{text}")
            continue
        s = res.signal
        for key, expected in want.items():
            got = getattr(s, key)
            if key == "tps":
                got = [round(v, 6) for v in got]
                expected = [round(float(v), 6) for v in expected]
            if got != expected:
                errs.append(f"{key}: انتظار {expected} ولی {got}\n  متن: {text!r}")
    return errs


def _check_updates() -> list[str]:
    errs = []
    for text, want in UPDATE_CASES:
        res = P.parse(text, is_reply=True)
        if res.update is None:
            errs.append(f"دستور تشخیص داده نشد: {text!r} ({res.rejected_reason})")
        elif res.update.kind is not want:
            errs.append(f"{text!r}: انتظار {want.value} ولی {res.update.kind.value}")
    return errs


def _check_rejects() -> list[str]:
    errs = []
    for text in REJECT_CASES:
        res = P.parse(text)
        if not res.rejected_reason:
            errs.append(f"باید رد می‌شد ولی قبول شد: {text!r}")
    return errs


def _check_ignores() -> list[str]:
    errs = []
    for text in IGNORE_CASES:
        res = P.parse(text)
        if not res.is_empty:
            errs.append(f"باید نادیده گرفته می‌شد: {text!r} -> {res}")
    return errs


def test_signals():
    assert not _check_signals()


def test_updates():
    assert not _check_updates()


def test_rejects():
    assert not _check_rejects()


def test_ignores():
    assert not _check_ignores()


if __name__ == "__main__":
    all_errs = _check_signals() + _check_updates() + _check_rejects() + _check_ignores()
    total = len(CASES) + len(UPDATE_CASES) + len(REJECT_CASES) + len(IGNORE_CASES)
    if all_errs:
        print(f"❌ {len(all_errs)} خطا از {total} مورد:\n")
        for err in all_errs:
            print(" -", err)
        sys.exit(1)
    print(f"✅ همه‌ی {total} مورد پاس شد.")
