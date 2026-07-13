"""Per-account risk supervisor: evaluates SL/TP rules on live snapshots and
executes closes/modifies via TradeGateway. Runs continuously via
run_risk_supervisor() and on-demand from handlers after account/position updates.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, fields
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Optional

import db
import telegram_notify
from grid_jobs import sl_tp_from_points

if TYPE_CHECKING:
    from session_manager import AccountSession

logger = logging.getLogger("risk_manager")

SUPERVISOR_INTERVAL_S = 1.0
ATR_CACHE_TTL_S = 300.0


def _utc_midnight_epoch() -> int:
    now = datetime.now(timezone.utc)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(midnight.timestamp())


def _position_pnl(p: dict) -> float:
    return float(p.get("profit", 0)) + float(p.get("swap", 0))


def compute_atr(bars: list[dict], period: int) -> float:
    """Wilder-style ATR from OHLC bars (sorted ascending by time)."""
    if period < 1 or len(bars) < period + 1:
        return 0.0
    sorted_bars = sorted(bars, key=lambda b: int(b["time"]))
    trs: list[float] = []
    for i in range(1, len(sorted_bars)):
        high = float(sorted_bars[i]["high"])
        low = float(sorted_bars[i]["low"])
        prev_close = float(sorted_bars[i - 1]["close"])
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)
    if len(trs) < period:
        return 0.0
    return sum(trs[-period:]) / period


@dataclass
class RiskConfig:
    enabled: bool = False
    account_action: str = "all"  # "all" | "matching"
    cooldown_seconds: float = 5.0

    # Account-level USD / %
    account_tp_usd: Optional[float] = None
    account_sl_usd: Optional[float] = None
    account_tp_pct: Optional[float] = None
    account_sl_pct: Optional[float] = None

    # Equity bounds
    equity_floor: Optional[float] = None
    equity_ceiling: Optional[float] = None

    # Drawdown & margin
    max_drawdown_pct: Optional[float] = None
    min_margin_level_pct: Optional[float] = None

    # Daily limits (USD, realized today + floating)
    daily_profit_target: Optional[float] = None
    daily_loss_limit: Optional[float] = None

    # Account trailing
    account_trailing_arm_usd: Optional[float] = None
    account_trailing_giveback_usd: Optional[float] = None
    account_trailing_arm_pct: Optional[float] = None
    account_trailing_giveback_pct: Optional[float] = None

    # Exposure
    max_positions: Optional[int] = None
    max_total_lot: Optional[float] = None

    # Time-based
    close_time: Optional[str] = None  # "HH:MM" local
    close_before_weekend: bool = False

    # Per-trade USD (server-monitored close)
    trade_tp_usd: Optional[float] = None
    trade_sl_usd: Optional[float] = None

    # Per-trade pip SL/TP on broker
    trade_tp_pips: Optional[float] = None
    trade_sl_pips: Optional[float] = None

    # ATR-based broker SL/TP
    atr_enabled: bool = False
    atr_timeframe: str = "H1"
    atr_period: int = 14
    atr_sl_multiplier: float = 1.5
    atr_tp_multiplier: float = 3.0

    # Per-trade trailing (broker modify)
    trade_trailing_arm_pips: Optional[float] = None
    trade_trailing_distance_pips: Optional[float] = None

    # Break-even
    breakeven_arm_pips: Optional[float] = None
    breakeven_buffer_pips: float = 0.0

    # Max hold time (minutes)
    max_hold_minutes: Optional[int] = None

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "RiskConfig":
        if not data:
            return cls()
        known = {f.name for f in fields(cls)}
        kwargs = {k: v for k, v in data.items() if k in known}
        return cls(**kwargs)

    def to_dict(self) -> dict:
        return {f.name: getattr(self, f.name) for f in fields(self)}


@dataclass
class _AtrCacheEntry:
    value: float
    fetched_at: float


class RiskManager:
    def __init__(self, session: "AccountSession"):
        self._session = session
        self.config = RiskConfig()
        self.enabled = False

        self._peak_profit: float = 0.0
        self._trailing_armed: bool = False
        self._trade_peaks: dict[int, float] = {}
        self._broker_sltp_applied: set[int] = set()
        self._last_sl_tp: dict[int, tuple[float, float]] = {}
        self._atr_cache: dict[str, _AtrCacheEntry] = {}
        self._last_action_ts: float = 0.0
        self._acting: bool = False
        self._last_trigger: Optional[str] = None
        self._close_time_fired_date: Optional[str] = None

        self.reload_config()

    @property
    def account_id(self) -> str:
        return self._session.account_id

    def reload_config(self) -> None:
        row = db.get_risk_config(self.account_id)
        if row is None:
            self.config = RiskConfig()
            self.enabled = False
            return
        self.config = RiskConfig.from_dict(row.get("config"))
        self.enabled = bool(row.get("enabled"))

    def status(self) -> dict:
        account = self._session.account_store.snapshot()
        total = self._session.store.total_profit()
        balance = float(account.get("balance") or 0)
        return {
            "enabled": self.enabled,
            "acting": self._acting,
            "last_trigger": self._last_trigger,
            "floating_profit": round(total, 2),
            "trailing_armed": self._trailing_armed,
            "peak_profit": round(self._peak_profit, 2),
            "open_positions": len(self._session.store.snapshot()),
            "balance": balance,
            "equity": account.get("equity"),
            "margin_level": account.get("margin_level"),
        }

    async def evaluate(self) -> None:
        if not self.enabled or not self._session.connected:
            return
        if self._acting:
            return
        now = time.monotonic()
        if now - self._last_action_ts < self.config.cooldown_seconds:
            return

        positions = self._session.store.snapshot()
        account = self._session.account_store.snapshot()
        total = self._session.store.total_profit()

        triggered = await self._check_account_rules(positions, account, total)
        if triggered:
            return

        await self._check_trade_usd_rules(positions)
        await self._apply_broker_rules(positions)

    async def _check_account_rules(
        self, positions: list[dict], account: dict, total: float
    ) -> bool:
        cfg = self.config
        balance = float(account.get("balance") or 0)
        equity = float(account.get("equity") or 0)
        margin_level = float(account.get("margin_level") or 0)

        reason: Optional[str] = None
        close_filter = "all"

        # USD floating TP/SL
        if reason is None and cfg.account_tp_usd is not None and total >= cfg.account_tp_usd:
            reason = f"Tổng lãi đạt {total:.2f} USD (TP {cfg.account_tp_usd})"
            close_filter = "profit" if cfg.account_action == "matching" else "all"

        if reason is None and cfg.account_sl_usd is not None and total <= -abs(cfg.account_sl_usd):
            reason = f"Tổng lỗ đạt {total:.2f} USD (SL {cfg.account_sl_usd})"
            close_filter = "loss" if cfg.account_action == "matching" else "all"

        # % balance
        if reason is None and balance > 0:
            pct = total / balance * 100
            if cfg.account_tp_pct is not None and pct >= cfg.account_tp_pct:
                reason = f"Tổng lãi {pct:.1f}% balance (TP {cfg.account_tp_pct}%)"
                close_filter = "profit" if cfg.account_action == "matching" else "all"
            if reason is None and cfg.account_sl_pct is not None and pct <= -abs(cfg.account_sl_pct):
                reason = f"Tổng lỗ {pct:.1f}% balance (SL {cfg.account_sl_pct}%)"
                close_filter = "loss" if cfg.account_action == "matching" else "all"

        # Equity bounds
        if reason is None and cfg.equity_floor is not None and equity > 0 and equity <= cfg.equity_floor:
            reason = f"Equity {equity:.2f} <= sàn {cfg.equity_floor}"
        if reason is None and cfg.equity_ceiling is not None and equity >= cfg.equity_ceiling:
            reason = f"Equity {equity:.2f} >= trần {cfg.equity_ceiling}"

        # Drawdown
        if reason is None and cfg.max_drawdown_pct is not None and balance > 0 and total < 0:
            dd = -total / balance * 100
            if dd >= cfg.max_drawdown_pct:
                reason = f"Drawdown {dd:.1f}% (max {cfg.max_drawdown_pct}%)"

        # Margin level
        if reason is None and cfg.min_margin_level_pct is not None and margin_level > 0:
            if margin_level <= cfg.min_margin_level_pct:
                reason = f"Margin level {margin_level:.1f}% <= {cfg.min_margin_level_pct}%"

        # Daily limits
        if reason is None and (cfg.daily_profit_target is not None or cfg.daily_loss_limit is not None):
            realized = db.realized_pnl_since(self.account_id, _utc_midnight_epoch())
            daily_total = realized + total
            if cfg.daily_profit_target is not None and daily_total >= cfg.daily_profit_target:
                reason = f"Lãi trong ngày {daily_total:.2f} USD (mục tiêu {cfg.daily_profit_target})"
                close_filter = "profit" if cfg.account_action == "matching" else "all"
            if reason is None and cfg.daily_loss_limit is not None and daily_total <= -abs(cfg.daily_loss_limit):
                reason = f"Lỗ trong ngày {daily_total:.2f} USD (giới hạn {cfg.daily_loss_limit})"
                close_filter = "loss" if cfg.account_action == "matching" else "all"

        # Account trailing
        if reason is None:
            reason, close_filter = self._check_account_trailing(total, balance, cfg)

        # Exposure limits -> close all
        if reason is None and cfg.max_positions is not None and len(positions) > cfg.max_positions:
            reason = f"Vượt giới hạn {cfg.max_positions} lệnh (đang mở {len(positions)})"
        if reason is None and cfg.max_total_lot is not None:
            total_lot = sum(float(p.get("volume", 0)) for p in positions)
            if total_lot > cfg.max_total_lot:
                reason = f"Vượt giới hạn lot {cfg.max_total_lot} (hiện {total_lot:.2f})"

        # Time-based close
        if reason is None:
            reason = self._check_time_rules()

        if reason:
            await self._execute_account_close(reason, close_filter, total)
            return True
        return False

    def _check_account_trailing(
        self, total: float, balance: float, cfg: RiskConfig
    ) -> tuple[Optional[str], str]:
        close_filter = "all"
        if total > self._peak_profit:
            self._peak_profit = total

        # USD trailing
        if cfg.account_trailing_arm_usd is not None and cfg.account_trailing_giveback_usd is not None:
            if total >= cfg.account_trailing_arm_usd:
                self._trailing_armed = True
            if self._trailing_armed and total <= self._peak_profit - cfg.account_trailing_giveback_usd:
                return (
                    f"Trailing tài khoản: lãi tụt từ đỉnh {self._peak_profit:.2f} xuống {total:.2f}",
                    close_filter,
                )

        # % trailing
        if balance > 0 and cfg.account_trailing_arm_pct is not None and cfg.account_trailing_giveback_pct is not None:
            pct = total / balance * 100
            peak_pct = self._peak_profit / balance * 100 if balance else 0
            if pct >= cfg.account_trailing_arm_pct:
                self._trailing_armed = True
            if self._trailing_armed and pct <= peak_pct - cfg.account_trailing_giveback_pct:
                return (
                    f"Trailing %: tụt từ {peak_pct:.1f}% xuống {pct:.1f}%",
                    close_filter,
                )

        if total <= 0:
            self._trailing_armed = False
            self._peak_profit = 0.0

        return None, close_filter

    def _check_time_rules(self) -> Optional[str]:
        cfg = self.config
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")

        if cfg.close_time:
            try:
                hh, mm = cfg.close_time.split(":")
                target_h, target_m = int(hh), int(mm)
                if now.hour > target_h or (now.hour == target_h and now.minute >= target_m):
                    if self._close_time_fired_date != today:
                        self._close_time_fired_date = today
                        return f"Đến giờ đóng lệnh ({cfg.close_time})"
            except (ValueError, AttributeError):
                pass

        if cfg.close_before_weekend and now.weekday() == 4 and now.hour >= 20:
            if self._close_time_fired_date != today:
                self._close_time_fired_date = today
                return "Đóng lệnh trước cuối tuần (Thứ 6 sau 20:00)"

        return None

    async def _execute_account_close(self, reason: str, close_filter: str, total: float) -> None:
        self._acting = True
        self._last_action_ts = time.monotonic()
        self._last_trigger = reason
        try:
            result = await self._session.gateway.close_all(close_filter)
            if result.get("ok"):
                logger.info("[%s] risk close_all(%s): %s", self.account_id, close_filter, reason)
                await telegram_notify.notify(
                    self.account_id,
                    telegram_notify.format_risk_triggered(
                        self.account_id, reason, f"Tổng P/L: {total:.2f} · filter={close_filter}"
                    ),
                )
                self._trailing_armed = False
                self._peak_profit = 0.0
            else:
                logger.warning("[%s] risk close failed: %s", self.account_id, result.get("error"))
        finally:
            self._acting = False

    async def _check_trade_usd_rules(self, positions: list[dict]) -> None:
        cfg = self.config
        for p in positions:
            ticket = int(p["ticket"])
            pnl = _position_pnl(p)

            reason: Optional[str] = None
            if cfg.trade_tp_usd is not None and pnl >= cfg.trade_tp_usd:
                reason = f"Lệnh #{ticket} lãi {pnl:.2f} USD (TP {cfg.trade_tp_usd})"
            elif cfg.trade_sl_usd is not None and pnl <= -abs(cfg.trade_sl_usd):
                reason = f"Lệnh #{ticket} lỗ {pnl:.2f} USD (SL {cfg.trade_sl_usd})"

            if cfg.max_hold_minutes is not None and p.get("time_open"):
                age_min = (time.time() - int(p["time_open"])) / 60
                if age_min >= cfg.max_hold_minutes:
                    reason = reason or f"Lệnh #{ticket} giữ quá {cfg.max_hold_minutes} phút"

            if reason:
                await self._close_single(ticket, reason, pnl)

    async def _close_single(self, ticket: int, reason: str, pnl: float) -> None:
        if self._acting:
            return
        self._acting = True
        self._last_action_ts = time.monotonic()
        self._last_trigger = reason
        try:
            result = await self._session.gateway.close_position(ticket)
            if result.get("ok"):
                logger.info("[%s] risk close #%s: %s", self.account_id, ticket, reason)
                await telegram_notify.notify(
                    self.account_id,
                    telegram_notify.format_risk_triggered(
                        self.account_id, reason, f"P/L lệnh: {pnl:.2f}"
                    ),
                )
                self._trade_peaks.pop(ticket, None)
                self._broker_sltp_applied.discard(ticket)
                self._last_sl_tp.pop(ticket, None)
            else:
                logger.warning("[%s] risk close #%s failed: %s", self.account_id, ticket, result.get("error"))
        finally:
            self._acting = False

    async def _apply_broker_rules(self, positions: list[dict]) -> None:
        cfg = self.config
        for p in positions:
            ticket = int(p["ticket"])
            symbol = p["symbol"]
            side = p["side"]
            price_open = float(p["price_open"])
            current_sl = float(p.get("sl") or 0)
            current_tp = float(p.get("tp") or 0)

            price_info = self._session.price_cache.get(symbol)
            if price_info is None:
                continue
            bid = float(price_info["bid"])
            ask = float(price_info["ask"])
            point = float(price_info.get("point") or 0)
            if point <= 0:
                continue

            market = bid if side == "BUY" else ask
            profit_pips = (market - price_open) / point if side == "BUY" else (price_open - market) / point

            new_sl = current_sl
            new_tp = current_tp
            modified = False

            # Initial pip SL/TP (once per ticket)
            if ticket not in self._broker_sltp_applied:
                if cfg.trade_sl_pips or cfg.trade_tp_pips:
                    sl_p = cfg.trade_sl_pips or 0
                    tp_p = cfg.trade_tp_pips or 0
                    calc_sl, calc_tp = sl_tp_from_points(side, price_open, sl_p, tp_p, point)
                    if calc_sl and (current_sl == 0 or abs(current_sl - calc_sl) > point):
                        new_sl = calc_sl
                        modified = True
                    if calc_tp and (current_tp == 0 or abs(current_tp - calc_tp) > point):
                        new_tp = calc_tp
                        modified = True
                    if modified:
                        self._broker_sltp_applied.add(ticket)

            # ATR-based SL/TP (once per ticket, or refresh if not set)
            if cfg.atr_enabled and ticket not in self._broker_sltp_applied:
                atr = await self._get_atr(symbol, cfg.atr_timeframe, cfg.atr_period)
                if atr > 0:
                    sl_dist = atr * cfg.atr_sl_multiplier
                    tp_dist = atr * cfg.atr_tp_multiplier
                    if side == "BUY":
                        calc_sl = price_open - sl_dist
                        calc_tp = price_open + tp_dist
                    else:
                        calc_sl = price_open + sl_dist
                        calc_tp = price_open - tp_dist
                    if current_sl == 0 or abs(current_sl - calc_sl) > point:
                        new_sl = calc_sl
                        modified = True
                    if current_tp == 0 or abs(current_tp - calc_tp) > point:
                        new_tp = calc_tp
                        modified = True
                    if modified:
                        self._broker_sltp_applied.add(ticket)

            # Break-even
            if cfg.breakeven_arm_pips is not None and profit_pips >= cfg.breakeven_arm_pips:
                buffer = cfg.breakeven_buffer_pips * point
                be_sl = price_open + buffer if side == "BUY" else price_open - buffer
                if side == "BUY" and (current_sl == 0 or be_sl > current_sl):
                    new_sl = be_sl
                    modified = True
                elif side == "SELL" and (current_sl == 0 or be_sl < current_sl):
                    new_sl = be_sl
                    modified = True

            # Per-trade trailing
            if cfg.trade_trailing_arm_pips is not None and cfg.trade_trailing_distance_pips is not None:
                peak = self._trade_peaks.get(ticket, profit_pips)
                if profit_pips > peak:
                    self._trade_peaks[ticket] = profit_pips
                    peak = profit_pips
                if peak >= cfg.trade_trailing_arm_pips:
                    trail_sl = market - cfg.trade_trailing_distance_pips * point if side == "BUY" else market + cfg.trade_trailing_distance_pips * point
                    if side == "BUY" and (current_sl == 0 or trail_sl > current_sl):
                        new_sl = trail_sl
                        modified = True
                    elif side == "SELL" and (current_sl == 0 or trail_sl < current_sl):
                        new_sl = trail_sl
                        modified = True

            if modified:
                await self._modify_sltp(ticket, new_sl, new_tp, symbol)

        # Prune state for closed tickets
        open_tickets = {int(p["ticket"]) for p in positions}
        for t in list(self._trade_peaks):
            if t not in open_tickets:
                del self._trade_peaks[t]
        self._broker_sltp_applied &= open_tickets
        self._last_sl_tp = {k: v for k, v in self._last_sl_tp.items() if k in open_tickets}

    async def _get_atr(self, symbol: str, timeframe: str, period: int) -> float:
        cache_key = f"{symbol}:{timeframe}:{period}"
        entry = self._atr_cache.get(cache_key)
        now = time.monotonic()
        if entry and now - entry.fetched_at < ATR_CACHE_TTL_S:
            return entry.value
        try:
            count = max(period + 20, 50)
            bars = await self._session.history_gateway.fetch(symbol, timeframe, count)
            atr = compute_atr(bars, period)
            self._atr_cache[cache_key] = _AtrCacheEntry(value=atr, fetched_at=now)
            return atr
        except Exception:
            logger.exception("[%s] ATR fetch failed for %s", self.account_id, symbol)
            return entry.value if entry else 0.0

    async def _modify_sltp(
        self, ticket: int, sl: float, tp: float, symbol: str
    ) -> None:
        prev = self._last_sl_tp.get(ticket)
        if prev and abs(prev[0] - sl) < 1e-8 and abs(prev[1] - tp) < 1e-8:
            return
        if self._acting:
            return
        self._acting = True
        try:
            result = await self._session.gateway.modify_position(ticket, sl, tp)
            if result.get("ok"):
                self._last_sl_tp[ticket] = (sl, tp)
                logger.debug("[%s] risk modify #%s SL=%.5f TP=%.5f", self.account_id, ticket, sl, tp)
            else:
                logger.warning("[%s] risk modify #%s failed: %s", self.account_id, ticket, result.get("error"))
        finally:
            self._acting = False


async def run_risk_supervisor(sessions: "SessionManager") -> None:
    """Background loop: evaluate risk rules for every connected account."""
    from session_manager import SessionManager  # noqa: F811 - runtime import avoids cycle at module load

    logger.info("risk supervisor started")
    while True:
        try:
            for session in list(sessions.sessions.values()):
                if session.connected and session.risk_manager.enabled:
                    await session.risk_manager.evaluate()
        except Exception:
            logger.exception("risk supervisor tick failed")
        await asyncio.sleep(SUPERVISOR_INTERVAL_S)
