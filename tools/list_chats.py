"""لیست کانال‌ها و گروه‌هایت را با آیدی عددی چاپ می‌کند.

اولین بار که اجرا کنی، تلگرام شماره‌ات و کد ورود را می‌پرسد و یک session
می‌سازد تا دفعات بعد لازم نباشد.

    python tools/list_chats.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from telethon import TelegramClient

from signalbot.config import ROOT, load_settings


async def main() -> None:
    settings = load_settings(require=("telegram",))
    session_dir = ROOT / "sessions"
    session_dir.mkdir(exist_ok=True)
    client = TelegramClient(
        str(session_dir / "user"), settings.telegram.api_id, settings.telegram.api_hash
    )
    await client.start()
    me = await client.get_me()
    print(f"وارد شدی به عنوان: {me.first_name} (@{me.username})\n")
    print(f"{'آیدی':>16}  نوع      عنوان")
    print("-" * 70)
    async for dialog in client.iter_dialogs():
        if dialog.is_user:
            continue
        kind = "کانال" if dialog.is_channel and not dialog.is_group else "گروه "
        print(f"{dialog.id:>16}  {kind}  {dialog.name}")
    print("\nآیدی کانال سیگنال را در config.yaml -> telegram.source_channel بگذار.")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
