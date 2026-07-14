"""Grid/DCA batch order scheduling.

Order #1 of a batch is placed immediately (see GridJobManager.start_job).
The remaining orders are placed lazily, driven by the live tick stream:
GridJobManager.on_price() is called from handlers.py's "tick" handler and
fires the next order once both the price has moved `spacing_points` in the
configured direction *and* `delay_seconds` have elapsed since the last
order in that job.
"""
import logging
import time
import uuid
from dataclasses import dataclass

from models import format_order_comment
from trade_gateway import TradeGateway

logger = logging.getLogger("grid_jobs")


@dataclass
class GridJob:
    job_id: str
    symbol: str
    side: str            # "BUY" | "SELL"
    remaining: int        # orders left to place, after the current one
    next_lot: float
    lot_mode: str          # "none" | "add" | "multiply"
    lot_value: float
    spacing_points: float
    direction: str          # "against" (DCA) | "with" (trend)
    delay_seconds: float
    sl_points: float
    tp_points: float
    last_price: float
    last_time: float
    comment_label: str = "Grid"
    next_index: int = 2     # order #1 placed in start_job; follow-ups start at #2


def scaled_lot(lot: float, mode: str, value: float) -> float:
    if mode == "add":
        return lot + value
    if mode == "multiply":
        return lot * value
    return lot


def sl_tp_from_points(side: str, price: float, sl_points: float, tp_points: float, point: float) -> tuple[float, float]:
    """Absolute SL/TP price given an offset in points from `price`."""
    sign = 1 if side == "BUY" else -1
    sl = price - sign * sl_points * point if sl_points else 0.0
    tp = price + sign * tp_points * point if tp_points else 0.0
    return sl, tp


def _price_condition_met(side: str, direction: str, last_price: float, price: float, spacing_price: float) -> bool:
    diff = price - last_price
    wants_up = (side == "BUY" and direction == "with") or (side == "SELL" and direction == "against")
    return diff >= spacing_price if wants_up else diff <= -spacing_price


class GridJobManager:
    def __init__(self, gateway: TradeGateway):
        self.gateway = gateway
        self._jobs: list[GridJob] = []

    async def start_job(self, *, symbol: str, side: str, volume: float,
                         sl_points: float, tp_points: float, count: int,
                         spacing_points: float, direction: str, delay_seconds: float,
                         lot_mode: str, lot_value: float, price: float, point: float,
                         comment_label: str = "Grid") -> dict:
        """Place order #1 immediately; register a job for the rest, if any."""
        sl, tp = sl_tp_from_points(side, price, sl_points, tp_points, point)
        result = await self.gateway.open_order(
            symbol, side, volume, sl, tp, format_order_comment(symbol, comment_label, 1)
        )
        if not result.get("ok"):
            return result

        remaining = count - 1
        if remaining > 0:
            self._jobs.append(GridJob(
                job_id=str(uuid.uuid4()), symbol=symbol, side=side,
                remaining=remaining, next_lot=scaled_lot(volume, lot_mode, lot_value),
                lot_mode=lot_mode, lot_value=lot_value,
                spacing_points=spacing_points, direction=direction,
                delay_seconds=delay_seconds, sl_points=sl_points, tp_points=tp_points,
                last_price=price, last_time=time.monotonic(),
                comment_label=comment_label,
            ))
        return result

    async def on_price(self, symbol: str, bid: float, ask: float, point: float) -> None:
        if not point:
            return
        now = time.monotonic()
        for job in [j for j in self._jobs if j.symbol == symbol]:
            if now - job.last_time < job.delay_seconds:
                continue

            price = ask if job.side == "BUY" else bid
            spacing_price = job.spacing_points * point
            if not _price_condition_met(job.side, job.direction, job.last_price, price, spacing_price):
                continue

            sl, tp = sl_tp_from_points(job.side, price, job.sl_points, job.tp_points, point)
            result = await self.gateway.open_order(
                job.symbol, job.side, job.next_lot, sl, tp,
                format_order_comment(job.symbol, job.comment_label, job.next_index),
            )
            if not result.get("ok"):
                logger.warning("grid job %s order failed (%s) - stopping job", job.job_id, result.get("error"))
                self._jobs.remove(job)
                continue

            job.remaining -= 1
            job.next_index += 1
            job.last_price = price
            job.last_time = now
            job.next_lot = scaled_lot(job.next_lot, job.lot_mode, job.lot_value)
            if job.remaining <= 0:
                self._jobs.remove(job)

    def active_jobs(self) -> list[dict]:
        return [
            {"job_id": j.job_id, "symbol": j.symbol, "side": j.side, "remaining": j.remaining}
            for j in self._jobs
        ]
