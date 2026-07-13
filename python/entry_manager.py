"""Per-account automated entry: evaluates triggers on live ticks and opens
orders via TradeGateway. Config in Supabase (account_entry.py).
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, fields
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

import account_entry
import telegram_notify
from grid_jobs import sl_tp_from_points

if TYPE_CHECKING:
    from session_manager import AccountSession

logger = logging.getLogger("entry_manager")

SUPERVISOR_INTERVAL_S = 1.0


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
    # trigger_mode: schedule | price_above | price_below | interval
    trigger_mode: str = "schedule"
    schedule_time: Optional[str] = None  # HH:MM local
    price_trigger: Optional[float] = None
    interval_minutes: Optional[int] = None

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

        reason = self._check_trigger(cfg, bid, ask, prev_bid, prev_ask)
        if not reason:
            return

        await self._execute_entry(cfg, symbol, bid, ask, point, reason)

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

    async def _execute_entry(
        self,
        cfg: EntryConfig,
        symbol: str,
        bid: float,
        ask: float,
        point: float,
        reason: str,
    ) -> None:
        self._acting = True
        self._last_trigger = reason
        try:
            price = ask if cfg.side == "BUY" else bid
            unit = self._unit_factor()
            sl_pts = (cfg.sl_distance or 0) * unit
            tp_pts = (cfg.tp_distance or 0) * unit
            sl, tp = sl_tp_from_points(cfg.side, price, sl_pts, tp_pts, point)

            result = await self._session.gateway.open_order(
                symbol, cfg.side, cfg.volume, sl, tp
            )
            if result.get("ok"):
                self._last_entry_ts = time.monotonic()
                today = _utc_today()
                if self._entries_day != today:
                    self._entries_day = today
                    self._entries_today = 0
                self._entries_today += 1
                logger.info("[%s] entry %s %s %.2f lot: %s", self.account_id, cfg.side, symbol, cfg.volume, reason)
                await telegram_notify.notify(
                    self.account_id,
                    telegram_notify.format_entry_triggered(
                        self.account_id,
                        cfg.side,
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
