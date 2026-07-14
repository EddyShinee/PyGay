"""In-memory snapshot of open positions, fed by EA position/positions_end
messages and pushed out to WebSocket subscribers on every update.

The EA sends a full snapshot each cycle (see README) instead of deltas -
much simpler to keep correct, and cheap enough for a handful of positions.
"""
import asyncio
import re
from typing import Optional

from models import Position

# `{Symbol}-{Algo}-#n` (new) or legacy `{Algo}-#n`
_ALGO_COMMENT_RE = re.compile(
    r"^(?:([A-Z0-9._]+)-)?([A-Za-z]+)-#(\d+)$"
)


def parse_algo_comment(comment: str) -> Optional[tuple[str, str, int]]:
    """Return (symbol_or_empty, algo, index) from an order comment, or None."""
    c = (comment or "").strip()
    m = _ALGO_COMMENT_RE.match(c)
    if not m:
        return None
    sym = (m.group(1) or "").upper()
    algo = m.group(2)
    return sym, algo, int(m.group(3))


class PositionStore:
    def __init__(self) -> None:
        self._positions: dict[int, Position] = {}
        self._pending: dict[int, Position] = {}
        self._subscribers: set[asyncio.Queue] = set()
        self._snapshot_wait: Optional[asyncio.Event] = None

    def begin_snapshot(self) -> None:
        self._pending = {}

    def prepare_snapshot_wait(self) -> None:
        """Call before requesting a fresh EA snapshot."""
        self._snapshot_wait = asyncio.Event()

    async def wait_snapshot(self, timeout: float = 2.0) -> None:
        """Wait until the next positions_end (or timeout)."""
        event = self._snapshot_wait
        if event is None:
            return
        await asyncio.wait_for(event.wait(), timeout)

    def add(self, position: Position) -> None:
        self._pending[position.ticket] = position

    async def end_snapshot(self) -> None:
        self._positions = self._pending
        self._pending = {}
        if self._snapshot_wait is not None:
            self._snapshot_wait.set()
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

    def totals_by_basket(self) -> dict[tuple[str, str], float]:
        """Net floating P/L grouped by (symbol, side)."""
        totals: dict[tuple[str, str], float] = {}
        for p in self._positions.values():
            key = (p.symbol, p.side)
            totals[key] = totals.get(key, 0.0) + p.profit + p.swap
        return totals

    def matching_positions(
        self,
        *,
        filter: str = "all",
        symbol: str = "",
        side: str = "",
    ) -> list[dict]:
        """Open positions matching optional symbol/side and profit/loss filter."""
        sym_filter = (symbol or "").strip().upper()
        side_filter = (side or "").strip().upper()
        out: list[dict] = []
        for p in self.snapshot():
            if sym_filter and (p.get("symbol") or "").upper() != sym_filter:
                continue
            if side_filter and (p.get("side") or "").upper() != side_filter:
                continue
            pnl = float(p.get("profit") or 0) + float(p.get("swap") or 0)
            if filter == "profit" and pnl <= 0:
                continue
            if filter == "loss" and pnl > 0:
                continue
            out.append(p)
        return out

    def max_algo_index(
        self,
        symbol: str,
        algo: str,
        *,
        side: str = "",
        positions: Optional[list[dict]] = None,
    ) -> int:
        """Highest `{algo}-#n` / `{Symbol}-{algo}-#n` on open tickets for this symbol.

        Side is optional: leave empty to count across BUY+SELL (Entry sequence).
        """
        sym_u = symbol.upper()
        side_u = (side or "").strip().upper()
        algo_u = algo
        rows = positions if positions is not None else self.snapshot()
        max_n = 0
        for p in rows:
            if (p.get("symbol") or "").upper() != sym_u:
                continue
            if side_u and (p.get("side") or "").upper() != side_u:
                continue
            parsed = parse_algo_comment(p.get("comment") or "")
            if parsed is None:
                continue
            c_sym, c_algo, n = parsed
            if c_algo.lower() != algo_u.lower():
                continue
            # Legacy `Entry-#n` has empty symbol; new form must match.
            if c_sym and c_sym != sym_u:
                continue
            max_n = max(max_n, n)
        return max_n

    def next_algo_index(
        self,
        symbol: str,
        side: str,
        algo: str,
        *,
        positions: Optional[list[dict]] = None,
        match_side: bool = True,
    ) -> int:
        """Next 1-based index for `{Symbol}-{algo}-#{n}`."""
        max_n = self.max_algo_index(
            symbol,
            algo,
            side=side if match_side else "",
            positions=positions,
        )
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
