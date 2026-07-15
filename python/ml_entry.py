"""Machine-learning entry models: logistic, XGBoost, LightGBM.

Predicts whether price goes UP or DOWN over the next `lookahead` bars from
OHLC (+ tick volume + bar time) features. The trained artefact is a
JSON-serialisable dict stored inside the account entry config (Supabase JSONB).

Feature set (see `feature_names()`):
  - lagged returns, RSI, MACD-hist/EMA-gap, Bollinger-z, momentum
  - candle microstructure (range/body/wicks), ATR%, volume z-score
  - Stochastic %K, CCI, Williams %R, ADX/+-DI, candlestick pattern score
  - hour-of-day / day-of-week (sin/cos) + trading-session flags
  - higher-timeframe EMA(200) distance/direction (optional `htf_bars`)

Algorithms:
  - logistic  — pure-Python gradient descent (no extra deps)
  - xgboost   — gradient-boosted trees (requires `xgboost`)
  - lightgbm  — LightGBM trees (requires `lightgbm`)

Training always reports a chronological hold-out accuracy (`accuracy` =
val accuracy) so the UI does not confuse in-sample fit with real skill.
"""
from __future__ import annotations

import base64
import math
import pickle
import time
from typing import Optional

import indicators as ind

MODEL_VERSION = 5

ALGORITHMS = ("logistic", "xgboost", "lightgbm")

ML_DEFAULTS = {
    "algo": "xgboost",
    "timeframe": "H1",
    "lookahead": 3,
    "lags": 5,
    "threshold": 0.58,
    "epochs": 400,       # logistic only
    "lr": 0.1,           # logistic only
    "l2": 0.001,         # logistic only
    "n_estimators": 400, # tree boosters (upper ceiling - early stopping trims it)
    "max_depth": 4,
    "learning_rate": 0.05,
    "val_ratio": 0.2,    # chronological hold-out fraction
}


def _closes(bars: list[dict]) -> list[float]:
    return [float(b["close"]) for b in bars]


def _rsi_series(closes: list[float], period: int = 14) -> list[Optional[float]]:
    n = len(closes)
    out: list[Optional[float]] = [None] * n
    if n < period + 1:
        return out
    gains = losses = 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        gains += d if d > 0 else 0.0
        losses += -d if d < 0 else 0.0
    avg_gain = gains / period
    avg_loss = losses / period
    out[period] = 100.0 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss)
    for i in range(period + 1, n):
        d = closes[i] - closes[i - 1]
        g = d if d > 0 else 0.0
        l = -d if d < 0 else 0.0
        avg_gain = (avg_gain * (period - 1) + g) / period
        avg_loss = (avg_loss * (period - 1) + l) / period
        out[i] = 100.0 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss)
    return out


def feature_names(lags: int) -> list[str]:
    names = [f"ret_{k}" for k in range(1, lags + 1)]
    names += [
        "rsi", "macd_hist", "ema_gap", "boll_z", "mom",
        "hl_range", "body", "upper_wick", "lower_wick",
        "atr_pct", "vol_z",
        "stoch_k", "cci", "williams_r", "adx", "di_diff", "pattern",
        "hour_sin", "hour_cos", "dow_sin", "dow_cos",
        "session_asia", "session_london", "session_ny",
        "htf_dist", "htf_dir",
    ]
    return names


def _safe_vol(bar: dict) -> float:
    v = bar.get("tick_volume")
    if v is None:
        v = bar.get("volume")
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _stoch_series(highs: list[float], lows: list[float], closes: list[float],
                  period: int = 14) -> list[Optional[float]]:
    n = len(closes)
    out: list[Optional[float]] = [None] * n
    for i in range(period - 1, n):
        hh = max(highs[i - period + 1:i + 1])
        ll = min(lows[i - period + 1:i + 1])
        if hh != ll:
            out[i] = 100 * (closes[i] - ll) / (hh - ll)
    return out


