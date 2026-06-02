"""
EVALUATE SIGNALS -- compare les alertes Telegram avec la realite du gold
========================================================================
Pour chacun des 33 signaux recus le 1-2 juin 2026, on regarde dans
gold_5m.csv ce que le prix a fait APRES la generation du signal : touche
de SL ou de TP en premier ? Combien de temps ? Quel PnL net (apres couts) ?

Resume statistique par type (MEAN-REV / TREND) et global : WR, PnL, PF.

PREREQUIS : lance d'abord
   python fetch_gold_5m.py
pour rafraichir gold_5m.csv jusqu'au 2 juin (sinon les signaux du 1-2 juin
ne pourront pas etre evalues -- il manquera les bougies).

Usage : python evaluate_signals.py
"""

import os
import pandas as pd
from datetime import datetime, timedelta

CSV_IN = "gold_5m.csv"

# Couts simules (spread aller-retour + slippage d'entree)
COST_PTS_PER_TRADE = 0.6

# Lookahead max (bougies 5min) avant TIME_OUT
MAX_LOOKAHEAD_MR = 24    # 2h pour mean-rev (TP serre 7pts)
MAX_LOOKAHEAD_TR = 48    # 4h pour trend  (TP large 20pts)

# ─────────────────────────────────────────────
# SIGNAUX recus sur Telegram (1-2 juin 2026)
# Format : (date, heure_UTC, "MR"|"TR", "BUY"|"SELL", entry, tp, sl)
# ─────────────────────────────────────────────
SIGNALS = [
    # ---- 1er juin ----
    ("2026-06-01", "06:32", "MR", "BUY",  4511.72, 4518.72, 4503.72),
    ("2026-06-01", "06:47", "MR", "BUY",  4502.02, 4509.02, 4494.02),
    ("2026-06-01", "07:01", "TR", "SELL", 4502.45, 4482.45, 4510.45),
    ("2026-06-01", "08:30", "MR", "BUY",  4495.02, 4502.02, 4487.02),
    ("2026-06-01", "09:17", "MR", "SELL", 4503.29, 4496.29, 4511.29),
    ("2026-06-01", "09:33", "MR", "BUY",  4491.70, 4498.70, 4483.70),
    ("2026-06-01", "10:31", "MR", "SELL", 4503.86, 4496.86, 4511.86),
    ("2026-06-01", "10:47", "MR", "SELL", 4506.28, 4499.28, 4514.28),
    ("2026-06-01", "11:19", "MR", "SELL", 4507.47, 4500.47, 4515.47),
    ("2026-06-01", "12:20", "MR", "BUY",  4499.80, 4506.80, 4491.80),
    ("2026-06-01", "12:36", "MR", "BUY",  4500.90, 4507.90, 4492.90),
    ("2026-06-01", "12:58", "MR", "BUY",  4499.48, 4506.48, 4491.48),
    ("2026-06-01", "13:14", "MR", "BUY",  4478.43, 4485.43, 4470.43),
    ("2026-06-01", "13:22", "TR", "SELL", 4468.98, 4448.98, 4476.98),
    ("2026-06-01", "13:37", "TR", "SELL", 4471.24, 4451.24, 4479.24),
    ("2026-06-01", "13:53", "TR", "SELL", 4457.15, 4437.15, 4465.15),
    ("2026-06-01", "14:08", "TR", "SELL", 4452.06, 4432.06, 4460.06),
    ("2026-06-01", "14:24", "TR", "SELL", 4459.25, 4439.25, 4467.25),
    ("2026-06-01", "14:40", "TR", "SELL", 4455.55, 4435.55, 4463.55),
    ("2026-06-01", "14:55", "TR", "SELL", 4456.17, 4436.17, 4464.17),
    # ---- 2 juin ----
    ("2026-06-02", "06:01", "TR", "BUY",  4520.31, 4540.31, 4512.31),
    ("2026-06-02", "06:42", "TR", "BUY",  4532.20, 4552.20, 4524.20),
    ("2026-06-02", "06:58", "TR", "BUY",  4533.52, 4553.52, 4525.52),
    ("2026-06-02", "07:35", "TR", "BUY",  4533.47, 4553.47, 4525.47),
    ("2026-06-02", "09:30", "MR", "BUY",  4525.99, 4532.99, 4517.99),
    ("2026-06-02", "10:58", "MR", "SELL", 4532.99, 4525.99, 4540.99),
    ("2026-06-02", "12:48", "MR", "BUY",  4521.73, 4528.73, 4513.73),
    ("2026-06-02", "13:03", "MR", "BUY",  4517.59, 4524.59, 4509.59),
    ("2026-06-02", "13:37", "TR", "SELL", 4507.76, 4487.76, 4515.76),
    ("2026-06-02", "13:53", "TR", "SELL", 4508.68, 4488.68, 4516.68),
    ("2026-06-02", "14:08", "TR", "SELL", 4503.22, 4483.22, 4511.22),
    ("2026-06-02", "14:24", "TR", "SELL", 4491.04, 4471.04, 4499.04),
    ("2026-06-02", "14:40", "TR", "SELL", 4502.52, 4482.52, 4510.52),
]


