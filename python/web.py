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
from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field

from session_manager import SessionManager, AccountSession
import account_links
import account_risk
import account_entry
import account_manage
import backtest as backtest_mod
import ml_entry
import auth
import history
import telegram_notify
import db
from position_close import close_matching, refresh_snapshot

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
    volume: float = Field(..., gt=0)
    sl_points: float = Field(0, ge=0)
    tp_points: float = Field(0, ge=0)
    count: int = Field(1, ge=1, le=50)
    spacing_points: float = Field(0, ge=0)
    direction: Literal["against", "with"] = "against"
    delay_seconds: float = Field(0, ge=0)
    lot_mode: Literal["none", "add", "multiply"] = "none"
    lot_value: float = 0


class ModifyRequest(BaseModel):
    sl: float = 0
    tp: float = 0


class CloseAllRequest(BaseModel):
    filter: Literal["all", "profit", "loss"] = "all"
    symbol: str = ""  # optional: close only this symbol's positions
    side: str = ""    # optional: "BUY" | "SELL"


class CloseByThresholdRequest(BaseModel):
    op: Literal[">=", "<="]
    amount: float


class MagicRequest(BaseModel):
    magic: int


class HistoryFetchRequest(BaseModel):
    symbol: str
    timeframe: Literal["M1", "M5", "M15", "M30", "H1", "H4", "D1", "W1", "MN1"] = "M1"
    count: int = Field(1000, ge=1, le=20000)


class TelegramConfigRequest(BaseModel):
    bot_token: str
    chat_id: str
    trade_symbol: str = ""
    trade_lot: float = 0.01


class RiskConfigRequest(BaseModel):
    enabled: bool = False
    account_action: Literal["all", "matching"] = "all"
    cooldown_seconds: float = 5.0
    account_tp_usd: Optional[float] = None
    account_sl_usd: Optional[float] = None
    account_tp_pct: Optional[float] = None
    account_sl_pct: Optional[float] = None
    equity_floor: Optional[float] = None
    equity_ceiling: Optional[float] = None
    max_drawdown_pct: Optional[float] = None
    min_margin_level_pct: Optional[float] = None
    daily_profit_target: Optional[float] = None
    daily_loss_limit: Optional[float] = None
    account_trailing_arm_usd: Optional[float] = None
    account_trailing_giveback_usd: Optional[float] = None
    account_trailing_arm_pct: Optional[float] = None
    account_trailing_giveback_pct: Optional[float] = None
    max_positions: Optional[int] = None
    max_total_lot: Optional[float] = None
    close_time: Optional[str] = None
    close_before_weekend: bool = False
    symbol_tp_usd: Optional[float] = None
    symbol_sl_usd: Optional[float] = None
    symbol_rules: list[dict[str, Any]] = []
    trade_tp_usd: Optional[float] = None
    trade_sl_usd: Optional[float] = None
    sltp_unit: Literal["points", "pips"] = "points"
    trade_tp_pips: Optional[float] = None
    trade_sl_pips: Optional[float] = None
    atr_enabled: bool = False
    atr_timeframe: str = "H1"
    atr_period: int = 14
    atr_sl_multiplier: float = 1.5
    atr_tp_multiplier: float = 3.0
    trade_trailing_arm_pips: Optional[float] = None
    trade_trailing_distance_pips: Optional[float] = None
    breakeven_arm_pips: Optional[float] = None
    breakeven_buffer_pips: float = 0.0
    max_hold_minutes: Optional[int] = None


class EntryConfigRequest(BaseModel):
    enabled: bool = False
    symbol: str = "XAUUSD"  # legacy single symbol (kept for back-compat)
    symbols: list[str] = []  # preferred: list of symbols to scan
    side: Literal["BUY", "SELL", "BOTH"] = "BUY"
    volume: float = Field(0.01, gt=0)
    sltp_unit: Literal["points", "pips"] = "points"
    sl_distance: Optional[float] = Field(None, ge=0)
    tp_distance: Optional[float] = Field(None, ge=0)
    cooldown_seconds: float = Field(60.0, ge=0)
    max_open_positions: Optional[int] = Field(None, ge=0)
    max_entries_per_day: Optional[int] = Field(None, ge=0)
    only_if_flat: bool = False
    max_spread_points: Optional[float] = Field(None, ge=0)
    # {"enabled": bool, "timeframe": "H4", "ema_period": 200}
    trend_filter: dict[str, Any] = {}
    trade_hours: Optional[str] = None  # "HH:MM-HH:MM" local, None = 24/7
    trigger_mode: Literal[
        "schedule", "price_above", "price_below", "interval",
        "indicators", "ml", "indicators_ml"
    ] = "schedule"
    schedule_time: Optional[str] = None
    price_trigger: Optional[float] = None
    interval_minutes: Optional[int] = Field(None, ge=0)
    indicator_timeframe: str = "H1"
    indicator_logic: Literal["all", "any", "majority", "threshold"] = "all"
    indicator_min_agree: int = Field(2, ge=1)
    indicator_min_margin: int = Field(1, ge=1)
    confirm_bars: int = Field(1, ge=1)
    indicators: dict[str, Any] = {}
    ml: dict[str, Any] = {}


