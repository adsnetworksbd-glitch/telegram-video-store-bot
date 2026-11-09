import hashlib
import hmac
import os
import json
import time
from urllib.parse import unquote

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")

def verify_telegram_init_data(init_data: str) -> dict:
    """Verify Telegram WebApp initData authenticity"""
    if not BOT_TOKEN:
        raise ValueError("TELEGRAM_BOT_TOKEN not set")

    parsed = {}
    for part in init_data.split("&"):
        if "=" in part:
            k, v = part.split("=", 1)
            parsed[k] = unquote(v)

    received_hash = parsed.pop("hash", None)
    if not received_hash:
        raise ValueError("Missing hash in initData")

    # Check auth_date freshness (max 1 hour)
    auth_date = int(parsed.get("auth_date", 0))
    if time.time() - auth_date > 3600:
        raise ValueError("initData expired")

    # Build data-check-string
    check_string = "\n".join(
        f"{k}={v}" for k, v in sorted(parsed.items())
    )

    secret_key = hmac.new(
        b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256
    ).digest()

    computed = hmac.new(
        secret_key, check_string.encode(), hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(computed, received_hash):
        raise ValueError("Invalid Telegram signature")

    user_data = parsed.get("user", "{}")
    return json.loads(user_data)
