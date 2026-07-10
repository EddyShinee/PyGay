"""Dashboard web layer: REST API + WebSocket, served on top of the same
asyncio loop as the MT5 socket server (see main.py).

Every trading route (except the accounts list) is scoped to one MT5
account via an `{account_id}` path segment, resolved through
SessionManager. Kept separate from socket_server/handlers on purpose:
this file only knows about SessionManager/AccountSession, never about the
raw EA protocol.

All of that is gated behind a login (python/auth.py) - a separate concept
from MT5 accounts: web users are people allowed to open this dashboard
(stored in Supabase Auth), MT5 accounts are the trading accounts it manages.
"""
import asyncio
import logging
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel

from session_manager import SessionManager, AccountSession
import auth
import history
import telegram_notify
import db

logger = logging.getLogger("web")

STATIC_DIR = Path(__file__).parent / "static"


def require_login(request: Request) -> None:
    if not request.session.get("user_id"):
        raise HTTPException(401, "Chưa đăng nhập")


def _read_static(name: str) -> HTMLResponse:
    return HTMLResponse((STATIC_DIR / name).read_text(encoding="utf-8"))


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


class HistoryFetchRequest(BaseModel):
    symbol: str
    timeframe: Literal["M1", "M5", "M15", "M30", "H1", "H4", "D1", "W1", "MN1"] = "M1"
    count: int = 1000


class TelegramConfigRequest(BaseModel):
    bot_token: str
    chat_id: str


class RegisterRequest(BaseModel):
    username: str
    password: str


class LoginRequest(BaseModel):
    username: str
    password: str


def _account_response(session: AccountSession) -> dict:
    positions = session.store.snapshot()
    buy = [p for p in positions if p["side"] == "BUY"]
    sell = [p for p in positions if p["side"] == "SELL"]
    return {
        **session.account_store.snapshot(),
        "account_id": session.account_id,
        "broker": session.info.get("broker", ""),
        "name": session.info.get("name", ""),
        "connected": session.connected,
        "floating_profit": session.store.total_profit(),
        "open_count": len(positions),
        "open_buy_count": len(buy),
        "open_sell_count": len(sell),
        "open_buy_volume": round(sum(p["volume"] for p in buy), 2),
        "open_sell_volume": round(sum(p["volume"] for p in sell), 2),
    }


