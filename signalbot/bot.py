"""حلقه‌ی اصلی: گوش دادن به کانال سیگنال + پنل کنترل تلگرام."""
from __future__ import annotations

import asyncio
import logging
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from telethon import Button, TelegramClient, events

from .config import ROOT, Settings, load_settings
from .broker import BrokerError, make_broker
from .executor import Executor, Plan
from .models import ParseResult, Signal, SignalUpdate
from .parser import SignalParser
from .store import Store
from .vision import VisionError, VisionExtraction, VisionReader

log = logging.getLogger("signalbot")

APPROVAL_TTL = timedelta(minutes=10)
RECONCILE_SECONDS = 30
IMAGE_DIR = ROOT / "images"


@dataclass
class PendingApproval:
    signal_id: int
    signal: Signal
    created_at: datetime


@dataclass
class PendingUpdate:
    """دستور مدیریتی که معلوم نیست به کدام سیگنال مربوط است."""

    update: SignalUpdate
    candidates: list[int] = field(default_factory=list)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class SignalBot:
    def __init__(self, settings: Settings,
                 user: Optional[TelegramClient] = None,
                 bot: Optional[TelegramClient] = None) -> None:
        self.settings = settings
        self.store = Store(ROOT / "state.db")
        self.mt5 = make_broker(settings)
        self.executor = Executor(self.mt5, self.store, settings.trading)
        self.parser = SignalParser(settings.symbols.aliases, settings.symbols.whitelist)
        self.paused = False
        self._approvals: dict[int, PendingApproval] = {}
        self._pending_updates: dict[int, PendingUpdate] = {}
        self._update_counter = 0
        self._mt5_failures = 0
        self._channel_handler = None
        self._reconcile_task: Optional[asyncio.Task] = None

        self.vision: Optional[VisionReader] = None
        if settings.vision.enabled:
            self.vision = VisionReader(
                api_key=settings.vision.api_key,
                model=settings.vision.model,
                aliases=settings.symbols.aliases,
                whitelist=settings.symbols.whitelist,
                min_confidence=settings.vision.min_confidence,
                proxy=settings.vision.proxy,
            )

        session_dir = ROOT / "sessions"
        session_dir.mkdir(exist_ok=True)
        # پنل تلگرامی کلاینت‌ها را خودش می‌سازد و به ربات می‌دهد
        self.user = user or TelegramClient(
            str(session_dir / "user"), settings.telegram.api_id, settings.telegram.api_hash
        )
        self.bot: Optional[TelegramClient] = bot
        if bot is None and settings.telegram.bot_token:
            self.bot = TelegramClient(
                str(session_dir / "control_bot"),
                settings.telegram.api_id,
                settings.telegram.api_hash,
            )

    # ------------------------------------------------------------- messaging

    async def notify(self, text: str, buttons=None, image: Optional[Path] = None) -> None:
        chat_id = self.settings.telegram.control_chat_id
        if not chat_id:
            log.info("[اعلان] %s", text)
            return
        client = self.bot or self.user
        try:
            if image is not None and image.exists():
                await client.send_file(chat_id, str(image), caption=text[:1024],
                                       buttons=buttons)
            else:
                await client.send_message(chat_id, text, buttons=buttons,
                                          link_preview=False)
        except Exception as exc:  # noqa: BLE001 - اعلان نباید ربات را زمین بزند
            log.error("ارسال اعلان ناموفق بود: %s", exc)

    # ----------------------------------------------------------------- image

    @staticmethod
    def _image_mime(message) -> Optional[str]:
        if message.photo:
            return "image/jpeg"
        document = getattr(message, "document", None)
        mime = getattr(document, "mime_type", "") or ""
        return mime if mime.startswith("image/") else None

    async def _download_image(self, message) -> tuple[bytes, Path]:
        data = await message.download_media(file=bytes)
        if self.settings.vision.save_images:
            IMAGE_DIR.mkdir(exist_ok=True)
            path = IMAGE_DIR / f"{message.id}.jpg"
        else:
            path = Path(tempfile.gettempdir()) / f"signal_{message.id}.jpg"
        path.write_bytes(data)
        return data, path

    async def _read_image(
        self, message, caption: str
    ) -> tuple[ParseResult, Optional[VisionExtraction], Optional[Path]]:
        data, path = await self._download_image(message)

        if self.vision is None:
            await self.notify(
                "🖼 یک عکس در کانال آمد ولی خواندن عکس خاموش است — خودت بررسی کن."
                + (f"\n\nمتن همراه عکس:\n{caption.strip()[:300]}" if caption.strip() else ""),
                image=path,
            )
            return ParseResult(), None, path

        mime = self._image_mime(message) or "image/jpeg"
        try:
            extraction = await asyncio.to_thread(
                self.vision.extract, data, mime, caption
            )
        except VisionError as exc:
            log.error("خواندن عکس ناموفق: %s", exc)
            await self.notify(f"❌ خواندن عکس ناموفق بود: {exc}", image=path)
            return ParseResult(), None, path

        log.info("عکس خوانده شد: %s (اطمینان %.2f)", extraction.kind, extraction.confidence)
        return self.vision.to_result(extraction), extraction, path

    # ---------------------------------------------------------- signal flow

    async def on_channel_message(self, event) -> None:
        message = event.message
        caption = message.message or ""
        chat_id = int(event.chat_id)

        if self.store.seen(chat_id, message.id):
            return

        is_reply = message.reply_to_msg_id is not None
        extraction: Optional[VisionExtraction] = None
        image_path: Optional[Path] = None

        if self._image_mime(message) is not None:
            result, extraction, image_path = await self._read_image(message, caption)
        elif caption.strip():
            result = self.parser.parse(caption, is_reply=is_reply)
        else:
            return

        if result.update is not None:
            await self._handle_update(chat_id, message, result.update, extraction, image_path)
            return

        if result.signal is None:
            if result.rejected_reason:
                log.info("پیام رد شد: %s", result.rejected_reason)
                await self.notify(
                    f"⚠️ پیامی آمد ولی اجرا نشد — {result.rejected_reason}",
                    image=image_path,
                )
            elif extraction is not None and extraction.kind == "info":
                # «TP1 خورد» و گزارش‌ها: فقط اطلاع، بدون اقدام
                note = extraction.notes or "گزارش/اطلاعیه"
                await self.notify(f"ℹ️ {note}", image=image_path)
            return

        signal = result.signal
        signal.chat_id, signal.msg_id = chat_id, message.id
        signal.received_at = message.date.astimezone(timezone.utc)

        if result.rejected_reason:
            self.store.add_signal(signal, status="rejected")
            await self.notify(
                f"⚠️ سیگنال رد شد — {result.rejected_reason}\n\n{signal.summary()}",
                image=image_path,
            )
            return

        if self.paused:
            self.store.add_signal(signal, status="paused")
            await self.notify(f"⏸ ربات متوقف است، سیگنال اجرا نشد:\n\n{signal.summary()}",
                              image=image_path)
            return

        signal_id = self.store.add_signal(signal)

        age_problem = self.executor.check_age(signal)
        if age_problem:
            self.store.set_signal_status(signal_id, "stale")
            await self.notify(f"⏱ {age_problem}\n\n{signal.summary()}", image=image_path)
            return

        try:
            plan = await asyncio.to_thread(self.executor.build_plan, signal)
        except BrokerError as exc:
            self.store.set_signal_status(signal_id, "failed")
            await self.notify(f"❌ آماده‌سازی سیگنال ناموفق بود: {exc}\n\n{signal.summary()}",
                              image=image_path)
            return

        guard = await asyncio.to_thread(self.executor.guard, len(plan.legs))
        if guard:
            self.store.set_signal_status(signal_id, "blocked")
            await self.notify(f"🛑 {guard}\n\n{signal.summary()}", image=image_path)
            return

        if self.settings.trading.mode == "auto":
            await self._execute(plan, signal_id, image=image_path)
            return

        self._approvals[signal_id] = PendingApproval(
            signal_id=signal_id, signal=signal, created_at=datetime.now(timezone.utc)
        )
        read_note = f"\n\n{VisionReader.describe(extraction)}" if extraction else ""
        await self.notify(
            f"📩 سیگنال جدید — تایید می‌کنی؟\n\n{plan.describe()}{read_note}",
            buttons=[[
                Button.inline("✅ اجرا کن", f"go:{signal_id}".encode()),
                Button.inline("❌ رد کن", f"no:{signal_id}".encode()),
            ]],
            image=image_path,
        )

    async def _execute(self, plan: Plan, signal_id: int,
                       image: Optional[Path] = None) -> None:
        done, errors = await asyncio.to_thread(self.executor.execute, plan, signal_id)
        lines = []
        if done:
            tickets = "، ".join(f"#{e.ticket} ({e.volume:g})" for e in done)
            lines.append(f"✅ اجرا شد: {tickets}")
            lines.append(plan.describe())
        if errors:
            lines.append("❌ خطاها:\n" + "\n".join(errors))
        # در حالت خودکار مرحله‌ی تایید نیست، پس عکس اصلی را هم می‌فرستیم تا
        # بتوانی بعداً چک کنی مدل درست خوانده است
        await self.notify("\n\n".join(lines) or "هیچ اردری ثبت نشد", image=image)

    # -------------------------------------------------- linking an update

    async def _handle_update(
        self,
        chat_id: int,
        message,
        update: SignalUpdate,
        extraction: Optional[VisionExtraction],
        image_path: Optional[Path],
    ) -> None:
        # ۱) بهترین حالت: دستور ریپلای به خود سیگنال است
        if message.reply_to_msg_id is not None:
            row = self.store.signal_by_msg(chat_id, message.reply_to_msg_id)
            if row is not None:
                await self._apply_update(int(row["id"]), update, image_path)
                return

        # ۲) وگرنه از روی نماد و قیمت، پوزیشن متناظر را پیدا کن
        symbol = None
        if extraction is not None:
            if self.vision is not None:
                symbol = self.vision.resolve_symbol(
                    extraction.refers_to_symbol or extraction.symbol
                )

        candidates = self.store.active_signals(symbol)
        if not candidates:
            await self.notify(
                f"🔧 دستور «{update.kind.value}» آمد ولی هیچ پوزیشن فعالی برایش پیدا نشد.",
                image=image_path,
            )
            return

        if len(candidates) == 1:
            await self._apply_update(int(candidates[0]["id"]), update, image_path)
            return

        # ۳) چند کاندید: اگر قیمت ورود در عکس بود، نزدیک‌ترین را بردار
        ref_entry = extraction.refers_to_entry if extraction else None
        if ref_entry is not None:
            scored = [
                (abs((row["entry"] or 0) - ref_entry), row)
                for row in candidates
                if row["entry"] is not None
            ]
            if scored:
                scored.sort(key=lambda item: item[0])
                await self._apply_update(int(scored[0][1]["id"]), update, image_path)
                return

        # ۴) مبهم است — تصمیم با خودت
        self._update_counter += 1
        token = self._update_counter
        self._pending_updates[token] = PendingUpdate(
            update=update, candidates=[int(row["id"]) for row in candidates]
        )
        buttons = [
            [Button.inline(
                f"{row['side']} {row['symbol']} @ {row['entry']:g}" if row["entry"]
                else f"{row['side']} {row['symbol']} (بازار)",
                f"upd:{token}:{row['id']}".encode(),
            )]
            for row in candidates[:5]
        ]
        buttons.append([Button.inline("❌ هیچ‌کدام", f"upd:{token}:0".encode())])
        await self.notify(
            f"🔧 دستور «{update.kind.value}» آمد ولی معلوم نیست به کدام پوزیشن مربوط است.\n"
            f"کدام را اعمال کنم؟",
            buttons=buttons,
            image=image_path,
        )

    async def _apply_update(
        self, signal_id: int, update: SignalUpdate, image_path: Optional[Path] = None
    ) -> None:
        report = await asyncio.to_thread(self.executor.apply_update, signal_id, update)
        await self.notify(f"🔧 دستور مدیریتی: {update.kind.value}\n{report}", image=image_path)

    # ------------------------------------------------------- control panel

    def _register_control_handlers(self) -> None:
        if self.bot is None:
            return
        owner = self.settings.telegram.control_chat_id

        @self.bot.on(events.CallbackQuery)
        async def on_click(event):  # noqa: ANN001
            if owner and event.sender_id != owner:
                await event.answer("اجازه‌ی دسترسی نداری", alert=True)
                return
            await self.handle_callback(event)

        @self.bot.on(events.NewMessage(pattern=r"^/(start|id)"))
        async def on_id(event):  # noqa: ANN001
            await event.reply(
                f"آیدی عددی این چت: `{event.chat_id}`\n"
                f"همین را در config.yaml در بخش telegram.control_chat_id بگذار.",
                parse_mode="md",
            )

        @self.bot.on(events.NewMessage(pattern=r"^/status"))
        async def on_status(event):  # noqa: ANN001
            if owner and event.sender_id != owner:
                return
            await event.reply(await self.status_text())

        @self.bot.on(events.NewMessage(pattern=r"^/(pause|resume)"))
        async def on_toggle(event):  # noqa: ANN001
            if owner and event.sender_id != owner:
                return
            self.paused = event.raw_text.startswith("/pause")
            await event.reply("⏸ متوقف شد — سیگنال‌ها فقط گزارش می‌شوند"
                              if self.paused else "▶️ دوباره فعال شد")

        @self.bot.on(events.NewMessage(pattern=r"^/closeall"))
        async def on_close_all(event):  # noqa: ANN001
            if owner and event.sender_id != owner:
                return
            await event.reply(await self.close_all())

    async def handle_callback(self, event) -> None:  # noqa: ANN001
        """دکمه‌های تایید سیگنال و انتخاب پوزیشن (بدون بررسی مالک)."""
        data = event.data.decode()

        if data.startswith("upd:"):
            _, raw_token, raw_signal = data.split(":")
            pending = self._pending_updates.pop(int(raw_token), None)
            if pending is None:
                await event.answer("این دستور منقضی شده", alert=True)
                return
            if raw_signal == "0":
                await event.edit("❌ اعمال نشد.")
                return
            await event.answer("در حال اعمال…")
            await self._apply_update(int(raw_signal), pending.update)
            return

        action, _, raw_id = data.partition(":")
        signal_id = int(raw_id)
        approval = self._approvals.pop(signal_id, None)
        if approval is None:
            await event.answer("این سیگنال منقضی شده یا قبلاً پاسخ داده شده", alert=True)
            return

        if action == "no":
            self.store.set_signal_status(signal_id, "declined")
            await event.edit(f"❌ رد شد.\n\n{approval.signal.summary()}")
            return

        await event.answer("در حال اجرا…")
        try:
            # نقشه را دوباره می‌سازیم تا با قیمت لحظه‌ی تایید بخواند
            plan = await asyncio.to_thread(self.executor.build_plan, approval.signal)
        except BrokerError as exc:
            self.store.set_signal_status(signal_id, "failed")
            await event.edit(f"❌ در لحظه‌ی تایید اجرا ممکن نبود: {exc}")
            return
        await event.edit(f"⏳ در حال ارسال…\n\n{plan.describe()}")
        await self._execute(plan, signal_id)

    async def status_text(self) -> str:
        info = await asyncio.to_thread(self.mt5.account)
        positions = await asyncio.to_thread(self.mt5.positions)
        pendings = await asyncio.to_thread(self.mt5.pending_orders)
        pnl = await asyncio.to_thread(self.mt5.realized_pnl_today)
        return (
            f"{'⏸ متوقف' if self.paused else '▶️ فعال'} | حالت: {self.settings.trading.mode}\n"
            f"بالانس: {info.balance:,.2f} {info.currency}\n"
            f"اکوییتی: {info.equity:,.2f}\n"
            f"سود/زیان امروز: {pnl:,.2f}\n"
            f"پوزیشن باز: {len(positions)} | پندینگ: {len(pendings)}"
        )

    async def close_all(self) -> str:
        positions = await asyncio.to_thread(self.mt5.positions)
        if not positions:
            return "پوزیشن بازی وجود ندارد"
        results = []
        for position in positions:
            try:
                await asyncio.to_thread(self.mt5.close, position.ticket, 1.0)
                self.store.set_trade_status(int(position.ticket), "closed")
                results.append(f"✅ #{position.ticket}")
            except BrokerError as exc:
                results.append(f"❌ #{position.ticket}: {exc}")
        return "\n".join(results)

    # ------------------------------------------------------------ background

    async def _reconcile_loop(self) -> None:
        while True:
            await asyncio.sleep(RECONCILE_SECONDS)
            try:
                changes = await asyncio.to_thread(self.executor.reconcile)
                for change in changes:
                    log.info("هماهنگ‌سازی: %s", change)
                    if "اتصال متاتریدر" in change:
                        await self.notify(f"🔌 {change}")
                self._mt5_failures = 0
            except Exception as exc:  # noqa: BLE001
                log.error("خطا در هماهنگ‌سازی: %s", exc)
                self._mt5_failures += 1
                # فقط یک بار هشدار بده، نه هر ۳۰ ثانیه
                if self._mt5_failures == 3:
                    await self.notify(
                        f"⚠️ متاتریدر پاسخ نمی‌دهد: {exc}\n"
                        f"ترمینال را روی سرور چک کن — تا وصل نشود سیگنالی اجرا نمی‌شود."
                    )

            now = datetime.now(timezone.utc)
            for signal_id, approval in list(self._approvals.items()):
                if now - approval.created_at > APPROVAL_TTL:
                    self._approvals.pop(signal_id, None)
                    self.store.set_signal_status(signal_id, "expired")
                    log.info("تایید سیگنال %s منقضی شد", signal_id)
            for token, pending in list(self._pending_updates.items()):
                if now - pending.created_at > APPROVAL_TTL:
                    self._pending_updates.pop(token, None)

    # ----------------------------------------------------------------- start

    async def run(self) -> None:
        if self.bot is not None:
            await self.bot.start(bot_token=self.settings.telegram.bot_token)
            self._register_control_handlers()
            log.info("پنل کنترل آماده است")

        await self.user.start()
        try:
            await self.start_engine()
        except BrokerError as exc:
            raise SystemExit(f"اتصال به بروکر ناموفق بود: {exc}") from exc
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        await self.user.run_until_disconnected()

    async def start_engine(self) -> str:
        """اتصال به بروکر و شروع گوش دادن به کانال. عنوان کانال را برمی‌گرداند.

        خطای بروکر به صورت BrokerError و کانالِ پیدانشده به صورت ValueError
        بالا می‌رود تا پنل بتواند آن را به کاربر نشان دهد.
        """
        await asyncio.to_thread(self.mt5.connect)

        source = self.settings.telegram.source_channel
        try:
            try:
                entity = await self.user.get_entity(source)
            except ValueError:
                # آیدی عددی فقط وقتی شناخته می‌شود که در حافظه‌ی نشست باشد؛
                # خواندن لیست چت‌ها آن را دوباره پر می‌کند
                await self.user.get_dialogs()
                entity = await self.user.get_entity(source)
        except Exception as exc:  # noqa: BLE001
            await asyncio.to_thread(self.mt5.shutdown)
            raise ValueError(
                f"کانال «{source}» پیدا نشد ({exc}). "
                f"با اجرای  python tools/list_chats.py  آیدی درست را پیدا کن."
            ) from exc

        self._channel_handler = events.NewMessage(chats=entity)
        self.user.add_event_handler(self.on_channel_message, self._channel_handler)
        title = getattr(entity, "title", str(source))
        log.info("در حال گوش دادن به: %s", title)
        vision_note = (
            f"خواندن عکس: {self.settings.vision.model}"
            if self.vision else "خواندن عکس: خاموش"
        )
        await self.notify(
            f"🚀 ربات راه افتاد\nکانال: {title}\n"
            f"حالت: {self.settings.trading.mode} | ریسک: {self.settings.trading.risk_percent:g}%\n"
            f"{vision_note}"
        )

        self._reconcile_task = asyncio.create_task(self._reconcile_loop())
        return title

    async def stop_engine(self) -> None:
        """گوش دادن به کانال را قطع می‌کند و اتصال بروکر را می‌بندد."""
        if self._channel_handler is not None:
            self.user.remove_event_handler(self.on_channel_message, self._channel_handler)
            self._channel_handler = None
        if self._reconcile_task is not None:
            self._reconcile_task.cancel()
            self._reconcile_task = None
        await asyncio.to_thread(self.cleanup)

    def cleanup(self) -> None:
        try:
            self.mt5.shutdown()
        finally:
            self.store.close()


def setup_logging(settings: Settings) -> None:
    log_path = ROOT / settings.log_file
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        handlers=[
            logging.FileHandler(log_path, encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )
    logging.getLogger("telethon").setLevel(logging.WARNING)
    logging.getLogger("google_genai").setLevel(logging.WARNING)


def main() -> None:
    settings = load_settings()
    setup_logging(settings)
    bot = SignalBot(settings)
    try:
        asyncio.run(bot.run())
    except KeyboardInterrupt:
        log.info("خروج با درخواست کاربر")
    finally:
        bot.cleanup()


if __name__ == "__main__":
    main()
