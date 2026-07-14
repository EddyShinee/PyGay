"""Backtest the indicator/ML entry logic over historical bars.

Replays the SAME signal code the live EntryManager uses
(indicators.indicator_signal + combine_signals, ml_entry features) bar by
bar on closed bars, simulates each entry's SL/TP outcome from later bars'
high/low, and reports win rate / trade count / profit factor - so an entry
config can be tuned on evidence instead of guesswork.

Realism choices:
  - signals are computed on closed bars only, one entry max per bar
    (matches the live once-per-bar gate);
  - entry price = next bar's open, +spread (from the bar's own spread
    field) for BUY - the cost a market order actually pays;
  - if a bar's range touches both SL and TP, it counts as SL (conservative);
  - only_if_flat is honored: no new entry while a simulated one is open.

Limitations (documented, deliberate):
  - per-indicator timeframe overrides fall back to the common timeframe
    (one bar series per run);
  - trade_hours uses bar timestamps, which are broker-server time - treat
    the window as approximate;
  - no swap/commission modeling; results are in points, not USD.
"""
from __future__ import annotations

import math
from datetime import datetime
from typing import Callable, Optional

import indicators
import ml_entry

# Live EntryManager fetches 300 bars for signals - use the same window so
# indicator values match what the live system would compute.
SIGNAL_WINDOW = 300
MIN_WARMUP = 60
MAX_BARS = 5000
# When no TP/SL configured, exit at close this many bars after entry.
DEFAULT_EXIT_BARS = 10


def _make_ml_predictor(model: dict) -> Optional[Callable[[list], Optional[float]]]:
    """predict_proba equivalent with the booster decoded ONCE - decoding a
    pickled tree model per bar would dominate the whole backtest."""
    if not model:
        return None
    algo = str(model.get("algo") or "logistic").lower()

    if algo == "logistic" or (model.get("weights") and not model.get("booster_b64")):
        def predict_logistic(bars: list) -> Optional[float]:
            return ml_entry.predict_proba(bars, model)  # cheap, no booster
        return predict_logistic

    b64 = model.get("booster_b64")
    if not b64:
        return None
    try:
        booster = ml_entry._decode_booster(b64)
        import numpy as np
    except Exception:
        return None

    def predict_tree(bars: list) -> Optional[float]:
        row = ml_entry._latest_features(bars, model)
        if row is None:
            return None
        try:
            proba = ml_entry._tree_predict_proba(booster, np.asarray([row], dtype=np.float64))[0]
            return float(proba[1])
        except Exception:
            return None

    return predict_tree


def _indicator_side(cfg: dict, bars: list) -> Optional[str]:
    """Same combination semantics as EntryManager._indicator_signal."""
    cfg_inds = cfg.get("indicators") or {}
    enabled = [
        (key, params)
        for key, params in cfg_inds.items()
        if isinstance(params, dict) and params.get("enabled")
    ]
    if not enabled:
        return None
    buys = sells = 0
    for key, params in enabled:
        sig = indicators.indicator_signal(key, bars, params)
        if sig == "BUY":
            buys += 1
        elif sig == "SELL":
            sells += 1
    return indicators.combine_signals(
        buys, sells, len(enabled),
        cfg.get("indicator_logic") or "all",
        cfg.get("indicator_min_agree") or 1,
        cfg.get("indicator_min_margin") or 1,
    )


def _trend_side_ok(side: str, bar_time: int, trend_times: list,
                   trend_ema: list, trend_closes: list) -> bool:
    """Binary-search the last CLOSED trend bar before `bar_time` and apply
    the same above/below-EMA rule as EntryManager._trend_allows."""
    if not trend_times:
        return True
    lo, hi = 0, len(trend_times) - 1
    idx = -1
    while lo <= hi:
        mid = (lo + hi) // 2
        if trend_times[mid] < bar_time:
            idx = mid
            lo = mid + 1
        else:
            hi = mid - 1
    if idx < 0 or trend_ema[idx] is None:
        return True
    if side == "BUY":
        return trend_closes[idx] > trend_ema[idx]
    return trend_closes[idx] < trend_ema[idx]