def _williams_series(highs: list[float], lows: list[float], closes: list[float],
                     period: int = 14) -> list[Optional[float]]:
    n = len(closes)
    out: list[Optional[float]] = [None] * n
    for i in range(period - 1, n):
        hh = max(highs[i - period + 1:i + 1])
        ll = min(lows[i - period + 1:i + 1])
        if hh != ll:
            out[i] = -100 * (hh - closes[i]) / (hh - ll)
    return out


def _cci_series(highs: list[float], lows: list[float], closes: list[float],
                period: int = 20) -> list[Optional[float]]:
    n = len(closes)
    out: list[Optional[float]] = [None] * n
    tp = [(highs[i] + lows[i] + closes[i]) / 3 for i in range(n)]
    for i in range(period - 1, n):
        window = tp[i - period + 1:i + 1]
        ma = sum(window) / period
        md = sum(abs(x - ma) for x in window) / period
        if md != 0:
            out[i] = (tp[i] - ma) / (0.015 * md)
    return out


def _adx_di_series(highs: list[float], lows: list[float], closes: list[float],
                   period: int = 14):
    """Wilder-smoothed ADX / +DI / -DI, one value per bar (None until warmed up)."""
    n = len(closes)
    adx: list[Optional[float]] = [None] * n
    plus_di: list[Optional[float]] = [None] * n
    minus_di: list[Optional[float]] = [None] * n
    if n < period * 2 + 1:
        return adx, plus_di, minus_di

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
            pdi_i = 100 * pdm[i] / str_[i]
            mdi_i = 100 * mdm[i] / str_[i]
            plus_di[i] = pdi_i
            minus_di[i] = mdi_i
            if pdi_i + mdi_i != 0:
                dx[i] = 100 * abs(pdi_i - mdi_i) / (pdi_i + mdi_i)

    window: list[float] = []
    for i in range(n):
        if dx[i] is not None:
            window.append(dx[i])
            if len(window) > period:
                window.pop(0)
            if len(window) == period:
                adx[i] = sum(window) / period
    return adx, plus_di, minus_di


def _pattern_score(bars: list[dict], i: int) -> float:
    """+1 bullish / -1 bearish candlestick signal at bar i, 0 if none.
    Mirrors the hammer/engulfing/three-soldiers rules in indicators.py."""
    o = float(bars[i].get("open") or 0.0)
    h = float(bars[i].get("high") or 0.0)
    l = float(bars[i].get("low") or 0.0)
    c = float(bars[i].get("close") or 0.0)
    body = abs(c - o)
    upper = h - max(o, c)
    lower = min(o, c) - l
    score = 0.0
    if body > 0:
        if lower >= 2.0 * body and upper <= body:
            score += 1.0  # hammer
        elif upper >= 2.0 * body and lower <= body:
            score -= 1.0  # shooting star

    if i >= 1:
        po = float(bars[i - 1].get("open") or 0.0)
        pc = float(bars[i - 1].get("close") or 0.0)
        prev_bear, prev_bull = pc < po, pc > po
        cur_bull, cur_bear = c > o, c < o
        if cur_bull and prev_bear and o <= pc and c >= po:
            score += 1.0  # bullish engulfing
        elif cur_bear and prev_bull and o >= pc and c <= po:
            score -= 1.0  # bearish engulfing

    if i >= 2:
        o1 = float(bars[i - 2].get("open") or 0.0)
        c1 = float(bars[i - 2].get("close") or 0.0)
        o2 = float(bars[i - 1].get("open") or 0.0)
        c2 = float(bars[i - 1].get("close") or 0.0)
        if c1 > o1 and c2 > o2 and c > o and c1 < c2 < c:
            score += 1.0  # three white soldiers
        elif c1 < o1 and c2 < o2 and c < o and c1 > c2 > c:
            score -= 1.0  # three black crows

    return max(-1.0, min(1.0, score))


