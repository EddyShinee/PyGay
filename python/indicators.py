"""Technical indicators computed from OHLC bars (list of dicts with
open/high/low/close, sorted ascending by time - as returned by
history_gateway.fetch / history.py).

Each indicator is implemented once as a "series" function
`series_<name>(bars, params) -> list[Optional[str]]` producing a
"BUY"/"SELL"/None signal at *every* bar index in a single O(n) (or
O(n*period) for small bounded periods) pass over the whole array - never
by re-slicing/re-scanning a trailing window per bar. `sig_<name>` (used by
the live EntryManager, which only ever wants the latest signal) is a thin
wrapper that just takes the last element of the series. This guarantees
the live path and the backtester can never disagree, and lets the
backtester precompute each enabled indicator once instead of recomputing
it from scratch on every simulated bar.

Pure Python, no numpy dependency. Used by entry_manager.py and
backtest.py.
"""
from __future__ import annotations

import math
from collections import deque
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
    # --- Fast / low-lag group ---
    "stoch_rsi": {"rsi_period": 14, "stoch_period": 14, "oversold": 20, "overbought": 80},
    "hull_cross": {"fast": 9, "slow": 21},
    "supertrend": {"period": 10, "multiplier": 3},
    "vortex": {"period": 14},
    "psar": {"step": 0.02, "max_step": 0.2},
    "dema_cross": {"fast": 9, "slow": 21},
    "tema_cross": {"fast": 9, "slow": 21},
    "awesome_oscillator": {"fast": 5, "slow": 34},
    "kama_cross": {"period": 10, "fast": 2, "slow": 30},
    "fisher_transform": {"period": 10},
    "donchian_breakout": {"period": 20},
    "keltner_breakout": {"period": 20, "multiplier": 2},
    "chandelier_exit": {"period": 22, "multiplier": 3},
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


def _rsi_series(vals: list[float], period: int) -> list[Optional[float]]:
    """Wilder RSI at every bar (not just the latest) - needed by StochRSI."""
    n = len(vals)
    out: list[Optional[float]] = [None] * n
    if period <= 0 or n < period + 1:
        return out
    gains = losses = 0.0
    for i in range(1, period + 1):
        d = vals[i] - vals[i - 1]
        if d >= 0:
            gains += d
        else:
            losses -= d
    avg_gain = gains / period
    avg_loss = losses / period
    out[period] = 100.0 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss)
    for i in range(period + 1, n):
        d = vals[i] - vals[i - 1]
        g = d if d > 0 else 0.0
        l = -d if d < 0 else 0.0
        avg_gain = (avg_gain * (period - 1) + g) / period
        avg_loss = (avg_loss * (period - 1) + l) / period
        out[i] = 100.0 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss)
    return out


def _wma_series(vals: list[float], period: int) -> list[Optional[float]]:
    n = len(vals)
    out: list[Optional[float]] = [None] * n
    if period <= 0:
        return out
    weight_sum = period * (period + 1) / 2
    for i in range(period - 1, n):
        s = 0.0
        for j in range(period):
            s += vals[i - j] * (period - j)
        out[i] = s / weight_sum
    return out