class BacktestRequest(BaseModel):
    config: dict[str, Any] = {}
    symbol: Optional[str] = None  # default: first symbol of the config
    bars_count: int = 1500


class MLTrainRequest(BaseModel):
    symbol: str = "XAUUSD"
    timeframe: str = "H1"
    # Upper bound matches the EA sending bars one socket message at a time -
    # tens of thousands risks the 30s history_gateway timeout.
    count: int = Field(3000, ge=200, le=20000)
    lookahead: int = Field(3, ge=1, le=50)
    lags: int = Field(5, ge=1, le=20)
    threshold: float = Field(0.58, gt=0.5, lt=1)
    epochs: int = Field(400, ge=10, le=5000)
    algo: Literal["logistic", "xgboost", "lightgbm"] = "xgboost"
    n_estimators: int = Field(400, ge=10, le=2000)
    max_depth: int = Field(4, ge=2, le=12)
    learning_rate: float = Field(0.05, gt=0, le=1)


class ManageConfigRequest(BaseModel):
    enabled: bool = False
    manage_magic: int = 0
    symbols: list[str] = []
    sltp_unit: Literal["points", "pips"] = "points"
    max_positions_per_basket: int = 10
    max_total_lot: float = 1.0
    max_lot_per_order: float = 1.0
    add_cooldown_seconds: float = 5.0
    # basket close
    basket_enabled: bool = False
    basket_tp_money: Optional[float] = None
    basket_sl_money: Optional[float] = None
    basket_tp_points: Optional[float] = None
    basket_sl_points: Optional[float] = None
    # DCA / Martingale
    dca_enabled: bool = False
    dca_step_points: float = 200.0
    dca_lot_mode: Literal["multiply", "add", "none"] = "multiply"
    dca_lot_value: float = 2.0
    dca_max_steps: int = 5
    # Grid
    grid_enabled: bool = False
    grid_step_points: float = 300.0
    grid_lot_mode: Literal["none", "add", "multiply"] = "none"
    grid_lot_value: float = 0.0
    grid_max_levels: int = 5
    # Pyramiding
    pyr_enabled: bool = False
    pyr_step_points: float = 200.0
    pyr_lot_mode: Literal["none", "add", "multiply"] = "none"
    pyr_lot_value: float = 0.0
    pyr_max_steps: int = 3
    pyr_trail_points: float = 0.0
    # Hedge
    hedge_enabled: bool = False
    hedge_dd_money: Optional[float] = None
    hedge_dd_points: Optional[float] = None
    hedge_lot_ratio: float = 1.0
    hedge_max_orders: int = 1
    hedge_step_points: float = 0.0


class RegisterRequest(BaseModel):
    username: str
    password: str


class LoginRequest(BaseModel):
    username: str
    password: str


class LinkAccountRequest(BaseModel):
    account_id: str
    linked_via: Literal["manual", "discovered"] = "manual"
    socket_host: str = "127.0.0.1"
    socket_port: int = 9090


class SocketUpdateRequest(BaseModel):
    socket_host: str = "127.0.0.1"
    socket_port: int = 9090


def get_current_user_id(request: Request) -> str:
    user_id = request.session.get("user_id")
    if not user_id:
        raise HTTPException(401, "Chưa đăng nhập")
    return str(user_id)


def _offline_account_summary(
    account_id: str,
    linked_via: str,
    socket_host: str = "127.0.0.1",
    socket_port: int = 9090,
) -> dict:
    return {
        "account_id": account_id,
        "connected": False,
        "broker": "",
        "name": "",
        "platform": "",
        "currency": "",
        "balance": None,
        "equity": None,
        "floating_profit": 0,
        "open_count": 0,
        "linked_via": linked_via,
        "socket_host": socket_host,
        "socket_port": socket_port,
    }


def _merge_link_meta(summary: dict, link: account_links.LinkedAccount) -> dict:
    summary["linked_via"] = link.linked_via
    summary["socket_host"] = link.socket_host
    summary["socket_port"] = link.socket_port
    return summary


def _build_accounts_for_user(user_id: str, sessions: SessionManager) -> list[dict]:
    linked = account_links.list_linked_accounts(user_id)
    accounts: list[dict] = []
    for link in linked:
        session = sessions.get(link.account_id)
        if session is not None:
            accounts.append(_merge_link_meta(session.summary(), link))
        else:
            accounts.append(
                _offline_account_summary(
                    link.account_id,
                    link.linked_via,
                    link.socket_host,
                    link.socket_port,
                )
            )
    return accounts


