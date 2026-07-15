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
from grid_jobs import pip_multiplier, sl_tp_from_points
from models import format_order_comment

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


def in_trade_hours(spec: Optional[str], now: Optional[datetime] = None) -> bool:
    """True if `now` falls inside a "HH:MM-HH:MM" window (local time).
    Overnight windows ("22:00-06:00") wrap around midnight. Malformed
    specs fail open (trade) so a typo never silently halts entries."""
    if not spec or not spec.strip():
        return True
    try:
        start_s, end_s = spec.split("-")
        sh, sm = (int(x) for x in start_s.strip().split(":"))
        eh, em = (int(x) for x in end_s.strip().split(":"))
    except (ValueError, AttributeError):
        return True
    now = now or datetime.now()
    cur = now.hour * 60 + now.minute
    start = sh * 60 + sm
    end = eh * 60 + em
    if start == end:
        return True
    if start < end:
        return start <= cur < end
    return cur >= start or cur < end  # overnight window


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
    # Skip entries while spread is wider than this many points (None = off).
    # Protects against entering during news spikes / rollover when the
    # broker widens spread 10-20x and a market order fills terribly.
    max_spread_points: Optional[float] = None
    # Higher-timeframe trend filter: only BUY above the EMA, only SELL
    # below it. {"enabled": bool, "timeframe": "H4", "ema_period": 200}
    trend_filter: dict = field(default_factory=dict)
    # Only enter inside this local-time window, e.g. "07:00-22:00".
    # None/empty = trade around the clock.
    trade_hours: Optional[str] = None
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
    # For indicator_logic == "majority": winning side must lead by at least
    # this many votes (1 = classic majority; 2 filters out 3-2 splits).
    indicator_min_margin: int = 1
    # Require the combined indicator signal to persist this many CONSECUTIVE
    # closed bars before entering (1 = enter on first bar). Filters signals
    # that flash on one bar and vanish on the next.
    confirm_bars: int = 1
    # { "rsi": {"enabled": true, "period": 14, ...}, ... }
    indicators: dict = field(default_factory=dict)
    # Machine-learning entry (trigger_mode == "ml"). Holds the trained model
    # plus runtime knobs: { enabled, timeframe, threshold, model: {...} }
    ml: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Defense-in-depth: web.py validates fresh writes, but a config
        loaded straight from Supabase (reload_config) skips that Pydantic
        layer entirely - a stale row saved before validation existed, or one
        edited directly in the DB, could carry a value that would place a
        dangerous order (e.g. a negative sl_distance flips the stop onto the
        profit side instead of being rejected). Clamp rather than raise so a
        bad value degrades to "no stop"/"default" instead of blocking
        startup or crashing evaluate()."""
        if self.volume is None or self.volume <= 0:
            self.volume = 0.01
        if self.sl_distance is not None and self.sl_distance < 0:
            self.sl_distance = None
        if self.tp_distance is not None and self.tp_distance < 0:
            self.tp_distance = None
        if self.cooldown_seconds is None or self.cooldown_seconds < 0:
            self.cooldown_seconds = 0.0
        if self.confirm_bars is None or self.confirm_bars < 1:
            self.confirm_bars = 1

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
    # True while this symbol's order is in flight - blocks re-entry on the
    # same symbol without freezing the scan of every OTHER symbol.
    acting: bool = False
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
    # Consecutive-closed-bar streak of the ML side, mirroring streak_side/
    # streak_count above but for the "ml.confirm_bars" anti-flicker filter.
    ml_streak_side: Optional[str] = None
    ml_streak_count: int = 0
    ml_streak_bar_ts: Optional[int] = None
    last_trigger: Optional[str] = None
    # Closed-bar time of the last indicator/ML entry: a cross/oversold signal
    # persists for the whole bar, so without this gate the same signal would
    # re-enter every cooldown_seconds until the bar closes.
    entered_bar_ts: Optional[int] = None
    # Consecutive-closed-bar streak of the combined indicator signal, for
    # the confirm_bars filter. Votes are kept for the status/UI display.
    streak_side: Optional[str] = None
    streak_count: int = 0
    streak_bar_ts: Optional[int] = None
    last_votes: tuple = (0, 0)  # (buys, sells) of the last computation
    # Monotonic Entry comment sequence for this symbol (survives restarts via
    # reseeding from open tickets; never reused when a ticket is closed).
    entry_seq: int = 0
    # Serializes evaluate() for this symbol: the tick handler and the 1s
    # supervisor loop both call evaluate() independently, and `acting` alone
    # doesn't close the race because it isn't set until deep inside
    # _execute_entry(), after several awaits (bars fetch, indicator/ML,
    # trend filter) - two concurrent calls could both pass the guards before
    # either sets it, sending two orders for the same signal.
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class EntryManager:
    def __init__(self, session: "AccountSession"):
        self._session = session
        self.config = EntryConfig()
        self.enabled = False

        self._acting: bool = False
        # Orders currently awaiting an EA reply, across all symbols - counted
        # into the max_open_positions guard so two symbols can't both slip
        # past the limit while neither shows in the snapshot yet.
        self._inflight: int = 0
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
            # Seed from open tickets so numbering continues after restart and
            # across legacy `Entry-#n` / new `SYMBOL-Entry-#n` formats.
            st.entry_seq = self._session.store.max_algo_index(symbol, "Entry", side="")
            self._sym[symbol] = st
        return st

    def _next_entry_index(self, symbol: str) -> int:
        """Next Entry # for this symbol — always increases, never reuses."""
        st = self._state(symbol)
        st.entry_seq = max(
            st.entry_seq,
            self._session.store.max_algo_index(symbol, "Entry", side=""),
        ) + 1
        return st.entry_seq

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

    def _unit_factor(self, point: float) -> float:
        if (self.config.sltp_unit or "points").lower() != "pips":
            return 1.0
        return pip_multiplier(point)

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

    def _symbol_blockers(self, cfg: EntryConfig, sym: str, st: Optional[_SymState],
                          open_count: int, total_open: int,
                          spread_pts: Optional[float]) -> list[str]:
        """Cheap synchronous checks mirroring evaluate()'s guards - so the
        UI can show WHY a symbol isn't entering right now."""
        now = time.monotonic()
        out: list[str] = []
        if st is not None:
            if st.acting:
                out.append("đang gửi lệnh")
            backoff = st.fail_until - now
            if backoff > 0:
                out.append(f"backoff sau lỗi ({backoff:.0f}s)")
            cooldown = cfg.cooldown_seconds - (now - st.last_entry_ts)
            if st.last_entry_ts > 0 and cooldown > 0:
                out.append(f"cooldown ({cooldown:.0f}s)")
        if cfg.max_spread_points and spread_pts is not None and spread_pts > cfg.max_spread_points:
            out.append(f"spread rộng ({spread_pts:.0f} > {cfg.max_spread_points:.0f})")
        if not in_trade_hours(cfg.trade_hours):
            out.append("ngoài giờ giao dịch")
        if cfg.only_if_flat and open_count > 0:
            out.append("đã có lệnh symbol này")
        if cfg.max_open_positions is not None and total_open + self._inflight >= cfg.max_open_positions:
            out.append(f"đủ max lệnh ({total_open}/{cfg.max_open_positions})")
        if cfg.max_entries_per_day is not None and self._entries_day == _utc_today() \
                and self._entries_today >= cfg.max_entries_per_day:
            out.append(f"đủ max lần/ngày ({self._entries_today})")
        return out

    def status(self) -> dict:
        cfg = self.config
        symbols = cfg.symbol_list()
        positions = self._session.store.snapshot()
        open_by_sym: dict[str, int] = {}
        for p in positions:
            s = (p.get("symbol") or "").upper()
            open_by_sym[s] = open_by_sym.get(s, 0) + 1
        total_open = len(positions)

        ml_threshold = float((cfg.ml or {}).get("threshold", 0.58))

        per_symbol = {}
        for sym in symbols:
            st = self._sym.get(sym)
            price = self._session.price_cache.get(sym)
            spread_pts = None
            if price and float(price.get("point") or 0) > 0:
                spread_pts = round(
                    (float(price["ask"]) - float(price["bid"])) / float(price["point"]), 1
                )
            # Combined verdicts for the UI's "Kết luận" column.
            buys, sells = (st.last_votes if st else (0, 0))
            ind_side = st.streak_side if st else None
            ml_side = None
            proba = st.last_ml_proba if st else None
            if proba is not None:
                if proba >= ml_threshold:
                    ml_side = "BUY"
                elif proba <= 1 - ml_threshold:
                    ml_side = "SELL"
            per_symbol[sym] = {
                "open": open_by_sym.get(sym, 0),
                "last_trigger": st.last_trigger if st else None,
                "last_ask": st.last_ask if st else None,
                "last_bid": st.last_bid if st else None,
                "spread_points": spread_pts,
                "ml_proba": proba,
                "ml_side": ml_side,
                "indicator_signals": dict(st.last_signals) if st else {},
                "indicator_side": ind_side,
                "votes_buy": buys,
                "votes_sell": sells,
                "streak_count": st.streak_count if st else 0,
                "confirm_bars": max(1, int(cfg.confirm_bars or 1)),
                "blockers": self._symbol_blockers(
                    cfg, sym, st, open_by_sym.get(sym, 0), total_open, spread_pts
                ) if self.enabled else [],
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
        if point <= 0:
            return
        st = self._state(symbol)
        if st.acting:
            return
        now = time.monotonic()
        if now < st.fail_until:
            return
        if now - st.last_entry_ts < cfg.cooldown_seconds:
            return
        if cfg.max_spread_points and (ask - bid) / point > cfg.max_spread_points:
            return
        if not in_trade_hours(cfg.trade_hours):
            return

        prev_ask, prev_bid = st.last_ask, st.last_bid
        st.last_ask, st.last_bid = ask, bid

        if not self._guards_ok(cfg, symbol):
            return

        # Everything from here on decides whether to send an order, so it
        # must run for at most one evaluate() call per symbol at a time -
        # see the comment on _SymState.lock. Another evaluate() for this
        # symbol may have already run (and changed acting/cooldown/guard
        # state) while we were waiting for the lock, so re-check.
        async with st.lock:
            if st.acting:
                return
            if time.monotonic() - st.last_entry_ts < cfg.cooldown_seconds:
                return
            if not self._guards_ok(cfg, symbol):
                return

            side = cfg.side
            bar_ts: Optional[int] = None
            mode = (cfg.trigger_mode or "").lower()
            if mode == "indicators":
                res = await self._indicator_signal(cfg, symbol, st)
                if not res:
                    return
                side, reason, bar_ts = res
            elif mode == "ml":
                res = await self._ml_signal(cfg, symbol, st)
                if not res:
                    return
                side, reason, bar_ts = res
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
                bar_ts = max(res_ind[2] or 0, res_ml[2] or 0) or None
            else:
                reason = self._check_trigger(cfg, symbol, st, bid, ask, prev_bid, prev_ask)
                if not reason:
                    return

            # One entry per closed bar: the same cross/oversold signal persists
            # until the next bar closes, so don't re-enter on it after cooldown.
            if bar_ts is not None and bar_ts == st.entered_bar_ts:
                return

            if not await self._trend_allows(cfg, symbol, side if side in ("BUY", "SELL") else "BUY"):
                return

            await self._execute_entry(cfg, symbol, st, bid, ask, point, reason, side, bar_ts)

    def _guards_ok(self, cfg: EntryConfig, symbol: str) -> bool:
        positions = self._session.store.snapshot()
        sym_pos = [p for p in positions if p.get("symbol") == symbol]

        if cfg.only_if_flat and sym_pos:
            return False
        # Count in-flight orders too: a just-sent order won't appear in the
        # snapshot for up to ~1s, which would let a second symbol overshoot.
        if cfg.max_open_positions is not None and len(positions) + self._inflight >= cfg.max_open_positions:
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
                # Arm the timer instead of firing immediately: a server
                # restart must never place an instant surprise order.
                st.last_entry_ts = time.monotonic()
                return None
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

    async def _trend_allows(self, cfg: EntryConfig, symbol: str, side: str) -> bool:
        """Higher-timeframe EMA filter: BUY only above the EMA, SELL only
        below. Fails open when bars are unavailable so a history hiccup
        doesn't silently stop all entries."""
        tf_cfg = cfg.trend_filter or {}
        if not tf_cfg.get("enabled"):
            return True
        tf = tf_cfg.get("timeframe") or "H4"
        period = max(2, int(tf_cfg.get("ema_period") or 200))
        bars = await self._get_bars(symbol, tf)
        bars = bars[:-1]  # closed bars only, same rule as the signals
        if len(bars) < period:
            return True
        closes = [float(b["close"]) for b in bars]
        ema = indicators._ema_series(closes, period)[-1]
        if ema is None:
            return True
        if side == "BUY":
            return closes[-1] > ema
        return closes[-1] < ema

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
        max_bar_ts = 0
        for key, params in enabled:
            # Each indicator may override the common timeframe with its own.
            tf = (params.get("timeframe") or default_tf)
            bars = await self._get_bars(symbol, tf)
            # Drop the forming (incomplete) bar: signals computed on it
            # repaint - they appear mid-bar and vanish when the bar closes.
            bars = bars[:-1]
            if len(bars) < 30:
                st.last_signals[key] = None
                continue
            max_bar_ts = max(max_bar_ts, int(bars[-1]["time"]))
            sig = indicators.indicator_signal(key, bars, params)
            st.last_signals[key] = sig
            if sig == "BUY":
                buys += 1
                fired.append(f"{key}↑")
            elif sig == "SELL":
                sells += 1
                fired.append(f"{key}↓")

        logic = (cfg.indicator_logic or "all").lower()
        side = indicators.combine_signals(
            buys, sells, len(enabled), logic,
            cfg.indicator_min_agree, cfg.indicator_min_margin,
        )
        st.last_votes = (buys, sells)

        # Track how many CONSECUTIVE closed bars produced this same side -
        # updated once per new bar (signals only change on bar close).
        bar_ts = max_bar_ts or None
        if bar_ts is not None and bar_ts != st.streak_bar_ts:
            if side is not None and side == st.streak_side:
                st.streak_count += 1
            elif side is not None:
                st.streak_side = side
                st.streak_count = 1
            else:
                st.streak_side = None
                st.streak_count = 0
            st.streak_bar_ts = bar_ts
        elif side != st.streak_side:
            # Config changed mid-bar and flipped the outcome - restart.
            st.streak_side = side
            st.streak_count = 1 if side is not None else 0

        if side is None:
            return None
        need = max(1, int(cfg.confirm_bars or 1))
        if st.streak_count < need:
            return None
        # Direction filter: BUY/SELL restricts to that side only; BOTH (or any
        # other value) accepts whichever direction the indicators produce.
        allowed = (cfg.side or "BOTH").upper()
        if allowed in ("BUY", "SELL") and side != allowed:
            return None
        confirm_note = f" ({st.streak_count} nến liên tiếp)" if need > 1 else ""
        reason = f"Chỉ báo [{logic}] {', '.join(fired)} — {side}{confirm_note}"
        st.ind_cached = (side, reason, bar_ts)
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
        combo = (cfg.trigger_mode or "").lower() == "indicators_ml"
        # In combo mode a separate, usually stricter threshold can be set so
        # double-confirmation doesn't ride on the same razor-thin edge used
        # when ML runs standalone - "both signals agree" alone isn't much of
        # an edge if ML's own bar is barely past 0.58. Default to a stricter
        # floor (0.62) unless the user explicitly configured combo_threshold.
        threshold = float(ml_cfg.get("threshold", 0.58))
        if combo:
            threshold = float(ml_cfg.get("combo_threshold") or max(threshold, 0.62))
        bars = await self._get_bars(symbol, tf)
        # Predict on the last CLOSED bar - the model was trained on completed
        # bars, so feeding it a half-formed candle is a distribution mismatch.
        bars = bars[:-1]
        if len(bars) < 30:
            return None
        bar_ts = int(bars[-1]["time"])

        htf_bars = None
        tf_cfg = cfg.trend_filter or {}
        if tf_cfg.get("enabled"):
            htf_tf = tf_cfg.get("timeframe") or "H4"
            htf_bars = (await self._get_bars(symbol, htf_tf))[:-1] or None

        proba = ml_entry.predict_proba(bars, model, htf_bars)
        st.last_ml_proba = proba
        side = ml_entry.predict_signal(bars, model, threshold, cfg.side, htf_bars)

        # Require the ML side to persist this many CONSECUTIVE closed bars -
        # same anti-flicker idea as the indicator confirm_bars filter, so a
        # single noisy bar that barely crosses the threshold doesn't count
        # the same as a well-established move. Combo mode defaults to 2
        # (unless overridden) since it's the mode most exposed to firing on
        # every bar where a lagging indicator and a flickering ML both
        # happen to line up for one instant.
        need = max(1, int(ml_cfg.get("confirm_bars") or (2 if combo else 1)))
        if bar_ts != st.ml_streak_bar_ts:
            if side is not None and side == st.ml_streak_side:
                st.ml_streak_count += 1
            elif side is not None:
                st.ml_streak_side, st.ml_streak_count = side, 1
            else:
                st.ml_streak_side, st.ml_streak_count = None, 0
            st.ml_streak_bar_ts = bar_ts
        elif side != st.ml_streak_side:
            st.ml_streak_side = side
            st.ml_streak_count = 1 if side is not None else 0

        if side is None or st.ml_streak_count < need:
            return None
        reason = f"ML[{model.get('algo', '?')}] p(up)={proba:.2f} ngưỡng {threshold:.2f} — {side}"
        st.ml_cached = (side, reason, bar_ts)
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
        bar_ts: Optional[int] = None,
    ) -> None:
        side = (side or cfg.side or "BUY").upper()
        if side not in ("BUY", "SELL"):
            # BOTH only makes sense for indicator mode (side chosen above);
            # for schedule/interval fall back to BUY so we never send BOTH.
            side = "BUY"
        self._acting = True
        st.acting = True
        self._inflight += 1
        self._last_trigger = f"{symbol}: {reason}"
        st.last_trigger = reason
        try:
            price = ask if side == "BUY" else bid
            unit = self._unit_factor(point)
            sl_pts = (cfg.sl_distance or 0) * unit
            tp_pts = (cfg.tp_distance or 0) * unit
            # Absolute SL/TP from our latest tick = fallback for older EAs;
            # the distances travel too so a new EA recomputes from its own
            # (fresher) price at send time.
            sl, tp = sl_tp_from_points(side, price, sl_pts, tp_pts, point)

            today = _utc_today()
            if self._entries_day != today:
                self._entries_day = today
                self._entries_today = 0
            idx = self._next_entry_index(symbol)
            comment = format_order_comment(symbol, "Entry", idx)

            result = await self._session.gateway.open_order(
                symbol, side, cfg.volume, sl, tp, comment,
                sl_points=sl_pts, tp_points=tp_pts,
            )
            if result.get("ok"):
                st.last_entry_ts = time.monotonic()
                st.entered_bar_ts = bar_ts
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
                error = str(result.get("error") or "")
                st.fail_until = time.monotonic() + FAIL_BACKOFF_S
                if "timeout" in error.lower():
                    # A timed-out order may still have FILLED on the broker
                    # (the order_result just came back late) - apply the normal
                    # cooldown + bar gate too so we never double-enter on it.
                    st.last_entry_ts = time.monotonic()
                    st.entered_bar_ts = bar_ts
                logger.warning(
                    "[%s] entry failed on %s: %s (backoff %.0fs)",
                    self.account_id, symbol, error, FAIL_BACKOFF_S,
                )
        finally:
            self._acting = False
            st.acting = False
            self._inflight = max(0, self._inflight - 1)


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