def _htf_trend_series(bars: list[dict], htf_bars: Optional[list[dict]],
                      period: int = 200) -> tuple[list[float], list[float]]:
    """Higher-timeframe EMA-distance/direction, aligned to each primary bar's
    time using only HTF bars already closed as of that bar (no lookahead)."""
    n = len(bars)
    dist = [0.0] * n
    direction = [0.0] * n
    if not htf_bars:
        return dist, direction
    htf_bars = sorted(htf_bars, key=lambda b: int(b["time"]))
    htf_times = [int(b["time"]) for b in htf_bars]
    htf_closes = [float(b["close"]) for b in htf_bars]
    ema = ind._ema_series(htf_closes, period)
    m = len(htf_bars)
    j = -1
    for i in range(n):
        t = int(bars[i].get("time") or 0)
        while j + 1 < m and htf_times[j + 1] <= t:
            j += 1
        if j < 0:
            continue
        e, c = ema[j], htf_closes[j]
        if e:
            dist[i] = max(-10.0, min(10.0, (c - e) / e * 100)) / 10.0
            direction[i] = 1.0 if c > e else (-1.0 if c < e else 0.0)
    return dist, direction


def _time_features(ts) -> list[float]:
    """Hour-of-day / day-of-week (cyclic sin/cos) + rough UTC session flags."""
    try:
        t = time.gmtime(int(ts or 0))
    except (TypeError, ValueError, OSError):
        t = time.gmtime(0)
    hour = t.tm_hour + t.tm_min / 60.0
    hour_angle = 2 * math.pi * hour / 24.0
    dow_angle = 2 * math.pi * t.tm_wday / 7.0
    return [
        math.sin(hour_angle), math.cos(hour_angle),
        math.sin(dow_angle), math.cos(dow_angle),
        1.0 if 0 <= t.tm_hour < 9 else 0.0,   # session_asia
        1.0 if 7 <= t.tm_hour < 16 else 0.0,  # session_london
        1.0 if 12 <= t.tm_hour < 21 else 0.0, # session_ny
    ]


def _feature_row(bars: list[dict], closes: list[float], i: int, lags: int,
                 series: dict) -> Optional[list[float]]:
    """Feature vector using data up to and including bar i. None if not enough."""
    if i < max(lags, 26, 20) or i < 14:
        return None
    rsi = series["rsi"]
    ema_fast = series["ema_fast"]
    ema_slow = series["ema_slow"]
    vols = series["vols"]
    stoch = series["stoch"]
    cci = series["cci"]
    williams = series["williams"]
    adx = series["adx"]
    plus_di = series["plus_di"]
    minus_di = series["minus_di"]
    htf_dist = series["htf_dist"]
    htf_dir = series["htf_dir"]
    row: list[float] = []
    for k in range(1, lags + 1):
        prev = closes[i - k]
        cur = closes[i - k + 1]
        row.append(((cur / prev) - 1) * 100 if prev else 0.0)

    r = rsi[i]
    row.append((r - 50) / 50 if r is not None else 0.0)

    ef, es = ema_fast[i], ema_slow[i]
    price = closes[i] or 1.0
    row.append(((ef - es) / price * 100) if (ef is not None and es is not None) else 0.0)
    row.append(((ef - es) / es * 100) if (ef is not None and es and es != 0) else 0.0)

    window = closes[i - 19:i + 1]
    if len(window) == 20:
        m = sum(window) / 20
        sd = (sum((x - m) ** 2 for x in window) / 20) ** 0.5
        row.append(((closes[i] - m) / sd) if sd else 0.0)
    else:
        row.append(0.0)

    p10 = closes[i - 10] if i >= 10 else 0.0
    row.append(((closes[i] / p10) - 1) * 100 if p10 else 0.0)

    # Candle microstructure (OHLC)
    o = float(bars[i].get("open") or closes[i])
    h = float(bars[i].get("high") or closes[i])
    l = float(bars[i].get("low") or closes[i])
    c = closes[i]
    denom = c if c else 1.0
    row.append((h - l) / denom * 100)                 # hl_range
    row.append((c - o) / denom * 100)                 # body
    upper = h - max(o, c)
    lower = min(o, c) - l
    row.append(upper / denom * 100)                   # upper_wick
    row.append(lower / denom * 100)                   # lower_wick

    # ATR-ish % over 14 bars
    if i >= 14:
        trs = []
        for j in range(i - 13, i + 1):
            hj = float(bars[j].get("high") or closes[j])
            lj = float(bars[j].get("low") or closes[j])
            pc = closes[j - 1] if j > 0 else closes[j]
            trs.append(max(hj - lj, abs(hj - pc), abs(lj - pc)))
        atr = sum(trs) / 14
        row.append(atr / denom * 100)
    else:
        row.append(0.0)

    # Volume z-score over 20
    if i >= 19:
        vw = vols[i - 19:i + 1]
        vm = sum(vw) / 20
        vs = (sum((x - vm) ** 2 for x in vw) / 20) ** 0.5
        row.append(((vols[i] - vm) / vs) if vs else 0.0)
    else:
        row.append(0.0)

    # Extra oscillators (Stochastic, CCI, Williams %R, ADX/+-DI)
    sk = stoch[i]
    row.append(((sk - 50) / 50) if sk is not None else 0.0)               # stoch_k

    cci_v = cci[i]
    row.append((max(-300.0, min(300.0, cci_v)) / 300.0) if cci_v is not None else 0.0)  # cci

    wr = williams[i]
    row.append(((wr + 50) / 50) if wr is not None else 0.0)               # williams_r

    adx_v = adx[i]
    row.append((adx_v / 100.0) if adx_v is not None else 0.0)             # adx

    pdi_v, mdi_v = plus_di[i], minus_di[i]
    row.append(((pdi_v - mdi_v) / 100.0) if (pdi_v is not None and mdi_v is not None) else 0.0)  # di_diff

    row.append(_pattern_score(bars, i))                                  # pattern

    # Time-of-day / day-of-week / session
    row.extend(_time_features(bars[i].get("time")))

    # Higher-timeframe trend context (0.0/neutral when htf_bars unavailable)
    row.append(htf_dist[i])
    row.append(htf_dir[i])

    return row


