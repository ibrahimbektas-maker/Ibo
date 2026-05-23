"""
BACKTEST MEAN-REVERSION v3.2 -- Variantes "trades courts"
=========================================================
Compare 6 versions, toutes avec MAX_DAILY_TRADES = 40 :
  CURRENT    : strategie actuelle (SL -8 / TP +14, pas de time exit)
  OPTION B   : TP reduit a +7 pts
  OPTION D   : TP +7 + close apres 5 min (TOUJOURS, peu importe PnL)
  OPTION E   : TP +7 + close apres 5 min UNIQUEMENT si en gain
  OPTION F   : TP +5 (tres serre) + close apres 5 min
  OPTION G   : TP +7 + close apres 3 min (encore plus court)
"""

import os
import requests
import pandas as pd
from datetime import datetime, timedelta
from collections import defaultdict

# ============================================================
# CHARGEMENT .env
# ============================================================
def load_env(path=".env"):
    env = {}
    if not os.path.exists(path):
        print(f"ERREUR : fichier {path} introuvable.")
        exit(1)
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, _, value = line.partition("=")
                env[key.strip()] = value.strip()
    return env

env = load_env()
API_KEY    = env.get("CAPITAL_API_KEY", "")
API_SECRET = env.get("CAPITAL_API_SECRET", "")
ACCOUNT_ID = env.get("CAPITAL_ACCOUNT_ID", "")
EPIC       = env.get("CAPITAL_EPIC", "GOLD")
BASE_URL   = env.get("CAPITAL_BASE_URL", "https://api-capital.backend-capital.com/api/v1")

# ============================================================
# PARAMETRES STRATEGIE
# ============================================================
MA_PERIOD       = 90
DEVIATION_PTS   = 5.0
SL_POINTS       = 8.0
COOLDOWN_BARS   = 5   # reduit pour permettre plus de trades
TRADING_HOUR_START_UTC = 7
TRADING_HOUR_END_UTC   = 15
NEWS_BLOCK_TIMES_UTC = [(8, 0), (9, 0), (12, 30)]
NEWS_BLOCK_MINUTES   = 5

# NOUVEAU : plafond journalier
MAX_DAILY_TRADES = 40

# ============================================================
# COUTS DE TRANSACTION (manquaient dans la version d'origine)
# ============================================================
# Sans ces couts, le PnL est surestime -- surtout pour les variantes
# a TP court et a forte frequence (D/E/F/G/H), ou le drag est maximal.
SPREAD_POINTS       = 0.4   # spread aller-retour du GOLD (a ajuster selon ton broker)
SLIPPAGE_PTS        = 0.2   # slippage sur ordre au marche (entree + sorties marche)
GUARANTEED_STOP_FEE = 0.5   # prime stop garanti, prelevee si le filet broker se declenche


# ============================================================
# CONNEXION + DONNEES
# ============================================================
def connect():
    headers = {"X-CAP-API-KEY": API_KEY, "Content-Type": "application/json"}
    r = requests.post(BASE_URL + "/session", headers=headers,
                      json={"identifier": ACCOUNT_ID, "password": API_SECRET}, timeout=15)
    if r.status_code != 200:
        print(f"ERREUR connexion: {r.text}")
        exit(1)
    headers["X-SECURITY-TOKEN"] = r.headers.get("X-SECURITY-TOKEN", "")
    headers["CST"] = r.headers.get("CST", "")
    print("Connexion Capital.com OK")
    return headers


def fetch_history(headers, days=14):
    end = datetime.utcnow()
    start = end - timedelta(days=days)
    all_prices = []
    cursor = start

    print(f"Telechargement {days} jours de bougies 1min...")
    while cursor < end:
        chunk_end = min(cursor + timedelta(hours=16), end)
        params = {
            "resolution": "MINUTE",
            "from": cursor.strftime("%Y-%m-%dT%H:%M:%S"),
            "to":   chunk_end.strftime("%Y-%m-%dT%H:%M:%S"),
            "max":  1000,
        }
        r = requests.get(BASE_URL + "/prices/" + EPIC, headers=headers, params=params, timeout=20)
        if r.status_code != 200:
            cursor = chunk_end
            continue
        prices = r.json().get("prices", [])
        all_prices.extend(prices)
        print(f"  {cursor.strftime('%Y-%m-%d %H:%M')} -> {len(prices)} bougies")
        cursor = chunk_end

    df = pd.DataFrame(all_prices)
    if df.empty:
        print("Aucune donnee recuperee.")
        exit(1)

    df["close"] = pd.to_numeric(df["closePrice"].apply(lambda x: x["bid"]))
    df["high"]  = pd.to_numeric(df["highPrice"].apply(lambda x:  x["bid"]))
    df["low"]   = pd.to_numeric(df["lowPrice"].apply(lambda x:   x["bid"]))
    df["open"]  = pd.to_numeric(df["openPrice"].apply(lambda x:  x["bid"]))
    df["snapshotTime"] = pd.to_datetime(df["snapshotTime"])
    df = df.drop_duplicates(subset="snapshotTime").sort_values("snapshotTime").reset_index(drop=True)
    print(f"Total : {len(df)} bougies sur {df['snapshotTime'].min()} -> {df['snapshotTime'].max()}")
    return df


