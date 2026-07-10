"""In-memory snapshot of open positions, fed by EA position/positions_end
messages and pushed out to WebSocket subscribers on every update.

The EA sends a full snapshot each cycle (see README) instead of deltas -
much simpler to keep correct, and cheap enough for a handful of positions.
"""
import asyncio
from typing import Optional

from models import Position


class PositionStore:
    def __init__(self) -> None:
        self._positions: dict[int, Position] = {}
        self._pending: dict[int, Position] = {}
        self._subscribers: set[asyncio.Queue] = set()

    def begin_snapshot(self) -> None:
        self._pending = {}

    def add(self, position: Position) -> None:
        self._pending[position.ticket] = position

    async def end_snapshot(self) -> None:
        self._positions = self._pending
        self._pending = {}
        await self._notify()

    def snapshot(self) -> list[dict]:
        return [p.to_dict() for p in self._positions.values()]

    def total_profit(self) -> float:
        return sum(p.profit + p.swap for p in self._positions.values())

    def get(self, ticket: int) -> Optional[Position]:
        return self._positions.get(ticket)

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    async def _notify(self) -> None:
        snap = self.snapshot()
        for queue in list(self._subscribers):
            await queue.put(snap)
