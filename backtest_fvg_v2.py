"""
BACKTEST FVG v2 -- methode COMPLETE Palomatrd (FVG + Fibo + bias + session)
============================================================================
V1 testait FVG seul -> 1000+ trades/mois, PF 0.89.
Elle prend 1-2 trades/jour avec ~80% WR (observation 4 mois live).
Donc elle FILTRE massivement. V2 ajoute les filtres qui collent a sa methode :

  FILTRE 1 -- ALIGNEMENT FIBONACCI
    Le FVG doit tomber dans la zone 0.5-0.786 du dernier swing
    (les niveaux qu'elle trace systematiquement).

  FILTRE 2 -- BIAIS TIMEFRAME SUPERIEUR (1h)
    BUY uniquement si MA1h en hausse (prix > MA, MA monte).
    SELL uniquement si MA1h en baisse.

  FILTRE 3 -- SESSION HORAIRE
    Trade pendant la fenetre de session active uniquement.

  FILTRE 4 -- MAX 2 TRADES / JOUR
    Une fois 2 trades pris, plus rien jusqu'au lendemain.

Cible : descendre de 1000+ a 30-40 trades/mois, et voir si l'edge apparait.

Tourne hors-ligne. Usage : python backtest_fvg_v2.py
"""

import os
import pandas as pd
import numpy as np
from collections import defaultdict
from datetime import timedelta

CSV_IN = "gold_5m.csv"
COST_PTS_PER_TRADE = 0.8

# Parametres FVG (sa valeur SL 15, TP1 11)
SL_PTS         = 15.0
TP_PTS         = 11.0       # TP1, son target le plus rapide
FVG_MAX_AGE    = 30         # bougies (2h30 sur 5min)

# Filtre Fibo
FIB_LOOKBACK   = 50         # bougies pour identifier swing high/low
FIB_MIN        = 0.50       # zone d'entree Fibo basse
FIB_MAX        = 0.786      # zone d'entree Fibo haute

# Filtre HTF (1 heure)
HTF_MA_PERIOD  = 50         # MA sur 50 bougies horaires (~2 jours)
HTF_SLOPE_LB   = 5          # pente sur 5 bougies horaires

# Filtre session
SESSION_HOURS_UTC = set(range(8, 16))   # 8h-15h UTC = matin Europe + ouverture NY

# Limite quotidienne
MAX_TRADES_PER_DAY = 2


def detect_fvg(c1, c3):
    if c1["high"] < c3["low"]:
        return ("bull", float(c3["low"]), float(c1["high"]))
    if c1["low"] > c3["high"]:
        return ("bear", float(c1["low"]), float(c3["high"]))
    return None


def compute_htf_bias(df, ma_period=HTF_MA_PERIOD):
    """Resample en 1h, calcule MA et pente, puis re-aligne sur la grille 5min.
    Renvoie une Serie 'htf_bias' : +1 = bullish, -1 = bearish, 0 = neutre."""
    df_h = df.set_index("time").resample("1h").agg(
        open=("open", "first"), high=("high", "max"),
        low=("low", "min"),     close=("close", "last")
    ).dropna()
    df_h["ma"] = df_h["close"].rolling(ma_period).mean()
    df_h["slope"] = df_h["ma"] - df_h["ma"].shift(HTF_SLOPE_LB)
    # Bias : prix > MA ET pente positive = bullish, inverse = bearish
    df_h["bias"] = 0
    df_h.loc[(df_h["close"] > df_h["ma"]) & (df_h["slope"] > 0), "bias"] = 1
    df_h.loc[(df_h["close"] < df_h["ma"]) & (df_h["slope"] < 0), "bias"] = -1
    # Reindex sur la grille 5min (forward fill : la valeur 1h s'applique a toutes les 5min suivantes)
    htf_bias = df_h["bias"].reindex(df["time"], method="ffill").fillna(0).values
    return htf_bias.astype(int)


def fib_zone(swing_low, swing_high, is_long):
    """Renvoie (zone_low, zone_high) pour une entree dans la zone Fibo
    0.5-0.786 d'un retracement.
    Pour un LONG : on s'attend a un retracement depuis swing_high vers swing_low.
        zone = [swing_low + 0.5*range, swing_low + 0.786*range]
    Wait, c'est plus subtil : Fibo 0 = sommet du swing (apres impulsion haussiere),
    Fibo 1 = base. Zone 0.5-0.786 = retracement de 50-78.6% depuis le sommet
    = sommet - 0.5*range a sommet - 0.786*range = base + 0.5*range a base + 0.214*range.
    Donc pour LONG (achat dans le retracement) :
        zone_high = base + 0.5*range = swing_low + 0.5*(swing_high - swing_low)
        zone_low  = base + 0.214*range = swing_low + 0.214*(swing_high - swing_low)
    """
    rng = swing_high - swing_low
    if is_long:
        # Retracement depuis high vers low : zone 50-78.6% du retracement
        zone_high = swing_low + 0.50  * rng
        zone_low  = swing_low + 0.214 * rng   # = swing_high - 0.786*rng
    else:
        # Retracement depuis low vers high : zone 50-78.6%
        zone_low  = swing_high - 0.50  * rng
        zone_high = swing_high - 0.214 * rng  # = swing_low + 0.786*rng
    return (zone_low, zone_high)


