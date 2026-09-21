"""Data structures shared across the bot."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderKind(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP = "STOP"


class UpdateKind(str, Enum):
    CLOSE_ALL = "CLOSE_ALL"          # «ببند» / «close now»
    CLOSE_PARTIAL = "CLOSE_PARTIAL"  # «نصفش رو ببند» / «close half»
    SL_TO_BE = "SL_TO_BE"            # «ریسک‌فری» / «sl to entry»
    MOVE_SL = "MOVE_SL"              # حد ضرر جدید با قیمت مشخص
    MOVE_TP = "MOVE_TP"
    CANCEL = "CANCEL"                # لغو اردر پندینگِ اجرانشده


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Signal:
    """یک سیگنال ورود که از متن پیام استخراج شده."""

    symbol: str                        # نماد پایه، مثلا XAUUSD
    side: Side
    entry: Optional[float] = None      # None = ورود در قیمت بازار
    entry_zone: Optional[tuple[float, float]] = None
    sl: Optional[float] = None
    tps: list[float] = field(default_factory=list)
    #: نوع اردری که کانال صریحاً اعلام کرده (buystop/selllimit/...)
    order_hint: Optional["OrderKind"] = None
    raw_text: str = ""
    msg_id: int = 0
    chat_id: int = 0
    received_at: datetime = field(default_factory=_utcnow)

    def summary(self) -> str:
        entry = f"{self.entry:g}" if self.entry is not None else "market"
        sl = f"{self.sl:g}" if self.sl is not None else "—"
        tps = " / ".join(f"{t:g}" for t in self.tps) or "—"
        return f"{self.side.value} {self.symbol} @ {entry}\nSL: {sl}\nTP: {tps}"


@dataclass
class SignalUpdate:
    """دستور مدیریتی روی یک سیگنالِ قبلی (معمولا به صورت ریپلای)."""

    kind: UpdateKind
    price: Optional[float] = None      # برای MOVE_SL / MOVE_TP
    fraction: float = 0.5              # برای CLOSE_PARTIAL
    raw_text: str = ""
    reply_to_msg_id: Optional[int] = None


@dataclass
class ParseResult:
    signal: Optional[Signal] = None
    update: Optional[SignalUpdate] = None
    rejected_reason: Optional[str] = None

    @property
    def is_empty(self) -> bool:
        return self.signal is None and self.update is None