def _build_series(bars: list[dict], closes: list[float],
                  htf_bars: Optional[list[dict]] = None) -> dict:
    highs = [float(b.get("high") or c) for b, c in zip(bars, closes)]
    lows = [float(b.get("low") or c) for b, c in zip(bars, closes)]
    adx, plus_di, minus_di = _adx_di_series(highs, lows, closes, 14)
    htf_dist, htf_dir = _htf_trend_series(bars, htf_bars, 200)
    return {
        "rsi": _rsi_series(closes, 14),
        "ema_fast": ind._ema_series(closes, 12),
        "ema_slow": ind._ema_series(closes, 26),
        "vols": [_safe_vol(b) for b in bars],
        "stoch": _stoch_series(highs, lows, closes, 14),
        "cci": _cci_series(highs, lows, closes, 20),
        "williams": _williams_series(highs, lows, closes, 14),
        "adx": adx,
        "plus_di": plus_di,
        "minus_di": minus_di,
        "htf_dist": htf_dist,
        "htf_dir": htf_dir,
    }


def _build_dataset(bars: list[dict], lags: int, lookahead: int,
                   htf_bars: Optional[list[dict]] = None):
    closes = _closes(bars)
    n = len(closes)
    series = _build_series(bars, closes, htf_bars)
    X: list[list[float]] = []
    y: list[int] = []
    for i in range(n):
        if i + lookahead >= n:
            break
        row = _feature_row(bars, closes, i, lags, series)
        if row is None:
            continue
        future = closes[i + lookahead]
        label = 1 if future > closes[i] else 0
        X.append(row)
        y.append(label)
    return X, y


def _standardize_fit(X: list[list[float]]):
    if not X:
        return [], [], []
    dim = len(X[0])
    means = [0.0] * dim
    stds = [1.0] * dim
    n = len(X)
    for j in range(dim):
        col = [row[j] for row in X]
        m = sum(col) / n
        means[j] = m
        var = sum((v - m) ** 2 for v in col) / n
        stds[j] = math.sqrt(var) if var > 0 else 1.0
    return means, stds