def overlaps(a_lo, a_hi, b_lo, b_hi):
    """True si les segments [a] et [b] se chevauchent."""
    return not (a_hi < b_lo or b_hi < a_lo)


def backtest_v2(df, htf_bias):
    trades = []
    active_fvgs = []
    in_pos = False
    pos = None
    daily_trades = defaultdict(int)

    times = df["time"].values
    highs = df["high"].values
    lows  = df["low"].values
    closes= df["close"].values
    hours = pd.DatetimeIndex(df["time"]).hour.values
    days  = pd.DatetimeIndex(df["time"]).date

    for i in range(max(FIB_LOOKBACK, 3), len(df)):
        high_i, low_i, close_i = float(highs[i]), float(lows[i]), float(closes[i])
        h_utc = int(hours[i])
        day_key = days[i]

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
                outcome, exit_price = "SL", pos["sl"]
            elif hit_sl:
                outcome, exit_price = "SL", pos["sl"]
            elif hit_tp:
                outcome, exit_price = "TP", pos["tp"]
            if outcome:
                gross = (exit_price - pos["entry"]) if pos["dir"] == "LONG" else (pos["entry"] - exit_price)
                trades.append({
                    "entry_time": pos["entry_time"], "exit_time": times[i],
                    "dir": pos["dir"], "pnl_pts": gross - COST_PTS_PER_TRADE,
                    "outcome": outcome, "bars": i - pos["entry_idx"],
                })
                in_pos = False
                pos = None

        # ---- nouveau FVG ----
        c1 = {"high": float(highs[i-2]), "low": float(lows[i-2])}
        c3 = {"high": high_i, "low": low_i}
        fvg = detect_fvg(c1, c3)
        if fvg is not None:
            kind, top, bottom = fvg
            active_fvgs.append({"kind": kind, "top": top, "bottom": bottom, "created_idx": i})
        active_fvgs = [f for f in active_fvgs if i - f["created_idx"] < FVG_MAX_AGE]

        # ---- tentative d'entree avec TOUS les filtres ----
        if in_pos:
            continue
        if h_utc not in SESSION_HOURS_UTC:
            continue
        if daily_trades[day_key] >= MAX_TRADES_PER_DAY:
            continue

        bias = int(htf_bias[i])
        # Swing high/low recents (sur les FIB_LOOKBACK dernieres bougies)
        sl_idx0 = max(0, i - FIB_LOOKBACK)
        swing_high = float(np.max(highs[sl_idx0:i+1]))
        swing_low  = float(np.min(lows[sl_idx0:i+1]))
        if swing_high <= swing_low:
            continue

        for fvg in list(active_fvgs):
            # FILTRE 1 : direction biais HTF
            if fvg["kind"] == "bull" and bias != 1:
                continue
            if fvg["kind"] == "bear" and bias != -1:
                continue

            # FILTRE 2 : FVG dans zone Fibo 0.5-0.786
            is_long = (fvg["kind"] == "bull")
            fib_lo, fib_hi = fib_zone(swing_low, swing_high, is_long)
            if not overlaps(fvg["bottom"], fvg["top"], fib_lo, fib_hi):
                continue

            # FILTRE 3 : prix touche le FVG dans cette bougie
            if fvg["kind"] == "bull" and low_i > fvg["top"]:
                continue
            if fvg["kind"] == "bear" and high_i < fvg["bottom"]:
                continue

            # ---- ENTREE ----
            if fvg["kind"] == "bull":
                entry = fvg["top"]
                pos = {"dir": "LONG", "entry": entry,
                       "sl": entry - SL_PTS, "tp": entry + TP_PTS,
                       "entry_time": times[i], "entry_idx": i}
            else:
                entry = fvg["bottom"]
                pos = {"dir": "SHORT", "entry": entry,
                       "sl": entry + SL_PTS, "tp": entry - TP_PTS,
                       "entry_time": times[i], "entry_idx": i}
            in_pos = True
            active_fvgs.remove(fvg)
            daily_trades[day_key] += 1
            break

    return trades


def stats(trades):
    if not trades:
        return {"n": 0, "wr": 0, "total": 0, "expect": 0, "pf": 0, "dd": 0, "avg_bars": 0,
                "tp_n": 0, "sl_n": 0}
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
        "tp_n": (s["outcome"] == "TP").sum() if "outcome" in s.columns else 0,
        "sl_n": (s["outcome"] == "SL").sum() if "outcome" in s.columns else 0,
    }


def pf_str(pf):
    return "inf" if pf == float("inf") else f"{pf:.2f}"


