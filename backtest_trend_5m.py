"""
BACKTEST TREND-FOLLOWING 5 MIN (lit gold_5m.csv)
================================================
Le mean-reversion ne marche plus dans le regime actuel (gold en tendance).
On teste l'inverse : TRADER DANS LE SENS de la pente de la MA90.

  Signal : LONG si pente(MA90, 30 barres) >= +SLOPE_MIN
           SHORT si pente <= -SLOPE_MIN
           (Inverse exact du filtre F3 : on veut une MA qui TREND)

  R:R asymetrique : SL serre (8 pts) / TP large (16, 20, 24 pts)

  PARTIE 1 -- Grille SLOPE_MIN x TP (9 combos) sur les 90j
  PARTIE 2 -- Walk-forward du meilleur combo (3 blocs)
  PARTIE 3 -- Variante "heures trendantes seulement" pour comparer

Specificite trend-following : WR souvent BAS (30-45%), un gros gain efface
plusieurs petites pertes. Le PF est le bon juge, pas le WR.

Tourne hors-ligne. Usage : python backtest_trend_5m.py
"""

import os
import pandas as pd
from collections import defaultdict
from datetime import timedelta

CSV_IN = "gold_5m.csv"

# ============================================================
# PARAMETRES
# ============================================================
MA_PERIOD       = 90
SLOPE_LOOKBACK  = 30
SL_POINTS       = 8.0      # SL serre (memes points que mean-rev pour comparer)
NEWS_BLOCK_TIMES_UTC = [(8, 0), (9, 0), (12, 30)]
NEWS_BLOCK_MINUTES   = 5
COOLDOWN_MIN     = 10
MAX_DAILY_TRADES = 5       # plus que mean-rev car signaux moins frequents

# Grille a tester
SLOPE_MIN_GRID = [3, 5, 8]    # seuil de "pente forte"
TP_GRID        = [16, 20, 24] # TP large pour R:R asymetrique

# Heures structurellement trendantes (analyse autocorr)
TRENDING_HOURS = {6, 8, 12, 13, 14}

SPREAD_POINTS       = 0.4
SLIPPAGE_PTS        = 0.2
GUARANTEED_STOP_FEE = 0.5


def is_news_window(dt):
    minutes = dt.hour * 60 + dt.minute
    return any(abs(minutes - (h * 60 + m)) <= NEWS_BLOCK_MINUTES for h, m in NEWS_BLOCK_TIMES_UTC)


def backtest_trend(df, slope_min, tp_pts, allowed_hours=None):
    """Trend-following : entrer dans le sens de la pente, SL/TP fixes.
       allowed_hours=None -> 24h ; sinon set d'heures UTC autorisees."""
    trades = []
    in_position = False
    pos_direction = entry_price = entry_time = None
    sl_price = tp_price = None
    last_close_time = None
    daily_trades = defaultdict(int)

    times = df["time"]
    close_arr = df["close"].values
    high_arr  = df["high"].values
    low_arr   = df["low"].values
    slope_arr = df["slope"].values
    hour_arr  = df["hour"].values

    for i in range(len(df)):
        t = times.iloc[i]
        price = close_arr[i]; high = high_arr[i]; low = low_arr[i]

        if in_position:
            exit_price = reason = None
            if pos_direction == "LONG":
                if low <= sl_price:     exit_price, reason = sl_price, "SL"
                elif high >= tp_price:  exit_price, reason = tp_price, "TP"
            else:
                if high >= sl_price:    exit_price, reason = sl_price, "SL"
                elif low <= tp_price:   exit_price, reason = tp_price, "TP"
            if exit_price is not None:
                gross = (exit_price - entry_price) if pos_direction == "LONG" else (entry_price - exit_price)
                # Couts : spread aller-retour + slippage entree + slippage sortie marche (SL au marche)
                cost = SPREAD_POINTS + SLIPPAGE_PTS
                if reason == "SL":
                    cost += SLIPPAGE_PTS
                # TP = ordre limite -> pas de slippage de sortie
                trades.append({"entry_time": entry_time, "pnl_pts": gross - cost, "reason": reason})
                in_position = False
                last_close_time = t
                continue

        if in_position:                   continue
        if allowed_hours is not None and hour_arr[i] not in allowed_hours:
            continue
        if is_news_window(t):             continue
        if last_close_time is not None and (t - last_close_time).total_seconds() < COOLDOWN_MIN * 60:
            continue
        day_key = t.strftime("%Y-%m-%d")
        if daily_trades[day_key] >= MAX_DAILY_TRADES:
            continue

        slope = slope_arr[i]
        if pd.isna(slope):
            continue

        if slope >= slope_min:
            in_position, pos_direction, entry_price, entry_time = True, "LONG", price, t
            sl_price = price - SL_POINTS
            tp_price = price + tp_pts
            daily_trades[day_key] += 1
        elif slope <= -slope_min:
            in_position, pos_direction, entry_price, entry_time = True, "SHORT", price, t
            sl_price = price + SL_POINTS
            tp_price = price - tp_pts
            daily_trades[day_key] += 1

    return trades


def stats(trades):
    if not trades:
        return {"n": 0, "winrate": 0, "total": 0, "expect": 0, "pf": 0, "dd": 0,
                "tp_n": 0, "sl_n": 0}
    s = pd.DataFrame(trades)
    n = len(s)
    gw = s.loc[s["pnl_pts"] > 0, "pnl_pts"].sum()
    gl = s.loc[s["pnl_pts"] < 0, "pnl_pts"].sum()
    cum = s["pnl_pts"].cumsum()
    return {"n": n, "winrate": (s["pnl_pts"] > 0).sum() / n * 100,
            "total": s["pnl_pts"].sum(), "expect": s["pnl_pts"].sum() / n,
            "pf": (gw / abs(gl)) if gl != 0 else float("inf"),
            "dd": (cum - cum.cummax()).min(),
            "tp_n": (s["reason"] == "TP").sum() if "reason" in s.columns else 0,
            "sl_n": (s["reason"] == "SL").sum() if "reason" in s.columns else 0}


