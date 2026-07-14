"""Machine-learning entry models: logistic, XGBoost, LightGBM.

Predicts whether price goes UP or DOWN over the next `lookahead` bars from
OHLC (+ tick volume) features. The trained artefact is a JSON-serialisable
dict stored inside the account entry config (Supabase JSONB).

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

MODEL_VERSION = 2

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
    "n_estimators": 200, # tree boosters
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


def _feature_row(bars: list[dict], closes: list[float], i: int, lags: int,
                 rsi: list[Optional[float]],
                 ema_fast: list[Optional[float]],
                 ema_slow: list[Optional[float]],
                 vols: list[float]) -> Optional[list[float]]:
    """Feature vector using data up to and including bar i. None if not enough."""
    if i < max(lags, 26, 20) or i < 14:
        return None
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

    return row


def _build_dataset(bars: list[dict], lags: int, lookahead: int):
    closes = _closes(bars)
    vols = [_safe_vol(b) for b in bars]
    n = len(closes)
    rsi = _rsi_series(closes, 14)
    ema_fast = ind._ema_series(closes, 12)
    ema_slow = ind._ema_series(closes, 26)
    X: list[list[float]] = []
    y: list[int] = []
    for i in range(n):
        if i + lookahead >= n:
            break
        row = _feature_row(bars, closes, i, lags, rsi, ema_fast, ema_slow, vols)
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

    model = xgb.XGBClassifier(
        n_estimators=int(p["n_estimators"]),
        max_depth=int(p["max_depth"]),
        learning_rate=float(p["learning_rate"]),
        subsample=0.85,
        colsample_bytree=0.85,
        min_child_weight=3,
        reg_lambda=1.0,
        reg_alpha=0.05,
        objective="binary:logistic",
        eval_metric="logloss",
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


def train(bars: list[dict], params: Optional[dict] = None) -> dict:
    """Fit a classifier. Returns a serialisable model dict.
    Raises ValueError if there isn't enough usable data / missing package."""
    p = {**ML_DEFAULTS, **(params or {})}
    algo = str(p.get("algo") or "xgboost").lower().strip()
    if algo not in ALGORITHMS:
        raise ValueError(f"algo không hợp lệ: {algo!r}. Chọn một trong {ALGORITHMS}.")

    lags = int(p["lags"])
    lookahead = int(p["lookahead"])
    val_ratio = float(p.get("val_ratio", 0.2))

    X, y = _build_dataset(bars, lags, lookahead)
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
        "samples": len(X),
        "train_samples": len(X_train),
        "val_samples": len(X_val) if has_holdout else 0,
        "accuracy": accuracy,
        "train_accuracy": fitted["train_accuracy"],
        "val_accuracy": fitted["val_accuracy"] if has_holdout else None,
        "up_rate": up_rate,
        "trained_at": int(time.time()),
    }


def _latest_features(bars: list[dict], model: dict) -> Optional[list[float]]:
    lags = int(model.get("lags", 5))
    closes = _closes(bars)
    vols = [_safe_vol(b) for b in bars]
    n = len(closes)
    if n < 27 or n <= lags:
        return None
    rsi = _rsi_series(closes, 14)
    ema_fast = ind._ema_series(closes, 12)
    ema_slow = ind._ema_series(closes, 26)
    return _feature_row(bars, closes, n - 1, lags, rsi, ema_fast, ema_slow, vols)


def predict_proba(bars: list[dict], model: dict) -> Optional[float]:
    """Probability that price goes UP over the next `lookahead` bars."""
    if not model:
        return None
    algo = str(model.get("algo") or "logistic").lower()
    row = _latest_features(bars, model)
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
