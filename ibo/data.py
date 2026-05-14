from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd


def fetch_yfinance_prices(
    ticker: str = "GC=F",
    interval: str = "15m",
    period: str = "60d",
) -> pd.DataFrame:
    """Télécharge l'historique OHLCV via yfinance et le ramène au format du backtest.

    Limites yfinance :
      - intervals < 1h : 60 jours max (15m/30m/60m)
      - 1h            : 730 jours max
      - 1d            : illimité
    """
    import yfinance as yf

    df = yf.download(
        ticker,
        period=period,
        interval=interval,
        progress=False,
        auto_adjust=False,
    )
    if df.empty:
        return df

    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    df = df.rename(
        columns={
            "Open": "open",
            "High": "high",
            "Low": "low",
            "Close": "close",
            "Volume": "volume",
        }
    )
    df.index = pd.to_datetime(df.index, utc=True)
    df.index.name = "time"
    keep = [c for c in ("open", "high", "low", "close", "volume") if c in df.columns]
    return df[keep].sort_index()


def fetch_macro_features(lookback_days: int = 30) -> pd.DataFrame:
    import yfinance as yf

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=lookback_days)
    tickers = {
        "DXY": "DX-Y.NYB",
        "US10Y": "^TNX",
        "VIX": "^VIX",
        "BTC": "BTC-USD",
    }
    frames = []
    for name, sym in tickers.items():
        df = yf.download(
            sym, start=start, end=end, interval="1d", progress=False, auto_adjust=False
        )
        if df.empty:
            continue
        close = df["Close"]
        if isinstance(close, pd.DataFrame):
            close = close.squeeze("columns")
        series = pd.Series(close).rename(name)
        frames.append(series)
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, axis=1).ffill().dropna(how="all")
    out.index = pd.to_datetime(out.index, utc=True)
    return out


def daily_change_pct(df: pd.DataFrame, column: str, periods: int = 1) -> float | None:
    if column not in df or len(df) < periods + 1:
        return None
    closes = df[column].dropna()
    if len(closes) < periods + 1:
        return None
    return float((closes.iloc[-1] / closes.iloc[-1 - periods] - 1.0) * 100.0)


def macro_snapshot(macro: pd.DataFrame) -> dict[str, float | None]:
    return {
        "dxy_change_1d_pct": daily_change_pct(macro, "DXY", 1),
        "dxy_change_5d_pct": daily_change_pct(macro, "DXY", 5),
        "us10y_change_1d_pct": daily_change_pct(macro, "US10Y", 1),
        "vix_change_1d_pct": daily_change_pct(macro, "VIX", 1),
        "btc_change_1d_pct": daily_change_pct(macro, "BTC", 1),
    }
