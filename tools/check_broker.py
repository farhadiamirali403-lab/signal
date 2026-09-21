"""تست اتصال به بروکر بدون باز کردن هیچ معامله‌ای.

با هر دو بروکر کار می‌کند — همان که در config.yaml انتخاب شده:

    python tools/check_broker.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from signalbot.broker import BrokerError, make_broker
from signalbot.config import load_settings


def main() -> None:
    settings = load_settings(require=("broker",))
    print(f"بروکر: {settings.broker}")
    if settings.broker == "ctrader":
        print(f"حساب: {'واقعی ⚠️' if settings.ctrader.live else 'دمو'}")

    client = make_broker(settings)
    try:
        client.connect()
    except BrokerError as exc:
        print(f"\n❌ {exc}\n")
        if settings.broker == "ctrader":
            print("چیزهایی که معمولاً باعث این خطا می‌شوند:")
            print("  • توکن منقضی شده — دوباره بزن: python tools/ctrader_auth.py")
            print("  • CTRADER_ACCOUNT_ID با حساب توکن نمی‌خواند")
            print("  • ctrader.live در config.yaml با نوع حساب نمی‌خواند")
            print("    (حساب دمو -> live: false ، حساب واقعی -> live: true)")
        else:
            print("  • ترمینال متاتریدر باز نیست یا وارد حساب نشده‌ای")
            print("  • Algo Trading خاموش است")
        return

    try:
        info = client.account()
        print("\n--- حساب ---")
        print(f"شماره: {info.login} @ {info.server}")
        print(f"بالانس: {info.balance:,.2f} {info.currency}")

        targets = settings.symbols.whitelist or ["XAUUSD"]
        for base in targets:
            print(f"\n--- {base} ---")
            try:
                symbol = client.resolve_symbol(base)
                spec = client.symbol_info(symbol)
                tick = client.tick(symbol)
                pip = client.pip_size(symbol)
                spread = (tick.ask - tick.bid) / pip
                print(f"نماد بروکر: {symbol} | رقم اعشار: {spec.digits} | پیپ: {pip:g}")
                print(f"قیمت: bid {tick.bid:g} / ask {tick.ask:g} | اسپرد: {spread:.1f} پیپ")
                print(f"حجم: حداقل {spec.volume_min:g} | گام {spec.volume_step:g} "
                      f"| حداکثر {spec.volume_max:g}")

                entry = tick.ask
                lot, note = client.calc_lot(symbol, entry, entry - 30 * pip)
                print(f"نمونه‌ی محاسبه‌ی لات (SL سی پیپ): {lot:g} لات")
                print(f"  {note}")

                parts = client.max_parts(symbol, lot)
                print(f"با این حجم، حداکثر {parts} تارگت پوشش داده می‌شود")
            except BrokerError as exc:
                print(f"❌ {exc}")

        positions = client.positions()
        pendings = client.pending_orders()
        print(f"\nپوزیشن باز ربات: {len(positions)} | اردر پندینگ: {len(pendings)}")
        print(f"سود/زیان امروز: {client.realized_pnl_today():,.2f}")
    finally:
        client.shutdown()

    print("\n✅ تست تمام شد — هیچ معامله‌ای باز نشد.")


if __name__ == "__main__":
    main()