def _hma_series(vals: list[float], period: int) -> list[Optional[float]]:
    """Hull MA - much lower lag than SMA/EMA of the same period."""
    n = len(vals)
    out: list[Optional[float]] = [None] * n
    if period <= 1 or n == 0:
        return out
    half = max(1, period // 2)
    sqrt_n = max(1, round(period ** 0.5))
    wma_half = _wma_series(vals, half)
    wma_full = _wma_series(vals, period)
    diff: list[Optional[float]] = [None] * n
    for i in range(n):
        if wma_half[i] is not None and wma_full[i] is not None:
            diff[i] = 2 * wma_half[i] - wma_full[i]
    start = next((i for i, d in enumerate(diff) if d is not None), None)
    if start is None:
        return out
    sub_hma = _wma_series(diff[start:], sqrt_n)
    for i, v in enumerate(sub_hma):
        if v is not None:
            out[start + i] = v
    return out


def _atr_series(highs: list[float], lows: list[float], closes: list[float],
                 period: int) -> list[Optional[float]]:
    """Wilder ATR at every bar. Same recurrence previously inlined in
    sig_supertrend - extracted so keltner_breakout/chandelier_exit can
    reuse it without duplicating the TR/ATR math."""
    n = len(closes)
    out: list[Optional[float]] = [None] * n
    if n == 0 or period <= 0 or n <= period:
        return out
    tr = [highs[0] - lows[0]] + [
        max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
        for i in range(1, n)
    ]
    out[period] = sum(tr[1:period + 1]) / period
    for i in range(period + 1, n):
        out[i] = (out[i - 1] * (period - 1) + tr[i]) / period
    return out


def _rolling_max_series(vals: list[float], period: int) -> list[Optional[float]]:
    """Highest value of the trailing `period` window ending at each index,
    via a monotonic deque - O(n) total, not O(n*period)."""
    n = len(vals)
    out: list[Optional[float]] = [None] * n
    if period <= 0:
        return out
    dq: deque[int] = deque()
    for i in range(n):
        while dq and vals[dq[-1]] <= vals[i]:
            dq.pop()
        dq.append(i)
        if dq[0] <= i - period:
            dq.popleft()
        if i >= period - 1:
            out[i] = vals[dq[0]]
    return out


def _rolling_min_series(vals: list[float], period: int) -> list[Optional[float]]:
    n = len(vals)
    out: list[Optional[float]] = [None] * n
    if period <= 0:
        return out
    dq: deque[int] = deque()
    for i in range(n):
        while dq and vals[dq[-1]] >= vals[i]:
            dq.pop()
        dq.append(i)
        if dq[0] <= i - period:
            dq.popleft()
        if i >= period - 1:
            out[i] = vals[dq[0]]
    return out


def _dema_series(vals: list[float], period: int) -> list[Optional[float]]:
    """DEMA = 2*EMA - EMA(EMA) - roughly half the lag of a plain EMA."""
    n = len(vals)
    out: list[Optional[float]] = [None] * n
    ema1 = _ema_series(vals, period)
    start = next((i for i, v in enumerate(ema1) if v is not None), None)
    if start is None:
        return out
    ema2 = _ema_series(ema1[start:], period)
    for j, v in enumerate(ema2):
        if v is not None:
            out[start + j] = 2 * ema1[start + j] - v
    return out


def _tema_series(vals: list[float], period: int) -> list[Optional[float]]:
    """TEMA = 3*EMA - 3*EMA(EMA) + EMA(EMA(EMA)) - less lag than DEMA."""
    n = len(vals)
    out: list[Optional[float]] = [None] * n
    ema1 = _ema_series(vals, period)
    start1 = next((i for i, v in enumerate(ema1) if v is not None), None)
    if start1 is None:
        return out
    ema2_local = _ema_series(ema1[start1:], period)
    start2_local = next((i for i, v in enumerate(ema2_local) if v is not None), None)
    if start2_local is None:
        return out
    ema3_local = _ema_series(ema2_local[start2_local:], period)
    for j, v in enumerate(ema3_local):
        if v is not None:
            idx = start1 + start2_local + j
            out[idx] = 3 * ema1[idx] - 3 * ema2_local[start2_local + j] + v
    return out


def _kama_series(vals: list[float], period: int = 10, fast: int = 2,
                  slow: int = 30) -> list[Optional[float]]:
    """Kaufman Adaptive MA - smoothing constant scales with the efficiency
    ratio, so it hugs price closely in a trend and flattens out in chop."""
    n = len(vals)
    out: list[Optional[float]] = [None] * n
    if period <= 0 or n < period + 1:
        return out
    fast_sc = 2.0 / (fast + 1)
    slow_sc = 2.0 / (slow + 1)
    diffs = [0.0] * n
    for i in range(1, n):
        diffs[i] = abs(vals[i] - vals[i - 1])
    vol = sum(diffs[1:period + 1])
    out[period] = vals[period]
    for i in range(period + 1, n):
        vol += diffs[i] - diffs[i - period]
        change = abs(vals[i] - vals[i - period])
        er = change / vol if vol != 0 else 0.0
        sc = (er * (fast_sc - slow_sc) + slow_sc) ** 2
        out[i] = out[i - 1] + sc * (vals[i] - out[i - 1])
    return out


def _fisher_series(highs: list[float], lows: list[float],
                    period: int = 10) -> list[Optional[float]]:
    """Ehlers Fisher Transform - normalizes price into a near-Gaussian
    distribution so turning points stand out earlier than raw price."""
    n = len(highs)
    out: list[Optional[float]] = [None] * n
    if period <= 1 or n < period:
        return out
    hh = _rolling_max_series(highs, period)
    ll = _rolling_min_series(lows, period)
    value1 = 0.0
    fish = 0.0
    for i in range(n):
        if hh[i] is None or ll[i] is None:
            continue
        rng = hh[i] - ll[i]
        mp = (highs[i] + lows[i]) / 2
        raw = 2 * ((mp - ll[i]) / rng - 0.5) if rng != 0 else 0.0
        value1 = 0.33 * raw + 0.67 * value1
        value1 = max(-0.999, min(0.999, value1))
        fish = 0.5 * math.log((1 + value1) / (1 - value1)) + 0.5 * fish
        out[i] = fish
    return out


def _cross_series(fast_s: list[Optional[float]],
                   slow_s: list[Optional[float]]) -> list[Optional[str]]:
    """BUY/SELL at every index where fast crosses over/under slow.
    Generalizes the old last-two-elements-only `_cross` check to the
    whole array, so a backtest can precompute a cross indicator once."""
    n = len(fast_s)
    out: list[Optional[str]] = [None] * n
    for i in range(1, n):
        f0, f1, s0, s1 = fast_s[i - 1], fast_s[i], slow_s[i - 1], slow_s[i]
        if f0 is None or f1 is None or s0 is None or s1 is None:
            continue
        if f0 <= s0 and f1 > s1:
            out[i] = "BUY"
        elif f0 >= s0 and f1 < s1:
            out[i] = "SELL"
    return out


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


# --------------------------------------------------------------------------
# Indicator signal series - one BUY/SELL/None per bar index, computed once
# over the whole `bars` array. `sig_<name>` further down just reads [-1].
# --------------------------------------------------------------------------

def series_rsi(bars: list[dict], p: dict) -> list[Optional[str]]:
    closes = _closes(bars)
    period = int(p.get("period", 14))
    oversold = float(p.get("oversold", 30))
    overbought = float(p.get("overbought", 70))
    n = len(closes)
    out: list[Optional[str]] = [None] * n
    rsi = _rsi_series(closes, period)
    for i in range(n):
        r = rsi[i]
        if r is None:
            continue
        if r < oversold:
            out[i] = "BUY"
        elif r > overbought:
            out[i] = "SELL"
    return out


def series_macd(bars: list[dict], p: dict) -> list[Optional[str]]:
    closes = _closes(bars)
    fast = int(p.get("fast", 12))
    slow = int(p.get("slow", 26))
    signal = int(p.get("signal", 9))
    n = len(closes)
    out: list[Optional[str]] = [None] * n
    ef = _ema_series(closes, fast)
    es = _ema_series(closes, slow)
    idxs = [i for i in range(n) if ef[i] is not None and es[i] is not None]
    if len(idxs) < signal + 2:
        return out
    macd_vals = [ef[i] - es[i] for i in idxs]
    sig = _ema_series(macd_vals, signal)
    csig = _cross_series(macd_vals, sig)
    for j, i in enumerate(idxs):
        out[i] = csig[j]
    return out


def series_sma_cross(bars: list[dict], p: dict) -> list[Optional[str]]:
    closes = _closes(bars)
    fast = int(p.get("fast", 10))
    slow = int(p.get("slow", 30))
    return _cross_series(_sma_series(closes, fast), _sma_series(closes, slow))


def series_ema_cross(bars: list[dict], p: dict) -> list[Optional[str]]:
    closes = _closes(bars)
    fast = int(p.get("fast", 12))
    slow = int(p.get("slow", 26))
    return _cross_series(_ema_series(closes, fast), _ema_series(closes, slow))


def series_bollinger(bars: list[dict], p: dict) -> list[Optional[str]]:
    closes = _closes(bars)
    period = int(p.get("period", 20))
    mult = float(p.get("stddev", 2))
    n = len(closes)
    out: list[Optional[str]] = [None] * n
    if period <= 0:
        return out
    for i in range(period - 1, n):
        window = closes[i - period + 1:i + 1]
        mid = sum(window) / period
        sd = _stddev(window)
        c = closes[i]
        if c < mid - mult * sd:
            out[i] = "BUY"
        elif c > mid + mult * sd:
            out[i] = "SELL"
    return out


def series_stochastic(bars: list[dict], p: dict) -> list[Optional[str]]:
    highs, lows, closes = _highs(bars), _lows(bars), _closes(bars)
    k_period = int(p.get("k_period", 14))
    oversold = float(p.get("oversold", 20))
    overbought = float(p.get("overbought", 80))
    n = len(closes)
    out: list[Optional[str]] = [None] * n
    hh = _rolling_max_series(highs, k_period)
    ll = _rolling_min_series(lows, k_period)
    for i in range(n):
        if hh[i] is None or ll[i] is None or hh[i] == ll[i]:
            continue
        k = 100 * (closes[i] - ll[i]) / (hh[i] - ll[i])
        if k < oversold:
            out[i] = "BUY"
        elif k > overbought:
            out[i] = "SELL"
    return out


def series_cci(bars: list[dict], p: dict) -> list[Optional[str]]:
    highs, lows, closes = _highs(bars), _lows(bars), _closes(bars)
    period = int(p.get("period", 20))
    threshold = float(p.get("threshold", 100))
    n = len(closes)
    out: list[Optional[str]] = [None] * n
    if period <= 0:
        return out
    tp = [(highs[i] + lows[i] + closes[i]) / 3 for i in range(n)]
    for i in range(period - 1, n):
        window = tp[i - period + 1:i + 1]
        ma = sum(window) / period
        md = sum(abs(x - ma) for x in window) / period
        if md == 0:
            continue
        cci = (tp[i] - ma) / (0.015 * md)
        if cci < -threshold:
            out[i] = "BUY"
        elif cci > threshold:
            out[i] = "SELL"
    return out


def series_momentum(bars: list[dict], p: dict) -> list[Optional[str]]:
    closes = _closes(bars)
    period = int(p.get("period", 10))
    threshold = float(p.get("threshold", 0.1))
    n = len(closes)
    out: list[Optional[str]] = [None] * n
    for i in range(period, n):
        prev = closes[i - period]
        if prev == 0:
            continue
        roc = (closes[i] / prev - 1) * 100
        if roc > threshold:
            out[i] = "BUY"
        elif roc < -threshold:
            out[i] = "SELL"
    return out


def series_williams(bars: list[dict], p: dict) -> list[Optional[str]]:
    highs, lows, closes = _highs(bars), _lows(bars), _closes(bars)
    period = int(p.get("period", 14))
    oversold = float(p.get("oversold", -80))
    overbought = float(p.get("overbought", -20))
    n = len(closes)
    out: list[Optional[str]] = [None] * n
    hh = _rolling_max_series(highs, period)
    ll = _rolling_min_series(lows, period)
    for i in range(n):
        if hh[i] is None or ll[i] is None or hh[i] == ll[i]:
            continue
        wr = -100 * (hh[i] - closes[i]) / (hh[i] - ll[i])
        if wr < oversold:
            out[i] = "BUY"
        elif wr > overbought:
            out[i] = "SELL"
    return out


def series_adx(bars: list[dict], p: dict) -> list[Optional[str]]:
    highs, lows, closes = _highs(bars), _lows(bars), _closes(bars)
    period = int(p.get("period", 14))
    threshold = float(p.get("threshold", 25))
    n = len(closes)
    out: list[Optional[str]] = [None] * n
    if n < period * 2 + 1:
        return out
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
        out_: list[Optional[float]] = [None] * n
        s = sum(arr[1:period + 1])
        out_[period] = s
        for i in range(period + 1, n):
            s = s - (s / period) + arr[i]
            out_[i] = s
        return out_

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

    # Rolling average of the last `period` *valid* (non-None) dx values -
    # matches the original's `sum(dx_valid[-period:]) / period`.
    dq: deque[float] = deque()
    running_sum = 0.0
    for i in range(n):
        if dx[i] is None:
            continue
        dq.append(dx[i])
        running_sum += dx[i]
        if len(dq) > period:
            running_sum -= dq.popleft()
        if len(dq) == period:
            adx = running_sum / period
            if adx >= threshold and str_[i]:
                pdi = 100 * pdm[i] / str_[i]
                mdi = 100 * mdm[i] / str_[i]
                out[i] = "BUY" if pdi > mdi else "SELL"
    return out


# --------------------------------------------------------------------------
# Fast / low-lag indicators
# --------------------------------------------------------------------------

def series_stoch_rsi(bars: list[dict], p: dict) -> list[Optional[str]]:
    """Stochastic applied to RSI itself - reacts much faster than plain RSI."""
    closes = _closes(bars)
    rsi_period = int(p.get("rsi_period", 14))
    stoch_period = int(p.get("stoch_period", 14))
    oversold = float(p.get("oversold", 20))
    overbought = float(p.get("overbought", 80))
    n = len(closes)
    out: list[Optional[str]] = [None] * n
    rsi = _rsi_series(closes, rsi_period)
    start = next((i for i, v in enumerate(rsi) if v is not None), None)
    if start is None:
        return out
    valid = rsi[start:]
    hh = _rolling_max_series(valid, stoch_period)
    ll = _rolling_min_series(valid, stoch_period)
    for j in range(len(valid)):
        if hh[j] is None or ll[j] is None or hh[j] == ll[j]:
            continue
        k = 100 * (valid[j] - ll[j]) / (hh[j] - ll[j])
        i = start + j
        if k < oversold:
            out[i] = "BUY"
        elif k > overbought:
            out[i] = "SELL"
    return out


def series_hull_cross(bars: list[dict], p: dict) -> list[Optional[str]]:
    """Hull MA crossover - same idea as sma_cross/ema_cross but far less lag."""
    closes = _closes(bars)
    fast = int(p.get("fast", 9))
    slow = int(p.get("slow", 21))
    return _cross_series(_hma_series(closes, fast), _hma_series(closes, slow))


def series_supertrend(bars: list[dict], p: dict) -> list[Optional[str]]:
    """ATR-based trend flip - fires right as the trend turns, no fixed lag."""
    period = int(p.get("period", 10))
    mult = float(p.get("multiplier", 3))
    highs, lows, closes = _highs(bars), _lows(bars), _closes(bars)
    n = len(closes)
    out: list[Optional[str]] = [None] * n
    if n < period * 2:
        return out
    atr = _atr_series(highs, lows, closes, period)

    final_upper: list[Optional[float]] = [None] * n
    final_lower: list[Optional[float]] = [None] * n
    uptrend: list[Optional[bool]] = [None] * n
    for i in range(period, n):
        if atr[i] is None:
            continue
        mid = (highs[i] + lows[i]) / 2
        basic_upper = mid + mult * atr[i]
        basic_lower = mid - mult * atr[i]
        if final_upper[i - 1] is None:
            final_upper[i] = basic_upper
            final_lower[i] = basic_lower
            uptrend[i] = closes[i] >= mid
            continue
        prev_upper, prev_lower = final_upper[i - 1], final_lower[i - 1]
        final_upper[i] = basic_upper if (basic_upper < prev_upper or closes[i - 1] > prev_upper) else prev_upper
        final_lower[i] = basic_lower if (basic_lower > prev_lower or closes[i - 1] < prev_lower) else prev_lower
        if uptrend[i - 1]:
            uptrend[i] = closes[i] >= final_lower[i]
        else:
            uptrend[i] = closes[i] > final_upper[i]

    for i in range(period + 1, n):
        if uptrend[i] is None or uptrend[i - 1] is None:
            continue
        if uptrend[i] and not uptrend[i - 1]:
            out[i] = "BUY"
        elif not uptrend[i] and uptrend[i - 1]:
            out[i] = "SELL"
    return out


def series_vortex(bars: list[dict], p: dict) -> list[Optional[str]]:
    """VI+/VI- crossover - flips direction faster than ADX's +DI/-DI."""
    period = int(p.get("period", 14))
    highs, lows, closes = _highs(bars), _lows(bars), _closes(bars)
    n = len(closes)
    out: list[Optional[str]] = [None] * n
    if n < period + 2:
        return out
    vm_plus = [0.0] * n
    vm_minus = [0.0] * n
    tr = [0.0] * n
    for i in range(1, n):
        vm_plus[i] = abs(highs[i] - lows[i - 1])
        vm_minus[i] = abs(lows[i] - highs[i - 1])
        tr[i] = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
    # SMA already computes a rolling sum/period - divide two of them and the
    # /period cancels, giving the same ratio as summing raw windows each bar.
    tr_sum = _sma_series(tr, period)
    vp_sum = _sma_series(vm_plus, period)
    vm_sum = _sma_series(vm_minus, period)
    vi_plus: list[Optional[float]] = [None] * n
    vi_minus: list[Optional[float]] = [None] * n
    for i in range(period, n):
        if tr_sum[i] is None or tr_sum[i] == 0:
            continue
        vi_plus[i] = vp_sum[i] / tr_sum[i]
        vi_minus[i] = vm_sum[i] / tr_sum[i]
    return _cross_series(vi_plus, vi_minus)


def series_psar(bars: list[dict], p: dict) -> list[Optional[str]]:
    """Parabolic SAR reversal - flags a trend flip the bar it happens."""
    step = float(p.get("step", 0.02))
    max_step = float(p.get("max_step", 0.2))
    highs, lows, closes = _highs(bars), _lows(bars), _closes(bars)
    n = len(closes)
    out: list[Optional[str]] = [None] * n
    if n < 5:
        return out
    uptrend = closes[1] >= closes[0]
    af = step
    ep = highs[0] if uptrend else lows[0]
    sar = lows[0] if uptrend else highs[0]
    trend_hist = [uptrend]
    for i in range(1, n):
        sar = sar + af * (ep - sar)
        if uptrend:
            sar = min(sar, lows[i - 1], lows[i - 2] if i >= 2 else lows[i - 1])
            if lows[i] < sar:
                uptrend, sar, ep, af = False, ep, lows[i], step
            elif highs[i] > ep:
                ep = highs[i]
                af = min(af + step, max_step)
        else:
            sar = max(sar, highs[i - 1], highs[i - 2] if i >= 2 else highs[i - 1])
            if highs[i] > sar:
                uptrend, sar, ep, af = True, ep, highs[i], step
            elif lows[i] < ep:
                ep = lows[i]
                af = min(af + step, max_step)
        trend_hist.append(uptrend)
    for i in range(1, n):
        if trend_hist[i] and not trend_hist[i - 1]:
            out[i] = "BUY"
        elif not trend_hist[i] and trend_hist[i - 1]:
            out[i] = "SELL"
    return out


def series_dema_cross(bars: list[dict], p: dict) -> list[Optional[str]]:
    """DEMA crossover - less lag than an equal-period EMA cross."""
    closes = _closes(bars)
    fast = int(p.get("fast", 9))
    slow = int(p.get("slow", 21))
    return _cross_series(_dema_series(closes, fast), _dema_series(closes, slow))


def series_tema_cross(bars: list[dict], p: dict) -> list[Optional[str]]:
    """TEMA crossover - even less lag than DEMA."""
    closes = _closes(bars)
    fast = int(p.get("fast", 9))
    slow = int(p.get("slow", 21))
    return _cross_series(_tema_series(closes, fast), _tema_series(closes, slow))


def series_awesome_oscillator(bars: list[dict], p: dict) -> list[Optional[str]]:
    """Bill Williams AO: SMA(5)-SMA(34) of median price, zero-line cross."""
    highs, lows = _highs(bars), _lows(bars)
    fast = int(p.get("fast", 5))
    slow = int(p.get("slow", 34))
    n = len(highs)
    mp = [(highs[i] + lows[i]) / 2 for i in range(n)]
    sma_fast = _sma_series(mp, fast)
    sma_slow = _sma_series(mp, slow)
    ao: list[Optional[float]] = [None] * n
    for i in range(n):
        if sma_fast[i] is not None and sma_slow[i] is not None:
            ao[i] = sma_fast[i] - sma_slow[i]
    out: list[Optional[str]] = [None] * n
    for i in range(1, n):
        a0, a1 = ao[i - 1], ao[i]
        if a0 is None or a1 is None:
            continue
        if a0 <= 0 < a1:
            out[i] = "BUY"
        elif a0 >= 0 > a1:
            out[i] = "SELL"
    return out


def series_kama_cross(bars: list[dict], p: dict) -> list[Optional[str]]:
    """Price crossing its own adaptive KAMA line - standard KAMA usage."""
    closes = _closes(bars)
    period = int(p.get("period", 10))
    fast = int(p.get("fast", 2))
    slow = int(p.get("slow", 30))
    kama = _kama_series(closes, period, fast, slow)
    return _cross_series(closes, kama)


def series_fisher_transform(bars: list[dict], p: dict) -> list[Optional[str]]:
    """Fisher Transform crossing its own 1-bar-lagged trigger line."""
    highs, lows = _highs(bars), _lows(bars)
    period = int(p.get("period", 10))
    n = len(highs)
    if n == 0:
        return []
    fish = _fisher_series(highs, lows, period)
    trigger: list[Optional[float]] = [None] + fish[:-1]
    return _cross_series(fish, trigger)


def series_donchian_breakout(bars: list[dict], p: dict) -> list[Optional[str]]:
    """Close breaking above/below the PRIOR N-bar high/low channel."""
    highs, lows, closes = _highs(bars), _lows(bars), _closes(bars)
    period = int(p.get("period", 20))
    n = len(closes)
    out: list[Optional[str]] = [None] * n
    hh = _rolling_max_series(highs, period)
    ll = _rolling_min_series(lows, period)
    for i in range(1, n):
        if hh[i - 1] is None or ll[i - 1] is None:
            continue
        if closes[i] > hh[i - 1]:
            out[i] = "BUY"
        elif closes[i] < ll[i - 1]:
            out[i] = "SELL"
    return out


def series_keltner_breakout(bars: list[dict], p: dict) -> list[Optional[str]]:
    """Close breaking outside an EMA +/- multiplier*ATR channel."""
    highs, lows, closes = _highs(bars), _lows(bars), _closes(bars)
    period = int(p.get("period", 20))
    mult = float(p.get("multiplier", 2))
    n = len(closes)
    out: list[Optional[str]] = [None] * n
    ema = _ema_series(closes, period)
    atr = _atr_series(highs, lows, closes, period)
    upper: list[Optional[float]] = [None] * n
    lower: list[Optional[float]] = [None] * n
    for i in range(n):
        if ema[i] is not None and atr[i] is not None:
            upper[i] = ema[i] + mult * atr[i]
            lower[i] = ema[i] - mult * atr[i]
    for i in range(1, n):
        if upper[i - 1] is None or lower[i - 1] is None or upper[i] is None or lower[i] is None:
            continue
        if closes[i - 1] <= upper[i - 1] and closes[i] > upper[i]:
            out[i] = "BUY"
        elif closes[i - 1] >= lower[i - 1] and closes[i] < lower[i]:
            out[i] = "SELL"
    return out


def series_chandelier_exit(bars: list[dict], p: dict) -> list[Optional[str]]:
    """ATR trailing stop from the N-bar high/low, ratcheted like Supertrend
    - direction flips (and fires) when price crosses the opposite stop."""
    highs, lows, closes = _highs(bars), _lows(bars), _closes(bars)
    period = int(p.get("period", 22))
    mult = float(p.get("multiplier", 3))
    n = len(closes)
    out: list[Optional[str]] = [None] * n
    if n < period + 2:
        return out
    atr = _atr_series(highs, lows, closes, period)
    hh = _rolling_max_series(highs, period)
    ll = _rolling_min_series(lows, period)

    long_stop: list[Optional[float]] = [None] * n
    short_stop: list[Optional[float]] = [None] * n
    direction: list[Optional[bool]] = [None] * n
    for i in range(n):
        if atr[i] is None or hh[i] is None or ll[i] is None:
            continue
        raw_long = hh[i] - mult * atr[i]
        raw_short = ll[i] + mult * atr[i]
        if i == 0 or long_stop[i - 1] is None:
            long_stop[i] = raw_long
            short_stop[i] = raw_short
            direction[i] = True
            continue
        prev_long, prev_short = long_stop[i - 1], short_stop[i - 1]
        long_stop[i] = max(raw_long, prev_long) if closes[i - 1] > prev_long else raw_long
        short_stop[i] = min(raw_short, prev_short) if closes[i - 1] < prev_short else raw_short
        if closes[i] > short_stop[i - 1]:
            direction[i] = True
        elif closes[i] < long_stop[i - 1]:
            direction[i] = False
        else:
            direction[i] = direction[i - 1]

    for i in range(1, n):
        if direction[i] is None or direction[i - 1] is None:
            continue
        if direction[i] and not direction[i - 1]:
            out[i] = "BUY"
        elif not direction[i] and direction[i - 1]:
            out[i] = "SELL"
    return out


# --------------------------------------------------------------------------
# Japanese candlestick indicators - already O(1)-O(3) per bar, series form
# is just the same per-bar check run at every index.
# --------------------------------------------------------------------------

def series_heikin_ashi(bars: list[dict], p: dict) -> list[Optional[str]]:
    trend = max(1, int(p.get("trend_bars", 2)))
    n = len(bars)
    out: list[Optional[str]] = [None] * n
    if n < trend + 1:
        return out
    ha = _heikin_ashi(bars)
    for i in range(trend - 1, n):
        window = ha[i - trend + 1:i + 1]
        if all(c["close"] > c["open"] for c in window):
            out[i] = "BUY"
        elif all(c["close"] < c["open"] for c in window):
            out[i] = "SELL"
    return out


def series_engulfing(bars: list[dict], p: dict) -> list[Optional[str]]:
    n = len(bars)
    out: list[Optional[str]] = [None] * n
    for i in range(1, n):
        po, ph, pl, pc = _ohlc(bars[i - 1])
        o, h, l, c = _ohlc(bars[i])
        prev_bear = pc < po
        prev_bull = pc > po
        cur_bull = c > o
        cur_bear = c < o
        if cur_bull and prev_bear and o <= pc and c >= po:
            out[i] = "BUY"
        elif cur_bear and prev_bull and o >= pc and c <= po:
            out[i] = "SELL"
    return out


def series_hammer(bars: list[dict], p: dict) -> list[Optional[str]]:
    ratio = float(p.get("wick_ratio", 2.0))
    n = len(bars)
    out: list[Optional[str]] = [None] * n
    for i in range(n):
        b = bars[i]
        body = _body(b)
        if body <= 0:
            continue
        up = _upper_wick(b)
        lo = _lower_wick(b)
        # Hammer: long lower wick, small upper wick -> bullish reversal
        if lo >= ratio * body and up <= body:
            out[i] = "BUY"
        # Shooting star: long upper wick, small lower wick -> bearish reversal
        elif up >= ratio * body and lo <= body:
            out[i] = "SELL"
    return out


def series_three_soldiers(bars: list[dict], p: dict) -> list[Optional[str]]:
    n = len(bars)
    out: list[Optional[str]] = [None] * n
    for i in range(2, n):
        c3 = bars[i - 2:i + 1]
        o = [float(b["open"]) for b in c3]
        c = [float(b["close"]) for b in c3]
        bull = all(c[k] > o[k] for k in range(3)) and c[0] < c[1] < c[2]
        bear = all(c[k] < o[k] for k in range(3)) and c[0] > c[1] > c[2]
        if bull:
            out[i] = "BUY"
        elif bear:
            out[i] = "SELL"
    return out


def series_marubozu(bars: list[dict], p: dict) -> list[Optional[str]]:
    body_ratio = float(p.get("body_ratio", 0.9))
    n = len(bars)
    out: list[Optional[str]] = [None] * n
    for i in range(n):
        o, h, l, c = _ohlc(bars[i])
        rng = h - l
        if rng <= 0:
            continue
        if _body(bars[i]) / rng < body_ratio:
            continue
        if c > o:
            out[i] = "BUY"
        elif c < o:
            out[i] = "SELL"
    return out


# --------------------------------------------------------------------------
# sig_<name> - thin "latest signal" wrappers used by the live EntryManager.
# --------------------------------------------------------------------------

def _last(series: list[Optional[str]]) -> Optional[str]:
    return series[-1] if series else None


def sig_rsi(bars: list[dict], p: dict) -> Optional[str]:
    return _last(series_rsi(bars, p))


def sig_macd(bars: list[dict], p: dict) -> Optional[str]:
    return _last(series_macd(bars, p))


def sig_sma_cross(bars: list[dict], p: dict) -> Optional[str]:
    return _last(series_sma_cross(bars, p))


def sig_ema_cross(bars: list[dict], p: dict) -> Optional[str]:
    return _last(series_ema_cross(bars, p))


def sig_bollinger(bars: list[dict], p: dict) -> Optional[str]:
    return _last(series_bollinger(bars, p))


def sig_stochastic(bars: list[dict], p: dict) -> Optional[str]:
    return _last(series_stochastic(bars, p))


def sig_cci(bars: list[dict], p: dict) -> Optional[str]:
    return _last(series_cci(bars, p))


def sig_momentum(bars: list[dict], p: dict) -> Optional[str]:
    return _last(series_momentum(bars, p))


def sig_williams(bars: list[dict], p: dict) -> Optional[str]:
    return _last(series_williams(bars, p))


def sig_adx(bars: list[dict], p: dict) -> Optional[str]:
    return _last(series_adx(bars, p))


def sig_stoch_rsi(bars: list[dict], p: dict) -> Optional[str]:
    return _last(series_stoch_rsi(bars, p))


def sig_hull_cross(bars: list[dict], p: dict) -> Optional[str]:
    return _last(series_hull_cross(bars, p))


def sig_supertrend(bars: list[dict], p: dict) -> Optional[str]:
    return _last(series_supertrend(bars, p))


def sig_vortex(bars: list[dict], p: dict) -> Optional[str]:
    return _last(series_vortex(bars, p))


def sig_psar(bars: list[dict], p: dict) -> Optional[str]:
    return _last(series_psar(bars, p))


def sig_dema_cross(bars: list[dict], p: dict) -> Optional[str]:
    return _last(series_dema_cross(bars, p))


def sig_tema_cross(bars: list[dict], p: dict) -> Optional[str]:
    return _last(series_tema_cross(bars, p))


def sig_awesome_oscillator(bars: list[dict], p: dict) -> Optional[str]:
    return _last(series_awesome_oscillator(bars, p))


def sig_kama_cross(bars: list[dict], p: dict) -> Optional[str]:
    return _last(series_kama_cross(bars, p))


def sig_fisher_transform(bars: list[dict], p: dict) -> Optional[str]:
    return _last(series_fisher_transform(bars, p))


def sig_donchian_breakout(bars: list[dict], p: dict) -> Optional[str]:
    return _last(series_donchian_breakout(bars, p))


def sig_keltner_breakout(bars: list[dict], p: dict) -> Optional[str]:
    return _last(series_keltner_breakout(bars, p))


def sig_chandelier_exit(bars: list[dict], p: dict) -> Optional[str]:
    return _last(series_chandelier_exit(bars, p))


def sig_heikin_ashi(bars: list[dict], p: dict) -> Optional[str]:
    return _last(series_heikin_ashi(bars, p))


def sig_engulfing(bars: list[dict], p: dict) -> Optional[str]:
    return _last(series_engulfing(bars, p))


def sig_hammer(bars: list[dict], p: dict) -> Optional[str]:
    return _last(series_hammer(bars, p))


def sig_three_soldiers(bars: list[dict], p: dict) -> Optional[str]:
    return _last(series_three_soldiers(bars, p))


def sig_marubozu(bars: list[dict], p: dict) -> Optional[str]:
    return _last(series_marubozu(bars, p))


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
    "stoch_rsi": sig_stoch_rsi,
    "hull_cross": sig_hull_cross,
    "supertrend": sig_supertrend,
    "vortex": sig_vortex,
    "psar": sig_psar,
    "dema_cross": sig_dema_cross,
    "tema_cross": sig_tema_cross,
    "awesome_oscillator": sig_awesome_oscillator,
    "kama_cross": sig_kama_cross,
    "fisher_transform": sig_fisher_transform,
    "donchian_breakout": sig_donchian_breakout,
    "keltner_breakout": sig_keltner_breakout,
    "chandelier_exit": sig_chandelier_exit,
    "heikin_ashi": sig_heikin_ashi,
    "engulfing": sig_engulfing,
    "hammer": sig_hammer,
    "three_soldiers": sig_three_soldiers,
    "marubozu": sig_marubozu,
}

