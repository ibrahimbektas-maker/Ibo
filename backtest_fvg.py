"""
BACKTEST FVG (Fair Value Gap) -- methode SMC/ICT vue chez Palomatrd
====================================================================
Detecte les FVG (gaps de prix non-rebalances) sur gold_5m.csv et simule
les trades qu'on aurait pris si on rentrait au retour du prix dans le FVG.

CONCEPT FVG :
  3 bougies consecutives (c1, c2, c3) :
    BULLISH FVG : c1.high < c3.low  -> gap entre c1.high et c3.low
                  (zone non tradee, prix tendrait a revenir dedans)
    BEARISH FVG : c1.low  > c3.high -> gap entre c3.high et c1.low

REGLE DE TRADE (V1, sans Fibo) :
  - On garde les FVG actifs (cree il y a < FVG_MAX_AGE_BARS bougies)
  - Quand le prix revient dans un FVG bullish -> on achete (LONG)
  - Quand le prix revient dans un FVG bearish -> on vend (SHORT)
  - SL et TP fixes en points (testes en grille)
  - 1 seule position a la fois

GRILLE TESTEE :
  - SL_PTS  in {10, 15, 20}      (Palomatrd : 15)
  - TP_PTS  in {15, 30, 45}      (Palomatrd TP1=11 / TP2=36)
  - FVG_MAX_AGE in {30, 60} bougies (~ 2h30 a 5h)

Couts inclus (spread + slippage entree + slippage sortie marche).
Walk-forward 3 blocs pour le meilleur combo.

Tourne hors-ligne. Usage : python backtest_fvg.py
"""

import os
import pandas as pd
from collections import defaultdict
from datetime import timedelta

CSV_IN = "gold_5m.csv"

COST_PTS_PER_TRADE = 0.8     # spread 0.4 + slippage entree 0.2 + slippage sortie 0.2

SL_GRID  = [10, 15, 20]
TP_GRID  = [15, 30, 45]
AGE_GRID = [30, 60]


def detect_fvg(c1, c3):
    """Renvoie ('bull', top, bottom), ('bear', top, bottom) ou None."""
    if c1["high"] < c3["low"]:
        return ("bull", float(c3["low"]), float(c1["high"]))
    if c1["low"] > c3["high"]:
        return ("bear", float(c1["low"]), float(c3["high"]))
    return None


def backtest_fvg(df, sl_pts, tp_pts, fvg_max_age_bars):
    """Backtest FVG basique avec SL/TP fixes."""
    trades = []
    active = []          # FVG actifs : liste de dicts
    in_pos = False
    pos = None

    times = df["time"].values
    highs = df["high"].values
    lows  = df["low"].values
    closes= df["close"].values

    for i in range(2, len(df)):
        high_i, low_i = float(highs[i]), float(lows[i])

        # ---- gestion position ouverte ----
        if in_pos:
            if pos["dir"] == "LONG":
                hit_sl = low_i  <= pos["sl"]
                hit_tp = high_i >= pos["tp"]
            else:
                hit_sl = high_i >= pos["sl"]
                hit_tp = low_i  <= pos["tp"]
            outcome = exit_price = None
            if hit_sl and hit_tp:
                outcome, exit_price = "SL", pos["sl"]       # conflit -> SL (conservateur)
            elif hit_sl:
                outcome, exit_price = "SL", pos["sl"]
            elif hit_tp:
                outcome, exit_price = "TP", pos["tp"]
            if outcome:
                gross = (exit_price - pos["entry"]) if pos["dir"] == "LONG" else (pos["entry"] - exit_price)
                trades.append({
                    "entry_time": pos["entry_time"],
                    "dir": pos["dir"],
                    "pnl_pts": gross - COST_PTS_PER_TRADE,
                    "outcome": outcome,
                    "bars": i - pos["entry_idx"],
                })
                in_pos = False
                pos = None

        # ---- detection nouveau FVG ----
        c1 = {"high": float(highs[i-2]), "low": float(lows[i-2])}
        c3 = {"high": high_i, "low": low_i}
        fvg = detect_fvg(c1, c3)
        if fvg is not None:
            kind, top, bottom = fvg
            active.append({"kind": kind, "top": top, "bottom": bottom, "created_idx": i})

        # ---- expiration FVG anciens ----
        active = [f for f in active if i - f["created_idx"] < fvg_max_age_bars]

        # ---- entree : prix revient dans un FVG actif ----
        if not in_pos:
            for fvg in list(active):
                if fvg["kind"] == "bull" and low_i <= fvg["top"]:
                    # prix descend dans le FVG bull -> LONG au sommet du FVG
                    entry = fvg["top"]
                    pos = {"dir": "LONG", "entry": entry,
                           "sl": entry - sl_pts, "tp": entry + tp_pts,
                           "entry_time": times[i], "entry_idx": i}
                    in_pos = True
                    active.remove(fvg)
                    break
                if fvg["kind"] == "bear" and high_i >= fvg["bottom"]:
                    entry = fvg["bottom"]
                    pos = {"dir": "SHORT", "entry": entry,
                           "sl": entry + sl_pts, "tp": entry - tp_pts,
                           "entry_time": times[i], "entry_idx": i}
                    in_pos = True
                    active.remove(fvg)
                    break

    return trades


