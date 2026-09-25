"""تست پنل تلگرامی با کلاینت‌های ساختگی (بدون اتصال به تلگرام یا بروکر).

اجرا:  python tests/test_panel.py
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import os

os.environ.setdefault("TG_API_ID", "111")
os.environ.setdefault("TG_API_HASH", "hash")
os.environ.setdefault("TG_BOT_TOKEN", "1:tok")

from telethon.errors import SessionPasswordNeededError

from signalbot import panel as panel_mod
from signalbot.config import load_settings
from signalbot.panel import Panel, PanelState


class FakeUser:
    def __init__(self):
        self.authorized = False
        self.signed = []
        self.need_password = False

    def is_connected(self):
        return True

    async def connect(self):
        pass

    async def is_user_authorized(self):
        return self.authorized

    async def get_me(self):
        return SimpleNamespace(first_name="Ali", username="ali", id=5)

    async def send_code_request(self, phone):
        self.phone = phone
        return SimpleNamespace(phone_code_hash="HASH")

    async def sign_in(self, phone=None, code=None, phone_code_hash=None, password=None):
        self.signed.append((phone, code, phone_code_hash, password))
        if self.need_password and password is None:
            raise SessionPasswordNeededError(request=None)
        self.authorized = True

    async def get_dialogs(self, limit=0):
        return [
            SimpleNamespace(id=-1001, name="Signals VIP", is_channel=True, is_group=False),
            SimpleNamespace(id=42, name="Friend", is_channel=False, is_group=False),
        ]


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat, text, buttons=None, link_preview=False):
        self.sent.append((chat, text, buttons))


class FakeEvent:
    """هم پیام و هم کلیک دکمه را شبیه‌سازی می‌کند."""

    def __init__(self, sender, text="", contact=None):
        self.sender_id = sender
        self.raw_text = text
        self.message = SimpleNamespace(contact=contact)
        self.replies, self.edits, self.answers = [], [], []
        self.deleted = False

    async def reply(self, text, **_kw):
        self.replies.append(text)

    async def edit(self, text, buttons=None):
        self.edits.append(text)

    async def answer(self, text=None, alert=False):
        self.answers.append(text)

    async def delete(self):
        self.deleted = True


def make_panel(tmp: Path) -> Panel:
    panel_mod.PANEL_FILE = tmp / "panel.json"
    settings = load_settings(require=("telegram",), allow_example=True)
    settings.telegram.control_chat_id = 0
    settings.vision.api_key = ""
    p = Panel.__new__(Panel)
    p.base = settings
    p.state = PanelState(tmp / "panel.json")
    p.bot, p.user = FakeBot(), FakeUser()
    p.engine, p.waiting, p.tmp, p.tg_name = None, None, {}, None
    return p


def texts(p: Panel) -> str:
    return "\n".join(t for _, t, _ in p.bot.sent)


async def scenario(tmp: Path) -> None:
    p = make_panel(tmp)

    # ۱) اولین /start مالک را ثبت می‌کند و مستقیم شماره را می‌خواهد
    ev = FakeEvent(777, "/start")
    await p.on_message(ev)
    assert p.owner == 777 and p.state.get("owner_id") == 777
    assert p.waiting == "phone"

    # غریبه‌ها جواب نمی‌گیرند
    stranger = FakeEvent(999, "/start")
    await p.on_message(stranger)
    assert stranger.replies == ["این ربات خصوصی است."]

    # ۲) شماره‌ی اشتراکی دیگران رد می‌شود، شماره‌ی خودش قبول
    await p.on_message(FakeEvent(777, contact=SimpleNamespace(user_id=1, phone_number="98912")))
    assert p.waiting == "phone"
    await p.on_message(FakeEvent(777, contact=SimpleNamespace(user_id=777,
                                                               phone_number="989121234567")))
    assert p.user.phone == "+989121234567" and p.tmp["phone_code_hash"] == "HASH"

    # ۳) کد با کیپد: تایپ، پاک کردن، تایید
    p.user.need_password = True
    click = FakeEvent(777)
    for key in "123459":
        await p.on_panel_click(click, f"k:{key}")
    await p.on_panel_click(click, "k:del")
    assert p.tmp["code"] == "12345"
    await p.on_panel_click(click, "k:ok")
    assert p.user.signed[-1][:3] == ("+989121234567", "12345", "HASH")
    assert p.waiting == "password"

    # ۴) رمز دو مرحله‌ای: پیام پاک می‌شود و ورود کامل می‌شود
    pw = FakeEvent(777, "secret-pass")
    await p.on_message(pw)
    assert pw.deleted and p.user.authorized and p.waiting is None
    assert "کانال سیگنال" in texts(p)  # مرحله‌ی بعد خودکار باز شد

    # ۵) فقط کانال‌ها در لیست‌اند؛ انتخاب ذخیره می‌شود
    assert p.tmp["channels"] == [(-1001, "Signals VIP")]
    await p.on_panel_click(FakeEvent(777), "c:0")
    assert p.state.get("source_channel") == -1001
    assert p.waiting == "gemini"

    # ۶) کلید Gemini — تست شبکه را جایگزین می‌کنیم
    from signalbot import vision

    class OkReader:
        def __init__(self, **_kw):
            self.client = SimpleNamespace(models=SimpleNamespace(list=lambda: iter([1])))

    original = vision.VisionReader
    vision.VisionReader = OkReader
    try:
        key_ev = FakeEvent(777, "AIzaTESTKEY")
        await p.on_message(key_ev)
    finally:
        vision.VisionReader = original
    assert key_ev.deleted and p.state.get("gemini_key") == "AIzaTESTKEY"
    assert "حساب معاملاتی" in p.bot.sent[-1][1]

    # ۷) روشن کردن بدون بروکر: خطای روشن
    error = await p.start_engine()
    assert error and "حساب معاملاتی" in error

    # ۸) بعد از ثبت بروکر، تنظیمات نهایی درست ساخته می‌شود
    p.state.set(broker="ctrader", ctrader={
        "client_id": "cid", "client_secret": "sec", "access_token": "tok",
        "refresh_token": "ref", "account_id": 123, "login": 9001, "live": False,
        "token_time": int(__import__("time").time()),
    })
    assert await p._missing() == []
    risk_click = FakeEvent(777)
    await p.on_panel_click(risk_click, "risk:2.0")
    await p.on_panel_click(risk_click, "mode:confirm")
    s = p.build_settings()
    assert s.broker == "ctrader" and s.ctrader.account_id == 123 and not s.ctrader.live
    assert s.telegram.source_channel == -1001 and s.telegram.control_chat_id == 777
    assert s.vision.api_key == "AIzaTESTKEY"
    assert s.trading.risk_percent == 2.0 and s.trading.mode == "confirm"
    assert p.base.telegram.source_channel != -1001  # پایه دست‌نخورده

    # ۹) ذخیره روی دیسک می‌ماند
    again = PanelState(tmp / "panel.json")
    assert again.get("source_channel") == -1001 and again.get("owner_id") == 777

    # ۱۰) متاتریدر روی غیرویندوز رد می‌شود
    if sys.platform != "win32":
        mt = FakeEvent(777)
        await p.on_panel_click(mt, "br:mt5")
        assert mt.answers and "ویندوز" in mt.answers[0]


    # ۱۱) روشن و خاموش کردن با بروکر ساختگی
    from signalbot import bot as bot_mod

    class FakeBroker:
        connected = False

        def connect(self):
            FakeBroker.connected = True

        def shutdown(self):
            FakeBroker.connected = False

        def account(self):
            return SimpleNamespace(balance=1000.0, equity=1000.0, currency="USD")

        positions = pending_orders = lambda self: []
        realized_pnl_today = lambda self: 0.0

    handlers = []
    p.user.get_entity = lambda src: _async(SimpleNamespace(title="Signals VIP"))
    p.user.add_event_handler = lambda fn, ev: handlers.append(ev)
    p.user.remove_event_handler = lambda fn, ev: handlers.remove(ev)
    original_make, original_root = bot_mod.make_broker, bot_mod.ROOT
    bot_mod.make_broker = lambda settings: FakeBroker()
    bot_mod.ROOT = tmp
    try:
        on = FakeEvent(777)
        await p.on_panel_click(on, "on")
        assert p.engine is not None and FakeBroker.connected and len(handlers) == 1
        assert p.state.get("running") is True
        assert "راه افتاد" in texts(p)
        assert "1,000.00" in await p.engine.status_text()

        # وقتی روشن است، تغییر کانال مجاز نیست
        busy = FakeEvent(777)
        await p.on_panel_click(busy, "ch")
        assert busy.answers and "خاموش" in busy.answers[0]

        await p.on_panel_click(FakeEvent(777), "off")
        assert p.engine is None and not FakeBroker.connected and handlers == []
        assert p.state.get("running") is False
    finally:
        bot_mod.make_broker, bot_mod.ROOT = original_make, original_root


async def _async(value):
    return value


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        asyncio.run(scenario(Path(tmp)))
    print("✅ همه‌ی بررسی‌های پنل پاس شد.")


if __name__ == "__main__":
    main()
