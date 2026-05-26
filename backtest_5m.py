"""
BACKTEST 5 MIN sur 3 mois (lit gold_5m.csv)
===========================================
Teste la strategie F3 (mean-rev + MA plate) en bougies 5 MINUTES sur les
~3 mois telecharges par fetch_gold_5m.py. Tourne hors-ligne (pas d'API).

Parametres identiques au test TradingView 5 min (MA90 barres, pente 30 barres),
+ couts + SL interne base close, comme nos autres backtests.

  PARTIE 1 -- Sensibilite a SLOPE_MAX
  PARTIE 2 -- Consistance semaine par semaine (13 semaines)

Usage : python backtest_5m.py   (necessite gold_5m.csv dans le meme dossier)
"""

import os
import pandas as pd
from collections import defaultdict

CSV_IN = "gold_5m.csv"

# ============================================================
# PARAMETRES (5 min, comme le test TradingView)
# ============================================================
MA_PERIOD       = 90      # barres (5 min) = 7h30 de moyenne
DEVIATION_PTS   = 5.0
SL_POINTS       = 8.0
TP_POINTS       = 7.0
SLOPE_LOOKBACK  = 30      # barres
TRADING_HOUR_START_UTC = 7
TRADING_HOUR_END_UTC   = 15
NEWS_BLOCK_TIMES_UTC = [(8, 0), (9, 0), (12, 30)]
NEWS_BLOCK_MINUTES   = 5
MAX_DAILY_TRADES = 10
COOLDOWN_MIN     = 10     # cooldown en minutes (time-based)

SLOPE_MAX_CHOSEN = 3.0
SLOPE_MAX_GRID   = [1, 2, 3, 4, 5, 6, 8, 999]   # 999 = filtre desactive

SPREAD_POINTS       = 0.4
SLIPPAGE_PTS        = 0.2
GUARANTEED_STOP_FEE = 0.5
LOT_SIZE            = 0.01


def is_news_window(dt):
    minutes = dt.hour * 60 + dt.minute
    return any(abs(minutes - (h * 60 + m)) <= NEWS_BLOCK_MINUTES for h, m in NEWS_BLOCK_TIMES_UTC)

def is_in_trading_window(dt):
    return TRADING_HOUR_START_UTC <= dt.hour < TRADING_HOUR_END_UTC


def backtest_f3(df, slope_max):
    trades = []
    in_position = False
    pos_direction = entry_price = entry_time = None
    sl_internal = sl_filet = take_profit = None
    last_close_time = None
    daily_trades = defaultdict(int)

    ma_arr    = df["ma"].values
    slope_arr = df["slope"].values
    t_arr     = df["time"].values
    close_arr = df["close"].values
    high_arr  = df["high"].values
    low_arr   = df["low"].values
    times     = df["time"]

    for i in range(len(df)):
        t = times.iloc[i]
        price = close_arr[i]; high = high_arr[i]; low = low_arr[i]

        if in_position:
            exit_price = reason = None
            if pos_direction == "LONG":
                if low <= sl_filet:        exit_price, reason = sl_filet, "SL_FILET"
                elif high >= take_profit:  exit_price, reason = take_profit, "TP"
                elif price <= sl_internal: exit_price, reason = sl_internal, "SL_INTERNE"
            else:
                if high >= sl_filet:       exit_price, reason = sl_filet, "SL_FILET"
                elif low <= take_profit:   exit_price, reason = take_profit, "TP"
                elif price >= sl_internal: exit_price, reason = sl_internal, "SL_INTERNE"
            if exit_price is not None:
                gross = (exit_price - entry_price) if pos_direction == "LONG" else (entry_price - exit_price)
                cost = SPREAD_POINTS + SLIPPAGE_PTS
                if reason == "SL_INTERNE": cost += SLIPPAGE_PTS
                elif reason == "SL_FILET": cost += GUARANTEED_STOP_FEE
                trades.append({"entry_time": entry_time, "pnl_pts": gross - cost})
                in_position = False
                last_close_time = t
                continue

        if in_position:                 continue
        if not is_in_trading_window(t): continue
        if is_news_window(t):           continue
        if last_close_time is not None and (t - last_close_time).total_seconds() < COOLDOWN_MIN * 60:
            continue
        day_key = t.strftime("%Y-%m-%d")
        if daily_trades[day_key] >= MAX_DAILY_TRADES:
            continue

        ma = ma_arr[i]; slope = slope_arr[i]
        if pd.isna(ma) or pd.isna(slope):
            continue
        if abs(slope) > slope_max:
            continue

        deviation = price - ma
        if deviation > DEVIATION_PTS:
            in_position, pos_direction, entry_price, entry_time = True, "SHORT", price, t
            sl_internal, sl_filet, take_profit = price + SL_POINTS, price + 50.0, price - TP_POINTS
            daily_trades[day_key] += 1
        elif deviation < -DEVIATION_PTS:
            in_position, pos_direction, entry_price, entry_time = True, "LONG", price, t
            sl_internal, sl_filet, take_profit = price - SL_POINTS, price - 50.0, price + TP_POINTS
            daily_trades[day_key] += 1

    return trades


