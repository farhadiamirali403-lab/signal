"""پنل تلگرامی: همه‌ی راه‌اندازی با دکمه، داخل چت با ربات.

با این پنل لازم نیست فایل تنظیمات را دستی ویرایش کنی. فقط سه مقدار یک بار در
.env می‌رود (TG_API_ID ، TG_API_HASH ، TG_BOT_TOKEN) و بقیه از داخل تلگرام:

    ۱) ورود به اکانت تلگرامت (شماره + کد با دکمه‌های عددی)
    ۲) انتخاب کانال سیگنال از لیست کانال‌هایت
    ۳) کلید Gemini برای خواندن عکس‌ها
    ۴) اتصال حساب معاملاتی (cTrader یا متاتریدر ۵)
    ۵) دکمه‌ی «روشن کردن»

هر چه در پنل تنظیم شود در panel.json ذخیره می‌شود (روی گیت نمی‌رود).
"""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Optional

from telethon import Button, TelegramClient, events
from telethon.errors import (
    FloodWaitError,
    MessageNotModifiedError,
    PasswordHashInvalidError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneNumberInvalidError,
    SessionPasswordNeededError,
)

from .broker import BrokerError, make_broker
from .config import ROOT, CTraderSettings, MT5Settings, Settings, load_settings

log = logging.getLogger("signalbot.panel")

PANEL_FILE = ROOT / "panel.json"
CTRADER_REDIRECT = "http://localhost/"
#: توکن cTrader حدود ۳۰ روز اعتبار دارد؛ زودتر از آن تمدیدش می‌کنیم
CTRADER_REFRESH_AFTER = 20 * 24 * 3600
CHANNELS_PER_PAGE = 8
RISK_CHOICES = (0.5, 1.0, 2.0, 3.0)

_SECRET_INPUTS = {"password", "gemini", "mt5_password", "ct_secret"}


# ------------------------------------------------------------------ storage

class PanelState:
    """تنظیمات پنل روی دیسک. شامل رمزهاست، پس فقط مالک فایل بخواندش."""

    def __init__(self, path: Path = PANEL_FILE) -> None:
        self.path = path
        self.data: dict[str, Any] = {}
        if path.exists():
            try:
                self.data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                log.error("panel.json خوانده نشد، از صفر شروع می‌شود: %s", exc)

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def set(self, **values: Any) -> None:
        self.data.update(values)
        self.save()

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        tmp.replace(self.path)


# -------------------------------------------------------------------- panel

