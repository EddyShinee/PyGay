"""Last known bid/ask/point per symbol, fed by the tick stream.

Used to translate "SL/TP in points" and grid spacing into absolute
prices when placing an order - see grid_jobs.sl_tp_from_points().
"""
import time
from typing import Optional


class PriceCache:
    def __init__(self) -> None:
        self._prices: dict[str, dict] = {}

    def update(self, symbol: str, bid: float, ask: float, point: float) -> None:
        self._prices[symbol] = {"bid": bid, "ask": ask, "point": point, "time": time.time()}

    def get(self, symbol: str) -> Optional[dict]:
        return self._prices.get(symbol)