def _apply_standardize(X: list[list[float]], means: list[float], stds: list[float]):
    dim = len(means)
    return [[(row[j] - means[j]) / (stds[j] or 1.0) for j in range(dim)] for row in X]


def _sigmoid(z: float) -> float:
    if z < -60:
        return 0.0
    if z > 60:
        return 1.0
    return 1.0 / (1.0 + math.exp(-z))


def _chrono_split(X: list, y: list, val_ratio: float):
    """Time-ordered split: earliest bars train, latest bars validate."""
    n = len(X)
    n_val = max(1, int(n * val_ratio)) if n >= 60 else max(1, n // 5)
    n_train = n - n_val
    if n_train < 40:
        # Too few samples: train on everything, report in-sample as both.
        return X, y, X, y, False
    return X[:n_train], y[:n_train], X[n_train:], y[n_train:], True


def _accuracy(preds: list[int], y: list[int]) -> float:
    if not y:
        return 0.0
    return round(sum(1 for a, b in zip(preds, y) if a == b) / len(y), 4)


def _encode_booster(obj) -> str:
    return base64.b64encode(pickle.dumps(obj, protocol=pickle.HIGHEST_PROTOCOL)).decode("ascii")


def _decode_booster(b64: str):
    return pickle.loads(base64.b64decode(b64.encode("ascii")))


def _train_logistic(X_train, y_train, X_val, y_val, epochs: int, lr: float, l2: float) -> dict:
    means, stds = _standardize_fit(X_train)
    Xs = _apply_standardize(X_train, means, stds)
    Xv = _apply_standardize(X_val, means, stds)
    dim = len(Xs[0])
    w = [0.0] * dim
    b = 0.0
    n = len(Xs)

    for _ in range(epochs):
        gw = [0.0] * dim
        gb = 0.0
        for xi, yi in zip(Xs, y_train):
            z = b + sum(w[j] * xi[j] for j in range(dim))
            err = _sigmoid(z) - yi
            for j in range(dim):
                gw[j] += err * xi[j]
            gb += err
        for j in range(dim):
            w[j] -= lr * (gw[j] / n + l2 * w[j])
        b -= lr * (gb / n)

    def _preds(rows):
        out = []
        for xi in rows:
            p = _sigmoid(b + sum(w[j] * xi[j] for j in range(dim)))
            out.append(1 if p >= 0.5 else 0)
        return out

    return {
        "algo": "logistic",
        "weights": w,
        "bias": b,
        "means": means,
        "stds": stds,
        "train_accuracy": _accuracy(_preds(Xs), y_train),
        "val_accuracy": _accuracy(_preds(Xv), y_val),
    }


def _train_xgboost(X_train, y_train, X_val, y_val, p: dict) -> dict:
    try:
        import numpy as np
        import xgboost as xgb
    except ImportError as exc:
        raise ValueError(
            "Chưa cài xgboost. Chạy: pip install xgboost"
        ) from exc

    Xt = np.asarray(X_train, dtype=np.float64)
    yt = np.asarray(y_train, dtype=np.int32)
    Xv = np.asarray(X_val, dtype=np.float64)
    yv = np.asarray(y_val, dtype=np.int32)

    # Reweight the minority side (up/down bars are rarely 50/50, especially
    # in a trending window) so the model can't just learn the majority class.
    n_pos = int(sum(y_train))
    n_neg = len(y_train) - n_pos
    scale_pos_weight = (n_neg / n_pos) if n_pos > 0 and n_neg > 0 else 1.0

    model = xgb.XGBClassifier(
        n_estimators=int(p["n_estimators"]),
        max_depth=int(p["max_depth"]),
        learning_rate=float(p["learning_rate"]),
        subsample=0.85,
        colsample_bytree=0.85,
        min_child_weight=3,
        reg_lambda=1.0,
        reg_alpha=0.05,
        scale_pos_weight=scale_pos_weight,
        objective="binary:logistic",
        eval_metric="logloss",
        early_stopping_rounds=30,
        n_jobs=2,
        random_state=42,
        verbosity=0,
    )
    try:
        model.fit(Xt, yt, eval_set=[(Xv, yv)], verbose=False)
    except TypeError:
        model.fit(Xt, yt, eval_set=[(Xv, yv)])
    _strip_feature_names(model)

    train_pred = [int(x) for x in _tree_predict(model, Xt)]
    val_pred = [int(x) for x in _tree_predict(model, Xv)]
    return {
        "algo": "xgboost",
        "booster_b64": _encode_booster(model),
        "means": [],
        "stds": [],
        "train_accuracy": _accuracy(train_pred, y_train),
        "val_accuracy": _accuracy(val_pred, y_val),
    }


def _train_lightgbm(X_train, y_train, X_val, y_val, p: dict) -> dict:
    try:
        import numpy as np
        import lightgbm as lgb
    except ImportError as exc:
        raise ValueError(
            "Chưa cài lightgbm. Chạy: pip install lightgbm"
        ) from exc

    Xt = np.asarray(X_train, dtype=np.float64)
    yt = np.asarray(y_train, dtype=np.int32)
    Xv = np.asarray(X_val, dtype=np.float64)
    yv = np.asarray(y_val, dtype=np.int32)

    model = lgb.LGBMClassifier(
        n_estimators=int(p["n_estimators"]),
        max_depth=int(p["max_depth"]),
        learning_rate=float(p["learning_rate"]),
        subsample=0.85,
        colsample_bytree=0.85,
        min_child_samples=8,
        reg_lambda=1.0,
        reg_alpha=0.05,
        objective="binary",
        class_weight="balanced",
        n_jobs=2,
        random_state=42,
        verbosity=-1,
    )
    try:
        model.fit(
            Xt, yt,
            eval_set=[(Xv, yv)],
            callbacks=[lgb.early_stopping(40, verbose=False), lgb.log_evaluation(0)],
        )
    except Exception:
        model.fit(Xt, yt)
    _strip_feature_names(model)

    train_pred = [int(x) for x in _tree_predict(model, Xt)]
    val_pred = [int(x) for x in _tree_predict(model, Xv)]
    return {
        "algo": "lightgbm",
        "booster_b64": _encode_booster(model),
        "means": [],
        "stds": [],
        "train_accuracy": _accuracy(train_pred, y_train),
        "val_accuracy": _accuracy(val_pred, y_val),
    }


def _strip_feature_names(model) -> None:
    """Avoid sklearn feature-name mismatch warnings when predicting from raw arrays."""
    if hasattr(model, "feature_names_in_"):
        try:
            delattr(model, "feature_names_in_")
        except Exception:
            pass


def _tree_predict(model, X):
    import warnings
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message="X does not have valid feature names",
            category=UserWarning,
        )
        return model.predict(X)


