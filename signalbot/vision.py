"""خواندن سیگنال از روی عکس با مدل بینایی Gemini.

خروجی مدل با یک اسکیمای سفت‌وسخت محدود شده تا همیشه JSON ساختاریافته برگردد،
ولی هیچ‌وقت به آن اعتماد نمی‌کنیم: هر عددی که برمی‌گردد دوباره با همان
اعتبارسنجی‌های پارسر متنی چک می‌شود.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Literal, Optional

from pydantic import BaseModel, Field

from .models import OrderKind, ParseResult, Side, Signal, SignalUpdate, UpdateKind
from .parser import SignalParser

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You read trading signal images from a Telegram forex/gold channel and extract \
their content as structured data. The text may be English, Persian (Farsi), or \
a mix, and digits may be Persian/Arabic numerals — always output ASCII numbers.

## What these images look like

Signals are TradingView chart screenshots with a long/short position tool drawn \
on them. The tool draws a shaded box with labels written INSIDE or at the LEFT \
EDGE of the box, each followed by its price in parentheses, for example:

    entry (4,347.133)
    sl    (4,339.168)
    TP1   (4,355.097)
    TP2   (4,363.061)

**Only those labelled values are the signal's prices.** A chart is full of other \
numbers that you must IGNORE completely:

- the price scale / axis running down the RIGHT side of the chart
- the highlighted current-price tag on that axis (often on a coloured badge)
- the OHLC header at the top left (`O4,354.815 H4,355.040 L4,351.980 C...`)
- indicator readouts such as `ATR - 5 (14, RMA)` and their values
- time axis labels, volume numbers, and the watermark

If you cannot clearly read a labelled entry/sl/TP value, set that field to null \
and lower `confidence`. Never substitute a number from the price axis for a \
missing label — that would open a wrong trade.

The Telegram caption next to the image usually carries the instrument and the \
order type, e.g. `XAUUSD` then `buystop`. Map the caption to `order_type`:
`buystop`/`sellstop` -> "STOP", `buylimit`/`selllimit` -> "LIMIT", plain \
`buy`/`sell`/`now` -> "MARKET". The caption also decides `side`: anything with \
"buy" is BUY, anything with "sell" is SELL. If the caption and the chart \
disagree, trust the caption for side and order type, and the chart for prices.

Common follow-up captions in this channel and how to classify them:

- `riskfree` / `ریسک فری`  -> kind "update", update_kind "SL_TO_BE"
- `TP1✅` / `TP2✅` / `TP1 hit`  -> kind "info" (no action, targets are already set)
- `cancel` / `کنسل` / `سیگنال کنسل شد`  -> kind "update", update_kind "CANCEL"
- `close` / `ببندید`  -> kind "update", update_kind "CLOSE_ALL"

Classify every image into exactly one `kind`:

- "signal"  — a NEW trade instruction: it names a direction (buy/sell/خرید/فروش) \
and at least one price level. This is the only kind that opens a trade.
- "update"  — an instruction to CHANGE an existing trade: close it, close part of \
it, cancel a pending order, or move the stop loss / take profit.
- "info"    — commentary, results, or a report that requires no action: \
"TP1 hit", profit screenshots, charts, analysis, greetings, advertising.
- "none"    — the image contains no trading content at all, or is unreadable.

Rules you must follow:

1. NEVER invent or infer a number. Copy digits exactly as shown. If a price is \
blurry, cropped, or ambiguous, leave that field null and lower `confidence`.
2. "TP1 hit", "target reached", "+120 pips", a profit screenshot, or a closed \
position report is ALWAYS "info" — never "update" and never "signal".
3. Only use "update" when the image tells the reader to DO something to an open \
trade right now (e.g. "close now", "cancel this signal", "move SL to entry", \
"ببندید", "کنسل کنید", "ریسک فری").
4. An entry given as a range (e.g. "3340-3344") goes in entry_low and entry_high, \
and entry stays null.
5. `refers_to_symbol` and `refers_to_entry` are for "update" images: the symbol \
and entry price of the ORIGINAL trade being modified, if the image shows them. \
Leave null if not shown.
6. `confidence` is your honest confidence that every extracted number is correct. \
Below 0.8 means a human must check it.
7. Put anything unusual, contradictory, or worth a human's attention in `notes`, \
in Persian.
"""


