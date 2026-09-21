"""گرفتن توکن cTrader و پیدا کردن شناسه‌ی حساب معاملاتی.

یک بار اجرا می‌شود و نتیجه را خودش در .env می‌نویسد:

    python tools/ctrader_auth.py

پیش‌نیاز: در https://connect.spotware.com یک اپ ساخته باشی و
CTRADER_CLIENT_ID و CTRADER_CLIENT_SECRET را در .env گذاشته باشی.
هنگام ساخت اپ، Redirect URI را دقیقاً  http://localhost/  بگذار.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ctrader_open_api import Auth

from signalbot.config import ROOT, load_settings

REDIRECT = "http://localhost/"


def write_env(values: dict[str, str]) -> None:
    """کلیدها را در .env به‌روز می‌کند بدون دست زدن به بقیه."""
    path = ROOT / ".env"
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    remaining = dict(values)

    out = []
    for line in lines:
        key = line.split("=", 1)[0].strip() if "=" in line else ""
        if key in remaining:
            out.append(f"{key}={remaining.pop(key)}")
        else:
            out.append(line)
    for key, value in remaining.items():
        out.append(f"{key}={value}")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


def main() -> None:
    settings = load_settings(require=())
    client_id = settings.ctrader.client_id
    client_secret = settings.ctrader.client_secret

    if not client_id or not client_secret:
        print("اول در https://connect.spotware.com یک اپ بساز.")
        print("بعد CTRADER_CLIENT_ID و CTRADER_CLIENT_SECRET را در .env بگذار.")
        print(f"\nهنگام ساخت اپ، Redirect URI را دقیقاً این بگذار:  {REDIRECT}")
        return

    auth = Auth(client_id, client_secret, REDIRECT)

    print("=" * 66)
    print("۱) این لینک را در مرورگر باز کن و حسابت را تایید کن:\n")
    print(auth.getAuthUri(scope="trading"))
    print()
    print("۲) بعد از تایید، مرورگر به آدرسی مثل این می‌رود:")
    print("      http://localhost/?code=ABC123...")
    print("   صفحه خطا می‌دهد — اشکالی ندارد. فقط مقدار بعد از code= را بردار.")
    print("=" * 66)

    code = input("\nکد را اینجا بچسبان: ").strip()
    if not code:
        print("کدی وارد نشد.")
        return
    if "code=" in code:  # اگر کل آدرس را چسبانده باشد
        code = code.split("code=", 1)[1].split("&")[0].strip()

    print("\nدر حال گرفتن توکن...")
    token = auth.getToken(code)
    if "accessToken" not in token:
        print(f"❌ گرفتن توکن ناموفق بود: {token}")
        return

    access = token["accessToken"]
    refresh = token.get("refreshToken", "")
    print("✅ توکن گرفته شد")

    print("\nدر حال خواندن حساب‌های معاملاتی...")
    accounts = list_accounts(access)
    if not accounts:
        print("❌ هیچ حساب معاملاتی پیدا نشد.")
        print("   در پنل LiteFinance یک حساب cTrader بساز و دوباره تلاش کن.")
        return

    print(f"\n{'#':>3}  {'شناسه':>12}  {'لاگین':>10}  نوع")
    print("-" * 48)
    for index, account in enumerate(accounts, 1):
        kind = "واقعی ⚠️" if account.isLive else "دمو"
        print(f"{index:>3}  {account.ctidTraderAccountId:>12}  "
              f"{account.traderLogin:>10}  {kind}")

    choice = input("\nکدام حساب؟ (شماره‌ی سمت چپ) ").strip()
    try:
        chosen = accounts[int(choice) - 1]
    except (ValueError, IndexError):
        print("انتخاب نامعتبر.")
        return

    write_env({
        "CTRADER_ACCESS_TOKEN": access,
        "CTRADER_REFRESH_TOKEN": refresh,
        "CTRADER_ACCOUNT_ID": str(chosen.ctidTraderAccountId),
    })

    print(f"\n✅ در .env ذخیره شد (حساب {chosen.traderLogin})")
    if chosen.isLive:
        print("\n⚠️  این حساب واقعی است. در config.yaml باید بگذاری:  ctrader.live: true")
        print("    ولی پیشنهاد جدی: اول با حساب دمو کار کن.")
    else:
        print("\nحساب دمو است — در config.yaml مقدار ctrader.live باید false بماند.")
    print("\nحالا تست کن:  python tools/check_broker.py")


def list_accounts(access_token: str):
    """حساب‌های معاملاتی متصل به این توکن را برمی‌گرداند."""
    import threading

    from ctrader_open_api import Client, EndPoints, Protobuf, TcpProtocol
    from ctrader_open_api.messages import OpenApiMessages_pb2 as M
    from twisted.internet import reactor

    result: list = []
    done = threading.Event()

    client = Client(EndPoints.PROTOBUF_DEMO_HOST, EndPoints.PROTOBUF_PORT, TcpProtocol)

    def on_connected(_c):
        req = M.ProtoOAGetAccountListByAccessTokenReq()
        req.accessToken = access_token
        deferred = client.send(req, responseTimeoutInSeconds=20)
        deferred.addCallback(handle)
        deferred.addErrback(fail)

    def handle(response):
        message = Protobuf.extract(response)
        result.extend(getattr(message, "ctidTraderAccount", []))
        done.set()

    def fail(failure):
        print(f"❌ خطا: {failure}")
        done.set()

    client.setConnectedCallback(on_connected)
    reactor.callWhenRunning(client.startService)
    threading.Thread(target=reactor.run,
                     kwargs={"installSignalHandlers": False}, daemon=True).start()
    done.wait(timeout=45)
    return result


if __name__ == "__main__":
    main()