def create_app(sessions: SessionManager) -> FastAPI:
    app = FastAPI(title="MT5 Dashboard")
    # Every trading route below requires a logged-in web user - grouped in
    # one router with a shared dependency instead of repeating Depends()
    # on ~13 routes individually.
    protected = APIRouter(dependencies=[Depends(require_login)])

    def get_session(account_id: str) -> AccountSession:
        session = sessions.get(account_id)
        if session is None:
            raise HTTPException(404, f"Chưa biết tài khoản {account_id} (chưa từng kết nối)")
        return session

    def require_connected(session: AccountSession) -> None:
        if not session.connected:
            raise HTTPException(409, f"Tài khoản {session.account_id} hiện đang offline")

    @protected.get("/api/accounts")
    async def get_accounts():
        return {"accounts": sessions.list_accounts()}

    @protected.get("/api/{account_id}/positions")
    async def get_positions(account_id: str):
        session = get_session(account_id)
        return {"positions": session.store.snapshot(), "total_profit": session.store.total_profit()}

    @protected.get("/api/{account_id}/account")
    async def get_account(account_id: str):
        return _account_response(get_session(account_id))

    @protected.get("/api/{account_id}/symbols")
    async def get_symbols(account_id: str):
        return {"symbols": get_session(account_id).symbol_store.snapshot()}

    @protected.get("/api/{account_id}/insights")
    async def get_insights(account_id: str, bucket: Literal["minute", "hour", "day", "month", "year"] = "day",
                            limit: int = 30):
        get_session(account_id)  # 404 if unknown
        return {"bucket": bucket, "rows": db.insights(account_id, bucket, limit)}

    @protected.get("/api/{account_id}/summary")
    async def get_summary(account_id: str):
        get_session(account_id)
        return db.summary(account_id)

    @protected.get("/api/{account_id}/jobs")
    async def get_jobs(account_id: str):
        return {"jobs": get_session(account_id).grid_manager.active_jobs()}

    @protected.post("/api/{account_id}/positions/refresh")
    async def refresh_positions(account_id: str):
        session = get_session(account_id)
        require_connected(session)
        await session.gateway.request_positions()
        return {"ok": True}

    @protected.post("/api/{account_id}/orders")
    async def place_order(account_id: str, req: OrderRequest):
        session = get_session(account_id)
        require_connected(session)
        cached = session.price_cache.get(req.symbol)
        if cached is None:
            raise HTTPException(400, f"Chưa có dữ liệu giá cho {req.symbol}, chờ EA gửi tick trước.")
        price = cached["ask"] if req.side == "BUY" else cached["bid"]
        result = await session.grid_manager.start_job(
            symbol=req.symbol, side=req.side, volume=req.volume,
            sl_points=req.sl_points, tp_points=req.tp_points, count=req.count,
            spacing_points=req.spacing_points, direction=req.direction,
            delay_seconds=req.delay_seconds, lot_mode=req.lot_mode, lot_value=req.lot_value,
            price=price, point=cached["point"],
        )
        if not result.get("ok"):
            raise HTTPException(400, result.get("error", "order failed"))
        return result

    @protected.post("/api/{account_id}/positions/{ticket}/close")
    async def close_position(account_id: str, ticket: int):
        session = get_session(account_id)
        require_connected(session)
        result = await session.gateway.close_position(ticket)
        if not result.get("ok"):
            raise HTTPException(400, result.get("error", "close failed"))
        return result

    @protected.post("/api/{account_id}/positions/close_all")
    async def close_all(account_id: str, req: CloseAllRequest):
        session = get_session(account_id)
        require_connected(session)
        result = await session.gateway.close_all(req.filter)
        if not result.get("ok"):
            raise HTTPException(400, result.get("error", "close_all failed"))
        return result

    @protected.post("/api/{account_id}/positions/close_by_threshold")
    async def close_by_threshold(account_id: str, req: CloseByThresholdRequest):
        session = get_session(account_id)
        total = session.store.total_profit()
        triggered = total >= req.amount if req.op == ">=" else total <= req.amount
        if not triggered:
            return {"triggered": False, "total_profit": total}
        require_connected(session)
        result = await session.gateway.close_all("all")
        result["triggered"] = True
        result["total_profit"] = total
        return result

    @protected.post("/api/{account_id}/positions/{ticket}/modify")
    async def modify_position(account_id: str, ticket: int, req: ModifyRequest):
        session = get_session(account_id)
        require_connected(session)
        result = await session.gateway.modify_position(ticket, req.sl, req.tp)
        if not result.get("ok"):
            raise HTTPException(400, result.get("error", "modify failed"))
        return result

    @protected.post("/api/{account_id}/magic")
    async def set_magic(account_id: str, req: MagicRequest):
        session = get_session(account_id)
        require_connected(session)
        result = await session.gateway.set_magic(req.magic)
        if not result.get("ok"):
            raise HTTPException(400, result.get("error", "set magic failed"))
        return result

    @protected.get("/api/{account_id}/telegram")
    async def get_telegram(account_id: str):
        get_session(account_id)  # 404 if unknown
        config = db.get_telegram_config(account_id)
        return {"configured": config is not None, "chat_id": config["chat_id"] if config else None}

    @protected.post("/api/{account_id}/telegram")
    async def set_telegram(account_id: str, req: TelegramConfigRequest):
        get_session(account_id)
        if not req.bot_token.strip() or not req.chat_id.strip():
            raise HTTPException(400, "Cần nhập cả Bot Token và Chat ID")
        db.set_telegram_config(account_id, req.bot_token.strip(), req.chat_id.strip())
        return {"ok": True}

    @protected.post("/api/{account_id}/telegram/test")
    async def test_telegram(account_id: str):
        get_session(account_id)
        config = db.get_telegram_config(account_id)
        if config is None:
            raise HTTPException(400, "Chưa cấu hình Telegram cho tài khoản này")
        try:
            await telegram_notify.send_test(config["bot_token"], config["chat_id"])
        except Exception as exc:
            raise HTTPException(400, f"Gửi thất bại: {exc}")
        return {"ok": True}

    @protected.post("/api/{account_id}/history/fetch")
    async def fetch_history(account_id: str, req: HistoryFetchRequest):
        """Pull `count` bars from the EA and append new ones to CSV. Meant to
        be called by an external cron job (see tools/fetch_history_cron.py) -
        this process must already be running since it owns the live EA link."""
        session = get_session(account_id)
        require_connected(session)
        try:
            bars = await session.history_gateway.fetch(req.symbol, req.timeframe, req.count)
        except (RuntimeError, asyncio.TimeoutError) as exc:
            raise HTTPException(400, str(exc))
        saved = history.append_bars(account_id, req.symbol, req.timeframe, bars)
        return {"symbol": req.symbol, "timeframe": req.timeframe, "fetched": len(bars), "saved": saved}

    app.include_router(protected)

    # --- Auth: not behind require_login, obviously ---

    @app.post("/api/auth/register")
    async def register(req: RegisterRequest, request: Request):
        try:
            user = auth.create_user(req.username, req.password)
        except auth.AuthConfigError as exc:
            raise HTTPException(500, str(exc))
        except auth.AuthUnavailable as exc:
            raise HTTPException(503, str(exc))
        except (auth.UsernameTaken, auth.InvalidPassword) as exc:
            raise HTTPException(400, str(exc))
        request.session["user_id"] = user.id
        request.session["username"] = user.username
        return {"username": user.username}

    @app.post("/api/auth/login")
    async def login(req: LoginRequest, request: Request):
        try:
            user = auth.verify_user(req.username, req.password)
        except auth.AuthConfigError as exc:
            raise HTTPException(500, str(exc))
        except auth.AuthUnavailable as exc:
            raise HTTPException(503, str(exc))
        if user is None:
            raise HTTPException(401, "Sai tên đăng nhập hoặc mật khẩu")
        request.session["user_id"] = user.id
        request.session["username"] = user.username
        return {"username": user.username}

    @app.post("/api/auth/logout")
    async def logout(request: Request):
        request.session.clear()
        return {"ok": True}

    @app.get("/api/auth/me")
    async def me(request: Request):
        user_id = request.session.get("user_id")
        if not user_id:
            raise HTTPException(401, "Chưa đăng nhập")
        return {
            "username": auth.get_username(
                user_id, request.session.get("username")
            ),
        }

    # --- Static pages ---

    @app.get("/login")
    async def login_page(request: Request):
        if request.session.get("user_id"):
            return RedirectResponse("/")
        return _read_static("login.html")

    @app.get("/register")
    async def register_page(request: Request):
        if request.session.get("user_id"):
            return RedirectResponse("/")
        return _read_static("register.html")

    @app.get("/")
    async def index_page(request: Request):
        if not request.session.get("user_id"):
            return RedirectResponse("/login")
        return _read_static("index.html")

    @app.websocket("/ws/accounts")
    async def ws_accounts(ws: WebSocket):
        if not ws.session.get("user_id"):
            await ws.close(code=4401)
            return
        await ws.accept()
        queue = sessions.subscribe()
        try:
            await ws.send_json(sessions.list_accounts())
            while True:
                accounts = await queue.get()
                await ws.send_json(accounts)
        except WebSocketDisconnect:
            pass
        finally:
            sessions.unsubscribe(queue)

    @app.websocket("/ws/{account_id}/positions")
    async def ws_positions(ws: WebSocket, account_id: str):
        if not ws.session.get("user_id"):
            await ws.close(code=4401)
            return
        session = sessions.get(account_id)
        if session is None:
            await ws.close(code=4404)
            return
        await ws.accept()
        queue = session.store.subscribe()
        try:
            await ws.send_json({"positions": session.store.snapshot(), "total_profit": session.store.total_profit()})
            while True:
                snapshot = await queue.get()
                await ws.send_json({
                    "positions": snapshot,
                    "total_profit": sum(p["profit"] + p["swap"] for p in snapshot),
                })
        except WebSocketDisconnect:
            pass
        finally:
            session.store.unsubscribe(queue)

    @app.websocket("/ws/{account_id}/account")
    async def ws_account(ws: WebSocket, account_id: str):
        if not ws.session.get("user_id"):
            await ws.close(code=4401)
            return
        session = sessions.get(account_id)
        if session is None:
            await ws.close(code=4404)
            return
        await ws.accept()
        queue = session.account_store.subscribe()
        try:
            await ws.send_json(_account_response(session))
            while True:
                await queue.get()
                await ws.send_json(_account_response(session))
        except WebSocketDisconnect:
            pass
        finally:
            session.account_store.unsubscribe(queue)

    @app.websocket("/ws/{account_id}/symbols")
    async def ws_symbols(ws: WebSocket, account_id: str):
        if not ws.session.get("user_id"):
            await ws.close(code=4401)
            return
        session = sessions.get(account_id)
        if session is None:
            await ws.close(code=4404)
            return
        await ws.accept()
        queue = session.symbol_store.subscribe()
        try:
            await ws.send_json(session.symbol_store.snapshot())
            while True:
                snapshot = await queue.get()
                await ws.send_json(snapshot)
        except WebSocketDisconnect:
            pass
        finally:
            session.symbol_store.unsubscribe(queue)

    return app
