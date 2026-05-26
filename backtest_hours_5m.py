"""
BACKTEST FENETRES HORAIRES (5 min, lit gold_5m.csv)
===================================================
L'analyse a montre que dans la fenetre 7-15 UTC, certaines heures TRENDENT
(8,12,13,14 -> mauvais pour fader) et d'autres MEAN-REVERTENT (9,10,11 -> bon).
Ce backtest compare differentes fenetres horaires pour voir si resserrer sur
les bonnes heures ameliore le profit factor et reduit le drawdown.

Base : strategie F3 (mean-rev + MA plate), SL8/TP7, couts inclus, 5 min.
Tourne hors-ligne. Usage : python backtest_hours_5m.py  (necessite gold_5m.csv)
"""

import os
import pandas as pd
from collections import defaultdict

CSV_IN = "gold_5m.csv"

# ============================================================
MA_PERIOD       = 90
DEVIATION_PTS   = 5.0
SL_POINTS       = 8.0
TP_POINTS       = 7.0
SLOPE_LOOKBACK  = 30
NEWS_BLOCK_TIMES_UTC = [(8, 0), (9, 0), (12, 30)]
NEWS_BLOCK_MINUTES   = 5
MAX_DAILY_TRADES = 10
COOLDOWN_MIN     = 10

SPREAD_POINTS       = 0.4
SLIPPAGE_PTS        = 0.2
GUARANTEED_STOP_FEE = 0.5

# Fenetres a comparer (ensembles d'heures UTC autorisees a OUVRIR un trade)
WINDOWS = [
    ("7-15 actuel",          set(range(7, 15))),     # 7,8,9,10,11,12,13,14
    ("7-15 sans 8/12/13/14", {7, 9, 10, 11}),
    ("9-11 seul",            {9, 10, 11}),
    ("9-11 +17",             {9, 10, 11, 17}),
    ("9-11 +0 +17",          {0, 9, 10, 11, 17}),
]
SLOPE_MAX_LIST = [3, 1]      # on teste 2 niveaux de filtre F3
WEEKLY_WINDOW  = ("9-11 seul", {9, 10, 11})   # fenetre pour le detail hebdo
WEEKLY_SLOPE   = 3


def is_news_window(dt):
    minutes = dt.hour * 60 + dt.minute
    return any(abs(minutes - (h * 60 + m)) <= NEWS_BLOCK_MINUTES for h, m in NEWS_BLOCK_TIMES_UTC)


def backtest(df, allowed_hours, slope_max):
    trades = []
    in_position = False
    pos_direction = entry_price = entry_time = None
    sl_internal = sl_filet = take_profit = None
    last_close_time = None
    daily_trades = defaultdict(int)

    times     = df["time"]
    close_arr = df["close"].values
    high_arr  = df["high"].values
    low_arr   = df["low"].values
    ma_arr    = df["ma"].values
    slope_arr = df["slope"].values
    hour_arr  = df["hour"].values

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

        if in_position:                       continue
        if hour_arr[i] not in allowed_hours:  continue
        if is_news_window(t):                 continue
        if last_close_time is not None and (t - last_close_time).total_seconds() < COOLDOWN_MIN * 60:
            continue
        day_key = t.strftime("%Y-%m-%d")
        if daily_trades[day_key] >= MAX_DAILY_TRADES:
            continue

        ma = ma_arr[i]; slope = slope_arr[i]
        if pd.isna(ma) or pd.isna(slope) or abs(slope) > slope_max:
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
    return {"n": n, "winrate": (s["pnl_pts"] > 0).sum() / n * 100,
            "total": s["pnl_pts"].sum(), "expectancy": s["pnl_pts"].sum() / n,
            "pf": (gw / abs(gl)) if gl != 0 else float("inf"),
            "dd": (cum - cum.cummax()).min()}


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
    df["hour"]  = df["time"].dt.hour
    days = max(1, (df["time"].iloc[-1] - df["time"].iloc[0]).days)

    print("=" * 88)
    print(f" COMPARAISON FENETRES HORAIRES -- 5 min, {len(df)} bougies, {days} jours")
    print("=" * 88)

    for sm in SLOPE_MAX_LIST:
        print(f"\n--- SLOPE_MAX = {sm} ---")
        fmt = "{:<24} {:>7} {:>7} {:>7} {:>9} {:>8} {:>8} {:>9}"
        print(fmt.format("Fenetre", "Trades", "/jour", "WR%", "PnL pts", "Expect", "PF", "MaxDD"))
        print("-" * 88)
        for name, hours in WINDOWS:
            s = stats(backtest(df, hours, sm))
            print(fmt.format(name, s["n"], f"{s['n']/days:.2f}", f"{s['winrate']:.1f}",
                             f"{s['total']:+.1f}", f"{s['expectancy']:+.3f}",
                             pf_str(s["pf"]), f"{s['dd']:+.1f}"))
    print("-" * 88)
    print("Objectif : une fenetre resserree doit MONTER le PF et BAISSER le |MaxDD|,")
    print("tout en gardant assez de trades. Compare a '7-15 actuel'.")

    # ---- Detail hebdomadaire de la fenetre candidate ----
    wname, whours = WEEKLY_WINDOW
    print(f"\n=== Consistance hebdo : {wname}, SLOPE_MAX={WEEKLY_SLOPE} ===")
    trades = backtest(df, whours, WEEKLY_SLOPE)
    by_week = defaultdict(list)
    for tr in trades:
        by_week[tr["entry_time"].strftime("%Y-W%U")].append(tr)
    fmt2 = "{:>10} {:>8} {:>8} {:>10} {:>8}"
    print(fmt2.format("Semaine", "Trades", "WR%", "PnL pts", "PF"))
    print("-" * 50)
    pos = 0
    for wk in sorted(by_week):
        s = stats(by_week[wk])
        if s["total"] > 0:
            pos += 1
        print(fmt2.format(wk, s["n"], f"{s['winrate']:.1f}", f"{s['total']:+.1f}", pf_str(s["pf"])))
    print("-" * 50)
    print(f"Semaines positives : {pos}/{len(by_week)}")
