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
import ml_entry
import telegram_notify
from grid_jobs import sl_tp_from_points

if TYPE_CHECKING:
    from session_manager import AccountSession

logger = logging.getLogger("entry_manager")

SUPERVISOR_INTERVAL_S = 1.0
BARS_CACHE_TTL_S = 30.0
INDICATOR_THROTTLE_S = 3.0
# After a failed/timed-out order we back off before retrying so a broken EA
# link or rejecting broker doesn't get hammered every few seconds.
FAIL_BACKOFF_S = 30.0


def _utc_today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


@dataclass
class EntryConfig:
    enabled: bool = False
    symbol: str = "XAUUSD"  # legacy single-symbol field (kept for back-compat)
    symbols: list = field(default_factory=list)  # preferred: list of symbols to scan
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
    indicator_logic: str = "all"  # all | any | majority | threshold
    # For indicator_logic == "threshold": enter when >= this many indicators
    # agree on a direction AND none point the other way.
    indicator_min_agree: int = 2
    # { "rsi": {"enabled": true, "period": 14, ...}, ... }
    indicators: dict = field(default_factory=dict)
    # Machine-learning entry (trigger_mode == "ml"). Holds the trained model
    # plus runtime knobs: { enabled, timeframe, threshold, model: {...} }
    ml: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "EntryConfig":
        if not data:
            return cls()
        known = {f.name for f in fields(cls)}
        kwargs = {k: v for k, v in data.items() if k in known}
        return cls(**kwargs)

    def to_dict(self) -> dict:
        return {f.name: getattr(self, f.name) for f in fields(self)}

    def symbol_list(self) -> list[str]:
        """Normalized, de-duplicated list of symbols to scan. Accepts either
        the `symbols` list or the legacy `symbol` field, and tolerates commas,
        spaces or semicolons as separators."""
        raw: list = list(self.symbols) if self.symbols else ([self.symbol] if self.symbol else [])
        out: list[str] = []
        seen: set[str] = set()
        for item in raw:
            for part in str(item).replace(";", ",").replace(" ", ",").split(","):
                s = part.strip().upper()
                if s and s not in seen:
                    seen.add(s)
                    out.append(s)
        return out


@dataclass
class _SymState:
    """Per-symbol runtime state so each symbol is scanned/cooled independently."""
    last_entry_ts: float = 0.0
    fail_until: float = 0.0
    last_ask: Optional[float] = None
    last_bid: Optional[float] = None
    schedule_fired_date: Optional[str] = None
    ind_calc_ts: float = 0.0
    ind_cached: Optional[tuple] = None
    last_signals: dict = field(default_factory=dict)
    # ML has its own throttle/cache so combined mode can compute both without
    # the two engines clobbering each other's cached result.
    ml_calc_ts: float = 0.0
    ml_cached: Optional[tuple] = None
    last_ml_proba: Optional[float] = None
    last_trigger: Optional[str] = None