class Panel:
    def __init__(self, base: Settings) -> None:
        self.base = base
        self.state = PanelState()
        session_dir = ROOT / "sessions"
        session_dir.mkdir(exist_ok=True)
        tg = base.telegram
        self.bot = TelegramClient(str(session_dir / "control_bot"), tg.api_id, tg.api_hash)
        self.user = TelegramClient(str(session_dir / "user"), tg.api_id, tg.api_hash)
        self.engine = None  # SignalBot وقتی روشن است
        self.waiting: Optional[str] = None
        self.tmp: dict[str, Any] = {}
        self.tg_name: Optional[str] = None

    # ------------------------------------------------------------ helpers

    @property
    def owner(self) -> int:
        return int(self.base.telegram.control_chat_id or self.state.get("owner_id") or 0)

    def _channel(self) -> tuple[Any, str]:
        source = self.state.get("source_channel")
        if source:
            return source, self.state.get("source_title") or str(source)
        base = self.base.telegram.source_channel
        if base and base != "@CHANGE_ME":
            return base, str(base)
        return None, ""

    def _gemini_key(self) -> str:
        return self.state.get("gemini_key") or self.base.vision.api_key

    def _broker(self) -> tuple[str, Optional[str]]:
        """(نوع بروکر، توضیح) — توضیح None یعنی هنوز وصل نشده."""
        kind = self.state.get("broker")
        if kind == "ctrader":
            ct = self.state.get("ctrader") or {}
            if ct.get("access_token") and ct.get("account_id"):
                label = "واقعی ⚠️" if ct.get("live") else "دمو"
                return kind, f"cTrader {label} — {ct.get('login') or ct['account_id']}"
        elif kind == "mt5":
            mt = self.state.get("mt5") or {}
            if mt.get("login") and mt.get("password") and mt.get("server"):
                return kind, f"متاتریدر ۵ — {mt['login']} ({mt['server']})"
        # سازگاری با نصب قدیمی که همه‌چیز در .env بود
        if not kind:
            base = self.base
            if base.broker == "ctrader" and base.ctrader.access_token and base.ctrader.account_id:
                return "ctrader", f"cTrader — {base.ctrader.account_id} (از .env)"
            if base.broker == "mt5" and base.mt5.login and base.mt5.password:
                return "mt5", f"متاتریدر ۵ — {base.mt5.login} (از .env)"
        return kind or "", None

    def _trading(self) -> tuple[float, str]:
        return (float(self.state.get("risk_percent", self.base.trading.risk_percent)),
                str(self.state.get("mode", self.base.trading.mode)))

    async def _tg_ready(self) -> bool:
        if not self.user.is_connected():
            await self.user.connect()
        if not await self.user.is_user_authorized():
            self.tg_name = None
            return False
        if self.tg_name is None:
            me = await self.user.get_me()
            self.tg_name = f"{me.first_name or ''} (@{me.username})" if me.username \
                else (me.first_name or str(me.id))
        return True

    async def _missing(self) -> list[str]:
        missing = []
        if not await self._tg_ready():
            missing.append("tg")
        if self._channel()[0] is None:
            missing.append("channel")
        if not self._gemini_key():
            missing.append("gemini")
        if self._broker()[1] is None:
            missing.append("broker")
        return missing

    def build_settings(self) -> Settings:
        """تنظیمات پایه + هر چه در پنل انتخاب شده."""
        settings = copy.deepcopy(self.base)
        settings.telegram.source_channel = self._channel()[0]
        settings.telegram.control_chat_id = self.owner
        settings.vision.enabled = True
        settings.vision.api_key = self._gemini_key()
        risk, mode = self._trading()
        settings.trading.risk_percent = risk
        settings.trading.mode = mode

        kind = self.state.get("broker")
        if kind == "ctrader":
            ct = self.state.get("ctrader") or {}
            settings.broker = "ctrader"
            settings.ctrader = CTraderSettings(
                client_id=ct.get("client_id", ""),
                client_secret=ct.get("client_secret", ""),
                access_token=ct.get("access_token", ""),
                refresh_token=ct.get("refresh_token", ""),
                account_id=int(ct.get("account_id") or 0),
                live=bool(ct.get("live")),
            )
        elif kind == "mt5":
            mt = self.state.get("mt5") or {}
            settings.broker = "mt5"
            settings.mt5 = MT5Settings(
                login=int(mt.get("login") or 0),
                password=mt.get("password", ""),
                server=mt.get("server", ""),
                path=mt.get("path") or self.base.mt5.path,
            )
        return settings

    async def send(self, text: str, buttons=None) -> None:
        await self.bot.send_message(self.owner, text, buttons=buttons, link_preview=False)

    # --------------------------------------------------------------- menu

    async def menu_content(self) -> tuple[str, list]:
        tg_ok = await self._tg_ready()
        channel, title = self._channel()
        gemini = bool(self._gemini_key())
        _, broker_desc = self._broker()
        risk, mode = self._trading()
        running = self.engine is not None

        def mark(ok: bool) -> str:
            return "✅" if ok else "❌"

        lines = [
            "🤖 پنل ربات کپی سیگنال",
            "",
            f"{mark(tg_ok)} اکانت تلگرام: {self.tg_name if tg_ok else 'وصل نیست'}",
            f"{mark(channel is not None)} کانال سیگنال: {title or 'انتخاب نشده'}",
            f"{mark(gemini)} کلید Gemini (خواندن عکس): {'ثبت شده' if gemini else 'ثبت نشده'}",
            f"{mark(broker_desc is not None)} حساب معاملاتی: {broker_desc or 'وصل نیست'}",
            "",
            f"⚙️ ریسک هر سیگنال: {risk:g}% | حالت: "
            f"{'خودکار' if mode == 'auto' else 'با تایید تو'}",
        ]
        if running:
            lines.append("وضعیت: 🟢 روشن" + (" (⏸ متوقف موقت)" if self.engine.paused else ""))
        else:
            lines.append("وضعیت: ⚪️ خاموش")

        buttons = [
            [Button.inline("📱 اکانت تلگرام", b"p:tg"),
             Button.inline("📢 کانال", b"p:ch")],
            [Button.inline("🧠 کلید Gemini", b"p:gm"),
             Button.inline("🏦 حساب معاملاتی", b"p:br")],
            [Button.inline("⚙️ ریسک و حالت", b"p:set")],
        ]
        if running:
            buttons.append([Button.inline("📊 وضعیت حساب", b"p:status"),
                            Button.inline("▶️ ادامه" if self.engine.paused else "⏸ توقف موقت",
                                          b"p:pause")])
            buttons.append([Button.inline("🔴 بستن همه‌ی پوزیشن‌ها", b"p:closeask")])
            buttons.append([Button.inline("⏹ خاموش کردن ربات", b"p:off")])
        else:
            buttons.append([Button.inline("▶️ روشن کردن ربات", b"p:on")])
        return "\n".join(lines), buttons

    async def show_menu(self, event=None) -> None:
        text, buttons = await self.menu_content()
        if event is not None and isinstance(event, events.CallbackQuery.Event):
            await event.edit(text, buttons=buttons)
        else:
            await self.send(text, buttons=buttons)

    async def continue_setup(self) -> None:
        """بعد از هر مرحله، مستقیم برو سراغ مرحله‌ی بعدیِ ناقص."""
        missing = await self._missing()
        if not missing:
            await self.send("🎉 همه‌چیز آماده است. «روشن کردن ربات» را بزن.")
            await self.show_menu()
            return
        step = missing[0]
        if step == "tg":
            await self.ask_phone()
        elif step == "channel":
            await self.show_channels()
        elif step == "gemini":
            await self.ask_gemini()
        elif step == "broker":
            await self.show_broker_choice()

    # ------------------------------------------------------ telegram login

    async def ask_phone(self) -> None:
        self.waiting = "phone"
        await self.send(
            "📱 **مرحله‌ی ۱: اتصال اکانت تلگرامت**\n\n"
            "ربات عکس‌های کانال را با اکانت خودت می‌خواند (مثل وقتی خودت کانال را "
            "باز می‌کنی).\n\n"
            "دکمه‌ی پایین صفحه «📱 ارسال شماره‌ی من» را بزن، یا شماره را با کد "
            "کشور تایپ کن، مثل  +989121234567",
            buttons=[Button.request_phone("📱 ارسال شماره‌ی من", resize=True, single_use=True)],
        )

    async def got_phone(self, phone: str) -> None:
        phone = "+" + "".join(ch for ch in phone if ch.isdigit())
        await self.send("⏳ در حال درخواست کد از تلگرام…", buttons=Button.clear())
        try:
            if not self.user.is_connected():
                await self.user.connect()
            sent = await self.user.send_code_request(phone)
        except PhoneNumberInvalidError:
            await self.send("❌ این شماره معتبر نیست. دوباره با کد کشور بفرست، مثل +98912…")
            return
        except FloodWaitError as exc:
            self.waiting = None
            await self.send(f"⛔ تلگرام گفته {exc.seconds} ثانیه صبر کن و بعد دوباره امتحان کن.")
            return
        self.waiting = None
        self.tmp.update(phone=phone, phone_code_hash=sent.phone_code_hash, code="")
        await self.bot.send_message(self.owner, self._keypad_text(), buttons=self._keypad())

    @staticmethod
    def _keypad() -> list:
        rows = [[Button.inline(str(d), f"p:k:{d}".encode()) for d in row]
                for row in ((1, 2, 3), (4, 5, 6), (7, 8, 9))]
        rows.append([Button.inline("⌫", b"p:k:del"), Button.inline("0", b"p:k:0"),
                     Button.inline("✅ تایید", b"p:k:ok")])
        return rows

    def _keypad_text(self, note: str = "") -> str:
        code = self.tmp.get("code", "")
        shown = " ".join(code) if code else "—"
        return (
            (f"{note}\n\n" if note else "")
            + "🔢 کد ورودی که تلگرام الان در **اپ تلگرام** برایت فرستاد را با این "
            "دکمه‌ها وارد کن.\n"
            "⚠️ کد را تایپ نکن و برای کسی نفرست — تلگرام کدی که در چت فرستاده شود را "
            "باطل می‌کند.\n\n"
            f"کد: {shown}"
        )

    async def on_keypad(self, event, key: str) -> None:
        if "phone_code_hash" not in self.tmp:
            await event.answer("اول شماره را بفرست", alert=True)
            return
        code = self.tmp.get("code", "")
        if key == "del":
            self.tmp["code"] = code[:-1]
        elif key.isdigit():
            if len(code) < 6:
                self.tmp["code"] = code + key
        elif key == "ok":
            await self._sign_in(event)
            return
        await event.edit(self._keypad_text(), buttons=self._keypad())

    async def _sign_in(self, event) -> None:
        code = self.tmp.get("code", "")
        if len(code) < 5:
            await event.answer("کد کامل نیست", alert=True)
            return
        await event.edit("⏳ در حال ورود…")
        try:
            await self.user.sign_in(self.tmp["phone"], code,
                                    phone_code_hash=self.tmp["phone_code_hash"])
        except SessionPasswordNeededError:
            self.waiting = "password"
            await event.edit(
                "🔐 اکانتت تایید دو مرحله‌ای دارد. رمز دوم (Cloud Password) را همین‌جا "
                "بفرست.\nپیامت بلافاصله پاک می‌شود."
            )
            return
        except PhoneCodeInvalidError:
            self.tmp["code"] = ""
            await event.edit(self._keypad_text("❌ کد اشتباه بود. دوباره وارد کن."),
                             buttons=self._keypad())
            return
        except PhoneCodeExpiredError:
            self.tmp.clear()
            await event.edit("⌛️ کد منقضی شد. دوباره از اول:")
            await self.ask_phone()
            return
        await self._logged_in(event)

    async def got_password(self, password: str) -> None:
        try:
            await self.user.sign_in(password=password)
        except PasswordHashInvalidError:
            await self.send("❌ رمز اشتباه بود. دوباره بفرست.")
            return
        self.waiting = None
        await self._logged_in(None)

    async def _logged_in(self, event) -> None:
        self.tmp.clear()
        self.tg_name = None
        await self._tg_ready()
        text = f"✅ وارد شدی به عنوان {self.tg_name}"
        if event is not None:
            await event.edit(text)
        else:
            await self.send(text)
        await self.continue_setup()

    async def show_tg(self, event) -> None:
        if await self._tg_ready():
            await event.edit(
                f"📱 اکانت وصل است: {self.tg_name}",
                buttons=[[Button.inline("🔄 خروج و ورود با اکانت دیگر", b"p:tg:out")],
                         [Button.inline("« برگشت", b"p:menu")]],
            )
        else:
            await event.delete()
            await self.ask_phone()

    # ------------------------------------------------------------ channel

    async def show_channels(self, event=None, page: int = 0) -> None:
        if not await self._tg_ready():
            text = "اول اکانت تلگرامت را وصل کن."
            if event:
                await event.answer(text, alert=True)
            else:
                await self.send(text)
            return
        if page == 0 or "channels" not in self.tmp:
            dialogs = await self.user.get_dialogs(limit=300)
            self.tmp["channels"] = [(d.id, d.name) for d in dialogs
                                    if d.is_channel or d.is_group]
        channels = self.tmp["channels"]
        if not channels:
            await self.send("هیچ کانال یا گروهی در اکانتت پیدا نشد. اول عضو کانال سیگنال شو.")
            return
        start = page * CHANNELS_PER_PAGE
        chunk = channels[start:start + CHANNELS_PER_PAGE]
        buttons = [[Button.inline(name[:40] or str(cid), f"p:c:{start + i}".encode())]
                   for i, (cid, name) in enumerate(chunk)]
        nav = []
        if page > 0:
            nav.append(Button.inline("« قبلی", f"p:cp:{page - 1}".encode()))
        if start + CHANNELS_PER_PAGE < len(channels):
            nav.append(Button.inline("بعدی »", f"p:cp:{page + 1}".encode()))
        if nav:
            buttons.append(nav)
        buttons.append([Button.inline("« منو", b"p:menu")])
        text = ("📢 **مرحله‌ی ۲: کانال سیگنال**\n\nکانالی که عکس سیگنال‌ها را می‌فرستد "
                f"انتخاب کن (صفحه‌ی {page + 1}):")
        if event is not None:
            await event.edit(text, buttons=buttons)
        else:
            await self.send(text, buttons=buttons)

    async def pick_channel(self, event, index: int) -> None:
        channels = self.tmp.get("channels") or []
        if index >= len(channels):
            await event.answer("لیست قدیمی است، دوباره باز کن", alert=True)
            return
        cid, name = channels[index]
        self.state.set(source_channel=cid, source_title=name)
        await event.edit(f"✅ کانال انتخاب شد: {name}")
        await self.continue_setup()

    # ------------------------------------------------------------- gemini

    async def ask_gemini(self) -> None:
        self.waiting = "gemini"
        await self.send(
            "🧠 **مرحله‌ی ۳: کلید Gemini** (برای خواندن عدد‌های روی عکس)\n\n"
            "۱) برو به aistudio.google.com/apikey (از ایران با VPN)\n"
            "۲) با جیمیل وارد شو و «Create API key» را بزن\n"
            "۳) کلیدی که با AIza شروع می‌شود را کپی کن و همین‌جا بفرست\n\n"
            "رایگان است. پیامت بعد از خواندن پاک می‌شود.",
            buttons=[[Button.url("🔗 باز کردن صفحه‌ی کلید", "https://aistudio.google.com/apikey")]],
        )

    async def got_gemini(self, key: str) -> None:
        key = key.strip()
        await self.send("⏳ در حال امتحان کلید…")
        from .vision import VisionReader

        reader = VisionReader(api_key=key, model=self.base.vision.model, aliases={},
                              proxy=self.base.vision.proxy)

        def probe() -> None:
            next(iter(reader.client.models.list()))

        try:
            await asyncio.to_thread(probe)
        except Exception as exc:  # noqa: BLE001
            await self.send(
                f"❌ کلید کار نکرد: {str(exc)[:200]}\n\n"
                "اگر مطمئنی کلید درست است، احتمالاً سرور به Google دسترسی ندارد "
                "(سرور داخل ایران). کلید درست را دوباره بفرست یا /cancel بزن."
            )
            return
        self.waiting = None
        self.state.set(gemini_key=key)
        await self.send("✅ کلید Gemini ثبت شد.")
        await self.continue_setup()

    # ------------------------------------------------------------- broker

    async def show_broker_choice(self, event=None) -> None:
        text = ("🏦 **مرحله‌ی ۴: حساب معاملاتی**\n\nحسابت روی کدام پلتفرم است؟\n"
                "• cTrader — روی هر سروری کار می‌کند\n"
                "• متاتریدر ۵ — فقط روی ویندوز، و ترمینال باید همیشه باز باشد")
        buttons = [[Button.inline("cTrader", b"p:br:ct")],
                   [Button.inline("MetaTrader 5", b"p:br:mt5")],
                   [Button.inline("« منو", b"p:menu")]]
        if event is not None:
            await event.edit(text, buttons=buttons)
        else:
            await self.send(text, buttons=buttons)

    # --- MT5

    async def start_mt5(self, event) -> None:
        if sys.platform != "win32":
            await event.answer(
                "متاتریدر ۵ فقط روی ویندوز کار می‌کند و این سرور ویندوز نیست. "
                "از cTrader استفاده کن.", alert=True)
            return
        self.tmp["mt5"] = {}
        self.waiting = "mt5_login"
        await event.edit("🔢 شماره‌ی حساب متاتریدر (Login) را بفرست:")

    async def got_mt5(self, field: str, value: str) -> None:
        data = self.tmp.setdefault("mt5", {})
        value = value.strip()
        if field == "mt5_login":
            if not value.isdigit():
                await self.send("شماره‌ی حساب فقط عدد است. دوباره بفرست:")
                return
            data["login"] = int(value)
            self.waiting = "mt5_password"
            await self.send("🔑 رمز حساب (Password) را بفرست — بلافاصله پاک می‌شود:")
            return
        if field == "mt5_password":
            data["password"] = value
            self.waiting = "mt5_server"
            await self.send("🖥 اسم سرور را بفرست، دقیقاً همان که در متاتریدر است "
                            "(مثلاً LiteFinance-MT5-Demo):")
            return
        data["server"] = value
        self.waiting = None
        await self.send("⏳ در حال اتصال به متاتریدر…")
        settings = self.build_settings()
        settings.broker = "mt5"
        settings.mt5 = MT5Settings(login=data["login"], password=data["password"],
                                   server=data["server"], path=self.base.mt5.path)
        account = await self._probe_broker(settings)
        if account is None:
            return
        self.state.set(broker="mt5", mt5=data)
        self.tmp.pop("mt5", None)
        await self.send(f"✅ به متاتریدر وصل شد — بالانس {account.balance:,.2f} {account.currency}")
        await self.continue_setup()

    # --- cTrader

    def _ct_app(self) -> tuple[str, str]:
        ct = self.state.get("ctrader") or {}
        return (ct.get("client_id") or self.base.ctrader.client_id,
                ct.get("client_secret") or self.base.ctrader.client_secret)

    async def start_ctrader(self, event) -> None:
        client_id, client_secret = self._ct_app()
        if client_id and client_secret:
            self.tmp["ct"] = {"client_id": client_id, "client_secret": client_secret}
            await event.delete()
            await self._ct_send_link()
            return
        self.tmp["ct"] = {}
        self.waiting = "ct_id"
        await event.edit(
            "cTrader برای اتصال ربات‌ها یک «اپ» می‌خواهد (فقط یک بار):\n\n"
            "۱) در پنل LiteFinance یک حساب cTrader (اول دمو) بساز\n"
            "۲) برو به openapi.ctrader.com و با همان cTrader ID وارد شو\n"
            "۳) Applications → Add new app\n"
            f"   Redirect URI را دقیقاً  {CTRADER_REDIRECT}  بگذار\n"
            "۴) صبر کن وضعیت اپ Active شود (گاهی چند ساعت)\n"
            "۵) روی Credentials بزن\n\n"
            "حالا **Client ID** را همین‌جا بفرست:",
            buttons=[[Button.url("🔗 باز کردن سایت", "https://openapi.ctrader.com/apps")]],
        )

    async def got_ct_app(self, field: str, value: str) -> None:
        data = self.tmp.setdefault("ct", {})
        if field == "ct_id":
            data["client_id"] = value.strip()
            self.waiting = "ct_secret"
            await self.send("حالا **Secret** را بفرست (بلافاصله پاک می‌شود):")
            return
        data["client_secret"] = value.strip()
        self.waiting = None
        await self._ct_send_link()

    async def _ct_send_link(self) -> None:
        from ctrader_open_api import Auth

        data = self.tmp["ct"]
        uri = Auth(data["client_id"], data["client_secret"], CTRADER_REDIRECT) \
            .getAuthUri(scope="trading")
        self.waiting = "ct_url"
        await self.send(
            "🔗 **اتصال حساب cTrader**\n\n"
            "۱) دکمه‌ی زیر را بزن، با cTrader ID وارد شو و Allow را بزن\n"
            "۲) بعدش صفحه‌ای باز می‌شود که خطا می‌دهد — طبیعی است\n"
            "۳) آدرس بالای آن صفحه را (که با http://localhost/?code= شروع می‌شود) "
            "کامل کپی کن و همین‌جا بفرست",
            buttons=[[Button.url("🔗 ورود به cTrader", uri)]],
        )

    async def got_ct_url(self, text: str) -> None:
        from ctrader_open_api import Auth

        from .ctrader_client import list_accounts

        code = text.strip()
        if "code=" in code:
            code = code.split("code=", 1)[1].split("&")[0].strip()
        if not code:
            await self.send("آدرس را کامل بفرست (شامل ?code=…)")
            return
        data = self.tmp["ct"]
        await self.send("⏳ در حال گرفتن دسترسی…")
        auth = Auth(data["client_id"], data["client_secret"], CTRADER_REDIRECT)
        try:
            token = await asyncio.to_thread(auth.getToken, code)
        except Exception as exc:  # noqa: BLE001
            await self.send(f"❌ ارتباط با cTrader ناموفق بود: {exc}")
            return
        if "accessToken" not in token:
            await self.send(
                f"❌ cTrader کد را نپذیرفت: {token.get('description') or token}\n"
                "کد فقط یک بار و چند دقیقه اعتبار دارد. دوباره دکمه‌ی ورود را بزن."
            )
            await self._ct_send_link()
            return
        data["access_token"] = token["accessToken"]
        data["refresh_token"] = token.get("refreshToken", "")
        try:
            accounts = await asyncio.to_thread(
                list_accounts, data["client_id"], data["client_secret"], data["access_token"]
            )
        except BrokerError as exc:
            await self.send(f"❌ {exc}")
            return
        if not accounts:
            await self.send("❌ هیچ حساب cTrader به این اکانت وصل نیست. "
                            "اول در پنل بروکر یک حساب cTrader بساز.")
            return
        self.waiting = None
        self.tmp["ct_accounts"] = [
            {"account_id": int(a.ctidTraderAccountId), "login": int(a.traderLogin),
             "live": bool(a.isLive)} for a in accounts
        ]
        buttons = [[Button.inline(
            f"{'💰 واقعی' if a['live'] else '🧪 دمو'} — {a['login']}", f"p:ca:{i}".encode()
        )] for i, a in enumerate(self.tmp["ct_accounts"])]
        await self.send("کدام حساب؟ (پیشنهاد: اول با دمو امتحان کن)", buttons=buttons)

    async def pick_ct_account(self, event, index: int) -> None:
        accounts = self.tmp.get("ct_accounts") or []
        if index >= len(accounts) or "ct" not in self.tmp:
            await event.answer("منقضی شده، دوباره وصل شو", alert=True)
            return
        chosen = accounts[index]
        data = dict(self.tmp["ct"], **chosen, token_time=int(time.time()))
        await event.edit("⏳ در حال اتصال به حساب…")
        settings = self.build_settings()
        settings.broker = "ctrader"
        settings.ctrader = CTraderSettings(
            client_id=data["client_id"], client_secret=data["client_secret"],
            access_token=data["access_token"], refresh_token=data["refresh_token"],
            account_id=data["account_id"], live=data["live"],
        )
        account = await self._probe_broker(settings)
        if account is None:
            return
        self.state.set(broker="ctrader", ctrader=data)
        self.tmp.pop("ct", None)
        self.tmp.pop("ct_accounts", None)
        warn = "\n⚠️ این حساب **واقعی** است و با پول واقعی معامله می‌شود." if data["live"] else ""
        await self.send(f"✅ به cTrader وصل شد — بالانس {account.balance:,.2f} "
                        f"{account.currency}{warn}")
        await self.continue_setup()

    async def _refresh_ctrader_token(self) -> None:
        ct = self.state.get("ctrader") or {}
        if self.state.get("broker") != "ctrader" or not ct.get("refresh_token"):
            return
        if time.time() - int(ct.get("token_time") or 0) < CTRADER_REFRESH_AFTER:
            return
        from ctrader_open_api import Auth

        auth = Auth(ct["client_id"], ct["client_secret"], CTRADER_REDIRECT)
        try:
            token = await asyncio.to_thread(auth.refreshToken, ct["refresh_token"])
        except Exception as exc:  # noqa: BLE001
            log.error("تمدید توکن cTrader ناموفق: %s", exc)
            return
        if "accessToken" in token:
            ct.update(access_token=token["accessToken"],
                      refresh_token=token.get("refreshToken", ct["refresh_token"]),
                      token_time=int(time.time()))
            self.state.set(ctrader=ct)
            log.info("توکن cTrader تمدید شد")
        else:
            log.error("تمدید توکن cTrader رد شد: %s", token)

    async def _probe_broker(self, settings: Settings):
        """یک اتصال آزمایشی؛ خروجی None یعنی خطا (و به کاربر گفته شده)."""
        client = make_broker(settings)

        def probe():
            client.connect()
            try:
                return client.account()
            finally:
                client.shutdown()

        try:
            return await asyncio.to_thread(probe)
        except BrokerError as exc:
            await self.send(f"❌ اتصال ناموفق بود:\n{exc}")
        except Exception as exc:  # noqa: BLE001
            log.exception("اتصال آزمایشی بروکر")
            await self.send(f"❌ اتصال ناموفق بود: {exc}")
        return None

    # ----------------------------------------------------------- settings

    async def show_settings(self, event) -> None:
        risk, mode = self._trading()
        risk_row = [Button.inline(f"{'• ' if r == risk else ''}{r:g}%", f"p:risk:{r}".encode())
                    for r in RISK_CHOICES]
        await event.edit(
            "⚙️ **ریسک و حالت اجرا**\n\n"
            "ریسک = چند درصد بالانس روی هر سیگنال در خطر باشد (اگر حد ضرر بخورد).\n\n"
            "حالت خودکار: هر سیگنال بدون پرسیدن اجرا می‌شود.\n"
            "حالت با تایید: هر سیگنال با دکمه‌ی «اجرا کن / رد کن» برایت می‌آید.",
            buttons=[
                risk_row,
                [Button.inline(("• " if mode == "auto" else "") + "🤖 خودکار", b"p:mode:auto"),
                 Button.inline(("• " if mode == "confirm" else "") + "✋ با تایید",
                               b"p:mode:confirm")],
                [Button.inline("« منو", b"p:menu")],
            ],
        )

    def _apply_trading_live(self) -> None:
        if self.engine is not None:
            risk, mode = self._trading()
            self.engine.settings.trading.risk_percent = risk
            self.engine.settings.trading.mode = mode

    # ------------------------------------------------------------- engine

    async def start_engine(self) -> Optional[str]:
        """ربات را روشن می‌کند. خروجی: پیام خطا، یا None اگر موفق بود."""
        from .bot import SignalBot

        missing = await self._missing()
        if missing:
            names = {"tg": "اکانت تلگرام", "channel": "کانال", "gemini": "کلید Gemini",
                     "broker": "حساب معاملاتی"}
            return "هنوز تنظیم نشده: " + "، ".join(names[m] for m in missing)
        await self._refresh_ctrader_token()
        engine = SignalBot(self.build_settings(), user=self.user, bot=self.bot)
        try:
            await engine.start_engine()
        except (BrokerError, ValueError) as exc:
            engine.store.close()
            return str(exc)
        except Exception as exc:  # noqa: BLE001
            log.exception("روشن کردن ربات")
            engine.store.close()
            return f"خطای غیرمنتظره: {exc}"
        self.engine = engine
        self.state.set(running=True)
        return None

    async def stop_engine(self) -> None:
        if self.engine is not None:
            await self.engine.stop_engine()
            self.engine = None
        self.state.set(running=False)

    def _busy(self) -> bool:
        return self.engine is not None

    # ------------------------------------------------------------ handlers

    def register(self) -> None:
        @self.bot.on(events.NewMessage(incoming=True, func=lambda e: e.is_private))
        async def on_message(event):  # noqa: ANN001
            try:
                await self.on_message(event)
            except Exception as exc:  # noqa: BLE001
                log.exception("خطا در پنل")
                await event.reply(f"❌ خطا: {exc}")

        @self.bot.on(events.CallbackQuery)
        async def on_click(event):  # noqa: ANN001
            if not self.owner or event.sender_id != self.owner:
                await event.answer("این ربات خصوصی است", alert=True)
                return
            data = event.data.decode()
            try:
                if data.startswith("p:"):
                    await self.on_panel_click(event, data[2:])
                elif self.engine is not None:
                    await self.engine.handle_callback(event)
                else:
                    await event.answer("ربات خاموش است", alert=True)
            except MessageNotModifiedError:
                await event.answer()
            except Exception as exc:  # noqa: BLE001
                log.exception("خطا در دکمه‌ی پنل")
                await self.send(f"❌ خطا: {exc}")

    async def on_message(self, event) -> None:
        text = (event.raw_text or "").strip()
        if not self.owner:
            if text.startswith("/start"):
                self.state.set(owner_id=event.sender_id)
                log.info("مالک پنل ثبت شد: %s", event.sender_id)
                await event.reply(
                    "👋 سلام! از این به بعد این ربات فقط به تو جواب می‌دهد.\n\n"
                    "در ۴ مرحله راه می‌افتیم: اکانت تلگرام، کانال سیگنال، کلید Gemini "
                    "و حساب معاملاتی. شروع کنیم:"
                )
                await self.continue_setup()
            return
        if event.sender_id != self.owner:
            await event.reply("این ربات خصوصی است.")
            return

        contact = event.message.contact
        if contact is not None and self.waiting == "phone":
            if contact.user_id and contact.user_id != event.sender_id:
                await event.reply("فقط شماره‌ی خودت را بفرست.")
                return
            await self.got_phone(contact.phone_number)
            return

        if text in ("/start", "/menu"):
            self.waiting = None
            await self.show_menu()
            return
        if text == "/cancel":
            self.waiting = None
            await self.send("لغو شد.", buttons=Button.clear())
            await self.show_menu()
            return
        if text == "/status":
            await self.send(await self.engine.status_text() if self.engine else "ربات خاموش است")
            return
        if text in ("/pause", "/resume"):
            if self.engine:
                self.engine.paused = text == "/pause"
            await self.show_menu()
            return
        if text == "/closeall":
            await self.send(await self.engine.close_all() if self.engine else "ربات خاموش است")
            return
        if text == "/id":
            await event.reply(f"آیدی عددی تو: `{event.sender_id}`", parse_mode="md")
            return

        step = self.waiting
        if step is None:
            await self.show_menu()
            return
        if step in _SECRET_INPUTS:
            try:
                await event.delete()
            except Exception:  # noqa: BLE001
                pass
        if step == "phone":
            await self.got_phone(text)
        elif step == "password":
            await self.got_password(text)
        elif step == "gemini":
            await self.got_gemini(text)
        elif step.startswith("mt5_"):
            await self.got_mt5(step, text)
        elif step in ("ct_id", "ct_secret"):
            await self.got_ct_app(step, text)
        elif step == "ct_url":
            await self.got_ct_url(text)

    async def on_panel_click(self, event, data: str) -> None:
        cmd, _, arg = data.partition(":")
        busy_msg = "اول ربات را خاموش کن (⏹)، بعد این را عوض کن"

        if cmd == "menu":
            self.waiting = None
            await self.show_menu(event)
        elif cmd == "k":
            await self.on_keypad(event, arg)
        elif cmd == "tg":
            if self._busy():
                await event.answer(busy_msg, alert=True)
            elif arg == "out":
                await self.user.log_out()
                self.tg_name = None
                await self.user.connect()
                await event.edit("از اکانت خارج شدی.")
                await self.ask_phone()
            else:
                await self.show_tg(event)
        elif cmd == "ch":
            if self._busy():
                await event.answer(busy_msg, alert=True)
            else:
                await self.show_channels(event)
        elif cmd == "cp":
            await self.show_channels(event, page=int(arg))
        elif cmd == "c":
            await self.pick_channel(event, int(arg))
        elif cmd == "gm":
            if self._busy():
                await event.answer(busy_msg, alert=True)
            else:
                await event.delete()
                await self.ask_gemini()
        elif cmd == "br":
            if self._busy():
                await event.answer(busy_msg, alert=True)
            elif arg == "ct":
                await self.start_ctrader(event)
            elif arg == "mt5":
                await self.start_mt5(event)
            else:
                await self.show_broker_choice(event)
        elif cmd == "ca":
            await self.pick_ct_account(event, int(arg))
        elif cmd == "set":
            await self.show_settings(event)
        elif cmd == "risk":
            self.state.set(risk_percent=float(arg))
            self._apply_trading_live()
            await self.show_settings(event)
        elif cmd == "mode":
            self.state.set(mode=arg)
            self._apply_trading_live()
            await self.show_settings(event)
        elif cmd == "on":
            await event.edit("⏳ در حال روشن کردن…")
            error = await self.start_engine()
            if error:
                await self.send(f"❌ روشن نشد:\n{error}")
            await self.show_menu()
        elif cmd == "off":
            await self.stop_engine()
            await event.edit("⏹ ربات خاموش شد. دیگر معامله‌ای باز نمی‌کند.")
            await self.show_menu()
        elif cmd == "pause" and self.engine is not None:
            self.engine.paused = not self.engine.paused
            await self.show_menu(event)
        elif cmd == "status":
            await event.answer()
            await self.send(await self.engine.status_text() if self.engine else "ربات خاموش است")
        elif cmd == "closeask":
            await event.edit("مطمئنی همه‌ی پوزیشن‌های ربات بسته شوند؟",
                             buttons=[[Button.inline("بله، ببند", b"p:closeall"),
                                       Button.inline("نه", b"p:menu")]])
        elif cmd == "closeall":
            result = await self.engine.close_all() if self.engine else "ربات خاموش است"
            await event.edit(result)
            await self.show_menu()
        else:
            await event.answer()

    # ---------------------------------------------------------------- run

    async def run(self) -> None:
        await self.bot.start(bot_token=self.base.telegram.bot_token)
        me = await self.bot.get_me()
        await self.user.connect()
        self.register()
        log.info("پنل آماده است: @%s", me.username)
        print(f"\n✅ ربات بالا آمد. در تلگرام به @{me.username} برو و /start بزن.\n")

        if self.owner:
            if self.state.get("running"):
                error = await self.start_engine()
                if error:
                    await self.send(f"⚠️ ربات بعد از ری‌استارت روشن نشد:\n{error}")
            try:
                await self.show_menu()
            except Exception as exc:  # noqa: BLE001
                log.warning("منو فرستاده نشد (هنوز /start نزده‌ای؟): %s", exc)

        await self.bot.run_until_disconnected()

    async def shutdown(self) -> None:
        if self.engine is not None:
            await self.engine.stop_engine()
            self.engine = None


