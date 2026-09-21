"""Turn free-form Telegram signal text into structured Signal / SignalUpdate objects.

پارسر عمداً محافظه‌کار است: اگر چیزی مبهم بود، به جای حدس زدن، سیگنال را رد
می‌کند و دلیلش را برمی‌گرداند تا در پنل کنترل ببینی.
"""
from __future__ import annotations

import re
from typing import Iterable, Optional

from .models import ParseResult, Side, Signal, SignalUpdate, UpdateKind

# ---------------------------------------------------------------- normalizing

_PERSIAN_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
_ARABIC_LETTERS = str.maketrans({"ي": "ی", "ك": "ک", "ۀ": "ه", "ة": "ه", "أ": "ا", "إ": "ا", "آ": "ا"})
_ZERO_WIDTH = dict.fromkeys(map(ord, "​‌‍‎‏﻿"), " ")


def normalize(text: str) -> str:
    """اعداد فارسی -> لاتین، حروف عربی -> فارسی، فاصله‌ها یکدست، حروف کوچک."""
    if not text:
        return ""
    out = text.translate(_PERSIAN_DIGITS).translate(_ARABIC_LETTERS).translate(_ZERO_WIDTH)
    out = out.replace("٫", ".").replace("،", " ")
    # emoji و علائم تزئینی را به فاصله تبدیل کن تا چسبندگی توکن‌ها خراب نشود
    out = re.sub(r"[^\w\s.,:;=@/\\+\-–—()\[\]%؀-ۿ]", " ", out)
    out = re.sub(r"[–—]", "-", out)
    return re.sub(r"[ \t]+", " ", out).lower().strip()


# ------------------------------------------------------------------- patterns

# حتماً داخل گروه بماند: بدون (?: ) عملگر | کل الگوی میزبان را می‌شکند
_NUM = r"(?:\d{1,7}(?:,\d{3})*(?:\.\d{1,6})?|\.\d{1,6})"

_BUY_WORDS = r"buy|long|bullish|bull|خرید|بخر|بای|لانگ|صعودی"
_SELL_WORDS = r"sell|short|bearish|bear|فروش|بفروش|سل|شورت|نزولی"

_SIDE_RE = re.compile(
    rf"\b(?P<buy>{_BUY_WORDS})\b|\b(?P<sell>{_SELL_WORDS})\b",
    re.IGNORECASE,
)

# «buy limit» / «sell stop» — نوع اردر را صریح اعلام می‌کند
_ORDER_HINT_RE = re.compile(rf"\b(?:{_BUY_WORDS}|{_SELL_WORDS})\s*(limit|stop)\b", re.IGNORECASE)

_SL_RE = re.compile(
    rf"(?:\bsl\b|\bs[./\\]l\b|stop\s*-?\s*loss|stoploss|حد\s*ضرر|حدضرر|استاپ\s*لاس|استاپ)"
    rf"\s*(?:در|به|at|is)?\s*[:=@\-]?\s*(?P<v>{_NUM})",
    re.IGNORECASE,
)

# نکته: بعد از «tp» مرز واژه نمی‌گذاریم تا «TP1» هم مچ شود، و شماره‌ی تارگت
# فقط وقتی مصرف می‌شود که بعدش جداکننده بیاید — وگرنه رقمِ قیمت را می‌بلعد.
_TP_RE = re.compile(
    rf"(?:\btp|\bt[./\\]p|take\s*-?\s*profit|\btarget|حد\s*سود|حدسود|تارگت|تی\s*پی|هدف)"
    rf"\s*(?:\d{{1,2}}(?=\s*[:=\-]))?"
    rf"\s*[:=@\-]?\s*(?P<v>{_NUM}(?:\s*[/,]\s*{_NUM})*)",
    re.IGNORECASE,
)

_ENTRY_RE = re.compile(
    rf"(?:\bentry\b|\benter\b|\bopen\b|\bprice\b|\bzone\b|\bat\b|نقطه\s*ورود|قیمت\s*ورود|ورود|محدوده)"
    rf"\s*[:=@\-]?\s*(?P<v>{_NUM}(?:\s*-\s*{_NUM})?)",
    re.IGNORECASE,
)

_MARKET_RE = re.compile(
    r"\b(?:now|market|instant|at\s*market|cmp)\b|بازار|همین\s*الان|مارکت|فوری",
    re.IGNORECASE,
)

# ------------------------------------------------------- management (updates)

