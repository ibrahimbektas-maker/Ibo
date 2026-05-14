from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import numpy as np
import pandas as pd

from .technical import compute_features, in_session

SentimentSizer = Callable[[pd.Timestamp, str], float]


@dataclass
class BacktestResult:
    trades: pd.DataFrame
    equity_curve: pd.Series
    metrics: dict[str, float]


def _summary(trades: pd.DataFrame, equity: pd.Series, capital: float) -> dict[str, float]:
    if trades.empty:
        return {
            "trades": 0,
            "win_rate": 0.0,
            "expectancy_eur": 0.0,
            "total_pnl_eur": 0.0,
            "sharpe": 0.0,
            "max_drawdown_pct": 0.0,
        }
    wins = trades[trades["pnl"] > 0]
    win_rate = len(wins) / len(trades)
    total = trades["pnl"].sum()
    expectancy = trades["pnl"].mean()
    returns = equity.pct_change().dropna()
    sharpe = (
        float(np.sqrt(252 * 24 * 4) * returns.mean() / returns.std())
        if returns.std() > 0
        else 0.0
    )
    running_max = equity.cummax()
    drawdown = (equity - running_max) / running_max
    max_dd = float(drawdown.min() * 100)
    return {
        "trades": int(len(trades)),
        "win_rate": float(win_rate),
        "expectancy_eur": float(expectancy),
        "total_pnl_eur": float(total),
        "sharpe": sharpe,
        "max_drawdown_pct": max_dd,
    }


def run_backtest(
    df: pd.DataFrame,
    cfg: dict,
    sentiment_sizer: SentimentSizer | None = None,
) -> BacktestResult:
    feats = compute_features(df, cfg)
    rcfg = cfg["risk"]
    capital = float(rcfg["capital_eur"])
    equity = capital
    equity_curve = []
    trades = []

    position = None
    for ts, row in feats.iterrows():
        if pd.isna(row["atr"]) or pd.isna(row["ema_slow"]):
            equity_curve.append((ts, equity))
            continue

        if position is not None:
            high, low = row["high"], row["low"]
            hit_sl = low <= position["sl"] if position["side"] == "LONG" else high >= position["sl"]
            hit_tp = high >= position["tp"] if position["side"] == "LONG" else low <= position["tp"]
            exit_price = None
            if hit_sl and hit_tp:
                exit_price = position["sl"]
            elif hit_sl:
                exit_price = position["sl"]
            elif hit_tp:
                exit_price = position["tp"]
            if exit_price is not None:
                direction = 1 if position["side"] == "LONG" else -1
                pnl = (exit_price - position["entry"]) * direction * position["size"]
                equity += pnl
                trades.append(
                    {
                        "entry_time": position["entry_time"],
                        "exit_time": ts,
                        "side": position["side"],
                        "entry": position["entry"],
                        "exit": exit_price,
                        "size": position["size"],
                        "sentiment_mult": position.get("sentiment_mult", 1.0),
                        "pnl": pnl,
                    }
                )
                position = None

        if position is None and in_session(ts, cfg["sessions"]):
            trend_up = row["ema_fast"] > row["ema_slow"]
            trend_dn = row["ema_fast"] < row["ema_slow"]
            breakout_up = row["close"] > row["donchian_high"]
            breakout_dn = row["close"] < row["donchian_low"]
            rsi_long = row["rsi"] < cfg["signals"]["rsi_overbought"]
            rsi_short = row["rsi"] > cfg["signals"]["rsi_oversold"]

            side = None
            if trend_up and breakout_up and rsi_long:
                side = "LONG"
            elif trend_dn and breakout_dn and rsi_short:
                side = "SHORT"

            sentiment_mult = 1.0
            if side is not None and sentiment_sizer is not None:
                sentiment_mult = sentiment_sizer(ts, side)
                if sentiment_mult <= 0:
                    side = None

            if side is not None:
                sl_dist = row["atr"] * rcfg["sl_atr_multiple"]
                tp_dist = row["atr"] * rcfg["tp_atr_multiple"]
                risk_amount = capital * rcfg["risk_per_trade_pct"] / 100.0
                size = (risk_amount / sl_dist) * sentiment_mult if sl_dist > 0 else 0
                if size > 0:
                    entry = row["close"]
                    sl = entry - sl_dist if side == "LONG" else entry + sl_dist
                    tp = entry + tp_dist if side == "LONG" else entry - tp_dist
                    position = {
                        "entry_time": ts,
                        "entry": entry,
                        "sl": sl,
                        "tp": tp,
                        "size": size,
                        "side": side,
                        "sentiment_mult": sentiment_mult,
                    }

        equity_curve.append((ts, equity))

    eq_series = pd.Series(
        [e for _, e in equity_curve],
        index=pd.DatetimeIndex([t for t, _ in equity_curve]),
        name="equity",
    )
    trades_df = pd.DataFrame(trades)
    metrics = _summary(trades_df, eq_series, capital)
    return BacktestResult(trades=trades_df, equity_curve=eq_series, metrics=metrics)
