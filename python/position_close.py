"""Shared ticket-by-ticket close helpers (dashboard + risk manager)."""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from session_manager import AccountSession

logger = logging.getLogger("position_close")

SNAPSHOT_WAIT_S = 2.0
# A close can race an in-flight open (e.g. a pyramid/DCA add sent moments
# before a TP/SL fired) - that ticket won't be in the snapshot yet on the
# first pass. Looping a few rounds, re-reading the snapshot each time, picks
# it up once the EA confirms it, and also retries anything that failed to
# close (timeout, broker rejection) instead of silently leaving it open.
MAX_CLOSE_ROUNDS = 3
CLOSE_ROUND_DELAY_S = 1.0


async def refresh_snapshot(session: "AccountSession", timeout: float = SNAPSHOT_WAIT_S) -> None:
    """Ask the EA for a fresh positions snapshot and wait for positions_end."""
    if not session.connected:
        return
    store = session.store
    store.prepare_snapshot_wait()
    try:
        await session.gateway.request_positions()
        await store.wait_snapshot(timeout)
    except asyncio.TimeoutError:
        logger.debug("[%s] positions snapshot wait timed out", session.account_id)


async def close_matching(
    session: "AccountSession",
    *,
    filter: str = "all",
    symbol: str = "",
    side: str = "",
    refresh: bool = True,
) -> dict:
    """Close open tickets matching filter/symbol/side using live P/L.

    Runs up to MAX_CLOSE_ROUNDS passes, re-reading the live snapshot between
    each: a ticket still in flight when the first pass ran, or one whose
    close failed transiently, gets picked up on a later pass rather than
    being left open with no retry."""
    sym = (symbol or "").strip().upper() or None
    side_u = (side or "").strip().upper() or None
    total_closed = 0
    matched_total = 0
    failed: list[dict] = []

    for round_idx in range(MAX_CLOSE_ROUNDS):
        if refresh or round_idx > 0:
            await refresh_snapshot(session)
        to_close = session.store.matching_positions(filter=filter, symbol=symbol, side=side)
        if not to_close:
            failed = []
            break
        matched_total += len(to_close)
        failed = []
        for p in to_close:
            ticket = int(p["ticket"])
            result = await session.gateway.close_position(ticket)
            if result.get("ok"):
                total_closed += 1
            else:
                failed.append({
                    "ticket": ticket,
                    "symbol": p.get("symbol"),
                    "side": p.get("side"),
                    "error": result.get("error") or "close failed",
                })
        if round_idx < MAX_CLOSE_ROUNDS - 1:
            await asyncio.sleep(CLOSE_ROUND_DELAY_S)

    if matched_total:
        try:
            await refresh_snapshot(session)
        except Exception:
            pass

    return {
        "ok": len(failed) == 0,
        "closed_count": total_closed,
        "failed": failed,
        "filter": filter,
        "symbol": sym,
        "side": side_u,
        "matched": matched_total,
        "error": failed[0]["error"] if failed else "",
    }