class VisionExtraction(BaseModel):
    """اسکیمایی که مدل موظف است دقیقاً همین را برگرداند."""

    kind: Literal["signal", "update", "info", "none"]
    symbol: Optional[str] = Field(
        None, description="Instrument as written, e.g. XAUUSD, GOLD, EURUSD, طلا"
    )
    side: Optional[Literal["BUY", "SELL"]] = None
    order_type: Optional[Literal["MARKET", "LIMIT", "STOP"]] = Field(
        None, description="From the caption: buystop/sellstop=STOP, buylimit/selllimit=LIMIT"
    )
    entry: Optional[float] = None
    entry_low: Optional[float] = None
    entry_high: Optional[float] = None
    sl: Optional[float] = None
    tps: list[float] = Field(default_factory=list)
    update_kind: Optional[
        Literal["CLOSE_ALL", "CLOSE_PARTIAL", "SL_TO_BE", "MOVE_SL", "MOVE_TP", "CANCEL"]
    ] = None
    update_price: Optional[float] = None
    update_fraction: Optional[float] = Field(
        None, description="For CLOSE_PARTIAL: portion to close, e.g. 0.5 for half"
    )
    refers_to_symbol: Optional[str] = None
    refers_to_entry: Optional[float] = None
    confidence: float = Field(0.0, ge=0.0, le=1.0)
    notes: str = ""


class VisionError(RuntimeError):
    pass


