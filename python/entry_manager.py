"""Per-account automated entry: evaluates triggers on live ticks and opens
orders via TradeGateway. Config in Supabase (account_entry.py).
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field, fields
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

import account_entry
import indicators
import telegram_notify
from grid_jobs import sl_tp_from_points

if TYPE_CHECKING:
    from session_manager import AccountSession

logger = logging.getLogger("entry_manager")

SUPERVISOR_INTERVAL_S = 1.0
BARS_CACHE_TTL_S = 30.0
INDICATOR_THROTTLE_S = 3.0


def _utc_today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


@dataclass
class EntryConfig:
    enabled: bool = False
    symbol: str = "XAUUSD"
    side: str = "BUY"  # BUY | SELL
    volume: float = 0.01
    sltp_unit: str = "points"  # points | pips
    sl_distance: Optional[float] = None
    tp_distance: Optional[float] = None
    cooldown_seconds: float = 60.0
    max_open_positions: Optional[int] = None
    max_entries_per_day: Optional[int] = None
    only_if_flat: bool = False
    # trigger_mode: schedule | price_above | price_below | interval | indicators
    trigger_mode: str = "schedule"
    schedule_time: Optional[str] = None  # HH:MM local
    price_trigger: Optional[float] = None
    interval_minutes: Optional[int] = None
    # Indicator-based entry (trigger_mode == "indicators")
    indicator_timeframe: str = "H1"
    indicator_logic: str = "all"  # all | any | majority
    # { "rsi": {"enabled": true, "period": 14, ...}, ... }
    indicators: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "EntryConfig":
        if not data:
            return cls()
        known = {f.name for f in fields(cls)}
        kwargs = {k: v for k, v in data.items() if k in known}
        return cls(**kwargs)

    def to_dict(self) -> dict:
        return {f.name: getattr(self, f.name) for f in fields(self)}


class EntryManager:
    def __init__(self, session: "AccountSession"):
        self._session = session
        self.config = EntryConfig()
        self.enabled = False

        self._last_entry_ts: float = 0.0
        self._acting: bool = False
        self._loaded: bool = False
        self._schedule_fired_date: Optional[str] = None
        self._entries_day: str = ""
        self._entries_today: int = 0
        self._last_ask: Optional[float] = None
        self._last_bid: Optional[float] = None
        self._last_trigger: Optional[str] = None
        # Indicator engine caches
        self._bars_cache: dict[str, tuple[float, list]] = {}
        self._ind_calc_ts: float = 0.0
        self._ind_cached: Optional[tuple[str, str]] = None
        self._last_signals: dict[str, Optional[str]] = {}

    @property
    def account_id(self) -> str:
        return self._session.account_id

    def _unit_factor(self) -> float:
        return 10.0 if (self.config.sltp_unit or "points").lower() == "pips" else 1.0

    async def reload_config(self) -> None:
        try:
            row = await asyncio.to_thread(account_entry.get_entry_config, self.account_id)
        except Exception:
            logger.exception("[%s] load entry config failed - keeping current", self.account_id)
            return
        if row is None:
            self.config = EntryConfig()
            self.enabled = False
        else:
            self.config = EntryConfig.from_dict(row.get("config"))
            self.enabled = bool(row.get("enabled"))
        self._loaded = True

    def status(self) -> dict:
        cfg = self.config
        positions = self._session.store.snapshot()
        sym_positions = [p for p in positions if p.get("symbol") == cfg.symbol]
        return {
            "enabled": self.enabled,
            "acting": self._acting,
            "last_trigger": self._last_trigger,
            "symbol": cfg.symbol,
            "side": cfg.side,
            "trigger_mode": cfg.trigger_mode,
            "open_on_symbol": len(sym_positions),
            "entries_today": self._entries_today,
            "last_ask": self._last_ask,
            "last_bid": self._last_bid,
            "indicator_signals": dict(self._last_signals),
        }

    async def evaluate(self, symbol: str, bid: float, ask: float, point: float) -> None:
        if not self.enabled or not self._session.connected:
            return
        cfg = self.config
        if symbol.upper() != (cfg.symbol or "").upper():
            return
        if self._acting or point <= 0:
            return
        now = time.monotonic()
        if now - self._last_entry_ts < cfg.cooldown_seconds:
            return

        prev_ask, prev_bid = self._last_ask, self._last_bid
        self._last_ask, self._last_bid = ask, bid

        if not self._guards_ok(cfg, symbol):
            return

        side = cfg.side
        if (cfg.trigger_mode or "").lower() == "indicators":
            res = await self._indicator_signal(cfg, symbol)
            if not res:
                return
            side, reason = res
        else:
            reason = self._check_trigger(cfg, bid, ask, prev_bid, prev_ask)
            if not reason:
                return

        await self._execute_entry(cfg, symbol, bid, ask, point, reason, side)

    def _guards_ok(self, cfg: EntryConfig, symbol: str) -> bool:
        positions = self._session.store.snapshot()
        sym_pos = [p for p in positions if p.get("symbol") == symbol]

        if cfg.only_if_flat and sym_pos:
            return False
        if cfg.max_open_positions is not None and len(positions) >= cfg.max_open_positions:
            return False

        today = _utc_today()
        if self._entries_day != today:
            self._entries_day = today
            self._entries_today = 0
        if cfg.max_entries_per_day is not None and self._entries_today >= cfg.max_entries_per_day:
            return False
        return True

    def _check_trigger(
        self,
        cfg: EntryConfig,
        bid: float,
        ask: float,
        prev_bid: Optional[float],
        prev_ask: Optional[float],
    ) -> Optional[str]:
        mode = (cfg.trigger_mode or "schedule").lower()

        if mode == "schedule":
            if not cfg.schedule_time:
                return None
            now = datetime.now()
            today = now.strftime("%Y-%m-%d")
            try:
                hh, mm = cfg.schedule_time.split(":")
                th, tm = int(hh), int(mm)
            except (ValueError, AttributeError):
                return None
            if now.hour > th or (now.hour == th and now.minute >= tm):
                if self._schedule_fired_date != today:
                    self._schedule_fired_date = today
                    return f"Lịch {cfg.schedule_time} — vào lệnh {cfg.side}"
            return None

        if mode == "interval":
            if not cfg.interval_minutes or cfg.interval_minutes <= 0:
                return None
            if self._last_entry_ts == 0:
                return f"Interval {cfg.interval_minutes} phút — lần đầu"
            elapsed = (time.monotonic() - self._last_entry_ts) / 60
            if elapsed >= cfg.interval_minutes:
                return f"Interval {cfg.interval_minutes} phút — đủ chu kỳ"
            return None

        if mode == "price_above":
            if cfg.price_trigger is None or cfg.side != "BUY":
                return None
            if prev_ask is not None and prev_ask < cfg.price_trigger <= ask:
                return f"Giá vượt {cfg.price_trigger} (ask {ask:.5f}) — BUY"
            return None

        if mode == "price_below":
            if cfg.price_trigger is None or cfg.side != "SELL":
                return None
            if prev_bid is not None and prev_bid > cfg.price_trigger >= bid:
                return f"Giá xuống {cfg.price_trigger} (bid {bid:.5f}) — SELL"
            return None

        return None

    async def _get_bars(self, symbol: str, timeframe: str) -> list:
        cache_key = f"{symbol}:{timeframe}"
        now = time.monotonic()
        cached = self._bars_cache.get(cache_key)
        if cached and now - cached[0] < BARS_CACHE_TTL_S:
            return cached[1]
        try:
            bars = await self._session.history_gateway.fetch(symbol, timeframe, 300)
            bars = sorted(bars, key=lambda b: int(b["time"]))
            self._bars_cache[cache_key] = (now, bars)
            return bars
        except Exception:
            logger.exception("[%s] bars fetch failed for %s", self.account_id, symbol)
            return cached[1] if cached else []

    async def _indicator_signal(
        self, cfg: EntryConfig, symbol: str
    ) -> Optional[tuple[str, str]]:
        """Evaluate enabled indicators and combine into a (side, reason) or None.
        Throttled so repeated ticks don't recompute more than every few seconds."""
        now = time.monotonic()
        if now - self._ind_calc_ts < INDICATOR_THROTTLE_S:
            return self._ind_cached

        self._ind_calc_ts = now
        self._ind_cached = None

        cfg_inds = cfg.indicators or {}
        enabled = [
            (key, params)
            for key, params in cfg_inds.items()
            if isinstance(params, dict) and params.get("enabled")
        ]
        self._last_signals = {}
        if not enabled:
            return None

        tf = cfg.indicator_timeframe or "H1"
        bars = await self._get_bars(symbol, tf)
        if len(bars) < 30:
            return None

        buys = 0
        sells = 0
        fired: list[str] = []
        for key, params in enabled:
            sig = indicators.indicator_signal(key, bars, params)
            self._last_signals[key] = sig
            if sig == "BUY":
                buys += 1
                fired.append(f"{key}↑")
            elif sig == "SELL":
                sells += 1
                fired.append(f"{key}↓")

        logic = (cfg.indicator_logic or "all").lower()
        total = len(enabled)
        side: Optional[str] = None
        if logic == "all":
            if buys == total:
                side = "BUY"
            elif sells == total:
                side = "SELL"
        elif logic == "any":
            if buys > 0 and sells == 0:
                side = "BUY"
            elif sells > 0 and buys == 0:
                side = "SELL"
        else:  # majority
            if buys > sells:
                side = "BUY"
            elif sells > buys:
                side = "SELL"

        if side is None:
            return None
        reason = f"Chỉ báo [{logic}] {', '.join(fired)} — {side}"
        self._ind_cached = (side, reason)
        return self._ind_cached

    async def _execute_entry(
        self,
        cfg: EntryConfig,
        symbol: str,
        bid: float,
        ask: float,
        point: float,
        reason: str,
        side: Optional[str] = None,
    ) -> None:
        side = side or cfg.side
        self._acting = True
        self._last_trigger = reason
        try:
            price = ask if side == "BUY" else bid
            unit = self._unit_factor()
            sl_pts = (cfg.sl_distance or 0) * unit
            tp_pts = (cfg.tp_distance or 0) * unit
            sl, tp = sl_tp_from_points(side, price, sl_pts, tp_pts, point)

            result = await self._session.gateway.open_order(
                symbol, side, cfg.volume, sl, tp
            )
            if result.get("ok"):
                self._last_entry_ts = time.monotonic()
                today = _utc_today()
                if self._entries_day != today:
                    self._entries_day = today
                    self._entries_today = 0
                self._entries_today += 1
                logger.info("[%s] entry %s %s %.2f lot: %s", self.account_id, side, symbol, cfg.volume, reason)
                await telegram_notify.notify(
                    self.account_id,
                    telegram_notify.format_entry_triggered(
                        self.account_id,
                        side,
                        symbol,
                        cfg.volume,
                        reason,
                    ),
                )
            else:
                logger.warning("[%s] entry failed: %s", self.account_id, result.get("error"))
        finally:
            self._acting = False


async def run_entry_supervisor(sessions: "SessionManager") -> None:
    """Fallback loop for schedule/interval triggers when tick stream is quiet."""
    logger.info("entry supervisor started")
    while True:
        try:
            for session in list(sessions.sessions.values()):
                em = session.entry_manager
                if not em._loaded:
                    await em.reload_config()
                if not session.connected or not em.enabled:
                    continue
                cfg = em.config
                sym = (cfg.symbol or "").upper()
                if not sym:
                    continue
                price = session.price_cache.get(sym)
                if price is None:
                    continue
                await em.evaluate(
                    sym,
                    float(price["bid"]),
                    float(price["ask"]),
                    float(price.get("point") or 0),
                )
        except Exception:
            logger.exception("entry supervisor tick failed")
        await asyncio.sleep(SUPERVISOR_INTERVAL_S)
