"""منطق تبدیل سیگنال به اردر، به‌علاوه‌ی محافظ‌های ریسک و اجرای دستورهای مدیریتی."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from .config import TradingSettings
from .models import OrderKind, Side, Signal, SignalUpdate, UpdateKind
from .broker import BrokerError, BrokerMath, Execution
from .store import Store

log = logging.getLogger(__name__)


@dataclass
class Leg:
    volume: float
    tp: Optional[float]


def select_tps(tps: list[float], count: int, mode: str) -> list[float]:
    """وقتی نمی‌شود همه‌ی تارگت‌ها را پوشش داد، کدام‌ها انتخاب شوند.

    ورودی همیشه از نزدیک‌ترین به دورترین مرتب است.

    - nearest : نزدیک‌ترین‌ها — سود زودتر و مطمئن‌تر قفل می‌شود
    - spread  : پخش یکنواخت شامل نزدیک‌ترین و دورترین — هم سود زود، هم یک رانر
    - farthest: دورترین‌ها — سود بیشتر ولی احتمال رسیدن کمتر
    """
    count = max(1, min(count, len(tps)))
    if count == len(tps):
        return list(tps)
    if mode == "farthest":
        return list(tps[-count:])
    if mode == "spread":
        if count == 1:
            return [tps[0]]
        last = len(tps) - 1
        picked = sorted({round(i * last / (count - 1)) for i in range(count)})
        # اگر گرد کردن باعث تکرار شد، از نزدیک‌ترین‌های استفاده‌نشده پر کن
        for index in range(len(tps)):
            if len(picked) >= count:
                break
            if index not in picked:
                picked.append(index)
        return [tps[i] for i in sorted(picked)[:count]]
    return list(tps[:count])  # nearest


@dataclass
class Plan:
    signal: Signal
    symbol: str
    kind: OrderKind
    price: float
    sl: float
    legs: list[Leg] = field(default_factory=list)
    note: str = ""
    #: توضیح اینکه کدام تارگت‌ها پوشش داده شدند و کدام‌ها نه
    coverage: str = ""

    @property
    def total_volume(self) -> float:
        return round(sum(leg.volume for leg in self.legs), 8)

    def describe(self) -> str:
        kind = {"MARKET": "بازار", "LIMIT": "لیمیت", "STOP": "استاپ"}[self.kind.value]
        lines = [
            f"{'خرید' if self.signal.side is Side.BUY else 'فروش'} {self.symbol} — {kind}",
            f"قیمت: {self.price:g}",
            f"حد ضرر: {self.sl:g}",
        ]
        for i, leg in enumerate(self.legs, 1):
            tp = f"{leg.tp:g}" if leg.tp is not None else "بدون TP"
            lines.append(f"  بخش {i}: {leg.volume:g} لات → {tp}")
        lines.append(f"حجم کل: {self.total_volume:g} لات")
        if self.coverage:
            lines.append(self.coverage)
        if self.note:
            lines.append(self.note)
        return "\n".join(lines)


class Executor:
    def __init__(self, mt5: BrokerMath, store: Store, trading: TradingSettings) -> None:
        self.mt5 = mt5
        self.store = store
        self.trading = trading

    # ----------------------------------------------------------- risk guards

    def guard(self, new_legs: int = 1) -> Optional[str]:
        """بررسی سقف‌های ریسک. خروجی None یعنی مجاز است."""
        open_count = len(self.mt5.positions()) + len(self.mt5.pending_orders())
        if open_count + new_legs > self.trading.max_open_positions:
            return (f"سقف پوزیشن‌های باز ({self.trading.max_open_positions}) پر است "
                    f"— الان {open_count} مورد باز است")

        if self.trading.max_daily_loss_percent > 0:
            balance = self.mt5.account().balance
            limit = -abs(balance * self.trading.max_daily_loss_percent / 100.0)
            pnl = self.mt5.realized_pnl_today()
            if pnl <= limit:
                return (f"سقف ضرر روزانه فعال شد: امروز {pnl:,.2f} "
                        f"(حد مجاز {limit:,.2f}) — تا فردا معامله‌ای باز نمی‌شود")
        return None

    def check_age(self, signal: Signal) -> Optional[str]:
        if self.trading.max_signal_age_seconds <= 0:
            return None
        age = (datetime.now(timezone.utc) - signal.received_at).total_seconds()
        if age > self.trading.max_signal_age_seconds:
            return f"سیگنال {int(age)} ثانیه قدیمی است (حد مجاز {self.trading.max_signal_age_seconds})"
        return None

    # ------------------------------------------------------------- planning

    def _natural_kind(self, symbol: str, side: Side, entry: float) -> tuple[OrderKind, float]:
        """نوع اردر صرفاً بر اساس رابطه‌ی قیمت ورود با بازار، بدون آستانه‌ی مارکت."""
        tick = self.mt5.tick(symbol)
        market = tick.ask if side is Side.BUY else tick.bid
        if side is Side.BUY:
            kind = OrderKind.LIMIT if entry < market else OrderKind.STOP
        else:
            kind = OrderKind.LIMIT if entry > market else OrderKind.STOP
        return kind, market

    def build_plan(self, signal: Signal) -> Plan:
        """از سیگنال یک نقشه‌ی اجرای کامل می‌سازد (بدون ارسال به بروکر)."""
        self.mt5.ensure_connected()
        symbol = self.mt5.resolve_symbol(signal.symbol)

        entry = signal.entry
        if entry is None and signal.entry_zone:
            lo, hi = signal.entry_zone
            mode = self.trading.entry_zone
            if mode == "mid":
                entry = (lo + hi) / 2
            elif mode == "best":
                entry = lo if signal.side is Side.BUY else hi
            else:  # worst
                entry = hi if signal.side is Side.BUY else lo

        kind, price = self.mt5.decide_order(symbol, signal.side, entry)

        # اگر کانال صریحاً buystop/selllimit گفته، همان را می‌گذاریم و منتظر
        # می‌مانیم قیمت به باکس برسد — حتی اگر الان به قیمت ورود نزدیک باشیم.
        if signal.order_hint in (OrderKind.LIMIT, OrderKind.STOP) and entry is not None:
            wanted = signal.order_hint
            natural, _ = self._natural_kind(symbol, signal.side, entry)
            if natural is not wanted:
                raise BrokerError(
                    f"کانال {wanted.value} اعلام کرده ولی قیمت فعلی بازار از قیمت ورود "
                    f"({entry:g}) رد شده — گذاشتن این اردر دیگر همان معامله نیست"
                )
            kind, price = wanted, entry

        sl = signal.sl
        if sl is None:
            if self.trading.require_sl:
                raise BrokerError("سیگنال حد ضرر ندارد و require_sl روشن است")
            raise BrokerError("بدون حد ضرر نمی‌توان حجم را بر اساس ریسک حساب کرد")

        # اگر مارکت اجرا می‌شود، لغزش نسبت به قیمت اعلامی را کنترل کن
        if kind is OrderKind.MARKET and entry is not None:
            slip = abs(price - entry) / self.mt5.pip_size(symbol)
            if slip > self.trading.max_slippage_pips:
                raise BrokerError(
                    f"قیمت بازار {slip:.1f} پیپ با قیمت سیگنال ({entry:g}) فاصله دارد "
                    f"— بیشتر از حد مجاز {self.trading.max_slippage_pips:g} پیپ"
                )

        total, note = self.mt5.calc_lot(symbol, price, sl)

        tps = signal.tps[: self.trading.max_tps]
        coverage = ""
        if not tps:
            legs = [Leg(total, None)]
        elif self.trading.tp_strategy == "first":
            legs = [Leg(total, tps[0])]
            coverage = f"از {len(signal.tps)} تارگت، فقط نزدیک‌ترین استفاده شد"
        elif self.trading.tp_strategy == "last":
            legs = [Leg(total, tps[-1])]
            coverage = f"از {len(signal.tps)} تارگت، فقط دورترین استفاده شد"
        else:  # split
            # حساب کوچک نمی‌تواند هر تارگت را یک پوزیشن جدا بدهد
            affordable = self.mt5.max_parts(symbol, total)
            count = min(len(tps), affordable)
            chosen = select_tps(tps, count, self.trading.tp_selection)
            volumes = self.mt5.split_volume(symbol, total, count)
            legs = [Leg(vol, tp) for vol, tp in zip(volumes, chosen)]
            if count < len(signal.tps):
                used = "، ".join(f"{t:g}" for t in chosen)
                coverage = (
                    f"⚠️ کانال {len(signal.tps)} تارگت داد ولی حجم فقط {count} بخش "
                    f"را می‌کشد (حداقل حجم بروکر) — انتخاب‌شده: {used}"
                )

        return Plan(signal=signal, symbol=symbol, kind=kind, price=price,
                    sl=sl, legs=legs, note=note, coverage=coverage)

    # ------------------------------------------------------------- execution

    def execute(self, plan: Plan, signal_id: int) -> tuple[list[Execution], list[str]]:
        """نقشه را روی حساب اجرا می‌کند. خروجی: (اجراهای موفق، خطاها)."""
        done: list[Execution] = []
        errors: list[str] = []
        for index, leg in enumerate(plan.legs, 1):
            try:
                execution = self.mt5.place(
                    signal=plan.signal,
                    symbol=plan.symbol,
                    volume=leg.volume,
                    kind=plan.kind,
                    price=plan.price,
                    sl=plan.sl,
                    tp=leg.tp,
                    comment=f"tg{plan.signal.msg_id}-{index}",
                )
                self.store.add_trade(signal_id, execution)
                done.append(execution)
                log.info("اجرا شد: تیکت %s حجم %g TP %s",
                         execution.ticket, execution.volume, leg.tp)
            except BrokerError as exc:
                errors.append(f"بخش {index}: {exc}")
                log.error("اجرای بخش %s ناموفق: %s", index, exc)

        self.store.set_signal_status(
            signal_id, "executed" if done else "failed"
        )
        return done, errors

    # ----------------------------------------------------- managing a signal

    def apply_update(self, signal_id: int, update: SignalUpdate) -> str:
        self.mt5.ensure_connected()
        trades = self.store.trades_for_signal(signal_id)
        if not trades:
            return "هیچ معامله‌ی فعالی برای این سیگنال وجود ندارد"

        results: list[str] = []
        for trade in trades:
            ticket = int(trade["ticket"])
            label = f"#{ticket}"
            try:
                results.append(f"{label}: {self._apply_one(trade, update)}")
            except BrokerError as exc:
                results.append(f"{label}: ❌ {exc}")
        return "\n".join(results)

    def _apply_one(self, trade, update: SignalUpdate) -> str:
        ticket = int(trade["ticket"])
        is_pending = bool(trade["is_pending"]) and not self.mt5.positions(ticket)

        if update.kind is UpdateKind.CANCEL:
            if is_pending:
                self.mt5.cancel(ticket)
                self.store.set_trade_status(ticket, "cancelled")
                return "✅ اردر پندینگ لغو شد"
            return "اردر قبلاً فعال شده؛ برای بستن از دستور close استفاده کن"

        if is_pending:
            if update.kind in (UpdateKind.CLOSE_ALL, UpdateKind.CLOSE_PARTIAL):
                self.mt5.cancel(ticket)
                self.store.set_trade_status(ticket, "cancelled")
                return "✅ هنوز فعال نشده بود، اردر لغو شد"
            return "اردر هنوز فعال نشده؛ تغییری اعمال نشد"

        if update.kind is UpdateKind.CLOSE_ALL:
            volume = self.mt5.close(ticket, 1.0)
            self.store.set_trade_status(ticket, "closed")
            return f"✅ بسته شد ({volume:g} لات)"

        if update.kind is UpdateKind.CLOSE_PARTIAL:
            volume = self.mt5.close(ticket, update.fraction)
            remaining = self.mt5.positions(ticket)
            if not remaining:
                self.store.set_trade_status(ticket, "closed")
            return f"✅ {volume:g} لات بسته شد ({int(update.fraction * 100)}٪)"

        if update.kind is UpdateKind.SL_TO_BE:
            entry = float(trade["entry_price"])
            self.mt5.modify(ticket, sl=entry)
            self.store.update_trade_sl(ticket, entry)
            return f"✅ حد ضرر روی نقطه‌ی ورود ({entry:g}) قرار گرفت"

        if update.kind is UpdateKind.MOVE_SL and update.price is not None:
            self.mt5.modify(ticket, sl=update.price)
            self.store.update_trade_sl(ticket, update.price)
            return f"✅ حد ضرر به {update.price:g} منتقل شد"

        if update.kind is UpdateKind.MOVE_TP and update.price is not None:
            self.mt5.modify(ticket, tp=update.price)
            return f"✅ حد سود به {update.price:g} منتقل شد"

        return "دستور قابل اعمال نبود"

    # ----------------------------------------------------------- reconciling

    def reconcile(self) -> list[str]:
        """وضعیت دیتابیس را با واقعیت حساب هماهنگ می‌کند (TP خورده، SL خورده، ...)."""
        changes: list[str] = []
        if self.mt5.ensure_connected():
            changes.append("اتصال متاتریدر دوباره برقرار شد")
        for trade in self.store.active_trades():
            ticket = int(trade["ticket"])
            if self.mt5.positions(ticket):
                if trade["status"] == "pending":
                    self.store.set_trade_status(ticket, "open")
                    changes.append(f"#{ticket} فعال شد")
                continue
            if self.mt5.pending_orders(ticket):
                continue
            self.store.set_trade_status(ticket, "closed")
            changes.append(f"#{ticket} بسته شد")
        return changes