def next_5min_bar(dt):
    """Plus petite frontiere de bougie 5min STRICTEMENT apres dt.
    Le signal a 06:32 -> on regarde la bougie qui commence a 06:35.
    Le signal a 06:55 -> on regarde la bougie qui commence a 07:00."""
    floored = dt.replace(second=0, microsecond=0) - timedelta(minutes=dt.minute % 5)
    return floored + timedelta(minutes=5)


def evaluate(df, sig):
    date_str, time_str, sig_type, direction, entry, tp, sl = sig
    sig_dt = datetime.fromisoformat(f"{date_str}T{time_str}:00")
    start_dt = next_5min_bar(sig_dt)

    sub = df[df["time"] >= start_dt].reset_index(drop=True)
    max_look = MAX_LOOKAHEAD_MR if sig_type == "MR" else MAX_LOOKAHEAD_TR
    sub = sub.head(max_look)

    if len(sub) == 0:
        return {"outcome": "NO_DATA", "pnl_pts": 0.0, "pnl_gross": 0.0,
                "exit_price": entry, "bars": 0}

    outcome = exit_price = None
    bars_to_close = 0
    for i in range(len(sub)):
        bar = sub.iloc[i]
        if direction == "BUY":
            hit_sl = bar["low"]  <= sl
            hit_tp = bar["high"] >= tp
        else:  # SELL
            hit_sl = bar["high"] >= sl
            hit_tp = bar["low"]  <= tp

        if hit_sl and hit_tp:
            # Conflit dans la meme bougie : on suppose le SL en premier (conservateur).
            outcome, exit_price = "SL_CONFLICT", sl
            bars_to_close = i + 1
            break
        if hit_sl:
            outcome, exit_price = "SL", sl
            bars_to_close = i + 1
            break
        if hit_tp:
            outcome, exit_price = "TP", tp
            bars_to_close = i + 1
            break

    if outcome is None:
        outcome = "TIME_OUT"
        exit_price = float(sub.iloc[-1]["close"])
        bars_to_close = len(sub)

    pnl_gross = (exit_price - entry) if direction == "BUY" else (entry - exit_price)
    pnl_net   = pnl_gross - COST_PTS_PER_TRADE
    return {"outcome": outcome, "pnl_pts": pnl_net, "pnl_gross": pnl_gross,
            "exit_price": exit_price, "bars": bars_to_close}


