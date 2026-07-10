"""Requests historical OHLC bars from the EA and collects the
history_begin/bar/.../history_end stream into a list, correlated by id -
same idea as TradeGateway, but for a multi-message reply instead of a
single order_result.
"""
import asyncio
import logging
import uuid
from typing import Optional

from socket_server import Client, SocketServer

logger = logging.getLogger("history_gateway")


class HistoryGateway:
    def __init__(self, server: SocketServer, account_id: str, timeout: float = 30.0):
        self.server = server
        self.account_id = account_id
        self.timeout = timeout
        self._pending: dict[str, dict] = {}  # id -> {"bars": [...], "future": Future}

    def on_bar(self, message: dict) -> None:
        entry = self._pending.get(message.get("id"))
        if entry is not None:
            entry["bars"].append(message)

    def on_end(self, message: dict) -> None:
        entry = self._pending.get(message.get("id"))
        if entry is not None and not entry["future"].done():
            entry["future"].set_result(entry["bars"])

    def _current_client(self) -> Optional[Client]:
        clients = [c for c in self.server.clients() if c.account_id == self.account_id]
        return clients[-1] if clients else None

    async def fetch(self, symbol: str, timeframe: str, count: int) -> list[dict]:
        client = self._current_client()
        if client is None:
            raise RuntimeError("no EA connected")

        req_id = str(uuid.uuid4())
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[req_id] = {"bars": [], "future": future}
        try:
            await client.send({
                "type": "get_history", "id": req_id,
                "symbol": symbol, "timeframe": timeframe, "count": count,
            })
            return await asyncio.wait_for(future, timeout=self.timeout)
        finally:
            self._pending.pop(req_id, None)