def _in_hours(spec: Optional[str], epoch: int) -> bool:
    if not spec or not spec.strip():
        return True
    try:
        start_s, end_s = spec.split("-")
        sh, sm = (int(x) for x in start_s.strip().split(":"))
        eh, em = (int(x) for x in end_s.strip().split(":"))
    except (ValueError, AttributeError):
        return True
    t = datetime.fromtimestamp(epoch)
    cur = t.hour * 60 + t.minute
    start, end = sh * 60 + sm, eh * 60 + em
    if start == end:
        return True
    if start < end:
        return start <= cur < end
    return cur >= start or cur < end


def run_backtest(
    bars: list[dict],
    config: dict,
    point: float,
    trend_bars: Optional[list[dict]] = None,
) -> dict:
    """Simulate the entry config over `bars` (ascending OHLC). Returns a
    metrics dict; raises ValueError on unusable input."""
    if point <= 0:
        raise ValueError("point phải > 0")
    bars = sorted(bars, key=lambda b: int(b["time"]))[-MAX_BARS:]
    if len(bars) < MIN_WARMUP + 10:
        raise ValueError(f"Không đủ nến để backtest (có {len(bars)}, cần >= {MIN_WARMUP + 10}).")

    mode = (config.get("trigger_mode") or "").lower()
    if mode not in ("indicators", "ml", "indicators_ml"):
        raise ValueError("Backtest chỉ hỗ trợ chế độ 'indicators', 'ml' hoặc 'indicators_ml'.")

    allowed = (config.get("side") or "BOTH").upper()
    unit = 10.0 if (config.get("sltp_unit") or "points").lower() == "pips" else 1.0
    sl_pts = float(config.get("sl_distance") or 0) * unit
    tp_pts = float(config.get("tp_distance") or 0) * unit
    only_if_flat = bool(config.get("only_if_flat"))
    trade_hours = config.get("trade_hours")

    ml_cfg = config.get("ml") or {}
    predictor: Optional[Callable] = None
    threshold = float(ml_cfg.get("threshold", 0.58))
    if mode in ("ml", "indicators_ml"):
        predictor = _make_ml_predictor(ml_cfg.get("model") or {})
        if predictor is None:
            raise ValueError("Chưa có mô hình ML đã huấn luyện trong cấu hình.")

    # Trend filter series, precomputed once.
    trend_times: list = []
    trend_ema: list = []
    trend_closes: list = []
    tf_cfg = config.get("trend_filter") or {}
    trend_enabled = bool(tf_cfg.get("enabled"))
    if trend_enabled and trend_bars:
        tb = sorted(trend_bars, key=lambda b: int(b["time"]))
        trend_times = [int(b["time"]) for b in tb]
        trend_closes = [float(b["close"]) for b in tb]
        period = max(2, int(tf_cfg.get("ema_period") or 200))
        trend_ema = indicators._ema_series(trend_closes, period)

    n = len(bars)
    times = [int(b["time"]) for b in bars]
    opens = [float(b["open"]) for b in bars]
    highs = [float(b["high"]) for b in bars]
    lows = [float(b["low"]) for b in bars]
    closes = [float(b["close"]) for b in bars]
    spreads = [float(b.get("spread") or 0) for b in bars]

    trades: list[dict] = []
    open_until = -1  # index of the bar the current simulated trade exits on
    signals_total = 0
    blocked_trend = 0
    blocked_hours = 0
    confirm_need = max(1, int(config.get("confirm_bars") or 1))
    streak_side: Optional[str] = None
    streak_count = 0

    for i in range(MIN_WARMUP, n - 1):
        window = bars[max(0, i - SIGNAL_WINDOW + 1):i + 1]

        side: Optional[str] = None
        if mode in ("indicators", "indicators_ml"):
            side = _indicator_side(config, window)
            # Consecutive-bar confirmation applies to the INDICATOR verdict,
            # before ML gets a veto - same order as the live EntryManager.
            if side is not None and side == streak_side:
                streak_count += 1
            elif side is not None:
                streak_side, streak_count = side, 1
            else:
                streak_side, streak_count = None, 0
            if side is not None and streak_count < confirm_need:
                side = None
            if side is not None and mode == "indicators_ml":
                proba = predictor(window)
                ml_side = None
                if proba is not None:
                    if proba >= threshold:
                        ml_side = "BUY"
                    elif proba <= 1 - threshold:
                        ml_side = "SELL"
                if ml_side != side:
                    side = None
        else:  # ml
            proba = predictor(window)
            if proba is not None:
                if proba >= threshold:
                    side = "BUY"
                elif proba <= 1 - threshold:
                    side = "SELL"

        if side is None:
            continue
        if only_if_flat and i <= open_until:
            continue
        if allowed in ("BUY", "SELL") and side != allowed:
            continue
        signals_total += 1

        if not _in_hours(trade_hours, times[i]):
            blocked_hours += 1
            continue
        if trend_enabled and not _trend_side_ok(side, times[i], trend_times, trend_ema, trend_closes):
            blocked_trend += 1
            continue

        # Enter at next bar's open; BUY pays the spread on top (OHLC is bid).
        entry_idx = i + 1
        spread_cost = spreads[entry_idx] * point
        entry = opens[entry_idx] + (spread_cost if side == "BUY" else 0.0)

        sign = 1 if side == "BUY" else -1
        sl = entry - sign * sl_pts * point if sl_pts else None
        tp = entry + sign * tp_pts * point if tp_pts else None

        exit_price = None
        exit_idx = None
        outcome = None
        for j in range(entry_idx, min(n, entry_idx + 1000)):
            # For SELL, exit crosses the ask: approximate with bid + spread.
            j_spread = spreads[j] * point
            hi_px = highs[j] + (j_spread if side == "SELL" else 0.0)
            lo_px = lows[j] + (j_spread if side == "SELL" else 0.0)
            if side == "BUY":
                if sl is not None and lows[j] <= sl:
                    exit_price, exit_idx, outcome = sl, j, "sl"
                    break
                if tp is not None and highs[j] >= tp:
                    exit_price, exit_idx, outcome = tp, j, "tp"
                    break
            else:
                if sl is not None and hi_px >= sl:
                    exit_price, exit_idx, outcome = sl, j, "sl"
                    break
                if tp is not None and lo_px <= tp:
                    exit_price, exit_idx, outcome = tp, j, "tp"
                    break
            if sl is None and tp is None and j >= entry_idx + DEFAULT_EXIT_BARS - 1:
                exit_price, exit_idx, outcome = closes[j], j, "time"
                break

        if exit_price is None:  # still open at the end of data
            exit_price, exit_idx, outcome = closes[-1], n - 1, "eod"

        pnl_pts = (exit_price - entry) / point * sign
        trades.append({
            "time": times[i],
            "side": side,
            "entry": entry,
            "exit": exit_price,
            "outcome": outcome,
            "pnl_points": round(pnl_pts, 1),
            "bars_held": exit_idx - entry_idx + 1,
        })
        open_until = exit_idx

    wins = [t for t in trades if t["pnl_points"] > 0]
    losses = [t for t in trades if t["pnl_points"] <= 0]
    gross_win = sum(t["pnl_points"] for t in wins)
    gross_loss = -sum(t["pnl_points"] for t in losses)
    total = len(trades)

    return {
        "bars_tested": n,
        "from": times[0],
        "to": times[-1],
        "signals": signals_total,
        "blocked_by_trend": blocked_trend,
        "blocked_by_hours": blocked_hours,
        "trades": total,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / total * 100, 1) if total else 0.0,
        "total_points": round(sum(t["pnl_points"] for t in trades), 1),
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else None,
        "avg_win_points": round(gross_win / len(wins), 1) if wins else 0.0,
        "avg_loss_points": round(-gross_loss / len(losses), 1) if losses else 0.0,
        "avg_bars_held": round(sum(t["bars_held"] for t in trades) / total, 1) if total else 0.0,
        "recent_trades": trades[-20:],
    }
