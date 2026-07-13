"""Fire-and-forget Telegram notifications, one bot/chat configured per MetaTrader
account (db.get_telegram_config). Never let a Telegram failure disturb the
actual trading flow - every call here is wrapped so it can only log, never
raise into its caller.
"""
import asyncio
import json
import logging
import time
import urllib.error
import urllib.request
from typing import Optional

import db

logger = logging.getLogger("telegram_notify")

API_TIMEOUT_S = 10

# --- Rate limiting -----------------------------------------------------------
# Telegram allows roughly 1 message/second per chat and ~30/second globally.
# Every outgoing message is funnelled through a single serialized sender that
# spaces requests out and honours the server's Retry-After on HTTP 429, so we
# never trip the limit no matter how many entries fire at once.
_MIN_INTERVAL_PER_CHAT_S = 1.2
_MIN_GLOBAL_INTERVAL_S = 0.05
_MAX_RETRY = 3
_QUEUE_MAX = 300

_last_chat_send: dict[str, float] = {}
_last_global_send: float = 0.0
_send_lock: Optional[asyncio.Lock] = None
_notify_queue: Optional[asyncio.Queue] = None
_worker_started = False


class TelegramSendError(Exception):
    """Raised by the low-level sender so callers can react to e.g. 429."""

    def __init__(self, status: int, retry_after: Optional[int] = None, detail: str = ""):
        super().__init__(f"Telegram HTTP {status}{(' - ' + detail) if detail else ''}")
        self.status = status
        self.retry_after = retry_after


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


def format_risk_triggered(account_id: str, reason: str, detail: str = "") -> str:
    lines = [
        "🛡️ RiskManager kích hoạt",
        f"#{account_id}",
        reason,
    ]
    if detail:
        lines.append(detail)
    return "\n".join(lines)


def format_entry_triggered(
    account_id: str, side: str, symbol: str, volume: float, reason: str
) -> str:
    icon = "🟢" if side == "BUY" else "🔴"
    return "\n".join([
        f"{icon} EntryManager — vào lệnh {side}",
        f"{symbol} · {volume:.2f} lot",
        reason,
        f"#{account_id}",
    ])


def _send_sync(
    bot_token: str,
    chat_id: str,
    text: str,
    reply_markup: dict | None = None,
) -> None:
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload: dict = {"chat_id": chat_id, "text": text}
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    body = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=API_TIMEOUT_S) as resp:
            resp.read()
            return
    except urllib.error.HTTPError as exc:
        # Pull Retry-After from the header first, then the JSON body Telegram
        # returns as {"parameters": {"retry_after": N}} for 429s.
        retry_after: Optional[int] = None
        header_ra = exc.headers.get("Retry-After") if exc.headers else None
        if header_ra:
            try:
                retry_after = int(header_ra)
            except (TypeError, ValueError):
                retry_after = None
        detail = ""
        try:
            data = json.loads(exc.read().decode() or "{}")
            detail = str(data.get("description") or "")
            if retry_after is None:
                ra = data.get("parameters", {}).get("retry_after")
                retry_after = int(ra) if ra is not None else None
        except Exception:
            pass
        raise TelegramSendError(exc.code, retry_after, detail) from None


def _get_lock() -> asyncio.Lock:
    global _send_lock
    if _send_lock is None:
        _send_lock = asyncio.Lock()
    return _send_lock


async def _throttle(key: str) -> None:
    """Sleep just long enough to respect the per-chat and global spacing.
    Must be called while holding the send lock so timestamps stay consistent."""
    global _last_global_send
    now = time.monotonic()
    wait = _MIN_GLOBAL_INTERVAL_S - (now - _last_global_send)
    chat_wait = _MIN_INTERVAL_PER_CHAT_S - (now - _last_chat_send.get(key, 0.0))
    wait = max(wait, chat_wait)
    if wait > 0:
        await asyncio.sleep(wait)
        now = time.monotonic()
    _last_global_send = now
    _last_chat_send[key] = now


async def _deliver(
    bot_token: str,
    chat_id: str,
    text: str,
    reply_markup: dict | None = None,
) -> None:
    """Serialized + rate-limited send with automatic 429 back-off. Raises
    TelegramSendError (or another Exception) if it ultimately fails."""
    key = f"{bot_token}:{chat_id}"
    for attempt in range(_MAX_RETRY + 1):
        try:
            async with _get_lock():
                await _throttle(key)
                await asyncio.to_thread(_send_sync, bot_token, chat_id, text, reply_markup)
            return
        except TelegramSendError as exc:
            if exc.status == 429 and attempt < _MAX_RETRY:
                wait = (exc.retry_after or 2) + 0.5
                logger.warning(
                    "Telegram 429 (chat %s), chờ %.1fs rồi thử lại", chat_id, wait
                )
                await asyncio.sleep(wait)
                continue
            raise


