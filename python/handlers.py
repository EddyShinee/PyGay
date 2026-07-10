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

logger = logging.getLogger("handlers")


def register(server: SocketServer) -> None:
    """Attach all message handlers to the server. Add new ones here."""

    @server.on("ping")
    async def on_ping(client: Client, message: dict) -> None:
        await client.send({"type": "pong", "t": time.time()})

    @server.on("tick")
    async def on_tick(client: Client, message: dict) -> None:
        symbol = message.get("symbol")
        bid = message.get("bid")
        ask = message.get("ask")
        logger.info("tick %s bid=%s ask=%s", symbol, bid, ask)

        signal = compute_signal(message)
        if signal is not None:
            await client.send(signal)


def compute_signal(tick: dict) -> Optional[dict]:
    """Plug your strategy / ML model here.

    Return a dict like {"type": "signal", "action": "BUY", "symbol": ...,
    "volume": ...} to send an order to the EA, or None to stay silent.
    """
    return None