# ============================================================
# UTILITAIRES
# ============================================================
def is_news_window(dt):
    minutes = dt.hour * 60 + dt.minute
    for h, m in NEWS_BLOCK_TIMES_UTC:
        if abs(minutes - (h * 60 + m)) <= NEWS_BLOCK_MINUTES:
            return True
    return False


def is_in_trading_window(dt):
    return TRADING_HOUR_START_UTC <= dt.hour < TRADING_HOUR_END_UTC


# ============================================================
# BACKTEST GENERIQUE avec MAX_DAILY_TRADES + time exit
# ============================================================
def backtest(df, name, tp_pts=14.0, time_exit_min=None, time_exit_profit_only=False):
    """
    tp_pts : take profit en points
    time_exit_min : duree max d'un trade (None = pas de time exit)
    time_exit_profit_only : True = ferme apres time_exit_min UNIQUEMENT si gain > 0
                            False = ferme apres time_exit_min PEU IMPORTE le PnL
    """
    trades = []
    in_position = False
    pos_direction = None
    entry_price = None
    entry_time = None
    sl_internal = None
    sl_filet = None
    take_profit = None
    last_close_time = None
    daily_trades = defaultdict(int)
    lot = 0.01

    for i in range(MA_PERIOD, len(df)):
        row = df.iloc[i]
        t = row["snapshotTime"]
        price = row["close"]
        high  = row["high"]
        low   = row["low"]

        # =========================
        # GESTION POSITION OUVERTE
        # =========================
        if in_position:
            exit_price = None
            reason = None
            elapsed_min = (t - entry_time).total_seconds() / 60

            # Filet broker (stop garanti) = intrabar sur low/high.
            # TP (ordre limite broker) = intrabar sur high/low.
            # SL interne = fermeture MANUELLE du bot sur un poll ~30s -> base sur
            #   le prix courant (close), pas sur le wick intrabar. Cela evite le
            #   biais "la bougie touche +TP et -8 dans la meme minute -> compte un TP"
            #   et colle au comportement reel du bot.
            if pos_direction == "LONG":
                if low <= sl_filet:
                    exit_price, reason = sl_filet, "SL_FILET"
                elif high >= take_profit:
                    exit_price, reason = take_profit, "TP"
                elif price <= sl_internal:
                    exit_price, reason = sl_internal, "SL_INTERNE"
            else:
                if high >= sl_filet:
                    exit_price, reason = sl_filet, "SL_FILET"
                elif low <= take_profit:
                    exit_price, reason = take_profit, "TP"
                elif price >= sl_internal:
                    exit_price, reason = sl_internal, "SL_INTERNE"

            # Time exit
            if exit_price is None and time_exit_min is not None and elapsed_min >= time_exit_min:
                gain = (price - entry_price) if pos_direction == "LONG" else (entry_price - price)
                if time_exit_profit_only:
                    if gain > 0:
                        exit_price, reason = price, "TIME_EXIT_WIN"
                else:
                    exit_price, reason = price, "TIME_EXIT"

            if exit_price is not None:
                gross_pts = (exit_price - entry_price) if pos_direction == "LONG" else (entry_price - exit_price)
                # Couts : spread aller-retour + slippage d'entree (ordre marche).
                cost_pts = SPREAD_POINTS + SLIPPAGE_PTS
                if reason in ("SL_INTERNE", "TIME_EXIT", "TIME_EXIT_WIN"):
                    cost_pts += SLIPPAGE_PTS          # sortie au marche -> slippage en plus
                elif reason == "SL_FILET":
                    cost_pts += GUARANTEED_STOP_FEE   # prime stop garanti au declenchement
                # (TP = ordre limite : pas de slippage de sortie)
                pnl_pts = gross_pts - cost_pts
                trades.append({
                    "entry_time":  entry_time,
                    "exit_time":   t,
                    "direction":   pos_direction,
                    "entry":       entry_price,
                    "exit":        exit_price,
                    "pnl_pts":     pnl_pts,
                    "pnl_eur":     pnl_pts * lot,
                    "duration_min": elapsed_min,
                    "reason":      reason,
                })
                in_position = False
                last_close_time = t
                continue

        # =========================
        # CHERCHE NOUVEAUX SIGNAUX
        # =========================
        if in_position:
            continue
        if not is_in_trading_window(t):
            continue
        if is_news_window(t):
            continue
        if last_close_time and (t - last_close_time).total_seconds() < COOLDOWN_BARS * 60:
            continue

        # Plafond journalier
        day_key = t.strftime("%Y-%m-%d")
        if daily_trades[day_key] >= MAX_DAILY_TRADES:
            continue

        ma = df["close"].iloc[i-MA_PERIOD:i].mean()
        deviation = price - ma

        if deviation > DEVIATION_PTS:
            in_position = True
            pos_direction = "SHORT"
            entry_price = price
            entry_time = t
            sl_internal = price + SL_POINTS
            sl_filet    = price + 50.0
            take_profit = price - tp_pts
            daily_trades[day_key] += 1
        elif deviation < -DEVIATION_PTS:
            in_position = True
            pos_direction = "LONG"
            entry_price = price
            entry_time = t
            sl_internal = price - SL_POINTS
            sl_filet    = price - 50.0
            take_profit = price + tp_pts
            daily_trades[day_key] += 1

    return trades


