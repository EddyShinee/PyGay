"""Technical indicators computed from OHLC bars (list of dicts with
open/high/low/close, sorted ascending by time - as returned by
history_gateway.fetch / history.py).

Each indicator exposes a signal function returning "BUY" | "SELL" | None
based on the latest (most recent) bar and the user's parameters. Pure
Python, no numpy dependency. Used by entry_manager.py.
"""
from __future__ import annotations

from typing import Callable, Optional

# Default params per indicator - MUST stay in sync with the frontend
# ENTRY_INDICATORS meta in static/index.html.
INDICATOR_DEFAULTS: dict[str, dict] = {
    "rsi": {"period": 14, "oversold": 30, "overbought": 70},
    "macd": {"fast": 12, "slow": 26, "signal": 9},
    "sma_cross": {"fast": 10, "slow": 30},
    "ema_cross": {"fast": 12, "slow": 26},
    "bollinger": {"period": 20, "stddev": 2},
    "stochastic": {"k_period": 14, "oversold": 20, "overbought": 80},
    "cci": {"period": 20, "threshold": 100},
    "momentum": {"period": 10, "threshold": 0.1},
    "williams": {"period": 14, "oversold": -80, "overbought": -20},
    "adx": {"period": 14, "threshold": 25},
    # --- Japanese candlestick group ---
    "heikin_ashi": {"trend_bars": 2},
    "engulfing": {},
    "hammer": {"wick_ratio": 2.0},
    "three_soldiers": {},
    "marubozu": {"body_ratio": 0.9},
}

INDICATOR_KEYS = list(INDICATOR_DEFAULTS.keys())


# --------------------------------------------------------------------------
# Series helpers
# --------------------------------------------------------------------------

def _closes(bars: list[dict]) -> list[float]:
    return [float(b["close"]) for b in bars]


def _highs(bars: list[dict]) -> list[float]:
    return [float(b["high"]) for b in bars]


def _lows(bars: list[dict]) -> list[float]:
    return [float(b["low"]) for b in bars]


def _sma_series(vals: list[float], period: int) -> list[Optional[float]]:
    out: list[Optional[float]] = [None] * len(vals)
    if period <= 0:
        return out
    s = 0.0
    for i, v in enumerate(vals):
        s += v
        if i >= period:
            s -= vals[i - period]
        if i >= period - 1:
            out[i] = s / period
    return out


