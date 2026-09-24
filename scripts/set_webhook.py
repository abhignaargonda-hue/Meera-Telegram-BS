"""Point Telegram at the Vercel deployment. Run once after the first deploy
(and again if the Vercel URL, bot token or webhook secret changes).

    python scripts/set_webhook.py https://your-project.vercel.app

Reads TELEGRAM_BOT_TOKEN and TELEGRAM_WEBHOOK_SECRET from the environment or from .env.
Uses only the standard library, so no install is needed.
"""

import json
import os
import sys
import urllib.request
from pathlib import Path


def load_env() -> None:
    env = Path(__file__).resolve().parent.parent / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip())


def telegram(token: str, method: str, **params) -> dict:
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=json.dumps(params).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req) as r:
        return json.load(r)


def main() -> None:
    if len(sys.argv) != 2 or not sys.argv[1].startswith("https://"):
        sys.exit("Usage: python scripts/set_webhook.py https://your-project.vercel.app")
    load_env()
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    secret = os.environ.get("TELEGRAM_WEBHOOK_SECRET")
    if not token or not secret:
        sys.exit("TELEGRAM_BOT_TOKEN and TELEGRAM_WEBHOOK_SECRET must be set (in .env or the environment).")

    url = sys.argv[1].rstrip("/") + "/api/telegram"
    result = telegram(
        token,
        "setWebhook",
        url=url,
        secret_token=secret,
        allowed_updates=["message"],
        max_connections=1,  # One note at a time; avoids overlapping drafts.
        drop_pending_updates=True,
    )
    print("setWebhook:", result.get("description", result))
    info = telegram(token, "getWebhookInfo")["result"]
    print("Webhook URL:", info.get("url"))
    if info.get("last_error_message"):
        print("Last delivery error:", info["last_error_message"])


if __name__ == "__main__":
    main()