class VisionReader:
    """عکس → داده‌ی ساختاریافته. اعتبارسنجی نهایی با همان قواعد پارسر متنی است."""

    def __init__(
        self,
        api_key: str,
        model: str,
        aliases: dict[str, str],
        whitelist=(),
        min_confidence: float = 0.8,
        proxy: str = "",
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.min_confidence = min_confidence
        self.proxy = proxy
        self.aliases = {k.upper(): v.upper() for k, v in aliases.items()}
        self.whitelist = {s.upper() for s in whitelist}
        # فقط برای استفاده از اعتبارسنجی مشترک
        self._validator = SignalParser(aliases, whitelist)
        self._client = None

    @property
    def client(self):
        """کلاینت با تاخیر ساخته می‌شود تا ساخت شیء نیازی به شبکه نداشته باشد."""
        if self._client is None:
            if not self.api_key:
                raise VisionError("کلید GEMINI_API_KEY تنظیم نشده است")
            from google import genai
            from google.genai import types

            options = None
            if self.proxy:
                log.info("اتصال به Gemini از طریق پراکسی %s", self.proxy)
                options = types.HttpOptions(client_args={"proxy": self.proxy})
            self._client = genai.Client(api_key=self.api_key, http_options=options)
        return self._client

    # --------------------------------------------------------------- calling

    @staticmethod
    def shrink(image: bytes, max_side: int = 1400, quality: int = 85) -> tuple[bytes, str]:
        """عکس را به JPEG فشرده تبدیل می‌کند.

        هم توکن کمتری مصرف می‌شود، هم روی اتصال‌های ناپایدار (پراکسی) احتمال
        قطع شدن درخواست کمتر است. اگر تبدیل نشد، اصل عکس برگردانده می‌شود.
        """
        try:
            import io

            from PIL import Image

            with Image.open(io.BytesIO(image)) as im:
                im = im.convert("RGB")
                if max(im.size) > max_side:
                    im.thumbnail((max_side, max_side))
                buf = io.BytesIO()
                im.save(buf, "JPEG", quality=quality, optimize=True)
            return buf.getvalue(), "image/jpeg"
        except Exception as exc:  # noqa: BLE001
            log.debug("فشرده‌سازی عکس انجام نشد: %s", exc)
            return image, "image/jpeg"

    def extract(self, image: bytes, mime_type: str = "image/jpeg",
                caption: str = "", attempts: int = 3) -> VisionExtraction:
        """عکس را می‌خواند. خطاهای گذرا چند بار تلاش مجدد می‌شوند."""
        image, mime_type = self.shrink(image)
        last: Optional[Exception] = None
        for attempt in range(1, attempts + 1):
            try:
                return self._extract_once(image, mime_type, caption)
            except VisionError as exc:
                last = exc
                if not self._is_transient(exc) or attempt == attempts:
                    raise
                delay = 2 * attempt
                log.warning("تلاش %s ناموفق (%s) — %s ثانیه دیگر دوباره",
                            attempt, str(exc)[:80], delay)
                time.sleep(delay)
        raise last  # type: ignore[misc]

    @staticmethod
    def _is_transient(exc: Exception) -> bool:
        """خطاهایی که تلاش مجدد منطقی است: شلوغی سرویس یا قطعی اتصال."""
        text = str(exc)
        return any(marker in text for marker in (
            "503", "UNAVAILABLE", "500", "INTERNAL", "504", "DEADLINE",
            "RemoteProtocolError", "ConnectError", "ReadTimeout", "ConnectTimeout",
        ))

    def _extract_once(self, image: bytes, mime_type: str,
                      caption: str) -> VisionExtraction:
        from google.genai import errors, types

        prompt = "Extract the trading content of this image."
        if caption.strip():
            prompt += (
                "\n\nThe image was posted with this caption — use it as additional "
                f"context, it may contain the actual numbers:\n{caption.strip()}"
            )

        try:
            response = self.client.models.generate_content(
                model=self.model,
                contents=[
                    types.Part.from_bytes(data=image, mime_type=mime_type),
                    prompt,
                ],
                config=types.GenerateContentConfig(
                    system_instruction=SYSTEM_PROMPT,
                    response_mime_type="application/json",
                    response_schema=VisionExtraction,
                    temperature=0.0,
                ),
            )
        except errors.APIError as exc:
            raise VisionError(f"فراخوانی مدل بینایی ناموفق بود: {exc}") from exc
        except Exception as exc:  # noqa: BLE001
            # قطعی شبکه یا پراکسی نباید ربات را زمین بزند؛ سیگنال رد می‌شود و
            # عکس برای بررسی دستی فرستاده می‌شود.
            raise VisionError(
                f"ارتباط با مدل بینایی قطع شد ({type(exc).__name__}: {exc}). "
                f"اگر تکرار شد، پراکسی را چک کن."
            ) from exc

        parsed = response.parsed
        if isinstance(parsed, VisionExtraction):
            return parsed
        # اگر SDK نتوانست خودش پارس کند، دستی تلاش می‌کنیم
        raw = getattr(response, "text", None)
        if not raw:
            raise VisionError("مدل بینایی پاسخ خالی برگرداند")
        try:
            return VisionExtraction.model_validate(json.loads(raw))
        except Exception as exc:  # noqa: BLE001
            raise VisionError(f"پاسخ مدل قابل خواندن نبود: {raw[:200]}") from exc

    # ------------------------------------------------------------ converting

    def to_result(self, data: VisionExtraction) -> ParseResult:
        """خروجی مدل را به همان ساختارهای داخلی ربات تبدیل و اعتبارسنجی می‌کند."""
        if data.kind in ("info", "none"):
            return ParseResult()

        if data.confidence < self.min_confidence:
            return ParseResult(
                rejected_reason=(
                    f"اطمینان مدل از خواندن عکس پایین است ({data.confidence:.0%})"
                    + (f" — {data.notes}" if data.notes else "")
                )
            )

        if data.kind == "update":
            return self._to_update(data)
        return self._to_signal(data)

    def resolve_symbol(self, name: Optional[str]) -> Optional[str]:
        if not name:
            return None
        key = name.strip().upper()
        if key in self.aliases:
            return self.aliases[key]
        # «XAUUSD SELL» یا «GOLD/USD» را هم بگیر
        for alias, base in self.aliases.items():
            if alias in key:
                return base
        return None

    def _to_signal(self, data: VisionExtraction) -> ParseResult:
        symbol = self.resolve_symbol(data.symbol)
        if symbol is None:
            return ParseResult(
                rejected_reason=f"نماد «{data.symbol}» شناخته نشد — به aliases اضافه‌اش کن"
            )
        if self.whitelist and symbol not in self.whitelist:
            return ParseResult(rejected_reason=f"نماد {symbol} در whitelist نیست")
        if data.side is None:
            return ParseResult(rejected_reason="جهت معامله (خرید/فروش) در عکس پیدا نشد")

        zone = None
        if data.entry_low is not None and data.entry_high is not None:
            zone = (min(data.entry_low, data.entry_high),
                    max(data.entry_low, data.entry_high))

        signal = Signal(
            symbol=symbol,
            side=Side(data.side),
            entry=data.entry,
            entry_zone=zone,
            sl=data.sl,
            order_hint=OrderKind(data.order_type) if data.order_type else None,
            tps=sorted(set(data.tps), reverse=data.side == "SELL"),
            raw_text=f"[از روی عکس] {data.notes}".strip(),
        )
        reason = self._validator.validate(signal)
        return ParseResult(signal=signal, rejected_reason=reason)

    def _to_update(self, data: VisionExtraction) -> ParseResult:
        if data.update_kind is None:
            return ParseResult(rejected_reason="نوع دستور مدیریتی در عکس مشخص نبود")
        kind = UpdateKind(data.update_kind)
        if kind in (UpdateKind.MOVE_SL, UpdateKind.MOVE_TP) and data.update_price is None:
            return ParseResult(
                rejected_reason=f"دستور {kind.value} بدون قیمت مشخص — اعمال نشد"
            )
        fraction = data.update_fraction if data.update_fraction else 0.5
        update = SignalUpdate(
            kind=kind,
            price=data.update_price,
            fraction=min(max(fraction, 0.01), 0.99),
            raw_text=f"[از روی عکس] {data.notes}".strip(),
        )
        return ParseResult(update=update)

    # ------------------------------------------------------------- utilities

    @staticmethod
    def describe(data: VisionExtraction) -> str:
        """خلاصه‌ی خوانا از چیزی که مدل دید — برای نمایش در پنل تایید."""
        lines = [f"مدل تشخیص داد: {data.kind} (اطمینان {data.confidence:.0%})"]
        if data.symbol:
            lines.append(f"نماد: {data.symbol}")
        if data.side:
            kind = {"MARKET": "بازار", "LIMIT": "لیمیت", "STOP": "استاپ"}
            order = f" ({kind[data.order_type]})" if data.order_type else ""
            lines.append(f"جهت: {data.side}{order}")
        if data.entry is not None:
            lines.append(f"ورود: {data.entry:g}")
        if data.entry_low is not None and data.entry_high is not None:
            lines.append(f"محدوده‌ی ورود: {data.entry_low:g} تا {data.entry_high:g}")
        if data.sl is not None:
            lines.append(f"حد ضرر: {data.sl:g}")
        if data.tps:
            lines.append("حد سود: " + " / ".join(f"{t:g}" for t in data.tps))
        if data.update_kind:
            price = f" @ {data.update_price:g}" if data.update_price is not None else ""
            lines.append(f"دستور: {data.update_kind}{price}")
        if data.refers_to_symbol or data.refers_to_entry is not None:
            ref = data.refers_to_symbol or "?"
            entry = f" @ {data.refers_to_entry:g}" if data.refers_to_entry is not None else ""
            lines.append(f"مربوط به: {ref}{entry}")
        if data.notes:
            lines.append(f"یادداشت: {data.notes}")
        return "\n".join(lines)
