from __future__ import annotations

from dataclasses import dataclass
from datetime import time
from typing import Literal

import numpy as np
import pandas as pd

Side = Literal["LONG", "SHORT", "NONE"]


@dataclass
class TechnicalSignal:
    side: Side
    score: float
    atr: float
    reason: str


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = -delta.clip(upper=0).rolling(period).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high = df["high"]
    low = df["low"]
    close = df["close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [(high - low), (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    return tr.rolling(period).mean()


def in_session(ts: pd.Timestamp, sessions: dict) -> bool:
    if not sessions.get("trade_only_in_sessions", False):
        return True
    t = ts.tz_convert("UTC").time() if ts.tzinfo else ts.time()
    for key in ("london", "newyork"):
        win = sessions.get(key)
        if not win:
            continue
        start = time.fromisoformat(win["start"])
        end = time.fromisoformat(win["end"])
        if start <= t <= end:
            return True
    return False


def compute_features(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    s = cfg["signals"]
    out = df.copy()
    out["ema_fast"] = ema(out["close"], s["ema_fast"])
    out["ema_slow"] = ema(out["close"], s["ema_slow"])
    out["rsi"] = rsi(out["close"], s["rsi_period"])
    out["atr"] = atr(out, cfg["risk"]["atr_period"])
    look = s["breakout_lookback"]
    out["donchian_high"] = out["high"].rolling(look).max().shift(1)
    out["donchian_low"] = out["low"].rolling(look).min().shift(1)
    return out


def evaluate(df: pd.DataFrame, cfg: dict) -> TechnicalSignal:
    if len(df) < max(cfg["signals"]["ema_slow"], cfg["signals"]["breakout_lookback"]) + 2:
        return TechnicalSignal("NONE", 0.0, 0.0, "not_enough_data")

    feats = compute_features(df, cfg)
    last = feats.iloc[-1]

    if pd.isna(last["atr"]) or pd.isna(last["ema_slow"]):
        return TechnicalSignal("NONE", 0.0, 0.0, "indicators_warmup")

    if not in_session(feats.index[-1], cfg["sessions"]):
        return TechnicalSignal("NONE", 0.0, float(last["atr"]), "outside_session")

    trend_up = last["ema_fast"] > last["ema_slow"]
    trend_dn = last["ema_fast"] < last["ema_slow"]
    rsi_ok_long = last["rsi"] < cfg["signals"]["rsi_overbought"]
    rsi_ok_short = last["rsi"] > cfg["signals"]["rsi_oversold"]
    breakout_up = last["close"] > last["donchian_high"]
    breakout_dn = last["close"] < last["donchian_low"]

    if trend_up and breakout_up and rsi_ok_long:
        score = min(1.0, (last["close"] - last["donchian_high"]) / max(last["atr"], 1e-9))
        return TechnicalSignal(
            "LONG", float(score), float(last["atr"]), "trend+breakout_up"
        )
    if trend_dn and breakout_dn and rsi_ok_short:
        score = min(1.0, (last["donchian_low"] - last["close"]) / max(last["atr"], 1e-9))
        return TechnicalSignal(
            "SHORT", float(score), float(last["atr"]), "trend+breakout_dn"
        )

    return TechnicalSignal("NONE", 0.0, float(last["atr"]), "no_setup")
