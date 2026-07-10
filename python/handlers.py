"""Business logic lives here, separate from the transport layer.

Add a new message type from MT5 by adding another `@server.on("...")`
function in `register()`. To push something to MT5 (a trade signal,
a heartbeat, ...), call `await client.send({...})` or
`await server.broadcast({...})`.
"""
import logging
import time
from typing import Optional

from socket_server import Client, SocketServer
from position_store import PositionStore
from trade_gateway import TradeGateway
from grid_jobs import GridJobManager
from price_cache import PriceCache
from account_store import AccountStore
from symbol_store import SymbolStore
from history_gateway import HistoryGateway
from models import Position
import db

logger = logging.getLogger("handlers")

ACCOUNT_FIELDS = ("balance", "equity", "margin", "margin_free", "margin_level", "currency", "leverage", "magic")


def register(server: SocketServer, store: PositionStore, gateway: TradeGateway,
             grid_manager: GridJobManager, price_cache: PriceCache,
             account_store: AccountStore, symbol_store: SymbolStore,
             history_gateway: HistoryGateway) -> None:
    """Attach all message handlers to the server. Add new ones here."""

    @server.on("ping")
    async def on_ping(client: Client, message: dict) -> None:
        await client.send({"type": "pong", "t": time.time()})

    @server.on("tick")
    async def on_tick(client: Client, message: dict) -> None:
        symbol = message.get("symbol")
        bid = message.get("bid")
        ask = message.get("ask")
        point = message.get("point")

        if symbol and bid is not None and ask is not None:
            bid, ask, point = float(bid), float(ask), float(point or 0)
            price_cache.update(symbol, bid, ask, point)
            await grid_manager.on_price(symbol, bid, ask, point)

        signal = compute_signal(message)
        if signal is not None:
            await client.send(signal)

    @server.on("positions_begin")
    async def on_positions_begin(client: Client, message: dict) -> None:
        store.begin_snapshot()

    @server.on("position")
    async def on_position(client: Client, message: dict) -> None:
        store.add(Position.from_message(message))

    @server.on("positions_end")
    async def on_positions_end(client: Client, message: dict) -> None:
        await store.end_snapshot()

    @server.on("order_result")
    async def on_order_result(client: Client, message: dict) -> None:
        gateway.resolve(message)

    @server.on("account")
    async def on_account(client: Client, message: dict) -> None:
        await account_store.update({k: message[k] for k in ACCOUNT_FIELDS if k in message})

    @server.on("symbols")
    async def on_symbols(client: Client, message: dict) -> None:
        raw = message.get("list", "")
        symbols = [s for s in raw.split(",") if s]
        await symbol_store.update(symbols)

    @server.on("history_begin")
    async def on_history_begin(client: Client, message: dict) -> None:
        pass  # nothing to do - HistoryGateway.fetch() already starts with an empty list

    @server.on("bar")
    async def on_bar(client: Client, message: dict) -> None:
        history_gateway.on_bar(message)

    @server.on("history_end")
    async def on_history_end(client: Client, message: dict) -> None:
        history_gateway.on_end(message)

    @server.on("deal_closed")
    async def on_deal_closed(client: Client, message: dict) -> None:
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
        db.upsert_deal(deal)
        logger.info("deal closed #%s %s %s profit=%.2f", deal["ticket"], deal["side"], deal["symbol"], deal["profit"])


def compute_signal(tick: dict) -> Optional[dict]:
    """Plug your strategy / ML model here.

    Return a dict like {"type": "signal", "action": "BUY", "symbol": ...,
    "volume": ...} to send an order to the EA, or None to stay silent.
    """
    return None