def _ensure_worker() -> None:
    global _notify_queue, _worker_started
    if _notify_queue is None:
        _notify_queue = asyncio.Queue(maxsize=_QUEUE_MAX)
    if not _worker_started:
        _worker_started = True
        asyncio.ensure_future(_notify_worker())


async def _notify_worker() -> None:
    """Drains queued fire-and-forget notifications one at a time."""
    assert _notify_queue is not None
    while True:
        bot_token, chat_id, text = await _notify_queue.get()
        try:
            await _deliver(bot_token, chat_id, text)
        except Exception as exc:
            logger.warning("Bỏ qua thông báo Telegram (chat %s): %s", chat_id, exc)
        finally:
            _notify_queue.task_done()


def trade_keyboard_buttons(symbol: str) -> list[list[str]]:
    """Same layout as python-telegram-bot ReplyKeyboardMarkup keyboard=[[...]]."""
    sym = (symbol or "XAUUSD").strip().upper()
    return [
        [f"{sym} BUY", f"{sym} - SELL"],
        ["CLOSE PROFIT", "CLOSE LOSS", "CLOSE ALL"],
    ]


def trade_keyboard_markup(symbol: str) -> dict:
    """ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=False)."""
    keyboard = trade_keyboard_buttons(symbol)
    return {
        "keyboard": [[{"text": label} for label in row] for row in keyboard],
        "resize_keyboard": True,
        "one_time_keyboard": False,
    }


def remove_keyboard_markup() -> dict:
    return {"remove_keyboard": True}


def trade_command_hint(symbol: str) -> str:
    sym = (symbol or "XAUUSD").strip().upper()
    return (
        f"{sym} BUY · {sym} - SELL · "
        "CLOSE PROFIT · CLOSE LOSS · CLOSE ALL"
    )


async def notify(account_id: str, text: str) -> None:
    """Best-effort, non-blocking: enqueues the message for the rate-limited
    background sender and returns immediately, so a slow/failing Telegram
    never stalls or spams the trading flow. Silently does nothing if the
    account has no Telegram config."""
    config = db.get_telegram_config(account_id)
    if config is None:
        return
    _ensure_worker()
    assert _notify_queue is not None
    item = (config["bot_token"], config["chat_id"], text)
    try:
        _notify_queue.put_nowait(item)
    except asyncio.QueueFull:
        # Backlog full: drop the oldest queued message so the newest alert
        # still gets through, rather than growing memory unbounded.
        try:
            _notify_queue.get_nowait()
            _notify_queue.task_done()
        except Exception:
            pass
        try:
            _notify_queue.put_nowait(item)
        except asyncio.QueueFull:
            logger.warning("Hàng đợi Telegram đầy, bỏ 1 thông báo (chat %s)", config["chat_id"])


async def send_reply(
    bot_token: str,
    chat_id: str,
    text: str,
    reply_markup: dict | None = None,
) -> None:
    """Send a message to a chat; raises on failure (for command replies)."""
    await _deliver(bot_token, chat_id, text, reply_markup)


async def setup_trade_keyboard(bot_token: str, chat_id: str, symbol: str) -> None:
    sym = (symbol or "XAUUSD").strip().upper()
    await _deliver(bot_token, chat_id, "Chọn thao tác:", trade_keyboard_markup(sym))


async def remove_trade_keyboard(bot_token: str, chat_id: str) -> None:
    await _deliver(
        bot_token,
        chat_id,
        "Đã gỡ kết nối Telegram với tài khoản này.",
        remove_keyboard_markup(),
    )


async def send_test(bot_token: str, chat_id: str, symbol: str = "XAUUSD") -> None:
    """Used by the "Test" button - raises on failure so the caller can
    report a clear error, unlike notify() which is always silent."""
    sym = (symbol or "XAUUSD").strip().upper()
    text = "\n".join([
        "🔔 Test thông báo",
        "MetaTrader Dashboard",
        "Kết nối Telegram thành công ✅",
        "",
        "Chọn thao tác:",
    ])
    await _deliver(bot_token, chat_id, text, trade_keyboard_markup(sym))