def pf_str(pf):
    return "inf" if pf == float("inf") else f"{pf:.2f}"


if __name__ == "__main__":
    if not os.path.exists(CSV_IN):
        print(f"ERREUR : {CSV_IN} introuvable.")
        exit(1)

    df = pd.read_csv(CSV_IN, parse_dates=["time"])
    df = df.drop_duplicates(subset="time").sort_values("time").reset_index(drop=True)
    df["ma"]    = df["close"].rolling(MA_PERIOD).mean().shift(1)
    df["slope"] = df["ma"] - df["ma"].shift(SLOPE_LOOKBACK)
    df["hour"]  = df["time"].dt.hour

    t0, t1 = df["time"].iloc[0], df["time"].iloc[-1]
    span_days = (t1 - t0).days
    print("=" * 90)
    print(f" BACKTEST TREND-FOLLOWING 5 MIN  --  SL fixe {SL_POINTS} pts, R:R asymetrique")
    print(f" Donnees : {len(df)} bougies, {span_days} jours ({t0:%Y-%m-%d} -> {t1:%Y-%m-%d})")
    print("=" * 90)

    # ---- PARTIE 1 : Grille SLOPE_MIN x TP ----
    print("\n PARTIE 1 -- Grille SLOPE_MIN (force de la tendance) x TP (largeur du gain)")
    fmt = "{:>6} {:>4} {:>6} {:>4} {:>4} {:>6} {:>9} {:>8} {:>7} {:>9}"
    print(fmt.format("SL_MIN", "TP", "Trades", "TP_n", "SL_n", "WR%", "PnL pts",
                     "Expect", "PF", "MaxDD"))
    print("-" * 90)
    results = []
    for sm in SLOPE_MIN_GRID:
        for tp in TP_GRID:
            s = stats(backtest_trend(df, sm, tp))
            results.append((sm, tp, s))
            print(fmt.format(sm, tp, s["n"], s["tp_n"], s["sl_n"],
                             f"{s['winrate']:.1f}", f"{s['total']:+.1f}",
                             f"{s['expect']:+.3f}", pf_str(s["pf"]), f"{s['dd']:+.1f}"))
    print("-" * 90)

    # Meilleur combo selon PF (avec >=20 trades pour eviter le bruit)
    viable = [(sm, tp, s) for sm, tp, s in results if s["n"] >= 20]
    if not viable:
        print("\nPas de combo viable (>=20 trades). Trend-following sans edge ici.")
        exit(0)
    viable.sort(key=lambda x: x[2]["pf"], reverse=True)
    best_sm, best_tp, best_stats = viable[0]
    print(f"\n MEILLEUR COMBO : SLOPE_MIN={best_sm}, TP={best_tp} "
          f"-> PF {pf_str(best_stats['pf'])}, expect +{best_stats['expect']:.2f} pts/trade")

    # ---- PARTIE 2 : Walk-forward du meilleur combo ----
    print(f"\n PARTIE 2 -- Walk-forward (3 blocs) du meilleur combo")
    fmt2 = "{:>3} {:<24} {:>8} {:>7} {:>10} {:>8} {:>8} {:>9}"
    print(fmt2.format("#", "Periode", "Trades", "WR%", "PnL pts", "Expect", "PF", "MaxDD"))
    print("-" * 90)
    block_size = (t1 - t0) / 3
    block_stats = []
    for i in range(3):
        bstart = t0 + i * block_size
        bend = t0 + (i + 1) * block_size if i < 2 else t1
        sub = df[(df["time"] >= bstart) & (df["time"] < bend)].reset_index(drop=True)
        s = stats(backtest_trend(sub, best_sm, best_tp))
        block_stats.append(s)
        print(fmt2.format(i + 1, f"{bstart:%m-%d}->{bend:%m-%d}", s["n"],
                         f"{s['winrate']:.1f}", f"{s['total']:+.1f}",
                         f"{s['expect']:+.3f}", pf_str(s["pf"]), f"{s['dd']:+.1f}"))
    print("-" * 90)
    pos = sum(1 for b in block_stats if b["total"] > 0)
    print(f" Blocs positifs : {pos}/3")

    # ---- PARTIE 3 : Variante heures trendantes seulement ----
    print(f"\n PARTIE 3 -- Meme combo, mais HEURES TRENDANTES seulement {sorted(TRENDING_HOURS)} UTC")
    s = stats(backtest_trend(df, best_sm, best_tp, allowed_hours=TRENDING_HOURS))
    print(fmt.format(best_sm, best_tp, s["n"], s["tp_n"], s["sl_n"],
                     f"{s['winrate']:.1f}", f"{s['total']:+.1f}",
                     f"{s['expect']:+.3f}", pf_str(s["pf"]), f"{s['dd']:+.1f}"))

    print("\n" + "=" * 90)
    print(" LECTURE")
    print("=" * 90)
    print(" Trend-following : WR souvent 30-45% (c'est NORMAL). Le PF compte.")
    print(" - PF > 1.3 sur 90j ET 3 blocs positifs au walk-forward -> piste serieuse.")
    print(" - Si grille toute negative -> trend-following naif ne marche pas non plus.")
    print(" - Si Partie 3 (heures trendantes) est nettement mieux -> filtrer par heure.")
