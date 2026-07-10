"""Dashboard web layer: REST API + WebSocket, served on top of the same
asyncio loop as the MT5 socket server (see main.py).

Kept separate from socket_server/handlers on purpose: this file only knows
about PositionStore/TradeGateway/GridJobManager/PriceCache, never about the
raw EA protocol.
"""
import asyncio
import logging
from pathlib import Path
from typing import Literal, Optional

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from position_store import PositionStore
from trade_gateway import TradeGateway
from grid_jobs import GridJobManager
from price_cache import PriceCache
from account_store import AccountStore
import db

logger = logging.getLogger("web")

STATIC_DIR = Path(__file__).parent / "static"


class OrderRequest(BaseModel):
    symbol: str
    side: Literal["BUY", "SELL"]
    volume: float
    sl_points: float = 0
    tp_points: float = 0
    count: int = 1
    spacing_points: float = 0
    direction: Literal["against", "with"] = "against"
    delay_seconds: float = 0
    lot_mode: Literal["none", "add", "multiply"] = "none"
    lot_value: float = 0


class ModifyRequest(BaseModel):
    sl: float = 0
    tp: float = 0


class CloseAllRequest(BaseModel):
    filter: Literal["all", "profit", "loss"] = "all"


class CloseByThresholdRequest(BaseModel):
    op: Literal[">=", "<="]
    amount: float


class MagicRequest(BaseModel):
    magic: int


def _account_response(store: PositionStore, account_store: AccountStore) -> dict:
    positions = store.snapshot()
    buy = [p for p in positions if p["side"] == "BUY"]
    sell = [p for p in positions if p["side"] == "SELL"]
    return {
        **account_store.snapshot(),
        "floating_profit": store.total_profit(),
        "open_count": len(positions),
        "open_buy_count": len(buy),
        "open_sell_count": len(sell),
        "open_buy_volume": round(sum(p["volume"] for p in buy), 2),
        "open_sell_volume": round(sum(p["volume"] for p in sell), 2),
    }


def create_app(store: PositionStore, gateway: TradeGateway,
                grid_manager: GridJobManager, price_cache: PriceCache,
                account_store: AccountStore) -> FastAPI:
    app = FastAPI(title="MT5 Dashboard")

    @app.get("/api/positions")
    async def get_positions():
        return {"positions": store.snapshot(), "total_profit": store.total_profit()}

    @app.get("/api/account")
    async def get_account():
        return _account_response(store, account_store)

    @app.get("/api/insights")
    async def get_insights(bucket: Literal["minute", "hour", "day", "month", "year"] = "day",
                            limit: int = 30):
        return {"bucket": bucket, "rows": db.insights(bucket, limit)}

    @app.get("/api/summary")
    async def get_summary():
        return db.summary()

    @app.get("/api/jobs")
    async def get_jobs():
        return {"jobs": grid_manager.active_jobs()}

    @app.post("/api/positions/refresh")
    async def refresh_positions():
        await gateway.request_positions()
        return {"ok": True}

    @app.post("/api/orders")
    async def place_order(req: OrderRequest):
        cached = price_cache.get(req.symbol)
        if cached is None:
            raise HTTPException(400, f"Chưa có dữ liệu giá cho {req.symbol}, chờ EA gửi tick trước.")
        price = cached["ask"] if req.side == "BUY" else cached["bid"]
        result = await grid_manager.start_job(
            symbol=req.symbol, side=req.side, volume=req.volume,
            sl_points=req.sl_points, tp_points=req.tp_points, count=req.count,
            spacing_points=req.spacing_points, direction=req.direction,
            delay_seconds=req.delay_seconds, lot_mode=req.lot_mode, lot_value=req.lot_value,
            price=price, point=cached["point"],
        )
        if not result.get("ok"):
            raise HTTPException(400, result.get("error", "order failed"))
        return result

    @app.post("/api/positions/{ticket}/close")
    async def close_position(ticket: int):
        result = await gateway.close_position(ticket)
        if not result.get("ok"):
            raise HTTPException(400, result.get("error", "close failed"))
        return result

    @app.post("/api/positions/close_all")
    async def close_all(req: CloseAllRequest):
        result = await gateway.close_all(req.filter)
        if not result.get("ok"):
            raise HTTPException(400, result.get("error", "close_all failed"))
        return result

    @app.post("/api/positions/close_by_threshold")
    async def close_by_threshold(req: CloseByThresholdRequest):
        total = store.total_profit()
        triggered = total >= req.amount if req.op == ">=" else total <= req.amount
        if not triggered:
            return {"triggered": False, "total_profit": total}
        result = await gateway.close_all("all")
        result["triggered"] = True
        result["total_profit"] = total
        return result

    @app.post("/api/positions/{ticket}/modify")
    async def modify_position(ticket: int, req: ModifyRequest):
        result = await gateway.modify_position(ticket, req.sl, req.tp)
        if not result.get("ok"):
            raise HTTPException(400, result.get("error", "modify failed"))
        return result

    @app.post("/api/magic")
    async def set_magic(req: MagicRequest):
        result = await gateway.set_magic(req.magic)
        if not result.get("ok"):
            raise HTTPException(400, result.get("error", "set magic failed"))
        return result

    @app.websocket("/ws/positions")
    async def ws_positions(ws: WebSocket):
        await ws.accept()
        queue = store.subscribe()
        try:
            await ws.send_json({"positions": store.snapshot(), "total_profit": store.total_profit()})
            while True:
                snapshot = await queue.get()
                await ws.send_json({
                    "positions": snapshot,
                    "total_profit": sum(p["profit"] + p["swap"] for p in snapshot),
                })
        except WebSocketDisconnect:
            pass
        finally:
            store.unsubscribe(queue)

    @app.websocket("/ws/account")
    async def ws_account(ws: WebSocket):
        await ws.accept()
        queue = account_store.subscribe()
        try:
            await ws.send_json(_account_response(store, account_store))
            while True:
                await queue.get()
                await ws.send_json(_account_response(store, account_store))
        except WebSocketDisconnect:
            pass
        finally:
            account_store.unsubscribe(queue)

    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
    return app
