"""Load config.yaml + .env into a typed settings object."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class TelegramSettings:
    api_id: int
    api_hash: str
    bot_token: str
    source_channel: str | int
    control_chat_id: int


@dataclass
class MT5Settings:
    login: int
    password: str
    server: str
    path: str | None


@dataclass
class CTraderSettings:
    client_id: str = ""
    client_secret: str = ""
    access_token: str = ""
    refresh_token: str = ""
    account_id: int = 0
    live: bool = False          # False = حساب دمو


@dataclass
class TradingSettings:
    mode: str = "confirm"
    risk_percent: float = 1.0
    max_lot: float = 1.0
    max_open_positions: int = 15
    max_daily_loss_percent: float = 5.0
    require_sl: bool = True
    tp_strategy: str = "split"
    max_tps: int = 6
    tp_selection: str = "nearest"   # nearest | spread | farthest
    entry_zone: str = "mid"
    market_threshold_pips: float = 15
    max_slippage_pips: float = 5
    max_signal_age_seconds: int = 300
    pending_expiry_hours: int = 12
    deviation_points: int = 30
    magic: int = 778899


@dataclass
class SymbolSettings:
    aliases: dict[str, str] = field(default_factory=dict)
    whitelist: list[str] = field(default_factory=list)


@dataclass
class VisionSettings:
    enabled: bool = True
    model: str = "gemini-3.5-flash"
    min_confidence: float = 0.8
    save_images: bool = True
    api_key: str = ""
    #: مثلا http://127.0.0.1:10809 — اگر سرویس از شبکه‌ی تو مستقیم در دسترس نیست
    proxy: str = ""


@dataclass
class Settings:
    telegram: TelegramSettings
    broker: str                 # mt5 | ctrader
    mt5: MT5Settings
    ctrader: CTraderSettings
    trading: TradingSettings
    symbols: SymbolSettings
    vision: VisionSettings
    log_level: str = "INFO"
    log_file: str = "logs/signalbot.log"


#: مقادیر نمونه‌ی .env.example — اگر دست‌نخورده مانده‌اند یعنی هنوز پر نشده
_PLACEHOLDERS = {
    "1234567", "your_api_hash_here", "123456:aaa...", "aiza...",
    "12345678", "your_password", "your-api-key", "changeme",
}


def _require_env(name: str, required: bool = True) -> str:
    value = os.getenv(name, "").strip()
    if value.lower() in _PLACEHOLDERS:
        if required:
            raise SystemExit(
                f"متغیر {name} در فایل .env هنوز مقدار نمونه دارد («{value}»).\n"
                f"مقدار واقعی خودت را جایگزین کن."
            )
        value = ""
    if not value and required:
        hint = "" if (ROOT / ".env").exists() else \
            "\nفایل .env هنوز ساخته نشده — اول این را بزن:  copy .env.example .env"
        raise SystemExit(
            f"متغیر {name} در فایل .env تنظیم نشده.{hint}"
        )
    return value


def _int_env(name: str, required: bool = True) -> int:
    raw = _require_env(name, required)
    if not raw:
        return 0
    try:
        return int(raw)
    except ValueError as exc:
        raise SystemExit(f"مقدار {name} باید عدد باشد، ولی «{raw}» داده شده.") from exc


def _filter_kwargs(cls: type, data: dict[str, Any]) -> dict[str, Any]:
    """Drop unknown keys so an outdated config.yaml doesn't crash startup."""
    known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
    return {k: v for k, v in data.items() if k in known}


#: بخش‌هایی که هر ابزار واقعاً به آن‌ها نیاز دارد
ALL_SECTIONS = ("telegram", "broker", "vision")