def _tree_predict_proba(model, X):
    import warnings
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message="X does not have valid feature names",
            category=UserWarning,
        )
        return model.predict_proba(X)


def train(bars: list[dict], params: Optional[dict] = None,
         htf_bars: Optional[list[dict]] = None) -> dict:
    """Fit a classifier. Returns a serialisable model dict.
    `htf_bars` (optional) are higher-timeframe bars (e.g. H4) used to add a
    higher-timeframe trend feature - see `_htf_trend_series`.
    Raises ValueError if there isn't enough usable data / missing package."""
    p = {**ML_DEFAULTS, **(params or {})}
    algo = str(p.get("algo") or "xgboost").lower().strip()
    if algo not in ALGORITHMS:
        raise ValueError(f"algo không hợp lệ: {algo!r}. Chọn một trong {ALGORITHMS}.")

    lags = int(p["lags"])
    lookahead = int(p["lookahead"])
    val_ratio = float(p.get("val_ratio", 0.2))

    X, y = _build_dataset(bars, lags, lookahead, htf_bars)
    if len(X) < 50:
        raise ValueError(f"Không đủ dữ liệu để huấn luyện (chỉ {len(X)} mẫu, cần >= 50).")

    X_train, y_train, X_val, y_val, has_holdout = _chrono_split(X, y, val_ratio)

    if algo == "logistic":
        fitted = _train_logistic(
            X_train, y_train, X_val, y_val,
            epochs=int(p["epochs"]), lr=float(p["lr"]), l2=float(p["l2"]),
        )
    elif algo == "xgboost":
        fitted = _train_xgboost(X_train, y_train, X_val, y_val, p)
    else:
        fitted = _train_lightgbm(X_train, y_train, X_val, y_val, p)

    # Primary accuracy exposed to UI = hold-out when available.
    accuracy = fitted["val_accuracy"] if has_holdout else fitted["train_accuracy"]
    up_rate = round(sum(y) / len(y), 4)

    return {
        "version": MODEL_VERSION,
        "algo": fitted["algo"],
        "weights": fitted.get("weights"),
        "bias": fitted.get("bias", 0.0),
        "booster_b64": fitted.get("booster_b64"),
        "means": fitted.get("means") or [],
        "stds": fitted.get("stds") or [],
        "lags": lags,
        "lookahead": lookahead,
        "feature_names": feature_names(lags),
        "uses_htf": bool(htf_bars),
        "samples": len(X),
        "train_samples": len(X_train),
        "val_samples": len(X_val) if has_holdout else 0,
        "accuracy": accuracy,
        "train_accuracy": fitted["train_accuracy"],
        "val_accuracy": fitted["val_accuracy"] if has_holdout else None,
        "up_rate": up_rate,
        "trained_at": int(time.time()),
    }