_UPDATE_PATTERNS: list[tuple[UpdateKind, re.Pattern[str]]] = [
    # ترتیب مهم است: الگوهای خاص‌تر اول می‌آیند
    (
        UpdateKind.SL_TO_BE,
        re.compile(
            r"break\s*-?\s*even|breakeven|\brisk\s*-?\s*free\b|riskfree|"
            r"\bsl\s*(?:to|at|->|=)\s*(?:be|b/e|entry|open)\b|"
            r"ریسک\s*فری|سر\s*به\s*سر|سربه\s*سر|حد\s*ضرر\s*(?:را\s*)?(?:به\s*)?(?:نقطه\s*)?ورود",
            re.IGNORECASE,
        ),
    ),
    (
        UpdateKind.CLOSE_PARTIAL,
        re.compile(
            r"clos\w*\s+(?:half|50%|partial|part)|half\s+clos\w*|partial\s*clos\w*|"
            r"take\s+(?:half|partial)|نصف|پارشال|بخشی\s*از\s*پوزیشن",
            re.IGNORECASE,
        ),
    ),
    (
        UpdateKind.CANCEL,
        re.compile(
            r"\bcancel\b|\bdelete\s*(?:the\s*)?order\b|order\s*cancel\w*|\bvoid\b|"
            r"کنسل|لغو|باطل",
            re.IGNORECASE,
        ),
    ),
    (
        UpdateKind.MOVE_SL,
        re.compile(
            rf"(?:move|shift|change|set|update|جابه\s*جا|منتقل|بذار|بگذار)?\s*"
            rf"(?:\bsl\b|stop\s*-?\s*loss|حد\s*ضرر)\s*(?:to|at|->|=|را\s*به|به)\s*(?P<v>{_NUM})",
            re.IGNORECASE,
        ),
    ),
    (
        UpdateKind.MOVE_TP,
        re.compile(
            rf"(?:move|shift|change|set|update|جابه\s*جا|منتقل)?\s*"
            rf"(?:\btp\b|take\s*-?\s*profit|حد\s*سود)\s*\d{{0,2}}\s*(?:to|at|->|=|را\s*به|به)\s*(?P<v>{_NUM})",
            re.IGNORECASE,
        ),
    ),
    (
        UpdateKind.CLOSE_ALL,
        re.compile(
            r"clos\w*\s*(?:it|all|now|the\s*trade|position|everything)?|\bexit\b|"
            r"\bbook\s*profit\b|out\s*of\s*(?:the\s*)?trade|"
            r"ببند|بستن|خروج|کلوز|از\s*معامله\s*خارج",
            re.IGNORECASE,
        ),
    ),
]

# «tp1 hit» به تنهایی یک خبر است نه دستور؛ نباید باعث اقدام شود
_NEWS_ONLY_RE = re.compile(
    r"\b(?:tp\s*\d?|target)\s*\d?\s*(?:hit|reached|done|✅)|"
    r"حد\s*سود\s*\d?\s*(?:زده|خورد|رسید)|تارگت\s*\d?\s*(?:زده|خورد)",
    re.IGNORECASE,
)


def _to_float(raw: str) -> float:
    return float(raw.replace(",", "").strip())


def _extract_numbers(blob: str) -> list[float]:
    return [_to_float(m.group(0)) for m in re.finditer(_NUM, blob)]


# -------------------------------------------------------------------- parsing