def load_settings(
    config_path: Path | str | None = None,
    require: tuple[str, ...] = ALL_SECTIONS,
) -> Settings:
    """تنظیمات را می‌خواند.

    `require` مشخص می‌کند نبودِ کدام بخش خطای مرگبار است. مثلاً ابزار تست
    Gemini فقط به بخش vision نیاز دارد و نباید به خاطر نبودِ کلید متاتریدر
    متوقف شود.
    """
    load_dotenv(ROOT / ".env")
    path = Path(config_path) if config_path else ROOT / "config.yaml"
    if not path.exists():
        example = path.parent / "config.example.yaml"
        hint = ("\nاز روی نمونه بسازش:  cp config.example.yaml config.yaml"
                if example.exists() else "")
        raise SystemExit(f"فایل تنظیمات پیدا نشد: {path}{hint}")
    raw: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    tg_raw = raw.get("telegram", {})
    source = tg_raw.get("source_channel", "")
    if isinstance(source, str) and source.lstrip("-").isdigit():
        source = int(source)

    need_tg = "telegram" in require
    telegram = TelegramSettings(
        api_id=_int_env("TG_API_ID", need_tg),
        api_hash=_require_env("TG_API_HASH", need_tg),
        bot_token=os.getenv("TG_BOT_TOKEN", "").strip(),
        source_channel=source,
        control_chat_id=int(tg_raw.get("control_chat_id") or 0),
    )

    broker = str(raw.get("broker", "mt5")).lower()
    if broker not in {"mt5", "ctrader"}:
        raise SystemExit("broker در config.yaml باید mt5 یا ctrader باشد.")

    # فقط تنظیمات بروکری که واقعاً استفاده می‌شود اجباری است
    need_broker = "broker" in require or "mt5" in require
    need_mt5 = need_broker and broker == "mt5"
    need_ct = need_broker and broker == "ctrader"

    mt5 = MT5Settings(
        login=_int_env("MT5_LOGIN", need_mt5),
        password=_require_env("MT5_PASSWORD", need_mt5),
        server=_require_env("MT5_SERVER", need_mt5),
        path=os.getenv("MT5_PATH", "").strip() or None,
    )

    ct_raw = raw.get("ctrader", {}) or {}
    ctrader = CTraderSettings(
        client_id=_require_env("CTRADER_CLIENT_ID", need_ct),
        client_secret=_require_env("CTRADER_CLIENT_SECRET", need_ct),
        access_token=_require_env("CTRADER_ACCESS_TOKEN", need_ct),
        refresh_token=os.getenv("CTRADER_REFRESH_TOKEN", "").strip(),
        account_id=_int_env("CTRADER_ACCOUNT_ID", need_ct),
        live=bool(ct_raw.get("live", False)),
    )

    trading = TradingSettings(**_filter_kwargs(TradingSettings, raw.get("trading", {})))
    if trading.mode not in {"confirm", "auto"}:
        raise SystemExit("trading.mode باید confirm یا auto باشد.")

    sym_raw = raw.get("symbols", {}) or {}
    symbols = SymbolSettings(
        aliases={str(k).upper(): str(v).upper() for k, v in (sym_raw.get("aliases") or {}).items()},
        whitelist=[str(s).upper() for s in (sym_raw.get("whitelist") or [])],
    )

    vision = VisionSettings(**_filter_kwargs(VisionSettings, raw.get("vision", {})))
    vision.api_key = _require_env("GEMINI_API_KEY", required=False)
    vision.proxy = (
        os.getenv("GEMINI_PROXY", "").strip()
        or os.getenv("HTTPS_PROXY", "").strip()
        or vision.proxy
    )
    if "vision" in require and vision.enabled and not vision.api_key:
        raise SystemExit(
            "سیگنال‌ها به صورت عکس می‌آیند و vision.enabled روشن است، ولی "
            "GEMINI_API_KEY در .env تنظیم نشده.\n"
            "کلید رایگان را از https://aistudio.google.com/apikey بگیر، "
            "یا اگر فعلاً کلید نداری در config.yaml مقدار vision.enabled را false کن "
            "(در آن حالت عکس‌ها فقط برایت فوروارد می‌شوند)."
        )

    log_raw = raw.get("logging", {}) or {}
    return Settings(
        telegram=telegram,
        broker=broker,
        mt5=mt5,
        ctrader=ctrader,
        trading=trading,
        symbols=symbols,
        vision=vision,
        log_level=str(log_raw.get("level", "INFO")).upper(),
        log_file=str(log_raw.get("file", "logs/signalbot.log")),
    )
