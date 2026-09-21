"""ریاضیات مشترک بین بروکرها.

محاسبه‌ی لات از روی ریسک، تقسیم حجم بین تارگت‌ها و تصمیم نوع اردر هیچ ربطی به
اینکه پشت صحنه متاتریدر است یا cTrader ندارد. این کلاس همان منطق را یک بار
نگه می‌دارد و هر آداپتور بروکر فقط سه چیز را تامین می‌کند:

    symbol_info(symbol) -> مشخصات نماد (همان نام فیلدهای متاتریدر)
    tick(symbol)        -> قیمت لحظه‌ای bid/ask
    account()           -> اطلاعات حساب، با فیلد balance

با این کار، اگر بروکر عوض شود، منطق ریسک دست‌نخورده می‌ماند.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

from .models import OrderKind, Side


class BrokerError(RuntimeError):
    """خطای قابل نمایش به کاربر از سمت بروکر."""


@dataclass
class Execution:
    """یک اردر که واقعاً روی بروکر ثبت شد."""

    ticket: int
    symbol: str
    side: Side
    volume: float
    price: float
    sl: Optional[float]
    tp: Optional[float]
    kind: OrderKind
    is_pending: bool


#: پیپ برای نمادهایی که قاعده‌ی رقم اعشار درباره‌شان گمراه‌کننده است
PIP_OVERRIDES = {"XAUUSD": 0.1, "XAGUSD": 0.01, "BTCUSD": 1.0, "ETHUSD": 0.1}


class BrokerMath:
    """منطق مشترک حجم و نوع اردر. آداپتورها از این ارث می‌برند."""

    # این‌ها را آداپتور تامین می‌کند
    trading: object

    def symbol_info(self, symbol: str):  # pragma: no cover - در آداپتور پیاده می‌شود
        raise NotImplementedError

    def tick(self, symbol: str):  # pragma: no cover
        raise NotImplementedError

    def account(self):  # pragma: no cover
        raise NotImplementedError

    # ------------------------------------------------------------------ pips

    def pip_size(self, symbol: str) -> float:
        info = self.symbol_info(symbol)
        name = getattr(info, "name", symbol).upper()
        for base, pip in PIP_OVERRIDES.items():
            if name.startswith(base):
                return pip
        return info.point * 10 if info.digits in (3, 5) else info.point

    def _round(self, symbol: str, price: Optional[float]) -> Optional[float]:
        if price is None:
            return None
        return round(price, self.symbol_info(symbol).digits)

    # ------------------------------------------------------------ lot sizing

    def calc_lot(self, symbol: str, entry: float, sl: float) -> tuple[float, str]:
        """حجم را از روی درصد ریسک حساب می‌کند. خروجی: (لات، توضیح)."""
        info = self.symbol_info(symbol)
        balance = self.account().balance
        risk_money = balance * self.trading.risk_percent / 100.0  # type: ignore[attr-defined]

        distance = abs(entry - sl)
        if distance <= 0:
            raise BrokerError("فاصله‌ی حد ضرر صفر است")

        tick_size = info.trade_tick_size or info.point
        tick_value = info.trade_tick_value_loss or info.trade_tick_value
        if not tick_size or not tick_value:
            raise BrokerError(f"مشخصات تیک برای {symbol} ناقص است")

        loss_per_lot = (distance / tick_size) * tick_value
        raw_lot = risk_money / loss_per_lot

        step = info.volume_step or 0.01
        lot = math.floor(raw_lot / step) * step
        lot = round(lot, 8)
        lot = min(lot, info.volume_max, self.trading.max_lot)  # type: ignore[attr-defined]

        note = (
            f"ریسک {self.trading.risk_percent:g}% از {balance:,.2f} = "  # type: ignore[attr-defined]
            f"{risk_money:,.2f} | فاصله SL {distance:g} | لات {lot:g}"
        )
        if lot < info.volume_min:
            raise BrokerError(
                f"لات محاسبه‌شده ({raw_lot:.4f}) از حداقل مجاز ({info.volume_min:g}) کمتر است. "
                f"یا ریسک را بالا ببر یا حد ضرر نزدیک‌تری لازم است."
            )
        return lot, note

    def max_parts(self, symbol: str, total: float) -> int:
        """بیشترین تعداد بخشی که این حجم را می‌شود به آن تقسیم کرد."""
        info = self.symbol_info(symbol)
        step = info.volume_step or 0.01
        parts = 1
        while parts < 20:
            chunk = math.floor((total / (parts + 1)) / step) * step
            if chunk < info.volume_min - 1e-9:
                break
            parts += 1
        return parts

    def split_volume(self, symbol: str, total: float, parts: int) -> list[float]:
        """تقسیم حجم بین چند تارگت، با رعایت حداقل و گام حجم."""
        info = self.symbol_info(symbol)
        step = info.volume_step or 0.01
        parts = max(1, parts)
        while parts > 1:
            chunk = math.floor((total / parts) / step) * step
            if chunk >= info.volume_min:
                break
            parts -= 1
        if parts == 1:
            return [round(total, 8)]
        chunk = round(math.floor((total / parts) / step) * step, 8)
        volumes = [chunk] * (parts - 1)
        volumes.append(round(total - chunk * (parts - 1), 8))
        return volumes

    # -------------------------------------------------------- order decision

    def decide_order(self, symbol: str, side: Side,
                     entry: Optional[float]) -> tuple[OrderKind, float]:
        """تصمیم بین اجرای مارکت و اردر پندینگ، بر اساس فاصله‌ی قیمت."""
        tick = self.tick(symbol)
        market = tick.ask if side is Side.BUY else tick.bid
        if entry is None:
            return OrderKind.MARKET, market

        threshold = self.trading.market_threshold_pips * self.pip_size(symbol)  # type: ignore[attr-defined]
        if abs(entry - market) <= threshold:
            return OrderKind.MARKET, market

        if side is Side.BUY:
            kind = OrderKind.LIMIT if entry < market else OrderKind.STOP
        else:
            kind = OrderKind.LIMIT if entry > market else OrderKind.STOP
        return kind, entry

    def check_stops_distance(self, symbol: str, price: float, sl: Optional[float],
                             tp: Optional[float]) -> Optional[str]:
        """بروکر حداقل فاصله‌ای برای SL/TP دارد؛ نقضش باعث رد شدن اردر می‌شود."""
        info = self.symbol_info(symbol)
        min_dist = info.trade_stops_level * info.point
        if min_dist <= 0:
            return None
        if sl is not None and abs(price - sl) < min_dist:
            return f"حد ضرر به قیمت نزدیک‌تر از حد مجاز بروکر است ({min_dist:g})"
        if tp is not None and abs(price - tp) < min_dist:
            return f"حد سود به قیمت نزدیک‌تر از حد مجاز بروکر است ({min_dist:g})"
        return None


def make_broker(settings):
    """آداپتور بروکر را بر اساس config.yaml می‌سازد.

    ایمپورت‌ها عمداً با تاخیرند: روی لینوکس کتابخانه‌ی متاتریدر اصلاً نصب
    نمی‌شود و نباید فقط به خاطر ایمپورت، ربات بالا نیاید.
    """
    if settings.broker == "ctrader":
        from .ctrader_client import CTraderClient

        return CTraderClient(settings.ctrader, settings.trading)

    from .mt5_client import MT5Client

    return MT5Client(settings.mt5, settings.trading)