def _latest_features(bars: list[dict], model: dict,
                     htf_bars: Optional[list[dict]] = None) -> Optional[list[float]]:
    lags = int(model.get("lags", 5))
    closes = _closes(bars)
    n = len(closes)
    if n < 27 or n <= lags:
        return None
    series = _build_series(bars, closes, htf_bars)
    return _feature_row(bars, closes, n - 1, lags, series)


def predict_proba(bars: list[dict], model: dict,
                  htf_bars: Optional[list[dict]] = None) -> Optional[float]:
    """Probability that price goes UP over the next `lookahead` bars.
    `htf_bars` should be the same higher-timeframe bars used at training
    time (see `train()`); omitted/mismatched just yields a neutral htf feature."""
    if not model:
        return None
    algo = str(model.get("algo") or "logistic").lower()
    row = _latest_features(bars, model, htf_bars)
    if row is None:
        return None

    # Legacy logistic models (v1) or explicit logistic.
    if algo == "logistic" or (model.get("weights") and not model.get("booster_b64")):
        means = model.get("means") or []
        stds = model.get("stds") or []
        w = model.get("weights") or []
        b = float(model.get("bias", 0.0))
        if not w or not (len(row) == len(means) == len(stds) == len(w)):
            # Feature schema changed (v1 -> v2): refuse rather than mis-predict.
            return None
        z = b
        for j in range(len(row)):
            sd = stds[j] or 1.0
            z += w[j] * ((row[j] - means[j]) / sd)
        return _sigmoid(z)

    b64 = model.get("booster_b64")
    if not b64:
        return None
    try:
        booster = _decode_booster(b64)
        import numpy as np
        proba = _tree_predict_proba(booster, np.asarray([row], dtype=np.float64))[0]
        return float(proba[1])
    except Exception:
        return None


def predict_signal(bars: list[dict], model: dict, threshold: float,
                   allowed: str = "BOTH",
                   htf_bars: Optional[list[dict]] = None) -> Optional[str]:
    """Map probability to BUY/SELL/None, respecting an allowed-direction filter."""
    proba = predict_proba(bars, model, htf_bars)
    if proba is None:
        return None
    allowed = (allowed or "BOTH").upper()
    side: Optional[str] = None
    if proba >= threshold:
        side = "BUY"
    elif proba <= (1 - threshold):
        side = "SELL"
    if side is None:
        return None
    if allowed in ("BUY", "SELL") and side != allowed:
        return None
    return side
