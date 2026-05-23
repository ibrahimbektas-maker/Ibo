"""
BACKTEST v3 -- Grille (seuil spike + trailing stop)
====================================================
Teste 4 seuils de spike x 4 modes de trailing = 16 configurations
sur la base de la strategie v2 (momentum, fenetre 13-15 UTC).

Reutilise yahoo_cache.csv.

Usage : python backtest_v3.py
"""

import pandas as pd
import os

# ─── Parametres FIXES (heritage v2) ───────────────
SPIKE_WINDOW   = 5
CALM_WINDOW    = 15
CALM_MAX_RANGE = 20.0
RANGE_WINDOW   = 120
LOT_SIZE       = 1.0
SL_POINTS      = 10.0
TP_POINTS      = 10.0
COOLDOWN_BARS  = 5

TRADING_HOUR_START_UTC = 13
TRADING_HOUR_END_UTC   = 15

MAX_DAILY_TRADES       = 8
MAX_DAILY_LOSS         = 30.0
MAX_WEEKLY_LOSS        = 80.0
MAX_CONSECUTIVE_LOSSES = 4

SPREAD_POINTS       = 0.4
SLIPPAGE_PTS        = 0.2
GUARANTEED_STOP_FEE = 0.5

# ─── Grille a tester ──────────────────────────────
SPIKE_THRESHOLDS = [0.3, 0.4, 0.5, 0.6]   # En %

# Trailing : None = pas de trailing
# Sinon : (trigger_pts, distance_pts) = trailing s'active a +trigger pts,
#         et garde une distance de distance_pts derriere le prix.
TRAILING_MODES = [
    ("Pas de trailing", None),
    ("Trail +5pts",    (5,  5)),    # active a +5 pts, distance 5 pts
    ("Trail +10pts",   (10, 5)),    # active a +10 pts, distance 5 pts
    ("Trail +15pts",   (15, 5)),    # active a +15 pts, distance 5 pts
]

CACHE_FILE = "yahoo_cache.csv"

# ─── Logique strategie ────────────────────────────
def detect_spike(window, threshold):
    price_now    = window["close"].iloc[-1]
    price_before = window["close"].iloc[-(SPIKE_WINDOW + 1)]
    pct = (price_now - price_before) / price_before * 100
    if pct >= threshold:
        return "BUY", pct
    if pct <= -threshold:
        return "SELL", abs(pct)
    return None, 0.0

def is_market_calm(window):
    calm = window.iloc[-(CALM_WINDOW + SPIKE_WINDOW + 1):-(SPIKE_WINDOW + 1)]
    range_pts = calm["high"].max() - calm["low"].min()
    return range_pts < CALM_MAX_RANGE

