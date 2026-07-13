"""Lightweight, dependency-free machine-learning entry model.

A logistic-regression classifier that predicts whether price will go UP or
DOWN over the next `lookahead` bars, using features derived from recent
price action and a few classic indicators. Everything is pure Python (no
numpy / sklearn) so it runs anywhere the server does.

Flow:
  - `train(bars, params)` builds a feature matrix + labels from historical
    OHLC bars, standardizes features, fits weights via gradient descent and
    returns a serialisable model dict (weights, bias, means, stds, metrics).
  - `predict_signal(bars, model, threshold, allowed)` computes the latest
    feature vector and returns "BUY" | "SELL" | None.

The model dict is stored inside the account entry config (Supabase JSONB),
so no extra table or binary artefact is needed.
"""
from __future__ import annotations

import math
import time
from typing import Optional

import indicators as ind

MODEL_VERSION = 1

# Defaults kept in sync with the frontend ML form.
ML_DEFAULTS = {
    "timeframe": "H1",
    "lookahead": 3,      # bars ahead to define the label
    "lags": 5,           # number of lagged returns used as features
    "threshold": 0.58,   # min probability to act
    "epochs": 400,
    "lr": 0.1,
    "l2": 0.001,
}

FEATURE_NAMES = [
    "ret_lag",       # placeholder, expanded per lag below
]


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
    names += ["rsi", "macd_hist", "ema_gap", "boll_z", "mom"]
    return names


def _feature_row(closes: list[float], i: int, lags: int,
                 rsi: list[Optional[float]],
                 ema_fast: list[Optional[float]],
                 ema_slow: list[Optional[float]]) -> Optional[list[float]]:
    """Feature vector using data up to and including bar i. None if not enough."""
    if i < lags or i < 26:
        return None
    row: list[float] = []
    # Lagged simple returns (bounded, in %)
    for k in range(1, lags + 1):
        prev = closes[i - k]
        cur = closes[i - k + 1]
        row.append(((cur / prev) - 1) * 100 if prev else 0.0)
    # RSI centered
    r = rsi[i]
    row.append((r - 50) / 50 if r is not None else 0.0)
    # MACD histogram (ema12-ema26) normalized by price
    ef, es = ema_fast[i], ema_slow[i]
    price = closes[i] or 1.0
    row.append(((ef - es) / price * 100) if (ef is not None and es is not None) else 0.0)
    # EMA gap (fast vs slow) normalized
    row.append(((ef - es) / es * 100) if (ef is not None and es and es != 0) else 0.0)
    # Bollinger z-score over 20
    window = closes[i - 19:i + 1]
    if len(window) == 20:
        m = sum(window) / 20
        sd = (sum((x - m) ** 2 for x in window) / 20) ** 0.5
        row.append(((closes[i] - m) / sd) if sd else 0.0)
    else:
        row.append(0.0)
    # Momentum over 10 bars
    p10 = closes[i - 10] if i >= 10 else 0.0
    row.append(((closes[i] / p10) - 1) * 100 if p10 else 0.0)
    return row


def _build_dataset(bars: list[dict], lags: int, lookahead: int):
    closes = _closes(bars)
    n = len(closes)
    rsi = _rsi_series(closes, 14)
    ema_fast = ind._ema_series(closes, 12)
    ema_slow = ind._ema_series(closes, 26)
    X: list[list[float]] = []
    y: list[int] = []
    for i in range(n):
        if i + lookahead >= n:
            break
        row = _feature_row(closes, i, lags, rsi, ema_fast, ema_slow)
        if row is None:
            continue
        future = closes[i + lookahead]
        label = 1 if future > closes[i] else 0
        X.append(row)
        y.append(label)
    return X, y


def _standardize(X: list[list[float]]):
    if not X:
        return X, [], []
    dim = len(X[0])
    means = [0.0] * dim
    stds = [0.0] * dim
    n = len(X)
    for j in range(dim):
        col = [row[j] for row in X]
        m = sum(col) / n
        means[j] = m
        var = sum((v - m) ** 2 for v in col) / n
        stds[j] = math.sqrt(var) if var > 0 else 1.0
    Xs = [[(row[j] - means[j]) / stds[j] for j in range(dim)] for row in X]
    return Xs, means, stds


def _sigmoid(z: float) -> float:
    if z < -60:
        return 0.0
    if z > 60:
        return 1.0
    return 1.0 / (1.0 + math.exp(-z))


def train(bars: list[dict], params: Optional[dict] = None) -> dict:
    """Fit a logistic-regression model. Returns a serialisable model dict.
    Raises ValueError if there isn't enough usable data."""
    p = {**ML_DEFAULTS, **(params or {})}
    lags = int(p["lags"])
    lookahead = int(p["lookahead"])
    epochs = int(p["epochs"])
    lr = float(p["lr"])
    l2 = float(p["l2"])

    X, y = _build_dataset(bars, lags, lookahead)
    if len(X) < 50:
        raise ValueError(f"Không đủ dữ liệu để huấn luyện (chỉ {len(X)} mẫu, cần >= 50).")

    Xs, means, stds = _standardize(X)
    dim = len(Xs[0])
    w = [0.0] * dim
    b = 0.0
    n = len(Xs)

    for _ in range(epochs):
        gw = [0.0] * dim
        gb = 0.0
        for xi, yi in zip(Xs, y):
            z = b + sum(w[j] * xi[j] for j in range(dim))
            pred = _sigmoid(z)
            err = pred - yi
            for j in range(dim):
                gw[j] += err * xi[j]
            gb += err
        for j in range(dim):
            w[j] -= lr * (gw[j] / n + l2 * w[j])
        b -= lr * (gb / n)

    # In-sample metrics
    correct = 0
    for xi, yi in zip(Xs, y):
        pred = _sigmoid(b + sum(w[j] * xi[j] for j in range(dim)))
        if (1 if pred >= 0.5 else 0) == yi:
            correct += 1
    accuracy = round(correct / n, 4)
    up_rate = round(sum(y) / n, 4)

    return {
        "version": MODEL_VERSION,
        "weights": w,
        "bias": b,
        "means": means,
        "stds": stds,
        "lags": lags,
        "lookahead": lookahead,
        "feature_names": feature_names(lags),
        "samples": n,
        "accuracy": accuracy,
        "up_rate": up_rate,
        "trained_at": int(time.time()),
    }


def predict_proba(bars: list[dict], model: dict) -> Optional[float]:
    """Probability that price goes UP over the next `lookahead` bars."""
    if not model or "weights" not in model:
        return None
    lags = int(model.get("lags", 5))
    closes = _closes(bars)
    n = len(closes)
    if n < 27 or n <= lags:
        return None
    rsi = _rsi_series(closes, 14)
    ema_fast = ind._ema_series(closes, 12)
    ema_slow = ind._ema_series(closes, 26)
    row = _feature_row(closes, n - 1, lags, rsi, ema_fast, ema_slow)
    if row is None:
        return None
    means = model["means"]
    stds = model["stds"]
    w = model["weights"]
    b = float(model.get("bias", 0.0))
    if not (len(row) == len(means) == len(stds) == len(w)):
        return None
    z = b
    for j in range(len(row)):
        sd = stds[j] or 1.0
        z += w[j] * ((row[j] - means[j]) / sd)
    return _sigmoid(z)


def predict_signal(bars: list[dict], model: dict, threshold: float,
                   allowed: str = "BOTH") -> Optional[str]:
    """Map probability to BUY/SELL/None, respecting an allowed-direction filter."""
    proba = predict_proba(bars, model)
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