def stats(trades, name):
    if not trades:
        return None
    n = len(trades)
    wins = sum(1 for t in trades if t["pnl_pts"] > 0)
    losses = sum(1 for t in trades if t["pnl_pts"] < 0)
    breakeven = sum(1 for t in trades if t["pnl_pts"] == 0)
    total_pts = sum(t["pnl_pts"] for t in trades)
    total_eur = sum(t["pnl_eur"] for t in trades)
    reasons = defaultdict(int)
    for t in trades:
        reasons[t["reason"]] += 1
    return {
        "name": name, "n": n, "wins": wins, "losses": losses, "breakeven": breakeven,
        "winrate": wins / n * 100 if n else 0,
        "total_pts": total_pts, "total_eur": total_eur,
        "avg_pts": total_pts / n if n else 0,
        "best_pts": max(t["pnl_pts"] for t in trades),
        "worst_pts": min(t["pnl_pts"] for t in trades),
        "avg_duration": sum(t["duration_min"] for t in trades) / n if n else 0,
        "reasons": dict(reasons),
    }


def print_comparison(results):
    print("\n" + "=" * 105)
    print(f" COMPARAISON DES STRATEGIES  (MAX_DAILY_TRADES = {MAX_DAILY_TRADES}, COOLDOWN = {COOLDOWN_BARS}min)")
    print("=" * 105)
    headers = ["Strategie", "Trades", "Wins", "Loss", "WR%", "PnL pts", "PnL EUR", "AvgDur", "Best", "Worst"]
    fmt = "{:<28} {:>7} {:>5} {:>5} {:>6} {:>9} {:>9} {:>8} {:>7} {:>7}"
    print(fmt.format(*headers))
    print("-" * 105)
    for r in results:
        if r is None:
            continue
        print(fmt.format(
            r["name"], r["n"], r["wins"], r["losses"],
            f"{r['winrate']:.1f}",
            f"{r['total_pts']:+.1f}",
            f"{r['total_eur']:+.2f}",
            f"{r['avg_duration']:.1f}min",
            f"{r['best_pts']:+.1f}",
            f"{r['worst_pts']:+.1f}",
        ))
    print("\nMotifs de sortie :")
    for r in results:
        if r is None:
            continue
        print(f"  {r['name']:<28} -> {r['reasons']}")


# ============================================================
# MAIN
# ============================================================
if __name__ == "__main__":
    print("=" * 70)
    print(" BACKTEST -- Variantes trades courts (plafond 40/jour)")
    print("=" * 70)

    headers = connect()
    df = fetch_history(headers, days=14)

    print("\nLancement des backtests...")
    scenarios = [
        ("CURRENT (SL8/TP14)",           {"tp_pts": 14.0, "time_exit_min": None}),
        ("B (TP7, pas de time exit)",    {"tp_pts": 7.0,  "time_exit_min": None}),
        ("D (TP7 + close 5min ALWAYS)",  {"tp_pts": 7.0,  "time_exit_min": 5}),
        ("E (TP7 + close 5min IF WIN)",  {"tp_pts": 7.0,  "time_exit_min": 5, "time_exit_profit_only": True}),
        ("F (TP5 + close 5min ALWAYS)",  {"tp_pts": 5.0,  "time_exit_min": 5}),
        ("G (TP7 + close 3min ALWAYS)",  {"tp_pts": 7.0,  "time_exit_min": 3}),
        ("H (TP5 + close 3min ALWAYS)",  {"tp_pts": 5.0,  "time_exit_min": 3}),
    ]
    results = []
    for name, kw in scenarios:
        trades = backtest(df, name, **kw)
        results.append(stats(trades, name))
        if trades:
            tag = name.split()[0].lower()
            pd.DataFrame(trades).to_csv(f"bt_{tag}.csv", index=False)

    print_comparison(results)
    print("\nFichiers CSV detailles : bt_current.csv, bt_b.csv, bt_d.csv, etc.")
    print("Backtest termine.")
