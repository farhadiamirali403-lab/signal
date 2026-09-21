"""تست منطق حجم و ساخت نقشه، با یک متاتریدر ساختگی (بدون اتصال واقعی).

اجرا:  python tests/test_executor.py
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from signalbot.config import MT5Settings, TradingSettings
from signalbot.models import OrderKind, Side, Signal
from signalbot.mt5_client import MT5Client, MT5Error


@dataclass
class FakeSymbol:
    name: str = "XAUUSD"
    digits: int = 2
    point: float = 0.01
    trade_tick_size: float = 0.01
    trade_tick_value: float = 1.0
    trade_tick_value_loss: float = 1.0
    volume_min: float = 0.01
    volume_step: float = 0.01
    volume_max: float = 50.0
    trade_stops_level: int = 0
    filling_mode: int = 2


@dataclass
class FakeTick:
    bid: float = 3345.30
    ask: float = 3345.50


@dataclass
class FakeAccount:
    balance: float = 10_000.0
    equity: float = 10_000.0
    currency: str = "USD"


class FakeMT5(MT5Client):
    """فقط لایه‌ی تماس با ترمینال را جایگزین می‌کند؛ ریاضیات واقعی اجرا می‌شود."""

    def __init__(self, trading: TradingSettings) -> None:
        super().__init__(MT5Settings(0, "", "", None), trading)
        self.symbol = FakeSymbol()
        self._tick = FakeTick()

    def ensure_connected(self) -> bool:
        return False  # تست نباید به ترمینال واقعی دست بزند

    def resolve_symbol(self, base: str) -> str:
        return "XAUUSD"

    def symbol_info(self, symbol: str):
        return self.symbol

    def tick(self, symbol: str):
        return self._tick

    def account(self):
        return FakeAccount()


def approx(a: float, b: float, tol: float = 1e-6) -> bool:
    return abs(a - b) < tol


def run_checks() -> list[str]:
    errs: list[str] = []
    trading = TradingSettings(risk_percent=1.0, market_threshold_pips=15,
                              max_slippage_pips=5, tp_strategy="split", max_tps=3)
    mt5 = FakeMT5(trading)

    # پیپ طلا باید ۰.۱ باشد نه ۰.۰۱ (قاعده‌ی رقم اعشار برای فلزات گمراه‌کننده است)
    if not approx(mt5.pip_size("XAUUSD"), 0.1):
        errs.append(f"pip_size: انتظار 0.1 ولی {mt5.pip_size('XAUUSD')}")

    # ریسک ۱٪ از ۱۰۰۰۰ = ۱۰۰ دلار، فاصله SL هفت دلار = ۷۰۰ تیک × ۱ دلار
    lot, _ = mt5.calc_lot("XAUUSD", 3345.0, 3338.0)
    if not approx(lot, 0.14):
        errs.append(f"calc_lot: انتظار 0.14 ولی {lot}")

    # ریسک باید بین سه تارگت تقسیم شود و جمعش دست‌نخورده بماند
    volumes = mt5.split_volume("XAUUSD", 0.14, 3)
    if len(volumes) != 3 or not approx(sum(volumes), 0.14):
        errs.append(f"split_volume: {volumes} (جمع {sum(volumes)})")
    if any(v < mt5.symbol.volume_min for v in volumes):
        errs.append(f"split_volume زیر حداقل حجم رفت: {volumes}")

    # حجم خیلی کم نباید به بخش‌های غیرمجاز تقسیم شود
    small = mt5.split_volume("XAUUSD", 0.02, 3)
    if any(v < mt5.symbol.volume_min for v in small) or not approx(sum(small), 0.02):
        errs.append(f"split_volume با حجم کم: {small}")

    # فاصله‌ی کم تا قیمت بازار = اجرای مارکت
    kind, _ = mt5.decide_order("XAUUSD", Side.BUY, 3345.0)
    if kind is not OrderKind.MARKET:
        errs.append(f"decide_order نزدیک: انتظار MARKET ولی {kind}")

    # خرید در قیمت پایین‌تر از بازار = بای لیمیت
    kind, price = mt5.decide_order("XAUUSD", Side.BUY, 3300.0)
    if kind is not OrderKind.LIMIT or not approx(price, 3300.0):
        errs.append(f"decide_order پایین‌تر: انتظار LIMIT@3300 ولی {kind}@{price}")

    # خرید در قیمت بالاتر از بازار = بای استاپ
    kind, _ = mt5.decide_order("XAUUSD", Side.BUY, 3400.0)
    if kind is not OrderKind.STOP:
        errs.append(f"decide_order بالاتر: انتظار STOP ولی {kind}")

    # لات زیر حداقل مجاز باید خطا بدهد نه اینکه صفر بفرستد
    tiny = FakeMT5(TradingSettings(risk_percent=0.01))
    try:
        tiny.calc_lot("XAUUSD", 3345.0, 3338.0)
        errs.append("calc_lot: برای ریسک خیلی کم باید خطا می‌داد")
    except MT5Error:
        pass

    # --- ساخت نقشه‌ی کامل ---
    from signalbot.executor import Executor

    executor = Executor(mt5, store=None, trading=trading)  # type: ignore[arg-type]
    signal = Signal(symbol="XAUUSD", side=Side.BUY, entry=3345.0, sl=3338.0,
                    tps=[3352.0, 3360.0, 3375.0])
    plan = executor.build_plan(signal)
    if len(plan.legs) != 3:
        errs.append(f"build_plan: انتظار ۳ بخش ولی {len(plan.legs)}")
    # حجم از قیمت واقعیِ اجرا (ask=3345.50) حساب می‌شود نه قیمت اعلامی سیگنال:
    # فاصله ۷.۵ دلار = ۷۵۰ تیک، ۱۰۰ دلار ریسک -> ۰.۱۳ لات
    if not approx(plan.total_volume, 0.13):
        errs.append(f"build_plan: حجم کل {plan.total_volume} (انتظار 0.13)")
    if [leg.tp for leg in plan.legs] != [3352.0, 3360.0, 3375.0]:
        errs.append(f"build_plan: تارگت‌ها {[leg.tp for leg in plan.legs]}")
    if plan.kind is not OrderKind.MARKET:
        errs.append(f"build_plan: نوع اردر {plan.kind}")

    # سیگنالی که قیمتش خیلی دور رفته نباید مارکت اجرا شود
    far = Signal(symbol="XAUUSD", side=Side.BUY, entry=3344.0, sl=3338.0, tps=[3360.0])
    mt5._tick = FakeTick(bid=3345.20, ask=3345.40)  # ۱۴ پیپ فاصله، زیر آستانه‌ی مارکت
    try:
        executor.build_plan(far)
        errs.append("build_plan: باید به خاطر لغزش بیش از حد رد می‌شد")
    except MT5Error:
        pass

    # --- سیگنال‌های چندتارگتی (کانال گاهی ۶ تارگت می‌دهد) ---
    from signalbot.executor import select_tps

    six = [3352.0, 3360.0, 3368.0, 3376.0, 3384.0, 3392.0]
    if select_tps(six, 2, "nearest") != [3352.0, 3360.0]:
        errs.append(f"select nearest: {select_tps(six, 2, 'nearest')}")
    if select_tps(six, 2, "spread") != [3352.0, 3392.0]:
        errs.append(f"select spread: {select_tps(six, 2, 'spread')}")
    if select_tps(six, 2, "farthest") != [3384.0, 3392.0]:
        errs.append(f"select farthest: {select_tps(six, 2, 'farthest')}")
    if select_tps(six, 6, "nearest") != six:
        errs.append("وقتی همه جا می‌شوند، همه باید بمانند")
    if len(select_tps(six, 4, "spread")) != 4 or len(set(select_tps(six, 4, "spread"))) != 4:
        errs.append(f"spread نباید تارگت تکراری بدهد: {select_tps(six, 4, 'spread')}")
    if select_tps(six, 99, "nearest") != six:
        errs.append("درخواست بیش از تعداد موجود نباید خطا بدهد")

    # حساب بزرگ: هر شش تارگت پوشش داده شود و حجم گم نشود
    big_cfg = TradingSettings(risk_percent=1.0, tp_strategy="split", max_tps=6)
    big = Executor(FakeMT5(big_cfg), None, big_cfg)  # type: ignore[arg-type]
    six_signal = Signal(symbol="XAUUSD", side=Side.BUY, entry=3345.0, sl=3338.0, tps=six)
    plan6 = big.build_plan(six_signal)
    if len(plan6.legs) != 6:
        errs.append(f"شش تارگت: انتظار ۶ بخش ولی {len(plan6.legs)}")
    if not approx(plan6.total_volume, 0.13):
        errs.append(f"شش تارگت: حجم کل {plan6.total_volume}")
    if plan6.coverage:
        errs.append(f"وقتی همه پوشش داده شدند نباید هشدار بدهد: {plan6.coverage}")

    # حساب کوچک: باید کم کند و صریحاً گزارش دهد — نه اینکه بی‌صدا دور بریزد
    small_cfg = TradingSettings(risk_percent=0.16, tp_strategy="split", max_tps=6,
                                tp_selection="spread")
    small = Executor(FakeMT5(small_cfg), None, small_cfg)  # type: ignore[arg-type]
    plan_small = small.build_plan(six_signal)
    if len(plan_small.legs) != 2:
        errs.append(f"حساب کوچک: انتظار ۲ بخش ولی {len(plan_small.legs)}")
    if not approx(plan_small.total_volume, 0.02):
        errs.append(f"حساب کوچک: حجم باید کامل مصرف شود، نه {plan_small.total_volume}")
    if "6 تارگت" not in plan_small.coverage:
        errs.append(f"حساب کوچک باید هشدار پوشش بدهد: {plan_small.coverage!r}")
    if [leg.tp for leg in plan_small.legs] != [3352.0, 3392.0]:
        errs.append(f"حساب کوچک با spread: {[l.tp for l in plan_small.legs]}")

    # استراتژی تک‌تارگتی
    single = Executor(mt5, None, TradingSettings(tp_strategy="last"))  # type: ignore[arg-type]
    mt5._tick = FakeTick()
    plan2 = single.build_plan(signal)
    if len(plan2.legs) != 1 or plan2.legs[0].tp != 3375.0:
        errs.append(f"tp_strategy=last: {[(l.volume, l.tp) for l in plan2.legs]}")

    return errs


def test_executor():
    assert not run_checks()


if __name__ == "__main__":
    problems = run_checks()
    if problems:
        print(f"❌ {len(problems)} خطا:\n")
        for problem in problems:
            print(" -", problem)
        sys.exit(1)
    print("✅ همه‌ی بررسی‌های موتور اجرا پاس شد.")
