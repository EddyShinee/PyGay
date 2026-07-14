"""Shared ticket-by-ticket close helpers (dashboard + risk manager)."""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from session_manager import AccountSession

logger = logging.getLogger("position_close")

SNAPSHOT_WAIT_S = 2.0


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
    """Close open tickets matching filter/symbol/side using live P/L."""
    if refresh:
        await refresh_snapshot(session)

    to_close = session.store.matching_positions(filter=filter, symbol=symbol, side=side)
    if not to_close:
        sym = (symbol or "").strip().upper() or None
        side_u = (side or "").strip().upper() or None
        return {
            "ok": True,
            "closed_count": 0,
            "failed": [],
            "filter": filter,
            "symbol": sym,
            "side": side_u,
            "matched": 0,
        }

    closed = 0
    failed: list[dict] = []
    for p in to_close:
        ticket = int(p["ticket"])
        result = await session.gateway.close_position(ticket)
        if result.get("ok"):
            closed += 1
        else:
            failed.append({
                "ticket": ticket,
                "symbol": p.get("symbol"),
                "side": p.get("side"),
                "error": result.get("error") or "close failed",
            })

    try:
        await refresh_snapshot(session)
    except Exception:
        pass

    sym = (symbol or "").strip().upper() or None
    side_u = (side or "").strip().upper() or None
    return {
        "ok": len(failed) == 0,
        "closed_count": closed,
        "failed": failed,
        "filter": filter,
        "symbol": sym,
        "side": side_u,
        "matched": len(to_close),
        "error": failed[0]["error"] if failed else "",
    }