class SignalParser:
    def __init__(self, aliases: dict[str, str], whitelist: Iterable[str] = ()) -> None:
        self.aliases = {k.upper(): v.upper() for k, v in aliases.items()}
        self.whitelist = {s.upper() for s in whitelist}
        # طولانی‌ترین نام‌ها اول، تا XAUUSD قبل از XAU مچ شود
        keys = sorted(self.aliases, key=len, reverse=True)
        parts = []
        for key in keys:
            esc = re.escape(key.lower())
            # برای کلمات لاتین مرز واژه لازم است؛ برای فارسی مرز \b کار نمی‌کند
            parts.append(rf"\b{esc}\b" if key.isascii() else esc)
        self._symbol_re = re.compile("|".join(parts), re.IGNORECASE) if parts else None

    # ------------------------------------------------------------- public API

    def parse(self, text: str, *, is_reply: bool = False) -> ParseResult:
        norm = normalize(text)
        if not norm:
            return ParseResult()

        signal = self._parse_signal(norm, raw=text)
        if signal is not None:
            return signal

        update = self._parse_update(norm)
        if update is not None:
            if not is_reply:
                # دستور مدیریتی بدون ریپلای معلوم نیست به کدام پوزیشن مربوط است
                return ParseResult(rejected_reason="دستور مدیریتی بدون ریپلای به سیگنال")
            return ParseResult(update=update)
        return ParseResult()

    # ------------------------------------------------------------- internals

    def _find_symbol(self, norm: str) -> Optional[str]:
        if not self._symbol_re:
            return None
        match = self._symbol_re.search(norm)
        if not match:
            return None
        return self.aliases.get(match.group(0).upper())

    def _parse_signal(self, norm: str, raw: str) -> Optional[ParseResult]:
        side_match = _SIDE_RE.search(norm)
        if not side_match:
            return None
        symbol = self._find_symbol(norm)
        if symbol is None:
            return None

        side = Side.BUY if side_match.group("buy") else Side.SELL

        if self.whitelist and symbol not in self.whitelist:
            return ParseResult(rejected_reason=f"نماد {symbol} در whitelist نیست")

        sl_match = _SL_RE.search(norm)
        sl = _to_float(sl_match.group("v")) if sl_match else None

        tps: list[float] = []
        for m in _TP_RE.finditer(norm):
            for value in _extract_numbers(m.group("v")):
                if value not in tps:
                    tps.append(value)

        entry, zone = self._parse_entry(norm, side_match, sl, tps)

        signal = Signal(
            symbol=symbol,
            side=side,
            entry=entry,
            entry_zone=zone,
            sl=sl,
            tps=tps,
            raw_text=raw,
        )
        reason = self.validate(signal)
        if reason:
            return ParseResult(signal=signal, rejected_reason=reason)
        return ParseResult(signal=signal)

    def _parse_entry(
        self,
        norm: str,
        side_match: re.Match[str],
        sl: Optional[float],
        tps: list[float],
    ) -> tuple[Optional[float], Optional[tuple[float, float]]]:
        entry_match = _ENTRY_RE.search(norm)
        blob = entry_match.group("v") if entry_match else None

        if blob is None:
            if _MARKET_RE.search(norm):
                return None, None
            # عددی که بلافاصله بعد از کلمه‌ی خرید/فروش آمده: «sell xauusd 3350»
            tail = norm[side_match.end(): side_match.end() + 60]
            # اگر بلافاصله SL/TP آمده، یعنی ورود مارکت است
            tail = re.split(r"\bsl\b|\btp\b|حد\s*ضرر|حد\s*سود", tail)[0]
            numbers = _extract_numbers(tail)
            numbers = [n for n in numbers if n != sl and n not in tps]
            if not numbers:
                return None, None
            if len(numbers) >= 2 and re.search(rf"{_NUM}\s*-\s*{_NUM}", tail):
                lo, hi = sorted(numbers[:2])
                return None, (lo, hi)
            return numbers[0], None

        numbers = _extract_numbers(blob)
        if not numbers:
            return None, None
        if len(numbers) >= 2:
            lo, hi = sorted(numbers[:2])
            return None, (lo, hi)
        return numbers[0], None

    def _parse_update(self, norm: str) -> Optional[SignalUpdate]:
        if _NEWS_ONLY_RE.search(norm) and not re.search(
            r"clos|ببند|بستن|\bsl\b|حد\s*ضرر|break|ریسک\s*فری", norm
        ):
            return None
        for kind, pattern in _UPDATE_PATTERNS:
            match = pattern.search(norm)
            if not match:
                continue
            price = None
            if kind in (UpdateKind.MOVE_SL, UpdateKind.MOVE_TP):
                price = _to_float(match.group("v"))
            fraction = 0.5
            if kind is UpdateKind.CLOSE_PARTIAL:
                pct = re.search(r"(\d{1,3})\s*%", norm)
                if pct:
                    fraction = min(max(int(pct.group(1)) / 100, 0.01), 0.99)
            return SignalUpdate(kind=kind, price=price, fraction=fraction, raw_text=norm)
        return None

    # ------------------------------------------------------------ validation

    @staticmethod
    def validate(signal: Signal) -> Optional[str]:
        """بررسی منطقی بودن اعداد. خروجی None یعنی سالم است."""
        ref = signal.entry
        if ref is None and signal.entry_zone:
            ref = sum(signal.entry_zone) / 2
        if ref is None:
            # ورود مارکت: فقط رابطه‌ی SL و TP را نسبت به هم چک می‌کنیم
            if signal.sl is not None and signal.tps:
                if signal.side is Side.BUY and min(signal.tps) <= signal.sl:
                    return "برای خرید، حد سود باید بالاتر از حد ضرر باشد"
                if signal.side is Side.SELL and max(signal.tps) >= signal.sl:
                    return "برای فروش، حد سود باید پایین‌تر از حد ضرر باشد"
            return None

        if signal.sl is not None:
            if signal.side is Side.BUY and signal.sl >= ref:
                return f"برای خرید، حد ضرر ({signal.sl:g}) باید زیر قیمت ورود ({ref:g}) باشد"
            if signal.side is Side.SELL and signal.sl <= ref:
                return f"برای فروش، حد ضرر ({signal.sl:g}) باید بالای قیمت ورود ({ref:g}) باشد"

        for tp in signal.tps:
            if signal.side is Side.BUY and tp <= ref:
                return f"برای خرید، حد سود ({tp:g}) باید بالای قیمت ورود ({ref:g}) باشد"
            if signal.side is Side.SELL and tp >= ref:
                return f"برای فروش، حد سود ({tp:g}) باید زیر قیمت ورود ({ref:g}) باشد"
        return None


def order_hint(text: str) -> Optional[str]:
    """اگر متن صریحاً limit یا stop گفته باشد، همان را برگردان."""
    match = _ORDER_HINT_RE.search(normalize(text))
    return match.group(1).lower() if match else None
