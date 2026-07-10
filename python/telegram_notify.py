"""Fire-and-forget Telegram notifications, one bot/chat configured per MetaTrader
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


def profit_icon(net: float) -> str:
    if net > 0:
        return "💰"
    if net < 0:
        return "📉"
    return "➖"


def format_profit_line(net: float) -> str:
    icon = profit_icon(net)
    if net > 0:
        return f"{icon} Lãi: +{net:.2f}"
    if net < 0:
        return f"{icon} Lỗ: {net:.2f}"
    return f"{icon} Hòa: 0.00"


def format_close_deal(deal: dict) -> str:
    net = deal["profit"] + deal.get("swap", 0) + deal.get("commission", 0)
    return "\n".join([
        f"🔴 Đóng lệnh #{deal['ticket']}",
        f"{deal['side']} {deal['symbol']} · {deal['volume']} lot",
        f"Giá đóng: {deal['price_close']}",
        format_profit_line(net),
    ])


def format_new_position(ticket: int, position: dict) -> str:
    return "\n".join([
        f"🆕 Lệnh mới #{ticket}",
        f"{position['side']} {position['symbol']} · {position['volume']} lot",
        f"Giá vào: {position['price_open']}",
    ])


def format_modify_position(ticket: int, symbol: str, old: dict, new: dict) -> str:
    return "\n".join([
        f"✏️ Sửa lệnh #{ticket} {symbol}",
        f"SL: {old['sl']} → {new['sl']}",
        f"TP: {old['tp']} → {new['tp']}",
    ])


def format_drawdown_alert(account_id: str, pct: float, tier: int) -> str:
    return "\n".join([
        "⚠️ Cảnh báo Drawdown",
        f"Tài khoản: #{account_id}",
        f"Drawdown: {pct:.1f}% (vượt ngưỡng {tier}%)",
    ])


def format_account_connected(account_id: str, broker: str) -> str:
    broker_line = f"({broker})" if broker else ""
    return "\n".join([
        "🟢 Tài khoản đã kết nối",
        f"#{account_id} {broker_line}".strip(),
    ])


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


async def send_reply(bot_token: str, chat_id: str, text: str) -> None:
    """Send a message to a chat; raises on failure (for command replies)."""
    await asyncio.to_thread(_send_sync, bot_token, chat_id, text)


async def send_test(bot_token: str, chat_id: str) -> None:
    """Used by the "Test" button - raises on failure so the caller can
    report a clear error, unlike notify() which is always silent."""
    text = "\n".join([
        "🔔 Test thông báo",
        "MetaTrader Dashboard",
        "Kết nối Telegram thành công ✅",
        "",
        "Lệnh điều khiển:",
        "BUY — vào lệnh mua",
        "SELL — vào lệnh bán",
        "HOLD — đóng hết lệnh",
    ])
    await asyncio.to_thread(_send_sync, bot_token, chat_id, text)
