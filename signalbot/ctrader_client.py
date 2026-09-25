"""اتصال مستقیم به بروکر از راه cTrader Open API.

برخلاف متاتریدر که به ترمینال ویندوزی نیاز دارد، این آداپتور یک اتصال TCP
مستقیم به سرور بروکر می‌زند و روی لینوکس هم کار می‌کند.

معماری: SDK رسمی Spotware روی Twisted کار می‌کند که حلقه‌ی رویدادش با asyncio
تلگرام فرق دارد. برای همین reactor در یک ترد جدا اجرا می‌شود و متدهای این کلاس
همگام (blocking) می‌مانند — دقیقاً مثل آداپتور متاتریدر، تا موتور اجرا و ربات
هیچ تغییری لازم نداشته باشند.

واحدها (منبع اصلی اشتباه در این API):
  • قیمت‌های اسپات: عدد صحیح، باید بر 100000 تقسیم شود
  • حجم‌ها: بر حسب «سِنتی‌یونیت» یعنی واحد × ۱۰۰
  • مبالغ پولی: عدد صحیح با مقیاس 10^moneyDigits
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from .broker import BrokerError, BrokerMath, Execution
from .config import CTraderSettings, TradingSettings
from .models import OrderKind, Side, Signal

log = logging.getLogger(__name__)

#: قیمت‌های اسپات همیشه با این مقیاس می‌آیند
PRICE_SCALE = 100_000.0
#: حجم‌ها بر حسب واحد × ۱۰۰ رد و بدل می‌شوند
VOLUME_SCALE = 100.0

_reactor_started = threading.Event()
_reactor_lock = threading.Lock()


def _start_reactor() -> Any:
    """reactor توییستد را یک بار در یک ترد جدا بالا می‌آورد."""
    from twisted.internet import reactor

    with _reactor_lock:
        if not _reactor_started.is_set():
            thread = threading.Thread(
                target=reactor.run,
                kwargs={"installSignalHandlers": False},
                name="ctrader-reactor",
                daemon=True,
            )
            thread.start()
            _reactor_started.set()
            time.sleep(0.3)  # فرصت کوتاه تا reactor واقعاً بالا بیاید
    return reactor


def list_accounts(client_id: str, client_secret: str, access_token: str) -> list:
    """حساب‌های معاملاتی متصل به یک توکن (دمو و واقعی) را برمی‌گرداند.

    از همان reactor مشترک استفاده می‌کند، چون reactor توییستد فقط یک بار در
    هر پروسه قابل اجراست و ربات بعداً برای معامله دوباره به آن نیاز دارد.
    """
    from ctrader_open_api import Client, EndPoints, Protobuf, TcpProtocol
    from ctrader_open_api.messages import OpenApiMessages_pb2 as M
    from twisted.internet.threads import blockingCallFromThread

    reactor = _start_reactor()
    connected = threading.Event()
    client = Client(EndPoints.PROTOBUF_DEMO_HOST, EndPoints.PROTOBUF_PORT, TcpProtocol)
    client.setConnectedCallback(lambda _c: connected.set())
    blockingCallFromThread(reactor, client.startService)
    try:
        if not connected.wait(timeout=30):
            raise BrokerError("اتصال به سرور cTrader برقرار نشد")

        def send(message):
            deferred = client.send(message, responseTimeoutInSeconds=20)
            deferred.addCallback(Protobuf.extract)
            return deferred

        app_auth = M.ProtoOAApplicationAuthReq()
        app_auth.clientId = client_id
        app_auth.clientSecret = client_secret
        try:
            blockingCallFromThread(reactor, send, app_auth)
        except Exception as exc:  # noqa: BLE001
            raise BrokerError("Client ID یا Secret اشتباه است") from exc

        req = M.ProtoOAGetAccountListByAccessTokenReq()
        req.accessToken = access_token
        try:
            response = blockingCallFromThread(reactor, send, req)
        except Exception as exc:  # noqa: BLE001
            raise BrokerError(f"لیست حساب‌ها گرفته نشد: {exc}") from exc
        return list(getattr(response, "ctidTraderAccount", []))
    finally:
        try:
            blockingCallFromThread(reactor, client.stopService)
        except Exception:  # noqa: BLE001
            pass


@dataclass
class CTraderSymbol:
    """مشخصات نماد با همان نام فیلدهای متاتریدر، تا BrokerMath دست‌نخورده کار کند."""

    name: str
    symbol_id: int
    digits: int
    point: float
    trade_tick_size: float
    trade_tick_value: float
    trade_tick_value_loss: float
    volume_min: float
    volume_step: float
    volume_max: float
    trade_stops_level: int
    lot_size_units: float


@dataclass
class CTraderTick:
    bid: float
    ask: float


@dataclass
class CTraderAccount:
    balance: float
    equity: float
    currency: str
    login: int
    server: str
    trade_allowed: bool = True
    leverage: int = 0


class CTraderClient(BrokerMath):
    def __init__(self, settings: CTraderSettings, trading: TradingSettings) -> None:
        self.settings = settings
        self.trading = trading
        self.label = f"tg{trading.magic}"

        self._client = None
        self._connected = threading.Event()
        self._authorised = False
        self._symbols: dict[str, CTraderSymbol] = {}       # نام -> مشخصات
        self._symbol_names: dict[int, str] = {}            # شناسه -> نام
        self._light_symbols: dict[str, int] = {}           # نام -> شناسه
        self._prices: dict[int, CTraderTick] = {}
        self._deposit_asset_id: Optional[int] = None
        self._money_digits = 2
        self._account_currency = ""
        self._last_error: Optional[str] = None

    # ----------------------------------------------------------- زیرساخت

    def _reactor(self):
        return _start_reactor()

    def _blocking(self, fn, *args, **kwargs):
        """اجرای یک کار داخل reactor و منتظر ماندن برای نتیجه، از ترد اصلی."""
        from twisted.internet.threads import blockingCallFromThread

        return blockingCallFromThread(self._reactor(), fn, *args, **kwargs)

    def _request(self, message, timeout: int = 20):
        """یک پیام می‌فرستد و پاسخ تایپ‌شده را برمی‌گرداند."""
        from ctrader_open_api import Protobuf

        if self._client is None:
            raise BrokerError("هنوز به cTrader وصل نشده‌ایم")

        def send():
            deferred = self._client.send(message, responseTimeoutInSeconds=timeout)
            deferred.addCallback(Protobuf.extract)
            return deferred

        self._last_error = None
        try:
            response = self._blocking(send)
        except Exception as exc:  # noqa: BLE001
            # سرور خطاها را گاهی بدون شناسه‌ی درخواست می‌فرستد، پس اینجا به جای
            # «timeout» گنگ، خطای واقعی‌ای که رسیده را نشان می‌دهیم
            if self._last_error:
                raise BrokerError(self._last_error) from exc
            raise BrokerError(f"درخواست به cTrader ناموفق بود: {exc}") from exc

        name = type(response).__name__
        if name == "ProtoOAErrorRes":
            raise BrokerError(
                f"cTrader خطا داد: {response.errorCode} — "
                f"{getattr(response, 'description', '')}"
            )
        if name == "ProtoOAOrderErrorEvent":
            raise BrokerError(
                f"اردر رد شد: {response.errorCode} — "
                f"{getattr(response, 'description', '')}"
            )
        return response

    # ----------------------------------------------------------- اتصال

    def connect(self) -> None:
        from ctrader_open_api import Client, EndPoints, TcpProtocol
        from ctrader_open_api.messages import OpenApiMessages_pb2 as M

        host = (EndPoints.PROTOBUF_LIVE_HOST if self.settings.live
                else EndPoints.PROTOBUF_DEMO_HOST)
        log.info("اتصال به cTrader (%s)", "live" if self.settings.live else "demo")

        self._client = Client(host, EndPoints.PROTOBUF_PORT, TcpProtocol)
        self._connected.clear()
        self._client.setConnectedCallback(lambda _c: self._connected.set())
        self._client.setDisconnectedCallback(self._on_disconnect)
        self._client.setMessageReceivedCallback(self._on_message)
        self._blocking(self._client.startService)

        if not self._connected.wait(timeout=30):
            raise BrokerError(
                "اتصال TCP به سرور cTrader برقرار نشد. "
                "اینترنت سرور و باز بودن پورت 5035 را چک کن."
            )

        # ۱) احراز هویت اپلیکیشن
        app_auth = M.ProtoOAApplicationAuthReq()
        app_auth.clientId = self.settings.client_id
        app_auth.clientSecret = self.settings.client_secret
        try:
            self._request(app_auth)
        except BrokerError as exc:
            # سرور برای کلید اشتباه اصلاً پاسخ نمی‌دهد و درخواست تایم‌اوت می‌شود
            raise BrokerError(
                "احراز هویت اپلیکیشن ناموفق بود — سرور پاسخی نداد.\n"
                "معمولاً یعنی CTRADER_CLIENT_ID یا CTRADER_CLIENT_SECRET اشتباه است.\n"
                "از https://connect.spotware.com دوباره کپی‌شان کن."
            ) from exc

        # ۲) احراز هویت حساب معاملاتی
        acc_auth = M.ProtoOAAccountAuthReq()
        acc_auth.ctidTraderAccountId = self.settings.account_id
        acc_auth.accessToken = self.settings.access_token
        try:
            self._request(acc_auth)
        except BrokerError as exc:
            raise BrokerError(
                f"احراز هویت حساب {self.settings.account_id} ناموفق بود: {exc}\n"
                f"چک کن: توکن منقضی نشده باشد (python tools/ctrader_auth.py)، و "
                f"ctrader.live در config.yaml با نوع حساب بخواند "
                f"(الان روی {'واقعی' if self.settings.live else 'دمو'} تنظیم است)."
            ) from exc
        self._authorised = True

        account = self.account()
        log.info("متصل شد: حساب %s | بالانس %.2f %s",
                 account.login, account.balance, account.currency)

    def _on_disconnect(self, _client, reason) -> None:  # noqa: ANN001
        log.warning("اتصال cTrader قطع شد: %s", reason)
        self._connected.clear()
        self._authorised = False

    def _on_message(self, _client, message) -> None:  # noqa: ANN001
        """رویدادهای بدون درخواست — مهم‌ترینش قیمت لحظه‌ای است."""
        from ctrader_open_api import Protobuf

        try:
            event = Protobuf.extract(message)
        except Exception:  # noqa: BLE001
            return

        name = type(event).__name__
        if name in ("ProtoOAErrorRes", "ProtoOAOrderErrorEvent"):
            self._last_error = (
                f"cTrader خطا داد: {getattr(event, 'errorCode', '?')} — "
                f"{getattr(event, 'description', '')}"
            )
            log.error("%s", self._last_error)
            return
        if name != "ProtoOASpotEvent":
            return
        previous = self._prices.get(event.symbolId)
        bid = event.bid / PRICE_SCALE if event.bid else (previous.bid if previous else 0.0)
        ask = event.ask / PRICE_SCALE if event.ask else (previous.ask if previous else 0.0)
        self._prices[event.symbolId] = CTraderTick(bid=bid, ask=ask)

    def ensure_connected(self) -> bool:
        if self._connected.is_set() and self._authorised:
            return False
        log.warning("اتصال cTrader قطع بود — تلاش برای اتصال دوباره")
        self._symbols.clear()
        self._symbol_names.clear()
        self._light_symbols.clear()
        self._prices.clear()
        self.connect()
        return True

    def shutdown(self) -> None:
        if self._client is not None:
            try:
                self._blocking(self._client.stopService)
            except Exception:  # noqa: BLE001
                pass

    # ----------------------------------------------------------- حساب

    def account(self) -> CTraderAccount:
        from ctrader_open_api.messages import OpenApiMessages_pb2 as M

        req = M.ProtoOATraderReq()
        req.ctidTraderAccountId = self.settings.account_id
        trader = self._request(req).trader

        self._money_digits = trader.moneyDigits or 2
        self._deposit_asset_id = trader.depositAssetId
        scale = 10 ** self._money_digits
        balance = trader.balance / scale

        if not self._account_currency:
            self._account_currency = self._lookup_currency(trader.depositAssetId)

        return CTraderAccount(
            balance=balance,
            equity=balance,  # اکوییتی لحظه‌ای جدا محاسبه می‌شود؛ بالانس معیار ریسک است
            currency=self._account_currency,
            login=trader.traderLogin,
            server=trader.brokerName,
            leverage=int(trader.leverageInCents / 100) if trader.leverageInCents else 0,
        )

    def _lookup_currency(self, asset_id: int) -> str:
        from ctrader_open_api.messages import OpenApiMessages_pb2 as M

        try:
            req = M.ProtoOAAssetListReq()
            req.ctidTraderAccountId = self.settings.account_id
            for asset in self._request(req).asset:
                if asset.assetId == asset_id:
                    return asset.name
        except BrokerError:
            pass
        return "?"

    # ----------------------------------------------------------- نمادها

    def _load_symbol_list(self) -> None:
        from ctrader_open_api.messages import OpenApiMessages_pb2 as M

        req = M.ProtoOASymbolsListReq()
        req.ctidTraderAccountId = self.settings.account_id
        req.includeArchivedSymbols = False
        for light in self._request(req).symbol:
            self._light_symbols[light.symbolName.upper()] = light.symbolId
            self._symbol_names[light.symbolId] = light.symbolName

    def resolve_symbol(self, base: str) -> str:
        """نام نماد بروکر را پیدا می‌کند و اشتراک قیمت را برایش باز می‌کند."""
        base = base.upper()
        if not self._light_symbols:
            self._load_symbol_list()

        name = None
        if base in self._light_symbols:
            name = self._symbol_names[self._light_symbols[base]]
        else:
            # نمادهایی مثل XAUUSD.m یا XAU/USD را هم بگیر
            for candidate in self._light_symbols:
                stripped = candidate.replace(".", "").replace("_", "").replace("/", "")
                if stripped == base or stripped.startswith(base):
                    name = self._symbol_names[self._light_symbols[candidate]]
                    break
        if name is None:
            raise BrokerError(f"نماد {base} در این حساب cTrader پیدا نشد")

        self._ensure_symbol_details(name)
        return name

    def _ensure_symbol_details(self, name: str) -> CTraderSymbol:
        from ctrader_open_api.messages import OpenApiMessages_pb2 as M

        if name in self._symbols:
            return self._symbols[name]

        symbol_id = self._light_symbols[name.upper()]
        req = M.ProtoOASymbolByIdReq()
        req.ctidTraderAccountId = self.settings.account_id
        req.symbolId.append(symbol_id)
        details = self._request(req).symbol[0]

        digits = details.digits
        point = 10 ** -digits
        lot_units = details.lotSize / VOLUME_SCALE  # واحد واقعی در یک لات

        # ارزش هر تیک برای یک لات، وقتی ارز مظنه همان ارز حساب است
        tick_value = point * lot_units

        spec = CTraderSymbol(
            name=name,
            symbol_id=symbol_id,
            digits=digits,
            point=point,
            trade_tick_size=point,
            trade_tick_value=tick_value,
            trade_tick_value_loss=tick_value,
            volume_min=details.minVolume / details.lotSize,
            volume_step=details.stepVolume / details.lotSize,
            volume_max=details.maxVolume / details.lotSize,
            trade_stops_level=int(details.slDistance or 0),
            lot_size_units=lot_units,
        )
        self._symbols[name] = spec
        self._subscribe(symbol_id)
        return spec

    def _subscribe(self, symbol_id: int) -> None:
        from ctrader_open_api.messages import OpenApiMessages_pb2 as M

        req = M.ProtoOASubscribeSpotsReq()
        req.ctidTraderAccountId = self.settings.account_id
        req.symbolId.append(symbol_id)
        self._request(req)
        log.info("اشتراک قیمت برای %s باز شد", self._symbol_names.get(symbol_id, symbol_id))

    def symbol_info(self, symbol: str) -> CTraderSymbol:
        if symbol in self._symbols:
            return self._symbols[symbol]
        if not self._light_symbols:
            self._load_symbol_list()
        return self._ensure_symbol_details(symbol)

    def tick(self, symbol: str) -> CTraderTick:
        spec = self.symbol_info(symbol)
        # اولین قیمت بعد از اشتراک با کمی تاخیر می‌رسد
        for _ in range(50):
            price = self._prices.get(spec.symbol_id)
            if price and price.bid and price.ask:
                return price
            time.sleep(0.1)
        raise BrokerError(
            f"قیمت لحظه‌ای {symbol} نرسید (بازار بسته است یا اشتراک برقرار نشد)"
        )

    # ----------------------------------------------------------- اردر

    def _to_centi(self, symbol: str, lots: float) -> int:
        spec = self.symbol_info(symbol)
        return int(round(lots * spec.lot_size_units * VOLUME_SCALE))

    def _from_centi(self, symbol: str, centi: int) -> float:
        spec = self.symbol_info(symbol)
        return round(centi / VOLUME_SCALE / spec.lot_size_units, 8)

    def place(self, signal: Signal, symbol: str, volume: float, kind: OrderKind,
              price: float, sl: Optional[float], tp: Optional[float],
              comment: str = "") -> Execution:
        from ctrader_open_api.messages import OpenApiMessages_pb2 as M
        from ctrader_open_api.messages import OpenApiModelMessages_pb2 as Mod

        spec = self.symbol_info(symbol)
        price = self._round(symbol, price)
        sl = self._round(symbol, sl)
        tp = self._round(symbol, tp)

        problem = self.check_stops_distance(symbol, price, sl, tp)
        if problem:
            raise BrokerError(problem)

        req = M.ProtoOANewOrderReq()
        req.ctidTraderAccountId = self.settings.account_id
        req.symbolId = spec.symbol_id
        req.tradeSide = (Mod.ProtoOATradeSide.BUY if signal.side is Side.BUY
                         else Mod.ProtoOATradeSide.SELL)
        req.volume = self._to_centi(symbol, volume)
        req.label = self.label
        req.comment = (comment or f"tg{signal.msg_id}")[:100]

        pending = kind is not OrderKind.MARKET
        if kind is OrderKind.MARKET:
            req.orderType = Mod.ProtoOAOrderType.MARKET
            req.slippageInPoints = int(self.trading.deviation_points)
        elif kind is OrderKind.LIMIT:
            req.orderType = Mod.ProtoOAOrderType.LIMIT
            req.limitPrice = price
        else:
            req.orderType = Mod.ProtoOAOrderType.STOP
            req.stopPrice = price

        if sl is not None:
            req.stopLoss = sl
        if tp is not None:
            req.takeProfit = tp

        if pending and self.trading.pending_expiry_hours > 0:
            req.timeInForce = Mod.ProtoOATimeInForce.GOOD_TILL_DATE
            expiry = datetime.now(timezone.utc) + timedelta(
                hours=self.trading.pending_expiry_hours)
            req.expirationTimestamp = int(expiry.timestamp() * 1000)
        elif pending:
            req.timeInForce = Mod.ProtoOATimeInForce.GOOD_TILL_CANCEL

        event = self._request(req, timeout=30)
        order = getattr(event, "order", None)
        if order is None:
            raise BrokerError(f"پاسخ نامنتظره از cTrader: {type(event).__name__}")

        # برای اجرای مارکت، شناسه‌ی پوزیشن مهم است نه شناسه‌ی اردر
        ticket = int(order.positionId or order.orderId)
        filled = order.executionPrice or price
        return Execution(
            ticket=ticket,
            symbol=symbol,
            side=signal.side,
            volume=volume,
            price=float(filled),
            sl=sl,
            tp=tp,
            kind=kind,
            is_pending=pending,
        )

    # ----------------------------------------------------------- مدیریت

    def _reconcile(self):
        from ctrader_open_api.messages import OpenApiMessages_pb2 as M

        req = M.ProtoOAReconcileReq()
        req.ctidTraderAccountId = self.settings.account_id
        return self._request(req, timeout=30)

    def positions(self, ticket: Optional[int] = None) -> list:
        found = []
        for position in self._reconcile().position:
            if ticket is not None and position.positionId != ticket:
                continue
            if ticket is None and position.tradeData.label != self.label:
                continue
            found.append(self._wrap_position(position))
        return found

    def _wrap_position(self, position):
        """پوزیشن را با همان نام فیلدهایی که موتور اجرا انتظار دارد برمی‌گرداند."""
        symbol = self._symbol_names.get(position.tradeData.symbolId, "?")

        @dataclass
        class Wrapped:
            ticket: int
            symbol: str
            volume: float
            type: int          # 0 = خرید، 1 = فروش (مثل متاتریدر)
            price_open: float
            sl: float
            tp: float
            magic: int

        volume = position.tradeData.volume / VOLUME_SCALE
        spec = self._symbols.get(symbol)
        if spec:
            volume = volume / spec.lot_size_units
        return Wrapped(
            ticket=int(position.positionId),
            symbol=symbol,
            volume=round(volume, 8),
            type=0 if position.tradeData.tradeSide == 1 else 1,
            price_open=float(position.price or 0),
            sl=float(position.stopLoss or 0),
            tp=float(position.takeProfit or 0),
            magic=self.trading.magic,
        )

    def pending_orders(self, ticket: Optional[int] = None) -> list:
        found = []
        for order in self._reconcile().order:
            if order.orderType == 1:         # MARKET — پندینگ نیست
                continue
            if order.positionId:             # قبلاً پر شده
                continue
            if ticket is not None and order.orderId != ticket:
                continue
            if ticket is None and order.tradeData.label != self.label:
                continue
            found.append(order)
        return found

    def modify(self, ticket: int, sl: Optional[float] = None,
               tp: Optional[float] = None) -> None:
        from ctrader_open_api.messages import OpenApiMessages_pb2 as M

        current = self.positions(ticket)
        if not current:
            raise BrokerError(f"پوزیشن {ticket} باز نیست")
        position = current[0]

        req = M.ProtoOAAmendPositionSLTPReq()
        req.ctidTraderAccountId = self.settings.account_id
        req.positionId = int(ticket)
        new_sl = sl if sl is not None else position.sl
        new_tp = tp if tp is not None else position.tp
        if new_sl:
            req.stopLoss = self._round(position.symbol, new_sl)
        if new_tp:
            req.takeProfit = self._round(position.symbol, new_tp)
        self._request(req, timeout=30)

    def close(self, ticket: int, fraction: float = 1.0) -> float:
        import math

        from ctrader_open_api.messages import OpenApiMessages_pb2 as M

        current = self.positions(ticket)
        if not current:
            raise BrokerError(f"پوزیشن {ticket} باز نیست")
        position = current[0]
        spec = self.symbol_info(position.symbol)

        volume = position.volume
        if fraction < 1.0:
            step = spec.volume_step or 0.01
            volume = round(math.floor((position.volume * fraction) / step) * step, 8)
            if volume < spec.volume_min:
                raise BrokerError("حجم باقی‌مانده برای بستن جزئی از حداقل مجاز کمتر است")
            if position.volume - volume < spec.volume_min:
                volume = position.volume

        req = M.ProtoOAClosePositionReq()
        req.ctidTraderAccountId = self.settings.account_id
        req.positionId = int(ticket)
        req.volume = self._to_centi(position.symbol, volume)
        self._request(req, timeout=30)
        return volume

    def cancel(self, ticket: int) -> None:
        from ctrader_open_api.messages import OpenApiMessages_pb2 as M

        if not self.pending_orders(ticket):
            raise BrokerError(f"اردر پندینگ {ticket} وجود ندارد")
        req = M.ProtoOACancelOrderReq()
        req.ctidTraderAccountId = self.settings.account_id
        req.orderId = int(ticket)
        self._request(req, timeout=30)

    # ----------------------------------------------------------- ریسک

    def realized_pnl_today(self) -> float:
        from ctrader_open_api.messages import OpenApiMessages_pb2 as M

        start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        req = M.ProtoOADealListReq()
        req.ctidTraderAccountId = self.settings.account_id
        req.fromTimestamp = int(start.timestamp() * 1000)
        req.toTimestamp = int(datetime.now(timezone.utc).timestamp() * 1000)
        req.maxRows = 1000

        total = 0.0
        for deal in self._request(req, timeout=30).deal:
            detail = deal.closePositionDetail
            if not detail or not detail.ByteSize():
                continue
            scale = 10 ** (detail.moneyDigits or self._money_digits)
            total += (detail.grossProfit + detail.swap + detail.commission) / scale
        return total
