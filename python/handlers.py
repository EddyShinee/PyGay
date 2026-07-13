"""Business logic lives here, separate from the transport layer.

Every handler routes to the right AccountSession via client.account_id
(set once when "hello" arrives - see session_manager.py). Add a new
message type from MT5 by adding another `@server.on("...")` function in
`register()`; look up its session the same way the others do.
"""
import logging
import time
from typing import Optional

from socket_server import Client, SocketServer
from session_manager import SessionManager
from models import Position
import db
import telegram_notify

logger = logging.getLogger("handlers")

ACCOUNT_FIELDS = ("balance", "equity", "margin", "margin_free", "margin_level", "currency", "leverage", "magic")

# Drawdown = floating loss / balance, as a percentage. Checked highest-first
# so a jump straight from 40% to 85% (say) is reported at its true 70% tier,
# not silently skipped.
DRAWDOWN_TIERS = (100, 70, 60, 50)


def _drawdown_tier(pct: float) -> int:
    for tier in DRAWDOWN_TIERS:
        if pct >= tier:
            return tier
    return 0


def register(server: SocketServer, sessions: SessionManager) -> None:
    """Attach all message handlers to the server. Add new ones here."""

    server.on_disconnect(sessions.on_client_disconnect)

    @server.on("ping")
    async def on_ping(client: Client, message: dict) -> None:
        await client.send({"type": "pong", "t": time.time()})

    @server.on("hello")
    async def on_hello(client: Client, message: dict) -> None:
        account_id = str(message["account_id"])
        info = {
            "broker": message.get("broker", ""),
            "name": message.get("name", ""),
            "currency": message.get("currency", ""),
            "platform": (message.get("platform") or "").lower(),
        }
        await sessions.bind(client, account_id, info)
        logger.info("hello from account %s (%s)", account_id, info.get("broker"))

    @server.on("tick")
    async def on_tick(client: Client, message: dict) -> None:
        session = sessions.get(client.account_id)
        if session is None:
            return

        symbol = message.get("symbol")
        bid = message.get("bid")
        ask = message.get("ask")
        point = message.get("point")

        if symbol and bid is not None and ask is not None:
            bid, ask, point = float(bid), float(ask), float(point or 0)
            session.price_cache.update(symbol, bid, ask, point)
            await session.grid_manager.on_price(symbol, bid, ask, point)

        signal = compute_signal(message)
        if signal is not None:
            await client.send(signal)

    @server.on("positions_begin")
    async def on_positions_begin(client: Client, message: dict) -> None:
        session = sessions.get(client.account_id)
        if session is not None:
            session.store.begin_snapshot()

    @server.on("position")
    async def on_position(client: Client, message: dict) -> None:
        session = sessions.get(client.account_id)
        if session is not None:
            session.store.add(Position.from_message(message))

    @server.on("positions_end")
    async def on_positions_end(client: Client, message: dict) -> None:
        session = sessions.get(client.account_id)
        if session is None:
            return

        # Diff against the previous snapshot to detect new/modified orders -
        # this catches ANY change to the account (manual trades in MT5,
        # other EAs, this dashboard, ...), not just dashboard-initiated
        # ones. Skipped on the very first snapshot after connecting, or
        # every pre-existing open position would be reported as "new".
        old_by_ticket = {p["ticket"]: p for p in session.store.snapshot()} if session.has_synced_once else None
        await session.store.end_snapshot()

        if old_by_ticket is not None:
            for ticket, new_p in {p["ticket"]: p for p in session.store.snapshot()}.items():
                old_p = old_by_ticket.get(ticket)
                if old_p is None:
                    await telegram_notify.notify(
                        client.account_id,
                        telegram_notify.format_new_position(ticket, new_p),
                    )
                elif old_p["sl"] != new_p["sl"] or old_p["tp"] != new_p["tp"]:
                    await telegram_notify.notify(
                        client.account_id,
                        telegram_notify.format_modify_position(ticket, new_p["symbol"], old_p, new_p),
                    )
        session.has_synced_once = True
        await session.risk_manager.evaluate()

    @server.on("order_result")
    async def on_order_result(client: Client, message: dict) -> None:
        session = sessions.get(client.account_id)
        if session is not None:
            session.gateway.resolve(message)

    @server.on("account")
    async def on_account(client: Client, message: dict) -> None:
        session = sessions.get(client.account_id)
        if session is None:
            return
        await session.account_store.update({k: message[k] for k in ACCOUNT_FIELDS if k in message})

        balance = float(message.get("balance") or 0)
        if balance > 0:
            pct = max(0.0, -session.store.total_profit() / balance * 100)
            tier = _drawdown_tier(pct)
            if tier > session.last_drawdown_tier:
                await telegram_notify.notify(
                    client.account_id,
                    telegram_notify.format_drawdown_alert(client.account_id, pct, tier),
                )
            session.last_drawdown_tier = tier

        await session.risk_manager.evaluate()

    @server.on("symbols")
    async def on_symbols(client: Client, message: dict) -> None:
        session = sessions.get(client.account_id)
        if session is None:
            return
        raw = message.get("list", "")
        symbols = [s for s in raw.split(",") if s]
        await session.symbol_store.update(symbols)

    @server.on("history_begin")
    async def on_history_begin(client: Client, message: dict) -> None:
        pass  # nothing to do - HistoryGateway.fetch() already starts with an empty list

    @server.on("bar")
    async def on_bar(client: Client, message: dict) -> None:
        session = sessions.get(client.account_id)
        if session is not None:
            session.history_gateway.on_bar(message)

    @server.on("history_end")
    async def on_history_end(client: Client, message: dict) -> None:
        session = sessions.get(client.account_id)
        if session is not None:
            session.history_gateway.on_end(message)

    @server.on("deal_closed")
    async def on_deal_closed(client: Client, message: dict) -> None:
        if client.account_id is None:
            return
        deal = {
            "ticket": int(message["ticket"]),
            "symbol": message["symbol"],
            "side": message["side"],
            "volume": float(message["volume"]),
            "price_open": float(message["price_open"]),
            "price_close": float(message["price_close"]),
            "profit": float(message["profit"]),
            "swap": float(message.get("swap", 0)),
            "commission": float(message.get("commission", 0)),
            "time_open": int(message["time_open"]),
            "time_close": int(message["time_close"]),
        }
        db.upsert_deal(client.account_id, deal)
        logger.info("deal closed [%s] #%s %s %s profit=%.2f",
                    client.account_id, deal["ticket"], deal["side"], deal["symbol"], deal["profit"])

        await telegram_notify.notify(
            client.account_id,
            telegram_notify.format_close_deal(deal),
        )


def compute_signal(tick: dict) -> Optional[dict]:
    """Plug your strategy / ML model here.

    Return a dict like {"type": "signal", "action": "BUY", "symbol": ...,
    "volume": ...} to send an order to the EA, or None to stay silent.
    """
    return None