if __name__ == "__main__":
    if not os.path.exists(CSV_IN):
        print(f"ERREUR : {CSV_IN} introuvable. Lance fetch_gold_5m.py d'abord.")
        exit(1)

    df = pd.read_csv(CSV_IN, parse_dates=["time"])
    df = df.drop_duplicates(subset="time").sort_values("time").reset_index(drop=True)
    days_total = max(1, (df["time"].iloc[-1] - df["time"].iloc[0]).days)

    print("=" * 92)
    print(f" BACKTEST FVG v2 (FVG + Fibo + bias 1h + session) -- {len(df)} bougies, {days_total} jours")
    print(f" Cible Palomatrd : 1-2 trades/jour, WR ~80%")
    print("=" * 92)
    print(f" Parametres :")
    print(f"   SL/TP            : {SL_PTS:.0f}pts / {TP_PTS:.0f}pts (R:R 1:{TP_PTS/SL_PTS:.2f})")
    print(f"   Fibo zone        : {FIB_MIN:.3f} - {FIB_MAX:.3f} (lookback {FIB_LOOKBACK} bougies)")
    print(f"   HTF bias         : MA{HTF_MA_PERIOD} 1h, pente {HTF_SLOPE_LB} barres")
    print(f"   Session          : {sorted(SESSION_HOURS_UTC)} UTC")
    print(f"   Max trades/jour  : {MAX_TRADES_PER_DAY}")
    print(f"   Couts            : {COST_PTS_PER_TRADE} pt/trade")

    print("\nCalcul du bias 1h...")
    htf_bias = compute_htf_bias(df)

    print("Lancement du backtest...")
    trades = backtest_v2(df, htf_bias)
    s = stats(trades)

    print("\n" + "=" * 92)
    print(" RESULTAT GLOBAL")
    print("=" * 92)
    print(f"  Trades        : {s['n']}  ({s['n']/days_total:.2f}/jour) -- cible Palomatrd : 1-2/jour")
    print(f"  Outcomes      : TP={s['tp_n']}, SL={s['sl_n']}")
    print(f"  Win rate      : {s['wr']:.1f}%  -- cible Palomatrd : ~80%")
    print(f"  PnL net total : {s['total']:+.1f} pts")
    print(f"  Esperance     : {s['expect']:+.3f} pts/trade")
    print(f"  Profit factor : {pf_str(s['pf'])}  -- cible : > 1.3")
    print(f"  Max drawdown  : {s['dd']:+.1f} pts")
    print(f"  Duree moyenne : {s['avg_bars']:.1f} bougies ({s['avg_bars']*5:.0f} min)")

    # Walk-forward
    if s["n"] >= 30:
        print("\n" + "=" * 92)
        print(f" WALK-FORWARD (3 blocs)")
        print("=" * 92)
        t0, t1 = df["time"].iloc[0], df["time"].iloc[-1]
        block_size = (t1 - t0) / 3
        pos_blocks = 0
        fmt2 = "{:>3} {:<24} {:>8} {:>7} {:>10} {:>8} {:>8}"
        print(fmt2.format("#", "Periode", "Trades", "WR%", "PnL pts", "PF", "MaxDD"))
        print("-" * 92)
        for i in range(3):
            bstart = t0 + i * block_size
            bend = t0 + (i + 1) * block_size if i < 2 else t1
            sub_idx = (df["time"] >= bstart) & (df["time"] < bend)
            sub_trades = [t for t in trades if bstart <= pd.Timestamp(t["entry_time"]) < bend]
            ss = stats(sub_trades)
            if ss["total"] > 0:
                pos_blocks += 1
            print(fmt2.format(i + 1, f"{bstart:%m-%d}->{bend:%m-%d}", ss["n"],
                             f"{ss['wr']:.1f}", f"{ss['total']:+.1f}",
                             pf_str(ss["pf"]), f"{ss['dd']:+.1f}"))
        print("-" * 92)
        print(f" Blocs positifs : {pos_blocks}/3")
    else:
        print(f"\nWalk-forward saute (seulement {s['n']} trades, pas assez pour 3 blocs).")

    # Verdict
    print("\n" + "=" * 92)
    print(" VERDICT")
    print("=" * 92)
    if s["n"] < 20:
        print(" >>> TROP PEU DE TRADES : les filtres sont peut-etre trop stricts.")
        print("     Ajuste les parametres (session plus large, FIB_LOOKBACK different).")
    elif s["wr"] >= 70 and s["pf"] >= 1.5:
        print(" >>> EDGE TROUVE ! La methode Palomatrd se reproduit algorithmiquement.")
        print("     WR et PF coherents avec ce que tu observes en live.")
    elif s["pf"] >= 1.1:
        print(" >>> EDGE MARGINAL : pas aussi bon que ses 80% WR. Soit elle ajoute des")
        print("     elements visuels qu'on n'a pas codes (structure, news), soit ses")
        print("     resultats live sont un peu enjolives (selection memoire).")
    else:
        print(" >>> PAS D'EDGE EVEN AVEC SES FILTRES : sa methode codee ne reproduit pas")
        print("     son resultat. Son edge est probablement dans sa selection visuelle")
        print("     du contexte, impossible a coder fidelement.")
