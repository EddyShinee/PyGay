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

logger = logging.getLogger("handlers")

ACCOUNT_FIELDS = ("balance", "equity", "margin", "margin_free", "margin_level", "currency", "leverage", "magic")


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
        if session is not None:
            await session.store.end_snapshot()

    @server.on("order_result")
    async def on_order_result(client: Client, message: dict) -> None:
        session = sessions.get(client.account_id)
        if session is not None:
            session.gateway.resolve(message)

    @server.on("account")
    async def on_account(client: Client, message: dict) -> None:
        session = sessions.get(client.account_id)
        if session is not None:
            await session.account_store.update({k: message[k] for k in ACCOUNT_FIELDS if k in message})

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


def compute_signal(tick: dict) -> Optional[dict]:
    """Plug your strategy / ML model here.

    Return a dict like {"type": "signal", "action": "BUY", "symbol": ...,
    "volume": ...} to send an order to the EA, or None to stay silent.
    """
    return None