_SERIES_DISPATCH: dict[str, Callable[[list[dict], dict], list[Optional[str]]]] = {
    "rsi": series_rsi,
    "macd": series_macd,
    "sma_cross": series_sma_cross,
    "ema_cross": series_ema_cross,
    "bollinger": series_bollinger,
    "stochastic": series_stochastic,
    "cci": series_cci,
    "momentum": series_momentum,
    "williams": series_williams,
    "adx": series_adx,
    "stoch_rsi": series_stoch_rsi,
    "hull_cross": series_hull_cross,
    "supertrend": series_supertrend,
    "vortex": series_vortex,
    "psar": series_psar,
    "dema_cross": series_dema_cross,
    "tema_cross": series_tema_cross,
    "awesome_oscillator": series_awesome_oscillator,
    "kama_cross": series_kama_cross,
    "fisher_transform": series_fisher_transform,
    "donchian_breakout": series_donchian_breakout,
    "keltner_breakout": series_keltner_breakout,
    "chandelier_exit": series_chandelier_exit,
    "heikin_ashi": series_heikin_ashi,
    "engulfing": series_engulfing,
    "hammer": series_hammer,
    "three_soldiers": series_three_soldiers,
    "marubozu": series_marubozu,
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


def indicator_signal_series(key: str, bars: list[dict], params: dict) -> list[Optional[str]]:
    """Return the BUY/SELL/None signal at every bar index for one indicator,
    computed once over the whole `bars` array. Lets the backtester avoid
    recomputing each indicator from scratch on every simulated bar - same
    defaults-merge and never-raises contract as indicator_signal()."""
    fn = _SERIES_DISPATCH.get(key)
    if fn is None:
        return [None] * len(bars)
    merged = {**INDICATOR_DEFAULTS.get(key, {}), **(params or {})}
    try:
        return fn(bars, merged)
    except Exception:
        return [None] * len(bars)


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