class EntryManager:
    def __init__(self, session: "AccountSession"):
        self._session = session
        self.config = EntryConfig()
        self.enabled = False

        self._acting: bool = False
        self._loaded: bool = False
        self._entries_day: str = ""
        self._entries_today: int = 0
        self._last_trigger: Optional[str] = None
        # Per-symbol runtime state (cooldown, caches, last signals, ...).
        self._sym: dict[str, _SymState] = {}
        # Bars cache is shared, already keyed by "symbol:timeframe".
        self._bars_cache: dict[str, tuple[float, list]] = {}
        # Throttle Market Watch requests for symbols with no live price yet.
        self._watch_req: dict[str, float] = {}

    @property
    def account_id(self) -> str:
        return self._session.account_id

    def _state(self, symbol: str) -> _SymState:
        st = self._sym.get(symbol)
        if st is None:
            st = _SymState()
            self._sym[symbol] = st
        return st

    async def request_watch(self, symbol: str) -> None:
        """Ask the EA to add a symbol to Market Watch so it starts streaming
        prices. Throttled to at most once every 30s per symbol."""
        now = time.monotonic()
        if now - self._watch_req.get(symbol, 0.0) < 30.0:
            return
        self._watch_req[symbol] = now
        try:
            await self._session.gateway.watch_symbol(symbol)
        except Exception:
            logger.debug("[%s] watch_symbol(%s) failed", self.account_id, symbol)

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
        symbols = cfg.symbol_list()
        positions = self._session.store.snapshot()
        open_by_sym: dict[str, int] = {}
        for p in positions:
            s = (p.get("symbol") or "").upper()
            open_by_sym[s] = open_by_sym.get(s, 0) + 1

        per_symbol = {}
        for sym in symbols:
            st = self._sym.get(sym)
            per_symbol[sym] = {
                "open": open_by_sym.get(sym, 0),
                "last_trigger": st.last_trigger if st else None,
                "last_ask": st.last_ask if st else None,
                "last_bid": st.last_bid if st else None,
                "ml_proba": st.last_ml_proba if st else None,
                "indicator_signals": dict(st.last_signals) if st else {},
            }

        # First symbol drives the indicator badges / single-symbol UI fields.
        primary = symbols[0] if symbols else ""
        pst = self._sym.get(primary)
        return {
            "enabled": self.enabled,
            "acting": self._acting,
            "last_trigger": self._last_trigger,
            "symbol": ", ".join(symbols) if symbols else "—",
            "symbols": symbols,
            "side": cfg.side,
            "trigger_mode": cfg.trigger_mode,
            "open_on_symbol": open_by_sym.get(primary, 0),
            "open_total": sum(open_by_sym.get(s, 0) for s in symbols),
            "entries_today": self._entries_today,
            "per_symbol": per_symbol,
            "last_ask": pst.last_ask if pst else None,
            "last_bid": pst.last_bid if pst else None,
            "indicator_signals": dict(pst.last_signals) if pst else {},
            "ml_proba": pst.last_ml_proba if pst else None,
            "ml_trained_at": (cfg.ml or {}).get("model", {}).get("trained_at"),
            "ml_accuracy": (cfg.ml or {}).get("model", {}).get("accuracy"),
        }

    async def evaluate(self, symbol: str, bid: float, ask: float, point: float) -> None:
        if not self.enabled or not self._session.connected:
            return
        cfg = self.config
        symbol = symbol.upper()
        if symbol not in cfg.symbol_list():
            return
        if self._acting or point <= 0:
            return
        st = self._state(symbol)
        now = time.monotonic()
        if now < st.fail_until:
            return
        if now - st.last_entry_ts < cfg.cooldown_seconds:
            return

        prev_ask, prev_bid = st.last_ask, st.last_bid
        st.last_ask, st.last_bid = ask, bid

        if not self._guards_ok(cfg, symbol):
            return

        side = cfg.side
        mode = (cfg.trigger_mode or "").lower()
        if mode == "indicators":
            res = await self._indicator_signal(cfg, symbol, st)
            if not res:
                return
            side, reason = res
        elif mode == "ml":
            res = await self._ml_signal(cfg, symbol, st)
            if not res:
                return
            side, reason = res
        elif mode == "indicators_ml":
            # ML acts as a confirmation filter: indicators AND the model must
            # agree on the same direction before we enter.
            res_ind = await self._indicator_signal(cfg, symbol, st)
            res_ml = await self._ml_signal(cfg, symbol, st)
            if not res_ind or not res_ml:
                return
            if res_ind[0] != res_ml[0]:
                return
            side = res_ind[0]
            reason = f"Chỉ báo + ML cùng {side} ({res_ml[1]})"
        else:
            reason = self._check_trigger(cfg, symbol, st, bid, ask, prev_bid, prev_ask)
            if not reason:
                return

        await self._execute_entry(cfg, symbol, st, bid, ask, point, reason, side)

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
        symbol: str,
        st: _SymState,
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
                if st.schedule_fired_date != today:
                    st.schedule_fired_date = today
                    return f"Lịch {cfg.schedule_time} — vào lệnh {cfg.side}"
            return None

        if mode == "interval":
            if not cfg.interval_minutes or cfg.interval_minutes <= 0:
                return None
            if st.last_entry_ts == 0:
                return f"Interval {cfg.interval_minutes} phút — lần đầu"
            elapsed = (time.monotonic() - st.last_entry_ts) / 60
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
        self, cfg: EntryConfig, symbol: str, st: _SymState
    ) -> Optional[tuple[str, str]]:
        """Evaluate enabled indicators and combine into a (side, reason) or None.
        Throttled so repeated ticks don't recompute more than every few seconds."""
        now = time.monotonic()
        if now - st.ind_calc_ts < INDICATOR_THROTTLE_S:
            return st.ind_cached

        st.ind_calc_ts = now
        st.ind_cached = None

        cfg_inds = cfg.indicators or {}
        enabled = [
            (key, params)
            for key, params in cfg_inds.items()
            if isinstance(params, dict) and params.get("enabled")
        ]
        st.last_signals = {}
        if not enabled:
            return None

        default_tf = cfg.indicator_timeframe or "H1"

        buys = 0
        sells = 0
        fired: list[str] = []
        for key, params in enabled:
            # Each indicator may override the common timeframe with its own.
            tf = (params.get("timeframe") or default_tf)
            bars = await self._get_bars(symbol, tf)
            if len(bars) < 30:
                st.last_signals[key] = None
                continue
            sig = indicators.indicator_signal(key, bars, params)
            st.last_signals[key] = sig
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
        elif logic == "threshold":
            # Need at least N agreeing AND nothing pointing the other way.
            need = max(1, int(cfg.indicator_min_agree or 1))
            if buys >= need and sells == 0:
                side = "BUY"
            elif sells >= need and buys == 0:
                side = "SELL"
        else:  # majority
            if buys > sells:
                side = "BUY"
            elif sells > buys:
                side = "SELL"

        if side is None:
            return None
        # Direction filter: BUY/SELL restricts to that side only; BOTH (or any
        # other value) accepts whichever direction the indicators produce.
        allowed = (cfg.side or "BOTH").upper()
        if allowed in ("BUY", "SELL") and side != allowed:
            return None
        reason = f"Chỉ báo [{logic}] {', '.join(fired)} — {side}"
        st.ind_cached = (side, reason)
        return st.ind_cached

    async def _ml_signal(
        self, cfg: EntryConfig, symbol: str, st: _SymState
    ) -> Optional[tuple[str, str]]:
        """Evaluate the trained ML model on the latest bars. Throttled like
        the indicator path. Returns (side, reason) or None."""
        now = time.monotonic()
        if now - st.ml_calc_ts < INDICATOR_THROTTLE_S:
            return st.ml_cached
        st.ml_calc_ts = now
        st.ml_cached = None
        st.last_ml_proba = None

        ml_cfg = cfg.ml or {}
        model = ml_cfg.get("model")
        if not ml_cfg.get("enabled") or not model:
            return None

        tf = ml_cfg.get("timeframe") or model.get("timeframe") or "H1"
        threshold = float(ml_cfg.get("threshold", 0.58))
        bars = await self._get_bars(symbol, tf)
        if len(bars) < 30:
            return None

        proba = ml_entry.predict_proba(bars, model)
        st.last_ml_proba = proba
        side = ml_entry.predict_signal(bars, model, threshold, cfg.side)
        if side is None:
            return None
        reason = f"ML p(up)={proba:.2f} ngưỡng {threshold:.2f} — {side}"
        st.ml_cached = (side, reason)
        return st.ml_cached

    async def _execute_entry(
        self,
        cfg: EntryConfig,
        symbol: str,
        st: _SymState,
        bid: float,
        ask: float,
        point: float,
        reason: str,
        side: Optional[str] = None,
    ) -> None:
        side = (side or cfg.side or "BUY").upper()
        if side not in ("BUY", "SELL"):
            # BOTH only makes sense for indicator mode (side chosen above);
            # for schedule/interval fall back to BUY so we never send BOTH.
            side = "BUY"
        self._acting = True
        self._last_trigger = f"{symbol}: {reason}"
        st.last_trigger = reason
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
                st.last_entry_ts = time.monotonic()
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
                st.fail_until = time.monotonic() + FAIL_BACKOFF_S
                logger.warning(
                    "[%s] entry failed on %s: %s (backoff %.0fs)",
                    self.account_id, symbol, result.get("error"), FAIL_BACKOFF_S,
                )
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
                for sym in cfg.symbol_list():
                    price = session.price_cache.get(sym)
                    if price is None:
                        # Not streaming yet - nudge the EA to watch it.
                        await em.request_watch(sym)
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