def stats(trades):
    if not trades:
        return {"n": 0, "wr": 0, "total": 0, "expect": 0, "pf": 0, "dd": 0, "avg_bars": 0}
    s = pd.DataFrame(trades)
    n = len(s)
    gw = s.loc[s["pnl_pts"] > 0, "pnl_pts"].sum()
    gl = s.loc[s["pnl_pts"] < 0, "pnl_pts"].sum()
    cum = s["pnl_pts"].cumsum()
    return {
        "n": n,
        "wr": (s["pnl_pts"] > 0).sum() / n * 100,
        "total": s["pnl_pts"].sum(),
        "expect": s["pnl_pts"].sum() / n,
        "pf": (gw / abs(gl)) if gl else float("inf"),
        "dd": (cum - cum.cummax()).min(),
        "avg_bars": s["bars"].mean(),
    }


def pf_str(pf):
    return "inf" if pf == float("inf") else f"{pf:.2f}"


if __name__ == "__main__":
    if not os.path.exists(CSV_IN):
        print(f"ERREUR : {CSV_IN} introuvable. Lance fetch_gold_5m.py d'abord.")
        exit(1)

    df = pd.read_csv(CSV_IN, parse_dates=["time"])
    df = df.drop_duplicates(subset="time").sort_values("time").reset_index(drop=True)
    days = max(1, (df["time"].iloc[-1] - df["time"].iloc[0]).days)

    print("=" * 92)
    print(f" BACKTEST FVG (Fair Value Gap) -- {len(df)} bougies 5min, {days} jours")
    print(f" Couts simules : {COST_PTS_PER_TRADE} pt/trade")
    print("=" * 92)

    # ---- PARTIE 1 : grille SL x TP x AGE ----
    print("\n PARTIE 1 -- Grille SL x TP x FVG_MAX_AGE")
    fmt = "{:>4} {:>4} {:>5} {:>7} {:>6} {:>9} {:>8} {:>7} {:>9} {:>7}"
    print(fmt.format("SL", "TP", "AGE", "Trades", "WR%", "PnL pts", "Expect", "PF", "MaxDD", "Bars"))
    print("-" * 92)

    results = []
    for sl in SL_GRID:
        for tp in TP_GRID:
            for age in AGE_GRID:
                trs = backtest_fvg(df, sl, tp, age)
                s = stats(trs)
                results.append((sl, tp, age, s, trs))
                print(fmt.format(sl, tp, age, s["n"], f"{s['wr']:.1f}",
                                 f"{s['total']:+.1f}", f"{s['expect']:+.3f}",
                                 pf_str(s["pf"]), f"{s['dd']:+.1f}",
                                 f"{s['avg_bars']:.1f}"))
        print("-" * 92)

    # ---- Meilleur combo ----
    viable = [r for r in results if r[3]["n"] >= 20]
    if not viable:
        print("\nPas de combo viable (>=20 trades). Le concept FVG seul ne genere pas assez de signaux.")
        exit(0)
    viable.sort(key=lambda r: r[3]["pf"], reverse=True)
    best_sl, best_tp, best_age, best_s, best_trades = viable[0]

    print(f"\n MEILLEUR COMBO : SL={best_sl}, TP={best_tp}, AGE={best_age}")
    print(f"   PF {pf_str(best_s['pf'])} | WR {best_s['wr']:.1f}% | "
          f"PnL {best_s['total']:+.1f} pts | Expect {best_s['expect']:+.3f} pts/trade | "
          f"DD {best_s['dd']:+.1f}")

    # ---- PARTIE 2 : walk-forward du meilleur ----
    print(f"\n PARTIE 2 -- Walk-forward du meilleur combo (3 blocs)")
    fmt2 = "{:>3} {:<24} {:>8} {:>7} {:>10} {:>8} {:>8}"
    print(fmt2.format("#", "Periode", "Trades", "WR%", "PnL pts", "PF", "MaxDD"))
    print("-" * 92)

    t0, t1 = df["time"].iloc[0], df["time"].iloc[-1]
    block_size = (t1 - t0) / 3
    pos_blocks = 0
    for i in range(3):
        bstart = t0 + i * block_size
        bend = t0 + (i + 1) * block_size if i < 2 else t1
        sub = df[(df["time"] >= bstart) & (df["time"] < bend)].reset_index(drop=True)
        trs = backtest_fvg(sub, best_sl, best_tp, best_age)
        s = stats(trs)
        if s["total"] > 0:
            pos_blocks += 1
        print(fmt2.format(i + 1, f"{bstart:%m-%d}->{bend:%m-%d}", s["n"],
                         f"{s['wr']:.1f}", f"{s['total']:+.1f}",
                         pf_str(s["pf"]), f"{s['dd']:+.1f}"))
    print("-" * 92)
    print(f" Blocs positifs : {pos_blocks}/3")

    # ---- Verdict ----
    print("\n" + "=" * 92)
    print(" VERDICT")
    print("=" * 92)
    if best_s["pf"] >= 1.3 and pos_blocks >= 2:
        print(" >>> EDGE DETECTE : PF >1.3 sur la totalite + au moins 2/3 blocs positifs.")
        print("     On peut affiner : ajouter le filtre Fibonacci (0.5-0.786), filtrer")
        print("     par heure (sa methode + nos heures favorables), ou augmenter la taille.")
    elif best_s["pf"] >= 1.1:
        print(" >>> EDGE MARGINAL : positif mais sous le seuil 'solide'. La methode FVG")
        print("     seule (sans Fibo) capte quelque chose mais c'est mince. Ajouter le")
        print("     filtre Fibo peut aider, ou la methode complete reste fragile.")
    else:
        print(" >>> PAS D'EDGE : le concept FVG seul ne genere pas de signal exploitable")
        print("     sur le gold 5min 90j. Soit la methode complete (FVG + Fibo + contexte)")
        print("     est INDISSOCIABLE et le FVG seul ne suffit pas, soit elle se trompe.")
