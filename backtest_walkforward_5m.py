"""
WALK-FORWARD 5 MIN -- test de robustesse temporelle
====================================================
Decoupe les 90 jours de gold_5m.csv en blocs et applique la strategie
ACTUELLE (F3 + heures {7,9,10,11} + SLOPE_MAX=3 + couts) sur chaque bloc
independamment. Repond a la question : l'edge est-il CONSISTANT a travers
le temps, ou concentre dans une seule fenetre favorable ?

  PARTIE 1 -- 3 blocs contigus de ~30 jours chacun (samples independants)
  PARTIE 2 -- fenetres glissantes de 30 jours par pas de 7 jours
              (vue lissee de la consistance)

Regle de decision :
  - Tous les blocs > PF 1.1 et MaxDD raisonnable -> edge solide, on enchaine
    sur les ameliorations de sortie (breakeven, trailing).
  - 1 seul bloc porte tout le profit -> overfit, il faut rebatir.

Tourne hors-ligne. Usage : python backtest_walkforward_5m.py
"""

import os
import pandas as pd
from collections import defaultdict
from datetime import timedelta

CSV_IN = "gold_5m.csv"

# ============================================================
# PARAMETRES (config actuelle du bot)
# ============================================================
MA_PERIOD       = 90
DEVIATION_PTS   = 5.0
SL_POINTS       = 8.0
TP_POINTS       = 7.0
SLOPE_LOOKBACK  = 30
SLOPE_MAX       = 3.0
ALLOWED_HOURS   = {7, 9, 10, 11}     # fenetre actuelle du bot (apres blocage)
NEWS_BLOCK_TIMES_UTC = [(8, 0), (9, 0), (12, 30)]
NEWS_BLOCK_MINUTES   = 5
MAX_DAILY_TRADES = 10
COOLDOWN_MIN     = 10

SPREAD_POINTS       = 0.4
SLIPPAGE_PTS        = 0.2
GUARANTEED_STOP_FEE = 0.5

N_BLOCKS         = 3      # nb de blocs contigus pour la Partie 1
ROLLING_DAYS     = 30     # taille fenetre glissante Partie 2
ROLLING_STEP_D   = 7      # pas en jours


def is_news_window(dt):
    minutes = dt.hour * 60 + dt.minute
    return any(abs(minutes - (h * 60 + m)) <= NEWS_BLOCK_MINUTES for h, m in NEWS_BLOCK_TIMES_UTC)


def backtest(df):
    """Strategie ACTUELLE (figee) sur la sous-fenetre df. Retourne la liste des trades."""
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
        if hour_arr[i] not in ALLOWED_HOURS:  continue
        if is_news_window(t):                 continue
        if last_close_time is not None and (t - last_close_time).total_seconds() < COOLDOWN_MIN * 60:
            continue
        day_key = t.strftime("%Y-%m-%d")
        if daily_trades[day_key] >= MAX_DAILY_TRADES:
            continue

        ma = ma_arr[i]; slope = slope_arr[i]
        if pd.isna(ma) or pd.isna(slope) or abs(slope) > SLOPE_MAX:
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
        return {"n": 0, "winrate": 0, "total": 0, "expect": 0, "pf": 0, "dd": 0}
    s = pd.DataFrame(trades)
    n = len(s)
    gw = s.loc[s["pnl_pts"] > 0, "pnl_pts"].sum()
    gl = s.loc[s["pnl_pts"] < 0, "pnl_pts"].sum()
    cum = s["pnl_pts"].cumsum()
    return {"n": n, "winrate": (s["pnl_pts"] > 0).sum() / n * 100,
            "total": s["pnl_pts"].sum(), "expect": s["pnl_pts"].sum() / n,
            "pf": (gw / abs(gl)) if gl != 0 else float("inf"),
            "dd": (cum - cum.cummax()).min()}


def pf_str(pf):
    return "inf" if pf == float("inf") else f"{pf:.2f}"


