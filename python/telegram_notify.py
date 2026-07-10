"""Fire-and-forget Telegram notifications, one bot/chat configured per MT5
account (db.get_telegram_config). Never let a Telegram failure disturb the
actual trading flow - every call here is wrapped so it can only log, never
raise into its caller.
"""
import asyncio
import json
import logging
import urllib.error
import urllib.request

import db

logger = logging.getLogger("telegram_notify")

API_TIMEOUT_S = 10


def _send_sync(bot_token: str, chat_id: str, text: str) -> None:
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    body = json.dumps({"chat_id": chat_id, "text": text}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=API_TIMEOUT_S) as resp:
        if resp.status != 200:
            raise urllib.error.HTTPError(url, resp.status, "non-200 from Telegram", resp.headers, None)


async def notify(account_id: str, text: str) -> None:
    """Best-effort: silently does nothing if the account has no Telegram
    config yet, logs (but never raises) on delivery failure."""
    config = db.get_telegram_config(account_id)
    if config is None:
        return
    try:
        await asyncio.to_thread(_send_sync, config["bot_token"], config["chat_id"], text)
    except Exception:
        logger.exception("failed to send Telegram notification for account %s", account_id)


async def send_test(bot_token: str, chat_id: str) -> None:
    """Used by the "Test" button - raises on failure so the caller can
    report a clear error, unlike notify() which is always silent."""
    await asyncio.to_thread(_send_sync, bot_token, chat_id, "🔔 Test thông báo từ MT5 Dashboard")