def _user_owns_account(user_id: str, account_id: str) -> bool:
    linked = account_links.list_linked_accounts(user_id)
    return any(link.account_id == account_id for link in linked)


def _account_link_http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, account_links.LinkConfigError):
        return HTTPException(500, str(exc))
    if isinstance(exc, account_links.LinkUnavailable):
        return HTTPException(503, str(exc))
    if isinstance(exc, account_links.AccountAlreadyLinked):
        return HTTPException(409, str(exc))
    if isinstance(exc, account_links.InvalidAccountId):
        return HTTPException(400, str(exc))
    if isinstance(exc, account_links.AccountNotLinked):
        return HTTPException(404, str(exc))
    return HTTPException(400, str(exc))


def _account_response(session: AccountSession) -> dict:
    positions = session.store.snapshot()
    buy = [p for p in positions if p["side"] == "BUY"]
    sell = [p for p in positions if p["side"] == "SELL"]
    return {
        **session.account_store.snapshot(),
        "account_id": session.account_id,
        "broker": session.info.get("broker", ""),
        "name": session.info.get("name", ""),
        "platform": session.info.get("platform", ""),
        "connected": session.connected,
        "floating_profit": session.store.total_profit(),
        "open_count": len(positions),
        "open_buy_count": len(buy),
        "open_sell_count": len(sell),
        "open_buy_volume": round(sum(p["volume"] for p in buy), 2),
        "open_sell_volume": round(sum(p["volume"] for p in sell), 2),
    }