# --------------------------------------------------------------- first run

_ENV_PROMPTS = (
    ("TG_BOT_TOKEN",
     "توکن ربات (از @BotFather در تلگرام: /newbot)"),
    ("TG_API_ID",
     "api_id (از my.telegram.org → API development tools)"),
    ("TG_API_HASH",
     "api_hash (از همان صفحه)"),
)


def ensure_env() -> None:
    """اگر سه مقدار پایه در .env نیست، یک بار در ترمینال می‌پرسد و می‌نویسد."""
    from dotenv import load_dotenv

    env_path = ROOT / ".env"
    load_dotenv(env_path)
    placeholders = {"", "1234567", "your_api_hash_here", "123456:AAA..."}
    missing = [(k, q) for k, q in _ENV_PROMPTS if os.getenv(k, "").strip() in placeholders]
    if not missing:
        return
    if not sys.stdin.isatty():
        names = "، ".join(k for k, _ in missing)
        raise SystemExit(f"در .env این‌ها پر نشده: {names}\n"
                         f"یک بار دستی اجرا کن:  python run.py")

    print("\nفقط یک بار: سه مقدار لازم است، بقیه‌ی کارها از داخل تلگرام انجام می‌شود.\n")
    values: dict[str, str] = {}
    for key, question in missing:
        while True:
            value = input(f"{question}:\n> ").strip()
            if key == "TG_API_ID" and not value.isdigit():
                print("api_id فقط عدد است.")
                continue
            if value:
                break
        values[key] = value
        os.environ[key] = value

    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    out = []
    for line in lines:
        key = line.split("=", 1)[0].strip() if "=" in line else ""
        out.append(f"{key}={values.pop(key)}" if key in values else line)
    out.extend(f"{k}={v}" for k, v in values.items())
    env_path.write_text("\n".join(out) + "\n", encoding="utf-8")
    try:
        os.chmod(env_path, 0o600)
    except OSError:
        pass
    print("✅ در .env ذخیره شد.\n")


def main() -> None:
    from .bot import setup_logging

    ensure_env()
    settings = load_settings(require=("telegram",), allow_example=True)
    setup_logging(settings)
    panel = Panel(settings)

    async def runner() -> None:
        try:
            await panel.run()
        finally:
            await panel.shutdown()

    try:
        asyncio.run(runner())
    except KeyboardInterrupt:
        log.info("خروج با درخواست کاربر")


if __name__ == "__main__":
    main()