def backtest(df, spike_threshold, trailing_config):
    """
    trailing_config : None ou (trigger_pts, distance_pts)
    """
    min_history = RANGE_WINDOW + SPIKE_WINDOW + 5
    trades = []
    open_trade = None
    cooldown_until = -1

    current_day = None
    current_week = None
    daily_trades_count = 0
    daily_pnl = 0.0
    weekly_pnl = 0.0
    consecutive_losses = 0
    paused_by_safety = False

    for i in range(min_history, len(df)):
        bar = df.iloc[i]
        bar_time = bar["time"]
        bar_day  = bar_time.date()
        bar_week = bar_time.strftime("%Y-W%U")
        bar_hour = bar_time.hour

        if bar_day != current_day:
            current_day = bar_day
            daily_trades_count = 0
            daily_pnl = 0.0
            if bar_time.weekday() == 0:
                paused_by_safety = False
                consecutive_losses = 0

        if bar_week != current_week:
            current_week = bar_week
            weekly_pnl = 0.0

        # ─── Gestion position ouverte ───
        if open_trade is not None:
            high = bar["high"]
            low  = bar["low"]
            direction = open_trade["direction"]
            entry = open_trade["entry"]

            # Trailing : mise a jour du SL si applicable
            if trailing_config is not None:
                trigger_pts, distance_pts = trailing_config
                if direction == "LONG":
                    # Profit max atteint pendant la bougie
                    profit = high - entry
                    if profit >= trigger_pts:
                        # Le trailing s'active : on remonte le SL
                        # si possible
                        new_sl = high - distance_pts
                        if new_sl > open_trade["sl"]:
                            open_trade["sl"] = new_sl
                else:  # SHORT
                    profit = entry - low
                    if profit >= trigger_pts:
                        new_sl = low + distance_pts
                        if new_sl < open_trade["sl"]:
                            open_trade["sl"] = new_sl

            # Verifier hit SL / TP avec le SL eventuellement mis a jour
            sl = open_trade["sl"]
            tp = open_trade["tp"]

            # Si trailing actif et on a deja atteint le TP, on n'utilise plus le TP
            # (on laisse courir avec le trailing seulement)
            use_tp = (trailing_config is None) or (not open_trade.get("trailing_started", False))

            hit_sl = (low <= sl) if direction == "LONG" else (high >= sl)
            hit_tp = use_tp and ((high >= tp) if direction == "LONG" else (low <= tp))

            # Marquer le trailing comme actif si le profit atteint le trigger
            if trailing_config is not None and not open_trade.get("trailing_started", False):
                trigger_pts, _ = trailing_config
                if direction == "LONG" and (high - entry) >= trigger_pts:
                    open_trade["trailing_started"] = True
                elif direction == "SHORT" and (entry - low) >= trigger_pts:
                    open_trade["trailing_started"] = True

            exit_price = None
            reason = None
            if hit_sl and hit_tp:
                exit_price = sl
                reason = "SL (conflit)"
            elif hit_sl:
                exit_price = sl
                reason = "TRAIL" if open_trade.get("trailing_started", False) else "SL"
            elif hit_tp:
                exit_price = tp
                reason = "TP"

            if exit_price is not None:
                if direction == "LONG":
                    exit_price -= SLIPPAGE_PTS
                    pnl_pts = exit_price - open_trade["entry"]
                else:
                    exit_price += SLIPPAGE_PTS
                    pnl_pts = open_trade["entry"] - exit_price

                pnl_pts -= GUARANTEED_STOP_FEE
                pnl_eur = pnl_pts * LOT_SIZE

                trades.append({
                    "pnl_eur": pnl_eur,
                    "reason":  reason,
                })

                daily_pnl += pnl_eur
                weekly_pnl += pnl_eur
                if pnl_eur > 0:
                    consecutive_losses = 0
                else:
                    consecutive_losses += 1

                if daily_pnl <= -MAX_DAILY_LOSS:
                    paused_by_safety = True
                if weekly_pnl <= -MAX_WEEKLY_LOSS:
                    paused_by_safety = True
                if consecutive_losses >= MAX_CONSECUTIVE_LOSSES:
                    paused_by_safety = True

                open_trade = None
                cooldown_until = i + COOLDOWN_BARS
            continue

        # ─── Filtres entree ───
        if paused_by_safety:
            continue
        if i < cooldown_until:
            continue
        if not (TRADING_HOUR_START_UTC <= bar_hour < TRADING_HOUR_END_UTC):
            continue
        if daily_trades_count >= MAX_DAILY_TRADES:
            continue

        window = df.iloc[i - min_history + 1: i + 1]
        signal, pct = detect_spike(window, spike_threshold)
        if signal is None:
            continue
        if not is_market_calm(window):
            continue

        raw_entry = bar["close"]
        if signal == "BUY":
            entry = raw_entry + SPREAD_POINTS / 2 + SLIPPAGE_PTS
            sl = entry - SL_POINTS
            tp = entry + TP_POINTS
            direction = "LONG"
        else:
            entry = raw_entry - SPREAD_POINTS / 2 - SLIPPAGE_PTS
            sl = entry + SL_POINTS
            tp = entry - TP_POINTS
            direction = "SHORT"

        open_trade = {
            "entry":     entry,
            "sl":        sl,
            "tp":        tp,
            "direction": direction,
            "trailing_started": False,
        }
        daily_trades_count += 1

    return trades

def stats(trades):
    if not trades:
        return {"n": 0, "winrate": 0, "pnl": 0, "avg": 0, "dd": 0,
                "max_win": 0, "max_loss": 0}
    df = pd.DataFrame(trades)
    n = len(df)
    wins = (df["pnl_eur"] > 0).sum()
    cum = df["pnl_eur"].cumsum()
    peak = cum.cummax()
    return {
        "n": n,
        "winrate": wins / n * 100,
        "pnl": df["pnl_eur"].sum(),
        "avg": df["pnl_eur"].mean(),
        "dd": (cum - peak).min(),
        "max_win": df["pnl_eur"].max(),
        "max_loss": df["pnl_eur"].min(),
    }