def _ema_series(vals: list[float], period: int) -> list[Optional[float]]:
    out: list[Optional[float]] = [None] * len(vals)
    if period <= 0 or len(vals) < period:
        return out
    k = 2.0 / (period + 1)
    prev = sum(vals[:period]) / period
    out[period - 1] = prev
    for i in range(period, len(vals)):
        prev = vals[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def _stddev(vals: list[float]) -> float:
    n = len(vals)
    if n == 0:
        return 0.0
    m = sum(vals) / n
    return (sum((v - m) ** 2 for v in vals) / n) ** 0.5


# --------------------------------------------------------------------------
# Indicator signal functions
# --------------------------------------------------------------------------

def sig_rsi(bars: list[dict], p: dict) -> Optional[str]:
    closes = _closes(bars)
    period = int(p.get("period", 14))
    oversold = float(p.get("oversold", 30))
    overbought = float(p.get("overbought", 70))
    if len(closes) < period + 1:
        return None
    gains = losses = 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        if d >= 0:
            gains += d
        else:
            losses -= d
    avg_gain = gains / period
    avg_loss = losses / period
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        g = d if d > 0 else 0.0
        l = -d if d < 0 else 0.0
        avg_gain = (avg_gain * (period - 1) + g) / period
        avg_loss = (avg_loss * (period - 1) + l) / period
    if avg_loss == 0:
        rsi = 100.0
    else:
        rsi = 100 - 100 / (1 + avg_gain / avg_loss)
    if rsi < oversold:
        return "BUY"
    if rsi > overbought:
        return "SELL"
    return None


def sig_macd(bars: list[dict], p: dict) -> Optional[str]:
    closes = _closes(bars)
    fast = int(p.get("fast", 12))
    slow = int(p.get("slow", 26))
    signal = int(p.get("signal", 9))
    ef = _ema_series(closes, fast)
    es = _ema_series(closes, slow)
    macd_vals = [
        ef[i] - es[i]
        for i in range(len(closes))
        if ef[i] is not None and es[i] is not None
    ]
    if len(macd_vals) < signal + 2:
        return None
    sig = _ema_series(macd_vals, signal)
    if sig[-1] is None or sig[-2] is None:
        return None
    m_prev, m_last = macd_vals[-2], macd_vals[-1]
    s_prev, s_last = sig[-2], sig[-1]
    if m_prev <= s_prev and m_last > s_last:
        return "BUY"
    if m_prev >= s_prev and m_last < s_last:
        return "SELL"
    return None


def _cross(fast_s: list[Optional[float]], slow_s: list[Optional[float]]) -> Optional[str]:
    if any(x is None for x in (fast_s[-1], fast_s[-2], slow_s[-1], slow_s[-2])):
        return None
    if fast_s[-2] <= slow_s[-2] and fast_s[-1] > slow_s[-1]:
        return "BUY"
    if fast_s[-2] >= slow_s[-2] and fast_s[-1] < slow_s[-1]:
        return "SELL"
    return None


def sig_sma_cross(bars: list[dict], p: dict) -> Optional[str]:
    closes = _closes(bars)
    fast = int(p.get("fast", 10))
    slow = int(p.get("slow", 30))
    if len(closes) < slow + 2:
        return None
    return _cross(_sma_series(closes, fast), _sma_series(closes, slow))


def sig_ema_cross(bars: list[dict], p: dict) -> Optional[str]:
    closes = _closes(bars)
    fast = int(p.get("fast", 12))
    slow = int(p.get("slow", 26))
    if len(closes) < slow + 2:
        return None
    return _cross(_ema_series(closes, fast), _ema_series(closes, slow))


def sig_bollinger(bars: list[dict], p: dict) -> Optional[str]:
    closes = _closes(bars)
    period = int(p.get("period", 20))
    mult = float(p.get("stddev", 2))
    if len(closes) < period:
        return None
    window = closes[-period:]
    mid = sum(window) / period
    sd = _stddev(window)
    c = closes[-1]
    if c < mid - mult * sd:
        return "BUY"
    if c > mid + mult * sd:
        return "SELL"
    return None


def sig_stochastic(bars: list[dict], p: dict) -> Optional[str]:
    highs, lows, closes = _highs(bars), _lows(bars), _closes(bars)
    k_period = int(p.get("k_period", 14))
    oversold = float(p.get("oversold", 20))
    overbought = float(p.get("overbought", 80))
    if len(closes) < k_period:
        return None
    hh = max(highs[-k_period:])
    ll = min(lows[-k_period:])
    if hh == ll:
        return None
    k = 100 * (closes[-1] - ll) / (hh - ll)
    if k < oversold:
        return "BUY"
    if k > overbought:
        return "SELL"
    return None


def sig_cci(bars: list[dict], p: dict) -> Optional[str]:
    highs, lows, closes = _highs(bars), _lows(bars), _closes(bars)
    period = int(p.get("period", 20))
    threshold = float(p.get("threshold", 100))
    if len(closes) < period:
        return None
    tp = [(highs[i] + lows[i] + closes[i]) / 3 for i in range(len(closes))]
    window = tp[-period:]
    ma = sum(window) / period
    md = sum(abs(x - ma) for x in window) / period
    if md == 0:
        return None
    cci = (tp[-1] - ma) / (0.015 * md)
    if cci < -threshold:
        return "BUY"
    if cci > threshold:
        return "SELL"
    return None


def sig_momentum(bars: list[dict], p: dict) -> Optional[str]:
    closes = _closes(bars)
    period = int(p.get("period", 10))
    threshold = float(p.get("threshold", 0.1))
    if len(closes) < period + 1:
        return None
    prev = closes[-1 - period]
    if prev == 0:
        return None
    roc = (closes[-1] / prev - 1) * 100
    if roc > threshold:
        return "BUY"
    if roc < -threshold:
        return "SELL"
    return None


def sig_williams(bars: list[dict], p: dict) -> Optional[str]:
    highs, lows, closes = _highs(bars), _lows(bars), _closes(bars)
    period = int(p.get("period", 14))
    oversold = float(p.get("oversold", -80))
    overbought = float(p.get("overbought", -20))
    if len(closes) < period:
        return None
    hh = max(highs[-period:])
    ll = min(lows[-period:])
    if hh == ll:
        return None
    wr = -100 * (hh - closes[-1]) / (hh - ll)
    if wr < oversold:
        return "BUY"
    if wr > overbought:
        return "SELL"
    return None


def sig_adx(bars: list[dict], p: dict) -> Optional[str]:
    highs, lows, closes = _highs(bars), _lows(bars), _closes(bars)
    period = int(p.get("period", 14))
    threshold = float(p.get("threshold", 25))
    n = len(closes)
    if n < period * 2 + 1:
        return None
    plus_dm = [0.0] * n
    minus_dm = [0.0] * n
    tr = [0.0] * n
    for i in range(1, n):
        up = highs[i] - highs[i - 1]
        dn = lows[i - 1] - lows[i]
        plus_dm[i] = up if (up > dn and up > 0) else 0.0
        minus_dm[i] = dn if (dn > up and dn > 0) else 0.0
        tr[i] = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )

    def wilder(arr: list[float]) -> list[Optional[float]]:
        out: list[Optional[float]] = [None] * n
        s = sum(arr[1:period + 1])
        out[period] = s
        for i in range(period + 1, n):
            s = s - (s / period) + arr[i]
            out[i] = s
        return out

    str_ = wilder(tr)
    pdm = wilder(plus_dm)
    mdm = wilder(minus_dm)
    dx: list[Optional[float]] = [None] * n
    for i in range(period, n):
        if str_[i]:
            pdi = 100 * pdm[i] / str_[i]
            mdi = 100 * mdm[i] / str_[i]
            if pdi + mdi != 0:
                dx[i] = 100 * abs(pdi - mdi) / (pdi + mdi)
    dx_valid = [d for d in dx if d is not None]
    if len(dx_valid) < period:
        return None
    adx = sum(dx_valid[-period:]) / period
    if adx < threshold:
        return None
    i = n - 1
    if not str_[i]:
        return None
    pdi = 100 * pdm[i] / str_[i]
    mdi = 100 * mdm[i] / str_[i]
    return "BUY" if pdi > mdi else "SELL"