def stats(trades):
    if not trades:
        return {"n": 0, "winrate": 0, "total": 0, "expectancy": 0, "pf": 0, "dd": 0}
    s = pd.DataFrame(trades)
    n = len(s)
    gw = s.loc[s["pnl_pts"] > 0, "pnl_pts"].sum()
    gl = s.loc[s["pnl_pts"] < 0, "pnl_pts"].sum()
    cum = s["pnl_pts"].cumsum()
    return {
        "n": n,
        "winrate": (s["pnl_pts"] > 0).sum() / n * 100,
        "total": s["pnl_pts"].sum(),
        "expectancy": s["pnl_pts"].sum() / n,
        "pf": (gw / abs(gl)) if gl != 0 else float("inf"),
        "dd": (cum - cum.cummax()).min(),
    }


def pf_str(pf):
    return "inf" if pf == float("inf") else f"{pf:.2f}"


if __name__ == "__main__":
    if not os.path.exists(CSV_IN):
        print(f"ERREUR : {CSV_IN} introuvable. Lance d'abord : python fetch_gold_5m.py")
        exit(1)

    df = pd.read_csv(CSV_IN, parse_dates=["time"])
    df = df.drop_duplicates(subset="time").sort_values("time").reset_index(drop=True)
    df["ma"]    = df["close"].rolling(MA_PERIOD).mean().shift(1)
    df["slope"] = df["ma"] - df["ma"].shift(SLOPE_LOOKBACK)
    days = max(1, (df["time"].iloc[-1] - df["time"].iloc[0]).days)

    print("=" * 78)
    print(f" BACKTEST 5 MIN -- {len(df)} bougies sur {days} jours "
          f"({df['time'].iloc[0]} -> {df['time'].iloc[-1]})")
    print("=" * 78)

    # ---- PARTIE 1 : sensibilite ----
    print("\n PARTIE 1 -- Sensibilite SLOPE_MAX")
    fmt = "{:>10} {:>8} {:>8} {:>10} {:>9} {:>8} {:>10}"
    print(fmt.format("SLOPE_MAX", "Trades", "WR%", "PnL pts", "Expect", "PF", "MaxDD"))
    print("-" * 78)
    for sm in SLOPE_MAX_GRID:
        s = stats(backtest_f3(df, sm))
        label = "sans" if sm >= 999 else f"{sm}"
        print(fmt.format(label, s["n"], f"{s['winrate']:.1f}", f"{s['total']:+.1f}",
                         f"{s['expectancy']:+.3f}", pf_str(s["pf"]), f"{s['dd']:+.1f}"))
    print("-" * 78)
    print("ROBUSTE si le PF reste >1.3 sur une plage (ex. 1 a 3).")

    # ---- PARTIE 2 : consistance par semaine ----
    print(f"\n PARTIE 2 -- Consistance par semaine (SLOPE_MAX={SLOPE_MAX_CHOSEN})")
    trades = backtest_f3(df, SLOPE_MAX_CHOSEN)
    by_week = defaultdict(list)
    for tr in trades:
        by_week[tr["entry_time"].strftime("%Y-W%U")].append(tr)
    fmt2 = "{:>10} {:>8} {:>8} {:>10} {:>8}"
    print(fmt2.format("Semaine", "Trades", "WR%", "PnL pts", "PF"))
    print("-" * 78)
    pos = 0
    for wk in sorted(by_week):
        s = stats(by_week[wk])
        if s["total"] > 0:
            pos += 1
        print(fmt2.format(wk, s["n"], f"{s['winrate']:.1f}", f"{s['total']:+.1f}", pf_str(s["pf"])))
    print("-" * 78)
    print(f"Semaines positives : {pos}/{len(by_week)}")
    print("ROBUSTE si la majorite des semaines sont positives.")