# ─── MAIN ──────────────────────────────────────────
if __name__ == "__main__":
    if not os.path.exists(CACHE_FILE):
        print(f"ERREUR : {CACHE_FILE} introuvable.")
        exit(1)

    df = pd.read_csv(CACHE_FILE, parse_dates=["time"])

    print("=" * 100)
    print("    BACKTEST v3 -- Grille seuil de spike x trailing stop")
    print("=" * 100)
    print(f"Donnees : {len(df)} bougies sur "
          f"{(df['time'].iloc[-1] - df['time'].iloc[0]).days} jours")
    print(f"Base    : strategie v2 (momentum, 13-15 UTC, SL/TP=10/10, securite ON)")
    print("=" * 100)

    print(f"\n{'Spike%':>7} | {'Trailing':<18} | {'Trades':>6} | {'Winrate':>7} | "
          f"{'PnL':>10} | {'PnL/trade':>10} | {'Drawdown':>10} | "
          f"{'Best':>7} | {'Worst':>7}")
    print("-" * 100)

    all_results = []
    for spike_th in SPIKE_THRESHOLDS:
        for tr_label, tr_cfg in TRAILING_MODES:
            trades = backtest(df, spike_th, tr_cfg)
            s = stats(trades)
            marker = " <--" if s["pnl"] > 50 else (" *" if s["pnl"] > 0 else "")
            print(f"{spike_th:>6.1f}% | {tr_label:<18} | {s['n']:>6} | "
                  f"{s['winrate']:>6.1f}% | {s['pnl']:>+9.2f} EUR | "
                  f"{s['avg']:>+9.2f} EUR | {s['dd']:>+9.2f} EUR | "
                  f"{s['max_win']:>+6.2f} | {s['max_loss']:>+6.2f}{marker}")
            all_results.append((spike_th, tr_label, s))
        print("-" * 100)

    # ─── Synthese ──
    print("\n" + "=" * 100)
    print("                                 SYNTHESE")
    print("=" * 100)

    profitable = [r for r in all_results if r[2]["pnl"] > 0 and r[2]["n"] >= 8]
    profitable.sort(key=lambda r: r[2]["pnl"], reverse=True)

    print(f"Configs testees     : {len(all_results)}")
    print(f"Configs profitables : {sum(1 for r in all_results if r[2]['pnl'] > 0)}")
    print(f"Configs viables (>= 8 trades & PnL > 0) : {len(profitable)}")

    if profitable:
        print(f"\nTop 5 configs viables :")
        print("-" * 90)
        print(f"{'Spike':>6} | {'Trailing':<18} | {'Trades':>6} | "
              f"{'Winrate':>7} | {'PnL':>10} | {'Avg':>9} | {'DD':>9}")
        print("-" * 90)
        for spike_th, tr_label, s in profitable[:5]:
            print(f"{spike_th:>5.1f}% | {tr_label:<18} | {s['n']:>6} | "
                  f"{s['winrate']:>6.1f}% | {s['pnl']:>+9.2f} EUR | "
                  f"{s['avg']:>+8.2f} | {s['dd']:>+8.2f}")

        best = profitable[0]
        days = (df["time"].iloc[-1] - df["time"].iloc[0]).days
        print(f"\n>>> MEILLEURE CONFIG <<<")
        print(f"  Seuil spike   : {best[0]:.1f}%")
        print(f"  Trailing      : {best[1]}")
        print(f"  -> {best[2]['n']} trades sur {days} jours = "
              f"{best[2]['n']/days:.2f} trades/jour")
        print(f"  -> Winrate {best[2]['winrate']:.1f}%")
        print(f"  -> PnL {best[2]['pnl']:+.2f} EUR sur {days}j = "
              f"{best[2]['pnl']/days*30:+.2f} EUR/mois")
        print(f"  -> Drawdown max {best[2]['dd']:+.2f} EUR")

    print("\n" + "=" * 100)
    print("                            INTERPRETATION")
    print("=" * 100)
    print("""
COMMENT LIRE LE TABLEAU :
- 'Best' = meilleur trade individuel (montre l'effet du trailing)
- 'Worst' = pire trade individuel (typiquement -10.7 EUR avec SL+frais)
- Plus le seuil spike est haut, moins on a de trades (mais plus le signal
  devrait etre fort)
- Le trailing AMELIORE 'Best' mais peut DEGRADER le winrate (les trades
  qui auraient touche TP a +10 sortent maintenant plus bas)

SIGNAUX A SUIVRE :
- Si le 'Best' double avec trailing, il y a un effet momentum a capter
- Si le PnL augmente uniquement au seuil 0.5-0.6%, ca suggere que les
  petits spikes (0.3-0.4%) sont du bruit et les gros sont du vrai signal

ATTENTION OVERFITTING :
On a teste 16 configs. Sur des trades aleatoires, on peut s'attendre a
2-3 configs profitables PAR HASARD. Pour que le resultat soit credible,
il faut voir un PATTERN coherent : par exemple, plusieurs lignes voisines
profitables, une regle qui se confirme (genre "tous les seuils >=0.5%").
""")