# --------------------------------------------------------------------------
# Japanese candlestick indicators
# --------------------------------------------------------------------------

def _ohlc(b: dict) -> tuple[float, float, float, float]:
    return float(b["open"]), float(b["high"]), float(b["low"]), float(b["close"])


def _body(b: dict) -> float:
    o, _, _, c = _ohlc(b)
    return abs(c - o)


def _upper_wick(b: dict) -> float:
    o, h, _, c = _ohlc(b)
    return h - max(o, c)


def _lower_wick(b: dict) -> float:
    o, _, l, c = _ohlc(b)
    return min(o, c) - l


def _heikin_ashi(bars: list[dict]) -> list[dict]:
    """Return Heikin-Ashi OHLC series computed from raw bars."""
    ha: list[dict] = []
    for i, b in enumerate(bars):
        o, h, l, c = _ohlc(b)
        ha_close = (o + h + l + c) / 4
        if i == 0:
            ha_open = (o + c) / 2
        else:
            ha_open = (ha[i - 1]["open"] + ha[i - 1]["close"]) / 2
        ha.append({
            "open": ha_open,
            "high": max(h, ha_open, ha_close),
            "low": min(l, ha_open, ha_close),
            "close": ha_close,
        })
    return ha


def sig_heikin_ashi(bars: list[dict], p: dict) -> Optional[str]:
    trend = max(1, int(p.get("trend_bars", 2)))
    if len(bars) < trend + 1:
        return None
    ha = _heikin_ashi(bars)
    last = ha[-trend:]
    if all(c["close"] > c["open"] for c in last):
        return "BUY"
    if all(c["close"] < c["open"] for c in last):
        return "SELL"
    return None