if __name__ == "__main__":
    if not os.path.exists(CSV_IN):
        print(f"ERREUR : {CSV_IN} introuvable. Lance fetch_gold_5m.py d'abord.")
        exit(1)

    df = pd.read_csv(CSV_IN, parse_dates=["time"])
    df = df.drop_duplicates(subset="time").sort_values("time").reset_index(drop=True)
    df["ma"]    = df["close"].rolling(MA_PERIOD).mean().shift(1)
    df["slope"] = df["ma"] - df["ma"].shift(SLOPE_LOOKBACK)
    df["hour"]  = df["time"].dt.hour

    t0, t1 = df["time"].iloc[0], df["time"].iloc[-1]
    span_days = (t1 - t0).days

    print("=" * 86)
    print(f" WALK-FORWARD 5 MIN -- strategie ACTUELLE figee (F3, h={sorted(ALLOWED_HOURS)})")
    print(f" Donnees : {len(df)} bougies, {span_days} jours ({t0:%Y-%m-%d} -> {t1:%Y-%m-%d})")
    print("=" * 86)

    # ---- PARTIE 1 : N blocs contigus ----
    print(f"\n PARTIE 1 -- {N_BLOCKS} blocs contigus (samples independants)")
    block_size = (t1 - t0) / N_BLOCKS
    fmt = "{:>3} {:<24} {:>8} {:>7} {:>10} {:>8} {:>8} {:>10}"
    print(fmt.format("#", "Periode", "Trades", "WR%", "PnL pts", "Expect", "PF", "MaxDD"))
    print("-" * 86)
    block_stats = []
    for i in range(N_BLOCKS):
        bstart = t0 + i * block_size
        bend = t0 + (i + 1) * block_size if i < N_BLOCKS - 1 else t1
        sub = df[(df["time"] >= bstart) & (df["time"] < bend)].reset_index(drop=True)
        s = stats(backtest(sub))
        block_stats.append(s)
        print(fmt.format(i + 1, f"{bstart:%m-%d}->{bend:%m-%d}", s["n"],
                         f"{s['winrate']:.1f}", f"{s['total']:+.1f}",
                         f"{s['expect']:+.3f}", pf_str(s["pf"]), f"{s['dd']:+.1f}"))
    print("-" * 86)
    pos_blocks = sum(1 for b in block_stats if b["total"] > 0)
    pf_min = min(b["pf"] for b in block_stats if b["n"] > 0)
    pf_max = max(b["pf"] for b in block_stats if b["n"] > 0)
    print(f" Blocs positifs : {pos_blocks}/{N_BLOCKS}   |   PF : min {pf_min:.2f}, max {pf_str(pf_max)}")

    # ---- PARTIE 2 : fenetres glissantes ----
    print(f"\n PARTIE 2 -- fenetres glissantes de {ROLLING_DAYS}j (pas {ROLLING_STEP_D}j)")
    print(fmt.format("#", "Periode", "Trades", "WR%", "PnL pts", "Expect", "PF", "MaxDD"))
    print("-" * 86)
    rolling_stats = []
    cursor = t0
    k = 0
    while cursor + timedelta(days=ROLLING_DAYS) <= t1:
        wend = cursor + timedelta(days=ROLLING_DAYS)
        sub = df[(df["time"] >= cursor) & (df["time"] < wend)].reset_index(drop=True)
        s = stats(backtest(sub))
        rolling_stats.append(s)
        k += 1
        print(fmt.format(k, f"{cursor:%m-%d}->{wend:%m-%d}", s["n"],
                         f"{s['winrate']:.1f}", f"{s['total']:+.1f}",
                         f"{s['expect']:+.3f}", pf_str(s["pf"]), f"{s['dd']:+.1f}"))
        cursor += timedelta(days=ROLLING_STEP_D)
    print("-" * 86)
    pos = sum(1 for r in rolling_stats if r["total"] > 0)
    print(f" Fenetres glissantes positives : {pos}/{len(rolling_stats)}")
    if rolling_stats:
        pfs = [r["pf"] for r in rolling_stats if r["n"] > 0 and r["pf"] != float("inf")]
        if pfs:
            print(f" PF (fenetres glissantes) : min {min(pfs):.2f}, "
                  f"median {sorted(pfs)[len(pfs)//2]:.2f}, max {max(pfs):.2f}")

    # ---- Verdict ----
    print("\n" + "=" * 86)
    print(" REGLE DE DECISION")
    print("=" * 86)
    print(" - Si TOUS les 3 blocs PF > 1.1 -> edge consistant, on enchaine.")
    print(" - Si 1 seul bloc porte le profit (les autres ~0 ou negatifs) -> overfit.")
    print(" - Si fenetres glissantes : majorite positive ET PF median > 1.15 = solide.")
