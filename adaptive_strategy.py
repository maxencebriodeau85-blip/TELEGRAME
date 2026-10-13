"""Deterministic opportunity ranking, using only supplied closed bars.

The score is a heuristic, not a probability or a learned return prediction.
"""
import numpy as np
import pandas as pd


def assess_market(bars: pd.DataFrame) -> dict:
    rejected = {"regime": "unknown", "quality": 0.0, "entry_ok": False,
                "reason": "données insuffisantes ou invalides"}
    if len(bars) < 60 or not {"Close", "High", "Low"}.issubset(bars.columns):
        return rejected
    window = bars.iloc[-60:]
    values = window[["Close", "High", "Low"]].to_numpy(dtype=float)
    if not np.isfinite(values).all() or (values <= 0).any():
        return rejected
    if (window.High < window.Low).any() or ((window.Close > window.High) | (window.Close < window.Low)).any():
        return rejected
    if not window.index.is_monotonic_increasing or window.index.has_duplicates:
        return rejected
    close = window.Close
    path = float(close.diff().abs().iloc[-20:].sum())
    efficiency = abs(float(close.iloc[-1] - close.iloc[-21])) / path if path else 0.0
    tr = pd.concat([window.High-window.Low,
                    (window.High-close.shift()).abs(),
                    (window.Low-close.shift()).abs()], axis=1).max(axis=1)
    atr_pct = float(tr.ewm(alpha=1/14, adjust=False).mean().iloc[-1] / close.iloc[-1])
    ema20 = close.ewm(span=20, adjust=False).mean()
    ema50 = close.ewm(span=50, adjust=False).mean()
    momentum = float(close.iloc[-1] / close.iloc[-9] - 1)
    uptrend = ema20.iloc[-1] > ema50.iloc[-1] and ema20.iloc[-1] > ema20.iloc[-5]
    regime = "volatile" if atr_pct > .04 else ("trend" if efficiency >= .25 else "range")
    entry_ok = bool(uptrend and momentum > 0 and regime == "trend" and atr_pct > 0)
    quality = float(np.clip(60*efficiency + 40*np.clip(momentum/max(atr_pct, 1e-9)/4, 0, 1), 0, 100))
    return {"regime": regime, "quality": round(quality, 2), "entry_ok": entry_ok,
            "atr_pct": atr_pct, "efficiency": efficiency,
            "reason": "tendance et momentum confirmés" if entry_ok else "tendance, momentum ou volatilité défavorables"}
