"""Sends commands to the connected EA and correlates responses by id.

Assumes a single connected EA (the most recently connected client) -
matches the current one-terminal setup. `resolve()` is called by the
"order_result" handler in handlers.py to wake up whoever is awaiting.
"""
import asyncio
import logging
import uuid
from typing import Optional

from socket_server import Client, SocketServer

logger = logging.getLogger("trade_gateway")


class TradeGateway:
    def __init__(self, server: SocketServer, timeout: float = 5.0):
        self.server = server
        self.timeout = timeout
        self._pending: dict[str, asyncio.Future] = {}

    def resolve(self, message: dict) -> None:
        """Called when an order_result arrives from the EA."""
        req_id = message.get("id")
        future = self._pending.pop(req_id, None) if req_id else None
        if future and not future.done():
            future.set_result(message)

    def _current_client(self) -> Optional[Client]:
        clients = self.server.clients()
        return clients[-1] if clients else None

    async def _send(self, message: dict) -> dict:
        client = self._current_client()
        if client is None:
            return {"ok": False, "error": "no EA connected"}

        req_id = str(uuid.uuid4())
        message["id"] = req_id
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[req_id] = future
        try:
            await client.send(message)
            return await asyncio.wait_for(future, timeout=self.timeout)
        except asyncio.TimeoutError:
            return {"ok": False, "error": "timeout waiting for EA response"}
        except Exception as exc:
            logger.exception("send_command failed")
            return {"ok": False, "error": str(exc)}
        finally:
            self._pending.pop(req_id, None)

    async def open_order(self, symbol: str, side: str, volume: float,
                          sl: float = 0.0, tp: float = 0.0) -> dict:
        return await self._send({
            "type": "open_order", "symbol": symbol, "side": side,
            "volume": volume, "sl": sl, "tp": tp,
        })

    async def close_position(self, ticket: int) -> dict:
        return await self._send({"type": "close_position", "ticket": ticket})

    async def close_all(self, filter: str = "all") -> dict:
        return await self._send({"type": "close_all", "filter": filter})

    async def modify_position(self, ticket: int, sl: float, tp: float) -> dict:
        return await self._send({"type": "modify_position", "ticket": ticket, "sl": sl, "tp": tp})

    async def set_magic(self, magic: int) -> dict:
        return await self._send({"type": "set_magic", "magic": magic})

    async def request_positions(self) -> None:
        """Fire-and-forget: EA replies with a positions_begin/.../positions_end
        burst handled by handlers.py, not a single order_result."""
        client = self._current_client()
        if client is not None:
            await client.send({"type": "get_positions"})