def sig_engulfing(bars: list[dict], p: dict) -> Optional[str]:
    if len(bars) < 2:
        return None
    po, ph, pl, pc = _ohlc(bars[-2])
    o, h, l, c = _ohlc(bars[-1])
    prev_bear = pc < po
    prev_bull = pc > po
    cur_bull = c > o
    cur_bear = c < o
    if cur_bull and prev_bear and o <= pc and c >= po:
        return "BUY"
    if cur_bear and prev_bull and o >= pc and c <= po:
        return "SELL"
    return None


def sig_hammer(bars: list[dict], p: dict) -> Optional[str]:
    ratio = float(p.get("wick_ratio", 2.0))
    if len(bars) < 1:
        return None
    b = bars[-1]
    body = _body(b)
    if body <= 0:
        return None
    up = _upper_wick(b)
    lo = _lower_wick(b)
    # Hammer: long lower wick, small upper wick -> bullish reversal
    if lo >= ratio * body and up <= body:
        return "BUY"
    # Shooting star: long upper wick, small lower wick -> bearish reversal
    if up >= ratio * body and lo <= body:
        return "SELL"
    return None


def sig_three_soldiers(bars: list[dict], p: dict) -> Optional[str]:
    if len(bars) < 3:
        return None
    c3 = bars[-3:]
    o = [float(b["open"]) for b in c3]
    c = [float(b["close"]) for b in c3]
    bull = all(c[i] > o[i] for i in range(3)) and c[0] < c[1] < c[2]
    bear = all(c[i] < o[i] for i in range(3)) and c[0] > c[1] > c[2]
    if bull:
        return "BUY"
    if bear:
        return "SELL"
    return None


def sig_marubozu(bars: list[dict], p: dict) -> Optional[str]:
    body_ratio = float(p.get("body_ratio", 0.9))
    if len(bars) < 1:
        return None
    o, h, l, c = _ohlc(bars[-1])
    rng = h - l
    if rng <= 0:
        return None
    if _body(bars[-1]) / rng < body_ratio:
        return None
    if c > o:
        return "BUY"
    if c < o:
        return "SELL"
    return None


_DISPATCH: dict[str, Callable[[list[dict], dict], Optional[str]]] = {
    "rsi": sig_rsi,
    "macd": sig_macd,
    "sma_cross": sig_sma_cross,
    "ema_cross": sig_ema_cross,
    "bollinger": sig_bollinger,
    "stochastic": sig_stochastic,
    "cci": sig_cci,
    "momentum": sig_momentum,
    "williams": sig_williams,
    "adx": sig_adx,
    "heikin_ashi": sig_heikin_ashi,
    "engulfing": sig_engulfing,
    "hammer": sig_hammer,
    "three_soldiers": sig_three_soldiers,
    "marubozu": sig_marubozu,
}


def indicator_signal(key: str, bars: list[dict], params: dict) -> Optional[str]:
    """Return "BUY"|"SELL"|None for one indicator. Merges defaults so a
    partial params dict still works. Never raises - returns None on error."""
    fn = _DISPATCH.get(key)
    if fn is None:
        return None
    merged = {**INDICATOR_DEFAULTS.get(key, {}), **(params or {})}
    try:
        return fn(bars, merged)
    except Exception:
        return None


def combine_signals(buys: int, sells: int, total: int, logic: str,
                    min_agree: int = 1, min_margin: int = 1) -> Optional[str]:
    """Combine per-indicator votes into one side. Shared by the live
    EntryManager and the backtest so both always agree on semantics.

    min_margin only applies to "majority": the winning side must lead by
    at least this many votes (1 = classic majority, 2 = e.g. 3-1 wins but
    3-2 doesn't) - filters out barely-split votes."""
    logic = (logic or "all").lower()
    if logic == "all":
        if buys == total:
            return "BUY"
        if sells == total:
            return "SELL"
    elif logic == "any":
        if buys > 0 and sells == 0:
            return "BUY"
        if sells > 0 and buys == 0:
            return "SELL"
    elif logic == "threshold":
        # Need at least N agreeing AND nothing pointing the other way.
        need = max(1, int(min_agree or 1))
        if buys >= need and sells == 0:
            return "BUY"
        if sells >= need and buys == 0:
            return "SELL"
    else:  # majority
        margin = max(1, int(min_margin or 1))
        if buys - sells >= margin:
            return "BUY"
        if sells - buys >= margin:
            return "SELL"
    return None