def summarize(label, results):
    if not results:
        return
    n = len(results)
    tp_n = sum(1 for r in results if r["outcome"] == "TP")
    sl_n = sum(1 for r in results if r["outcome"] in ("SL", "SL_CONFLICT"))
    to_n = sum(1 for r in results if r["outcome"] == "TIME_OUT")
    nd_n = sum(1 for r in results if r["outcome"] == "NO_DATA")
    wins = sum(1 for r in results if r["pnl_pts"] > 0)
    losses = sum(1 for r in results if r["pnl_pts"] < 0)
    wr_net = wins / n * 100 if n else 0
    total_gross = sum(r["pnl_gross"] for r in results)
    total_net   = sum(r["pnl_pts"]   for r in results)
    gw = sum(r["pnl_pts"] for r in results if r["pnl_pts"] > 0)
    gl = sum(r["pnl_pts"] for r in results if r["pnl_pts"] < 0)
    pf = (gw / abs(gl)) if gl else float("inf")
    avg_bars = sum(r["bars"] for r in results) / n if n else 0

    print(f"\n{label} ({n} signaux)")
    print(f"  Outcomes        : TP={tp_n}, SL={sl_n}, TIMEOUT={to_n}, NO_DATA={nd_n}")
    print(f"  WR (PnL net>0)  : {wr_net:.1f}%  ({wins}W / {losses}L)")
    print(f"  PnL brut total  : {total_gross:+.1f} pts")
    print(f"  PnL net total   : {total_net:+.1f} pts   (couts simules {COST_PTS_PER_TRADE} pt/trade)")
    print(f"  Profit factor   : " + ("inf" if pf == float("inf") else f"{pf:.2f}"))
    print(f"  Duree moyenne   : {avg_bars*5:.0f} min  ({avg_bars:.1f} bougies 5min)")


def main():
    if not os.path.exists(CSV_IN):
        print(f"ERREUR : {CSV_IN} introuvable. Lance d'abord : python fetch_gold_5m.py")
        return

    df = pd.read_csv(CSV_IN, parse_dates=["time"])
    df = df.drop_duplicates(subset="time").sort_values("time").reset_index(drop=True)
    # tz-naive (au cas ou)
    if hasattr(df["time"].dt, "tz") and df["time"].dt.tz is not None:
        df["time"] = df["time"].dt.tz_localize(None)

    print("=" * 100)
    print(" EVALUATION DES SIGNAUX TELEGRAM vs REALITE GOLD")
    print(f" Donnees : {len(df)} bougies, jusqu'a {df['time'].iloc[-1]}")
    last_sig_dt = datetime.fromisoformat(f"{SIGNALS[-1][0]}T{SIGNALS[-1][1]}:00")
    if df["time"].iloc[-1] < last_sig_dt:
        print(f" /!\\ Donnees insuffisantes : dernier signal {last_sig_dt}, mais CSV "
              f"s'arrete a {df['time'].iloc[-1]}.")
        print("     -> relance python fetch_gold_5m.py pour rafraichir.\n")

    print(f" Couts simules : {COST_PTS_PER_TRADE} pts/trade (spread + slippage)")
    print(f" Lookahead     : {MAX_LOOKAHEAD_MR*5}min MR, {MAX_LOOKAHEAD_TR*5}min TR")
    print("=" * 100)

    fmt = "{:>3} {:<10} {:>5} {:<4} {:<4} {:>8} {:>8} {:<12} {:>8} {:>5}"
    print(fmt.format("#", "Date", "Heure", "Typ", "Dir", "Entry", "Exit",
                     "Outcome", "PnL net", "Bars"))
    print("-" * 100)

    results = []
    for i, sig in enumerate(SIGNALS, 1):
        r = evaluate(df, sig)
        results.append((sig, r))
        date_str, time_str, sig_type, direction, entry, *_ = sig
        print(fmt.format(i, date_str, time_str, sig_type, direction,
                         f"{entry:.2f}", f"{r['exit_price']:.2f}",
                         r["outcome"], f"{r['pnl_pts']:+.2f}", r["bars"]))

    print("-" * 100)

    # Resumes
    mr = [r for s, r in results if s[2] == "MR"]
    tr = [r for s, r in results if s[2] == "TR"]
    summarize("=== MEAN-REV ===", mr)
    summarize("=== TREND ===",    tr)
    summarize("=== TOUS SIGNAUX ===", [r for _, r in results])

    print("\n" + "=" * 100)
    print(" LECTURE")
    print(" - WR (net) compte un trade gagnant si le PnL apres couts est > 0.")
    print(" - TP = take-profit touche, SL = stop-loss, TIMEOUT = cloture forcee apres")
    print(f"   {MAX_LOOKAHEAD_MR*5}min (MR) ou {MAX_LOOKAHEAD_TR*5}min (TR) sans TP/SL.")
    print(" - PF > 1.3 = vraie piste exploitable. < 1 = perdant.")


if __name__ == "__main__":
    main()
