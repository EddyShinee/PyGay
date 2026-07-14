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

    def totals_by_symbol(self) -> dict[str, float]:
        """Net floating P/L (profit + swap) grouped by symbol."""
        totals: dict[str, float] = {}
        for p in self._positions.values():
            totals[p.symbol] = totals.get(p.symbol, 0.0) + p.profit + p.swap
        return totals

    def next_algo_index(
        self,
        symbol: str,
        side: str,
        algo: str,
        *,
        positions: Optional[list[dict]] = None,
    ) -> int:
        """Next 1-based index for `{Symbol}-{algo}-#{n}` on this symbol+side.

        Also recognizes legacy `{algo}-#{n}` comments from older builds.
        """
        sym_u = symbol.upper()
        side_u = side.upper()
        prefixes = (f"{sym_u}-{algo}-#", f"{algo}-#")
        rows = positions if positions is not None else self.snapshot()
        max_n = 0
        for p in rows:
            if (p.get("symbol") or "").upper() != sym_u:
                continue
            if (p.get("side") or "").upper() != side_u:
                continue
            c = (p.get("comment") or "").strip()
            for prefix in prefixes:
                if not c.startswith(prefix):
                    continue
                try:
                    max_n = max(max_n, int(c[len(prefix):]))
                except ValueError:
                    pass
                break
        return max_n + 1

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