def create_app(sessions: SessionManager) -> FastAPI:
    app = FastAPI(title="MetaTrader Dashboard")
    # Every trading route below requires a logged-in web user - grouped in
    # one router with a shared dependency instead of repeating Depends()
    # on ~13 routes individually.
    protected = APIRouter(dependencies=[Depends(require_login)])

    def require_account_access(request: Request, account_id: str) -> None:
        user_id = get_current_user_id(request)
        try:
            if not _user_owns_account(user_id, account_id):
                raise HTTPException(
                    403, f"Bạn chưa gắn tài khoản MetaTrader #{account_id}"
                )
        except account_links.LinkConfigError as exc:
            raise HTTPException(500, str(exc))
        except account_links.LinkUnavailable as exc:
            raise HTTPException(503, str(exc))

    def get_session(account_id: str, request: Request) -> AccountSession:
        require_account_access(request, account_id)
        session = sessions.get(account_id)
        if session is None:
            raise HTTPException(
                404, f"Tài khoản {account_id} chưa từng kết nối EA"
            )
        return session

    def require_connected(session: AccountSession) -> None:
        if not session.connected:
            raise HTTPException(409, f"Tài khoản {session.account_id} hiện đang offline")

    @protected.get("/api/accounts")
    async def get_accounts(request: Request):
        user_id = get_current_user_id(request)
        try:
            return {"accounts": _build_accounts_for_user(user_id, sessions)}
        except account_links.LinkConfigError as exc:
            raise HTTPException(500, str(exc))
        except account_links.LinkUnavailable as exc:
            raise HTTPException(503, str(exc))

    @protected.get("/api/accounts/pending")
    async def get_pending_accounts(request: Request):
        get_current_user_id(request)
        try:
            claimed = account_links.list_claimed_account_ids()
        except account_links.LinkConfigError as exc:
            raise HTTPException(500, str(exc))
        except account_links.LinkUnavailable as exc:
            raise HTTPException(503, str(exc))
        pending = [
            session.summary()
            for account_id, session in sessions.sessions.items()
            if account_id not in claimed
        ]
        return {"pending": pending}

    @protected.post("/api/accounts/link")
    async def link_account(req: LinkAccountRequest, request: Request):
        user_id = get_current_user_id(request)
        host = (req.socket_host or "127.0.0.1").strip() or "127.0.0.1"
        port = req.socket_port if 1 <= req.socket_port <= 65535 else 9090
        try:
            link = account_links.link_account(
                user_id, req.account_id, req.linked_via, host, port
            )
        except (
            account_links.LinkConfigError,
            account_links.LinkUnavailable,
            account_links.AccountAlreadyLinked,
            account_links.InvalidAccountId,
        ) as exc:
            raise _account_link_http_error(exc)
        return {
            "account_id": link.account_id,
            "linked_via": link.linked_via,
            "socket_host": link.socket_host,
            "socket_port": link.socket_port,
        }

    @protected.put("/api/accounts/{account_id}/socket")
    async def update_account_socket(
        account_id: str, req: SocketUpdateRequest, request: Request
    ):
        user_id = get_current_user_id(request)
        require_account_access(request, account_id)
        host = (req.socket_host or "127.0.0.1").strip() or "127.0.0.1"
        port = req.socket_port if 1 <= req.socket_port <= 65535 else 9090
        try:
            link = account_links.update_account_socket(
                user_id, account_id, host, port
            )
        except (
            account_links.LinkConfigError,
            account_links.LinkUnavailable,
            account_links.AccountNotLinked,
            account_links.InvalidAccountId,
        ) as exc:
            raise _account_link_http_error(exc)
        return {
            "account_id": link.account_id,
            "socket_host": link.socket_host,
            "socket_port": link.socket_port,
        }

    @protected.delete("/api/accounts/{account_id}/link")
    async def unlink_account(account_id: str, request: Request):
        user_id = get_current_user_id(request)
        try:
            removed = account_links.unlink_account(user_id, account_id)
        except account_links.LinkConfigError as exc:
            raise HTTPException(500, str(exc))
        except account_links.LinkUnavailable as exc:
            raise HTTPException(503, str(exc))
        if not removed:
            raise HTTPException(404, f"Chưa gắn tài khoản MetaTrader #{account_id}")
        return {"ok": True}

    @protected.get("/api/{account_id}/positions")
    async def get_positions(account_id: str, request: Request):
        session = get_session(account_id, request)
        return {"positions": session.store.snapshot(), "total_profit": session.store.total_profit()}

    @protected.get("/api/{account_id}/account")
    async def get_account(account_id: str, request: Request):
        return _account_response(get_session(account_id, request))

    @protected.get("/api/{account_id}/symbols")
    async def get_symbols(account_id: str, request: Request):
        return {"symbols": get_session(account_id, request).symbol_store.snapshot()}

    @protected.get("/api/{account_id}/insights")
    async def get_insights(account_id: str, request: Request,
                            bucket: Literal["minute", "hour", "day", "month", "year"] = "day",
                            limit: int = 30):
        require_account_access(request, account_id)
        return {"bucket": bucket, "rows": db.insights(account_id, bucket, limit)}

    @protected.get("/api/{account_id}/summary")
    async def get_summary(account_id: str, request: Request):
        require_account_access(request, account_id)
        return db.summary(account_id)

    @protected.get("/api/{account_id}/jobs")
    async def get_jobs(account_id: str, request: Request):
        return {"jobs": get_session(account_id, request).grid_manager.active_jobs()}

    @protected.post("/api/{account_id}/positions/refresh")
    async def refresh_positions(account_id: str, request: Request):
        session = get_session(account_id, request)
        require_connected(session)
        await session.gateway.request_positions()
        return {"ok": True}

    @protected.post("/api/{account_id}/orders")
    async def place_order(account_id: str, req: OrderRequest, request: Request):
        session = get_session(account_id, request)
        require_connected(session)
        cached = session.price_cache.get(req.symbol)
        if cached is None:
            # Symbol not streamed yet: ask the EA to add it to the Market Watch
            # so it starts quoting, then have the user retry in a moment.
            await session.gateway.watch_symbol(req.symbol)
            raise HTTPException(
                400,
                f"Đang thêm {req.symbol} vào Market Watch, chờ EA gửi giá rồi thử lại sau 1-2 giây.",
            )
        price = cached["ask"] if req.side == "BUY" else cached["bid"]
        result = await session.grid_manager.start_job(
            symbol=req.symbol, side=req.side, volume=req.volume,
            sl_points=req.sl_points, tp_points=req.tp_points, count=req.count,
            spacing_points=req.spacing_points, direction=req.direction,
            delay_seconds=req.delay_seconds, lot_mode=req.lot_mode, lot_value=req.lot_value,
            price=price, point=cached["point"],
            comment_label="Manual" if req.count <= 1 else "Grid",
        )
        if not result.get("ok"):
            raise HTTPException(400, result.get("error", "order failed"))
        return result

    @protected.post("/api/{account_id}/positions/{ticket}/close")
    async def close_position(account_id: str, ticket: int, request: Request):
        session = get_session(account_id, request)
        require_connected(session)
        result = await session.gateway.close_position(ticket)
        if not result.get("ok"):
            raise HTTPException(400, result.get("error", "close failed"))
        return result

    @protected.post("/api/{account_id}/positions/close_all")
    async def close_all(account_id: str, req: CloseAllRequest, request: Request):
        """Close matching open positions ticket-by-ticket.

        Uses the same `close_position` path as the per-row Đóng button instead of
        the EA's bulk `close_all` (which was returning ok without closing on some
        brokers / MT builds). Filter is applied server-side from the live store.
        """
        session = get_session(account_id, request)
        require_connected(session)

        result = await close_matching(
            session,
            filter=req.filter,
            symbol=(req.symbol or "").strip().upper(),
            side=(req.side or "").strip().upper(),
        )
        if result.get("matched", 0) > 0 and result.get("closed_count", 0) == 0 and result.get("failed"):
            raise HTTPException(400, result["failed"][0]["error"])
        return result

    @protected.post("/api/{account_id}/positions/close_by_threshold")
    async def close_by_threshold(account_id: str, req: CloseByThresholdRequest, request: Request):
        session = get_session(account_id, request)
        await refresh_snapshot(session)
        # Evaluate the threshold PER SYMBOL + SIDE on a fresh snapshot.
        per_basket = session.store.totals_by_basket()
        total = session.store.total_profit()
        triggered = [
            {"symbol": sym, "side": side, "profit": round(pnl, 2)}
            for (sym, side), pnl in per_basket.items()
            if (pnl >= req.amount if req.op == ">=" else pnl <= req.amount)
        ]
        per_basket_out = [
            {"symbol": sym, "side": side, "profit": round(pnl, 2)}
            for (sym, side), pnl in sorted(per_basket.items(), key=lambda x: (x[0][0], x[0][1]))
        ]
        if not triggered:
            return {
                "triggered": False,
                "total_profit": round(total, 2),
                "per_basket": per_basket_out,
            }
        require_connected(session)
        results = []
        for item in triggered:
            res = await close_matching(
                session,
                filter="all",
                symbol=item["symbol"],
                side=item["side"],
                refresh=False,
            )
            results.append({
                **item,
                "ok": bool(res.get("ok")),
                "closed_count": res.get("closed_count", 0),
                "error": res.get("error") or "",
            })
        return {
            "triggered": True,
            "total_profit": round(total, 2),
            "closed": results,
        }

    @protected.post("/api/{account_id}/positions/{ticket}/modify")
    async def modify_position(account_id: str, ticket: int, req: ModifyRequest, request: Request):
        session = get_session(account_id, request)
        require_connected(session)
        result = await session.gateway.modify_position(ticket, req.sl, req.tp)
        if not result.get("ok"):
            raise HTTPException(400, result.get("error", "modify failed"))
        return result

    @protected.post("/api/{account_id}/magic")
    async def set_magic(account_id: str, req: MagicRequest, request: Request):
        session = get_session(account_id, request)
        require_connected(session)
        result = await session.gateway.set_magic(req.magic)
        if not result.get("ok"):
            raise HTTPException(400, result.get("error", "set magic failed"))
        return result

    @protected.get("/api/{account_id}/telegram")
    async def get_telegram(account_id: str, request: Request):
        require_account_access(request, account_id)
        config = db.get_telegram_config(account_id)
        if config is None:
            return {"configured": False, "chat_id": None, "trade_symbol": "", "trade_lot": 0.01}
        return {
            "configured": True,
            "chat_id": config["chat_id"],
            "trade_symbol": config.get("trade_symbol") or "",
            "trade_lot": config.get("trade_lot") or 0.01,
        }

    @protected.post("/api/{account_id}/telegram")
    async def set_telegram(account_id: str, req: TelegramConfigRequest, request: Request):
        require_account_access(request, account_id)
        if not req.bot_token.strip() or not req.chat_id.strip():
            raise HTTPException(400, "Cần nhập cả Bot Token và Chat ID")
        existing = db.get_telegram_config(account_id)
        symbol = req.trade_symbol.strip().upper() if req.trade_symbol.strip() else (
            (existing or {}).get("trade_symbol") or ""
        )
        lot = req.trade_lot if req.trade_lot > 0 else (existing or {}).get("trade_lot") or 0.01
        db.set_telegram_config(
            account_id,
            req.bot_token.strip(),
            req.chat_id.strip(),
            trade_symbol=symbol or None,
            trade_lot=lot,
        )
        try:
            await telegram_notify.setup_trade_keyboard(
                req.bot_token.strip(),
                req.chat_id.strip(),
                symbol or "XAUUSD",
            )
        except Exception as exc:
            raise HTTPException(400, f"Đã lưu nhưng gửi bàn phím Telegram thất bại: {exc}")
        return {"ok": True}

    @protected.delete("/api/{account_id}/telegram")
    async def delete_telegram(account_id: str, request: Request):
        require_account_access(request, account_id)
        config = db.get_telegram_config(account_id)
        if config is None:
            return {"ok": True, "removed": False}
        try:
            await telegram_notify.remove_trade_keyboard(
                config["bot_token"], config["chat_id"]
            )
        except Exception:
            pass
        removed = db.clear_telegram_config(account_id)
        return {"ok": True, "removed": removed}

    @protected.post("/api/{account_id}/telegram/test")
    async def test_telegram(account_id: str, request: Request):
        require_account_access(request, account_id)
        config = db.get_telegram_config(account_id)
        if config is None:
            raise HTTPException(400, "Chưa cấu hình Telegram cho tài khoản này")
        try:
            await telegram_notify.send_test(
                config["bot_token"],
                config["chat_id"],
                config.get("trade_symbol") or "XAUUSD",
            )
        except Exception as exc:
            raise HTTPException(400, f"Gửi thất bại: {exc}")
        return {"ok": True}

    @protected.get("/api/{account_id}/risk")
    async def get_risk(account_id: str, request: Request):
        require_account_access(request, account_id)
        try:
            row = await asyncio.to_thread(account_risk.get_risk_config, account_id)
        except account_risk.RiskConfigError as exc:
            raise HTTPException(500, str(exc))
        except account_risk.RiskUnavailable as exc:
            raise HTTPException(503, str(exc))
        session = sessions.get(account_id)
        status = session.risk_manager.status() if session else {"enabled": False}
        if row is None:
            return {"configured": False, "config": {}, "status": status}
        return {
            "configured": True,
            "enabled": row["enabled"],
            "config": row["config"],
            "updated_at": row["updated_at"],
            "status": status,
        }

    @protected.post("/api/{account_id}/risk")
    async def set_risk(account_id: str, req: RiskConfigRequest, request: Request):
        require_account_access(request, account_id)
        config = req.model_dump()
        enabled = bool(config.pop("enabled", False))
        try:
            await asyncio.to_thread(account_risk.set_risk_config, account_id, enabled, config)
        except account_risk.RiskConfigError as exc:
            raise HTTPException(400, str(exc))
        except account_risk.RiskUnavailable as exc:
            raise HTTPException(503, str(exc))
        session = sessions.get(account_id)
        if session is not None:
            await session.risk_manager.reload_config()
            status = session.risk_manager.status()
        else:
            status = {"enabled": enabled}
        return {"ok": True, "status": status}

    @protected.delete("/api/{account_id}/risk")
    async def delete_risk(account_id: str, request: Request):
        require_account_access(request, account_id)
        try:
            removed = await asyncio.to_thread(account_risk.clear_risk_config, account_id)
        except account_risk.RiskConfigError as exc:
            raise HTTPException(500, str(exc))
        except account_risk.RiskUnavailable as exc:
            raise HTTPException(503, str(exc))
        session = sessions.get(account_id)
        if session is not None:
            await session.risk_manager.reload_config()
        return {"ok": True, "removed": removed}

    @protected.get("/api/{account_id}/entry")
    async def get_entry(account_id: str, request: Request):
        require_account_access(request, account_id)
        try:
            row = await asyncio.to_thread(account_entry.get_entry_config, account_id)
        except account_entry.EntryConfigError as exc:
            raise HTTPException(500, str(exc))
        except account_entry.EntryUnavailable as exc:
            raise HTTPException(503, str(exc))
        session = sessions.get(account_id)
        status = session.entry_manager.status() if session else {"enabled": False}
        if row is None:
            return {"configured": False, "config": {}, "status": status}
        return {
            "configured": True,
            "enabled": row["enabled"],
            "config": row["config"],
            "updated_at": row["updated_at"],
            "status": status,
        }

    @protected.post("/api/{account_id}/entry")
    async def set_entry(account_id: str, req: EntryConfigRequest, request: Request):
        require_account_access(request, account_id)
        config = req.model_dump()
        enabled = bool(config.pop("enabled", False))
        try:
            await asyncio.to_thread(account_entry.set_entry_config, account_id, enabled, config)
        except account_entry.EntryConfigError as exc:
            raise HTTPException(400, str(exc))
        except account_entry.EntryUnavailable as exc:
            raise HTTPException(503, str(exc))
        session = sessions.get(account_id)
        if session is not None:
            await session.entry_manager.reload_config()
            status = session.entry_manager.status()
        else:
            status = {"enabled": enabled}
        return {"ok": True, "status": status}

    @protected.delete("/api/{account_id}/entry")
    async def delete_entry(account_id: str, request: Request):
        require_account_access(request, account_id)
        try:
            removed = await asyncio.to_thread(account_entry.clear_entry_config, account_id)
        except account_entry.EntryConfigError as exc:
            raise HTTPException(500, str(exc))
        except account_entry.EntryUnavailable as exc:
            raise HTTPException(503, str(exc))
        session = sessions.get(account_id)
        if session is not None:
            await session.entry_manager.reload_config()
        return {"ok": True, "removed": removed}

    @protected.get("/api/{account_id}/manage")
    async def get_manage(account_id: str, request: Request):
        require_account_access(request, account_id)
        try:
            row = await asyncio.to_thread(account_manage.get_manage_config, account_id)
        except account_manage.ManageConfigError as exc:
            raise HTTPException(500, str(exc))
        except account_manage.ManageUnavailable as exc:
            raise HTTPException(503, str(exc))
        session = sessions.get(account_id)
        status = session.position_manager.status() if session else {"enabled": False}
        if row is None:
            return {"configured": False, "config": {}, "status": status}
        return {
            "configured": True,
            "enabled": row["enabled"],
            "config": row["config"],
            "updated_at": row["updated_at"],
            "status": status,
        }

    @protected.post("/api/{account_id}/manage")
    async def set_manage(account_id: str, req: ManageConfigRequest, request: Request):
        require_account_access(request, account_id)
        config = req.model_dump()
        enabled = bool(config.pop("enabled", False))
        try:
            await asyncio.to_thread(account_manage.set_manage_config, account_id, enabled, config)
        except account_manage.ManageConfigError as exc:
            raise HTTPException(400, str(exc))
        except account_manage.ManageUnavailable as exc:
            raise HTTPException(503, str(exc))
        session = sessions.get(account_id)
        if session is not None:
            await session.position_manager.reload_config()
            status = session.position_manager.status()
        else:
            status = {"enabled": enabled}
        return {"ok": True, "status": status}

    @protected.delete("/api/{account_id}/manage")
    async def delete_manage(account_id: str, request: Request):
        require_account_access(request, account_id)
        try:
            removed = await asyncio.to_thread(account_manage.clear_manage_config, account_id)
        except account_manage.ManageConfigError as exc:
            raise HTTPException(500, str(exc))
        except account_manage.ManageUnavailable as exc:
            raise HTTPException(503, str(exc))
        session = sessions.get(account_id)
        if session is not None:
            await session.position_manager.reload_config()
        return {"ok": True, "removed": removed}

    @protected.get("/api/{account_id}/entry/status")
    async def entry_status(account_id: str, request: Request):
        """Live EntryManager status only - no Supabase round-trip, safe to
        poll every few seconds from the UI."""
        require_account_access(request, account_id)
        session = sessions.get(account_id)
        if session is None:
            return {"enabled": False, "connected": False, "per_symbol": {}}
        status = session.entry_manager.status()
        status["connected"] = session.connected
        return status

    @protected.post("/api/{account_id}/entry/backtest")
    async def backtest_entry(account_id: str, req: BacktestRequest, request: Request):
        """Replay the given entry config over history fetched from the EA
        and report win rate / trades / profit factor."""
        session = get_session(account_id, request)
        require_connected(session)
        config = req.config or {}

        symbols = config.get("symbols") or ([config.get("symbol")] if config.get("symbol") else [])
        symbol = (req.symbol or (symbols[0] if symbols else "XAUUSD")).strip().upper()

        mode = (config.get("trigger_mode") or "").lower()
        if mode == "ml":
            tf = (config.get("ml") or {}).get("timeframe") or "H1"
        else:
            tf = config.get("indicator_timeframe") or "H1"

        count = max(200, min(int(req.bars_count or 1500), backtest_mod.MAX_BARS))
        try:
            bars = await session.history_gateway.fetch(symbol, tf, count)
        except (RuntimeError, asyncio.TimeoutError) as exc:
            raise HTTPException(400, f"Không lấy được dữ liệu lịch sử: {exc}")

        trend_bars = None
        tf_cfg = config.get("trend_filter") or {}
        if tf_cfg.get("enabled"):
            trend_tf = tf_cfg.get("timeframe") or "H4"
            try:
                trend_bars = await session.history_gateway.fetch(symbol, trend_tf, 500)
            except (RuntimeError, asyncio.TimeoutError):
                trend_bars = None  # filter fails open, same as live

        price = session.price_cache.get(symbol)
        point = float(price.get("point") or 0) if price else 0.0
        if point <= 0:
            raise HTTPException(400, f"Chưa có giá tick cho {symbol} — chờ EA stream giá rồi thử lại.")

        try:
            result = await asyncio.to_thread(
                backtest_mod.run_backtest, bars, config, point, trend_bars
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        result["symbol"] = symbol
        result["timeframe"] = tf
        return result

    @protected.post("/api/{account_id}/entry/ml/train")
    async def train_entry_ml(account_id: str, req: MLTrainRequest, request: Request):
        """Fetch history from the EA, train the selected ML model, and store
        it inside the account's entry config (Supabase)."""
        session = get_session(account_id, request)
        require_connected(session)
        try:
            bars = await session.history_gateway.fetch(req.symbol, req.timeframe, req.count)
        except (RuntimeError, asyncio.TimeoutError) as exc:
            raise HTTPException(400, f"Không lấy được dữ liệu lịch sử: {exc}")

        # Fetch the existing entry config first: if a trend filter is set up
        # (higher-timeframe EMA), reuse its timeframe as extra ML context -
        # same series the live trend filter and backtests already fetch.
        try:
            row = await asyncio.to_thread(account_entry.get_entry_config, account_id)
        except account_entry.EntryConfigError as exc:
            raise HTTPException(500, str(exc))
        except account_entry.EntryUnavailable as exc:
            raise HTTPException(503, str(exc))
        config = dict(row["config"]) if row else {}
        enabled = bool(row["enabled"]) if row else False

        htf_bars = None
        tf_cfg = config.get("trend_filter") or {}
        if tf_cfg.get("enabled"):
            htf_tf = tf_cfg.get("timeframe") or "H4"
            try:
                htf_bars = await session.history_gateway.fetch(req.symbol, htf_tf, 500)
            except (RuntimeError, asyncio.TimeoutError):
                htf_bars = None  # train without htf context rather than fail the whole request

        try:
            model = await asyncio.to_thread(
                ml_entry.train,
                bars,
                {
                    "algo": req.algo,
                    "lookahead": req.lookahead,
                    "lags": req.lags,
                    "epochs": req.epochs,
                    "n_estimators": req.n_estimators,
                    "max_depth": req.max_depth,
                    "learning_rate": req.learning_rate,
                },
                htf_bars,
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        model["timeframe"] = req.timeframe

        # Merge the trained model into the existing entry config's ml block.
        ml_block = dict(config.get("ml") or {})
        ml_block.update({
            "enabled": ml_block.get("enabled", True),
            "timeframe": req.timeframe,
            "threshold": req.threshold,
            "algo": req.algo,
            "model": model,
        })
        config["ml"] = ml_block
        try:
            await asyncio.to_thread(account_entry.set_entry_config, account_id, enabled, config)
        except account_entry.EntryConfigError as exc:
            raise HTTPException(400, str(exc))
        except account_entry.EntryUnavailable as exc:
            raise HTTPException(503, str(exc))
        if session is not None:
            await session.entry_manager.reload_config()
        return {
            "ok": True,
            "algo": model.get("algo"),
            "samples": model["samples"],
            "train_samples": model.get("train_samples"),
            "val_samples": model.get("val_samples"),
            "accuracy": model["accuracy"],
            "train_accuracy": model.get("train_accuracy"),
            "val_accuracy": model.get("val_accuracy"),
            "walkforward_accuracy": model.get("walkforward_accuracy"),
            "walkforward_folds": model.get("walkforward_folds"),
            "up_rate": model["up_rate"],
            "trained_at": model["trained_at"],
            "feature_names": model["feature_names"],
        }

    @protected.post("/api/{account_id}/history/fetch")
    async def fetch_history(account_id: str, req: HistoryFetchRequest, request: Request):
        """Pull `count` bars from the EA and append new ones to CSV. Meant to
        be called by an external cron job (see tools/fetch_history_cron.py) -
        this process must already be running since it owns the live EA link."""
        session = get_session(account_id, request)
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
        user_id = ws.session.get("user_id")
        if not user_id:
            await ws.close(code=4401)
            return
        user_id = str(user_id)
        await ws.accept()
        queue = sessions.subscribe()
        try:
            try:
                await ws.send_json(_build_accounts_for_user(user_id, sessions))
            except (account_links.LinkConfigError, account_links.LinkUnavailable):
                await ws.send_json([])
            while True:
                await queue.get()
                try:
                    await ws.send_json(_build_accounts_for_user(user_id, sessions))
                except (account_links.LinkConfigError, account_links.LinkUnavailable):
                    await ws.send_json([])
        except WebSocketDisconnect:
            pass
        finally:
            sessions.unsubscribe(queue)

    def _ws_account_allowed(user_id: str, account_id: str) -> bool:
        try:
            return _user_owns_account(user_id, account_id)
        except (account_links.LinkConfigError, account_links.LinkUnavailable):
            return False

    @app.websocket("/ws/{account_id}/positions")
    async def ws_positions(ws: WebSocket, account_id: str):
        user_id = ws.session.get("user_id")
        if not user_id:
            await ws.close(code=4401)
            return
        if not _ws_account_allowed(str(user_id), account_id):
            await ws.close(code=4403)
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
        user_id = ws.session.get("user_id")
        if not user_id:
            await ws.close(code=4401)
            return
        if not _ws_account_allowed(str(user_id), account_id):
            await ws.close(code=4403)
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
        user_id = ws.session.get("user_id")
        if not user_id:
            await ws.close(code=4401)
            return
        if not _ws_account_allowed(str(user_id), account_id):
            await ws.close(code=4403)
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
