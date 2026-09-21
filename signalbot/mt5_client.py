"""همه‌ی تعامل‌ها با ترمینال متاتریدر ۵.

این ماژول کاملاً همگام (sync) است؛ فراخوانی‌ها از سمت ربات داخل thread جدا
اجرا می‌شوند تا حلقه‌ی asyncio تلگرام بلاک نشود.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

import MetaTrader5 as mt5

from .broker import BrokerError, BrokerMath, Execution
from .config import MT5Settings, TradingSettings
from .models import OrderKind, Side, Signal

log = logging.getLogger(__name__)

#: نام قدیمی، تا کدهای موجود دست‌نخورده بمانند
MT5Error = BrokerError


class MT5Client(BrokerMath):
    def __init__(self, settings: MT5Settings, trading: TradingSettings) -> None:
        self.settings = settings
        self.trading = trading
        self._symbol_cache: dict[str, str] = {}

    # ------------------------------------------------------------ connection

    def connect(self) -> None:
        kwargs = {"login": self.settings.login,
                  "password": self.settings.password,
                  "server": self.settings.server}
        if self.settings.path:
            kwargs["path"] = self.settings.path
        if not mt5.initialize(**kwargs):
            raise MT5Error(f"اتصال به متاتریدر ناموفق بود: {mt5.last_error()}")
        info = mt5.account_info()
        if info is None:
            raise MT5Error(f"اطلاعات حساب خوانده نشد: {mt5.last_error()}")
        if not info.trade_allowed:
            log.warning("در ترمینال، Algo Trading خاموش است — اردرها رد می‌شوند.")
        log.info(
            "متصل شد: #%s (%s) | بالانس %.2f %s | لوریج 1:%s",
            info.login, info.server, info.balance, info.currency, info.leverage,
        )

    def ensure_connected(self) -> bool:
        """اگر ترمینال بسته یا ری‌استارت شده، دوباره وصل می‌شود.

        روی اجرای ۲۴ ساعته لازم است: متاتریدر برای آپدیت یا بعد از ری‌استارت
        ویندوز ممکن است قطع شود و بدون این، همه‌ی سیگنال‌های بعدی رد می‌شدند.
        خروجی True یعنی تازه وصل شده‌ایم (اتصال قبلی قطع بوده).
        """
        if mt5.terminal_info() is not None and mt5.account_info() is not None:
            return False
        log.warning("اتصال متاتریدر قطع شده بود — تلاش برای اتصال دوباره")
        try:
            mt5.shutdown()
        except Exception:  # noqa: BLE001 - ممکن است اصلا وصل نبوده باشد
            pass
        self._symbol_cache.clear()
        self.connect()
        return True

    def shutdown(self) -> None:
        mt5.shutdown()

    def account(self):
        info = mt5.account_info()
        if info is None:
            raise MT5Error(f"اطلاعات حساب در دسترس نیست: {mt5.last_error()}")
        return info

    # --------------------------------------------------------------- symbols

    def resolve_symbol(self, base: str) -> str:
        """نماد بروکر را پیدا می‌کند؛ پسوندهایی مثل XAUUSD.m یا XAUUSDm را هم می‌گیرد."""
        base = base.upper()
        if base in self._symbol_cache:
            return self._symbol_cache[base]

        candidates: list[str] = []
        if mt5.symbol_info(base) is not None:
            candidates.append(base)
        else:
            for sym in mt5.symbols_get() or ():
                name = sym.name.upper()
                stripped = name.replace(".", "").replace("_", "").replace("-", "")
                if stripped == base or stripped.startswith(base):
                    candidates.append(sym.name)
        if not candidates:
            raise MT5Error(f"نماد {base} در این بروکر پیدا نشد")

        # کوتاه‌ترین نام معمولاً نماد اصلی است، نه نسخه‌های ویژه
        chosen = min(candidates, key=len)
        if not mt5.symbol_select(chosen, True):
            raise MT5Error(f"نماد {chosen} در Market Watch فعال نشد: {mt5.last_error()}")
        self._symbol_cache[base] = chosen
        log.info("نماد %s -> %s", base, chosen)
        return chosen

    def symbol_info(self, symbol: str):
        info = mt5.symbol_info(symbol)
        if info is None:
            raise MT5Error(f"اطلاعات نماد {symbol} خوانده نشد")
        return info

    def tick(self, symbol: str):
        tick = mt5.symbol_info_tick(symbol)
        if tick is None or (tick.bid == 0 and tick.ask == 0):
            raise MT5Error(f"قیمت لحظه‌ای {symbol} در دسترس نیست (بازار بسته است؟)")
        return tick

    # -------------------------------------------------------------- ordering

    def _order_type(self, side: Side, kind: OrderKind) -> int:
        table = {
            (Side.BUY, OrderKind.MARKET): mt5.ORDER_TYPE_BUY,
            (Side.SELL, OrderKind.MARKET): mt5.ORDER_TYPE_SELL,
            (Side.BUY, OrderKind.LIMIT): mt5.ORDER_TYPE_BUY_LIMIT,
            (Side.SELL, OrderKind.LIMIT): mt5.ORDER_TYPE_SELL_LIMIT,
            (Side.BUY, OrderKind.STOP): mt5.ORDER_TYPE_BUY_STOP,
            (Side.SELL, OrderKind.STOP): mt5.ORDER_TYPE_SELL_STOP,
        }
        return table[(side, kind)]

    def _filling(self, symbol: str, pending: bool) -> int:
        if pending:
            return mt5.ORDER_FILLING_RETURN
        mode = self.symbol_info(symbol).filling_mode
        if mode & 2:            # SYMBOL_FILLING_IOC
            return mt5.ORDER_FILLING_IOC
        if mode & 1:            # SYMBOL_FILLING_FOK
            return mt5.ORDER_FILLING_FOK
        return mt5.ORDER_FILLING_RETURN

    def place(self, signal: Signal, symbol: str, volume: float, kind: OrderKind,
              price: float, sl: Optional[float], tp: Optional[float],
              comment: str = "") -> Execution:
        pending = kind is not OrderKind.MARKET
        price = self._round(symbol, price)
        sl = self._round(symbol, sl)
        tp = self._round(symbol, tp)

        problem = self.check_stops_distance(symbol, price, sl, tp)
        if problem:
            raise MT5Error(problem)

        request = {
            "action": mt5.TRADE_ACTION_PENDING if pending else mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": float(volume),
            "type": self._order_type(signal.side, kind),
            "price": price,
            "deviation": self.trading.deviation_points,
            "magic": self.trading.magic,
            "comment": (comment or f"tg{signal.msg_id}")[:31],
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": self._filling(symbol, pending),
        }
        if sl is not None:
            request["sl"] = sl
        if tp is not None:
            request["tp"] = tp
        if pending and self.trading.pending_expiry_hours > 0:
            request["type_time"] = mt5.ORDER_TIME_SPECIFIED
            request["expiration"] = int(
                (datetime.now() + timedelta(hours=self.trading.pending_expiry_hours)).timestamp()
            )

        result = mt5.order_send(request)
        if result is None:
            raise MT5Error(f"order_send پاسخی نداد: {mt5.last_error()}")
        if result.retcode not in (mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_PLACED):
            raise MT5Error(f"اردر رد شد (کد {result.retcode}): {result.comment}")

        if pending:
            ticket = result.order
        elif result.deal:
            # برای اجرای مارکت، تیکتِ پوزیشن را می‌خواهیم نه تیکت دیل
            ticket = self._position_of(result)
        else:
            ticket = result.order
        return Execution(
            ticket=int(ticket),
            symbol=symbol,
            side=signal.side,
            volume=float(volume),
            price=float(result.price or price),
            sl=sl,
            tp=tp,
            kind=kind,
            is_pending=pending,
        )

    @staticmethod
    def _position_of(result) -> int:
        """از روی دیل انجام‌شده، تیکت پوزیشن را پیدا می‌کند."""
        deals = mt5.history_deals_get(ticket=result.deal)
        if deals:
            return int(deals[0].position_id)
        return int(result.order)

    # ------------------------------------------------------------ management

    def positions(self, ticket: Optional[int] = None):
        if ticket is not None:
            return list(mt5.positions_get(ticket=ticket) or ())
        return [p for p in (mt5.positions_get() or ()) if p.magic == self.trading.magic]

    def pending_orders(self, ticket: Optional[int] = None):
        if ticket is not None:
            return list(mt5.orders_get(ticket=ticket) or ())
        return [o for o in (mt5.orders_get() or ()) if o.magic == self.trading.magic]

    def modify(self, ticket: int, sl: Optional[float] = None, tp: Optional[float] = None) -> None:
        found = self.positions(ticket)
        if not found:
            raise MT5Error(f"پوزیشن {ticket} باز نیست")
        position = found[0]
        request = {
            "action": mt5.TRADE_ACTION_SLTP,
            "position": int(ticket),
            "symbol": position.symbol,
            "sl": self._round(position.symbol, sl if sl is not None else position.sl) or 0.0,
            "tp": self._round(position.symbol, tp if tp is not None else position.tp) or 0.0,
        }
        result = mt5.order_send(request)
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            code = result.retcode if result else mt5.last_error()
            comment = result.comment if result else ""
            raise MT5Error(f"تغییر حد ضرر/سود ناموفق (کد {code}): {comment}")

    def close(self, ticket: int, fraction: float = 1.0) -> float:
        """بستن کامل یا جزئی. خروجی: حجمی که بسته شد."""
        found = self.positions(ticket)
        if not found:
            raise MT5Error(f"پوزیشن {ticket} باز نیست")
        position = found[0]
        info = self.symbol_info(position.symbol)
        step = info.volume_step or 0.01

        volume = position.volume
        if fraction < 1.0:
            volume = math.floor((position.volume * fraction) / step) * step
            volume = round(volume, 8)
            if volume < info.volume_min:
                raise MT5Error("حجم باقی‌مانده برای بستن جزئی از حداقل مجاز کمتر است")
            if position.volume - volume < info.volume_min:
                volume = position.volume  # باقی‌مانده قابل نگهداری نیست، کامل ببند

        tick = self.tick(position.symbol)
        is_buy = position.type == mt5.POSITION_TYPE_BUY
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": position.symbol,
            "volume": float(volume),
            "type": mt5.ORDER_TYPE_SELL if is_buy else mt5.ORDER_TYPE_BUY,
            "position": int(ticket),
            "price": tick.bid if is_buy else tick.ask,
            "deviation": self.trading.deviation_points,
            "magic": self.trading.magic,
            "comment": "tg-close",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": self._filling(position.symbol, pending=False),
        }
        result = mt5.order_send(request)
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            code = result.retcode if result else mt5.last_error()
            comment = result.comment if result else ""
            raise MT5Error(f"بستن پوزیشن ناموفق (کد {code}): {comment}")
        return float(volume)

    def cancel(self, ticket: int) -> None:
        if not self.pending_orders(ticket):
            raise MT5Error(f"اردر پندینگ {ticket} وجود ندارد")
        result = mt5.order_send({"action": mt5.TRADE_ACTION_REMOVE, "order": int(ticket)})
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            code = result.retcode if result else mt5.last_error()
            raise MT5Error(f"لغو اردر ناموفق (کد {code})")

    # ----------------------------------------------------------- risk guards

    def realized_pnl_today(self) -> float:
        start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        deals = mt5.history_deals_get(start, datetime.now()) or ()
        return sum(d.profit + d.swap + d.commission
                   for d in deals if d.magic == self.trading.magic)
