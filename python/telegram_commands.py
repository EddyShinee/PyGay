"""Poll Telegram for BUY / SELL / HOLD commands and execute on the linked account."""
from __future__ import annotations

import asyncio
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

import db
import telegram_notify
from session_manager import SessionManager

logger = logging.getLogger("telegram_commands")

POLL_TIMEOUT_S = 25
_offsets: dict[str, int] = {}


def parse_command(text: str) -> Optional[str]:
    if not text:
        return None
    cmd = text.strip().upper()
    if cmd.startswith("/"):
        cmd = cmd[1:]
    if cmd in ("BUY", "SELL", "HOLD"):
        return cmd
    return None


def _accounts_for_message(configs: list[dict], bot_token: str, chat_id: str) -> list[dict]:
    return [
        c for c in configs
        if c["bot_token"] == bot_token and c["chat_id"] == str(chat_id)
    ]


def _resolve_symbol(session, configured: Optional[str]) -> Optional[str]:
    if configured:
        return configured
    symbols = session.symbol_store.snapshot()
    return symbols[0] if symbols else None


async def _execute_buy_sell(session, side: str, symbol: str, volume: float) -> dict:
    if not session.connected:
        return {"ok": False, "error": "EA chưa kết nối"}
    if volume <= 0:
        return {"ok": False, "error": "Lot phải > 0"}
    if session.price_cache.get(symbol) is None:
        return {"ok": False, "error": f"Chưa có giá cho {symbol}, chờ EA gửi tick"}
    return await session.gateway.open_order(symbol, side, volume)


async def _execute_hold(session) -> dict:
    if not session.connected:
        return {"ok": False, "error": "EA chưa kết nối"}
    return await session.gateway.close_all("all")


def _format_ok_buy_sell(account_id: str, side: str, symbol: str, volume: float) -> str:
    icon = "🟢" if side == "BUY" else "🔴"
    return "\n".join([
        f"{icon} Đã vào lệnh {side}",
        f"{symbol} · {volume:.2f} lot",
        f"#{account_id}",
    ])


def _format_ok_hold(account_id: str) -> str:
    return "\n".join([
        "⏸️ HOLD — đã gửi lệnh đóng tất cả",
        f"#{account_id}",
    ])


def _format_error(account_id: str, error: str) -> str:
    return "\n".join([
        "❌ Không thực hiện được",
        error,
        f"#{account_id}",
    ])


async def _handle_command(
    sessions: SessionManager,
    config: dict,
    command: str,
) -> str:
    account_id = config["account_id"]
    session = sessions.get(account_id)
    if session is None:
        return _format_error(account_id, "Tài khoản chưa từng kết nối EA")

    if command == "HOLD":
        result = await _execute_hold(session)
        if result.get("ok"):
            return _format_ok_hold(account_id)
        return _format_error(account_id, result.get("error", "đóng lệnh thất bại"))

    symbol = _resolve_symbol(session, config.get("trade_symbol"))
    if not symbol:
        return _format_error(
            account_id,
            "Chưa cấu hình Symbol — nhập trong tab Telegram trên dashboard",
        )
    volume = float(config.get("trade_lot") or 0.01)
    result = await _execute_buy_sell(session, command, symbol, volume)
    if result.get("ok"):
        return _format_ok_buy_sell(account_id, command, symbol, volume)
    return _format_error(account_id, result.get("error", "vào lệnh thất bại"))


def _fetch_updates_sync(bot_token: str, offset: int) -> list[dict]:
    params = urllib.parse.urlencode({"timeout": POLL_TIMEOUT_S, "offset": offset})
    url = f"https://api.telegram.org/bot{bot_token}/getUpdates?{params}"
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=POLL_TIMEOUT_S + 5) as resp:
        payload = json.loads(resp.read().decode())
    if not payload.get("ok"):
        raise urllib.error.HTTPError(url, 500, str(payload), {}, None)
    return payload.get("result", [])


async def _poll_bot(sessions: SessionManager, bot_token: str, configs: list[dict]) -> None:
    offset = _offsets.get(bot_token, 0)
    try:
        updates = await asyncio.to_thread(_fetch_updates_sync, bot_token, offset)
    except Exception:
        logger.exception("Telegram getUpdates failed")
        return

    for update in updates:
        update_id = update.get("update_id", 0)
        if update_id >= offset:
            offset = update_id + 1

        message = update.get("message") or update.get("edited_message")
        if not message:
            continue
        chat = message.get("chat") or {}
        chat_id = str(chat.get("id", ""))
        text = message.get("text") or ""
        command = parse_command(text)
        if command is None:
            continue

        matches = _accounts_for_message(configs, bot_token, chat_id)
        if not matches:
            continue

        if len(matches) > 1:
            ids = ", ".join(f"#{c['account_id']}" for c in matches)
            reply = "\n".join([
                "⚠️ Nhiều tài khoản dùng chat này",
                f"Chỉ hỗ trợ 1 tài khoản / chat ({ids})",
            ])
            try:
                await telegram_notify.send_reply(bot_token, chat_id, reply)
            except Exception:
                logger.exception("failed to reply to Telegram chat %s", chat_id)
            continue

        try:
            reply = await _handle_command(sessions, matches[0], command)
            await telegram_notify.send_reply(bot_token, chat_id, reply)
        except Exception:
            logger.exception(
                "Telegram command %s failed for account %s",
                command,
                matches[0]["account_id"],
            )

    _offsets[bot_token] = offset


async def run_poller(sessions: SessionManager) -> None:
    logger.info("Telegram command poller started (BUY / SELL / HOLD)")
    while True:
        configs = db.list_telegram_configs()
        tokens = {c["bot_token"] for c in configs}
        for bot_token in tokens:
            await _poll_bot(sessions, bot_token, configs)
        await asyncio.sleep(0.5)
