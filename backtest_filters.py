"""
BACKTEST FILTRES D'ENTREE -- ameliorer le signal mean-reversion
===============================================================
Probleme identifie : l'ecart fixe de 5 pts par rapport a la MA90 fade
souvent des TENDANCES (40-60% de WR seulement) au lieu de vrais exces.
Ce backtest teste des filtres qui ne gardent que les exces "fadables".

Base commune : strategie B (SL -8 / TP +7, pas de time exit), config LIVE
(10 trades/jour, cooldown 10min), couts inclus, SL interne base close.

Filtres testes :
  BASE         : signal brut (ecart > 5 pts)                  -> reference
  F1 Z-SCORE   : ecart normalise par la volatilite (z >= 2)   -> 5pts veut dire
                 quelque chose de different en marche calme vs agitee
  F2 +RSI      : + RSI(14) en zone d'epuisement (<30 / >70)   -> ne fade que
                 les mouvements "essouffles"
  F3 +MA PLATE : + MA90 ~horizontale (marche en range)        -> ne fade pas
                 contre une tendance
  F4 +RSI+PLATE: combinaison des deux                          -> le plus selectif

ATTENTION OVERFITTING : on teste plusieurs filtres. Un filtre n'est credible
que si (a) il ameliore NETTEMENT le profit factor, (b) il REDUIT le drawdown,
(c) il garde assez de trades (>30-40), et (d) l'effet est coherent. Un seul
chiffre flatteur sur peu de trades = probablement du hasard.

Usage : python backtest_filters.py
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
# PARAMETRES (alignes bot live, base = strategie B)
# ============================================================
MA_PERIOD       = 90
DEVIATION_PTS   = 5.0
SL_POINTS       = 8.0
TP_POINTS       = 7.0     # base B
TRADING_HOUR_START_UTC = 7
TRADING_HOUR_END_UTC   = 15
NEWS_BLOCK_TIMES_UTC = [(8, 0), (9, 0), (12, 30)]
NEWS_BLOCK_MINUTES   = 5

MAX_DAILY_TRADES = 10
COOLDOWN_BARS    = 10
BACKTEST_DAYS    = 30

# --- Parametres des filtres (a tuner) ---
Z_THRESH       = 2.0    # F1 : nb d'ecarts-types mini pour entrer
RSI_PERIOD     = 14
RSI_OVERSOLD   = 30     # F2 : long si RSI <= 30
RSI_OVERBOUGHT = 70     # F2 : short si RSI >= 70
SLOPE_LOOKBACK = 30     # F3 : fenetre (barres) pour mesurer la pente de la MA
SLOPE_MAX      = 3.0    # F3 : MA "plate" si |variation MA sur la fenetre| <= 3 pts

# ============================================================
# COUTS
# ============================================================
SPREAD_POINTS       = 0.4
SLIPPAGE_PTS        = 0.2
GUARANTEED_STOP_FEE = 0.5
LOT_SIZE            = 0.01


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


def fetch_history(headers, days):
    end = datetime.utcnow()
    start = end - timedelta(days=days)
    all_prices = []
    cursor = start
    print(f"Telechargement {days} jours de bougies 1min...")
    while cursor < end:
        chunk_end = min(cursor + timedelta(hours=16), end)
        params = {"resolution": "MINUTE",
                  "from": cursor.strftime("%Y-%m-%dT%H:%M:%S"),
                  "to":   chunk_end.strftime("%Y-%m-%dT%H:%M:%S"),
                  "max":  1000}
        r = requests.get(BASE_URL + "/prices/" + EPIC, headers=headers, params=params, timeout=20)
        if r.status_code != 200:
            cursor = chunk_end
            continue
        all_prices.extend(r.json().get("prices", []))
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


def add_indicators(df):
    """Precalcule MA, ecart-type, RSI et pente de MA (sans look-ahead)."""
    # MA et std sur les MA_PERIOD barres PRECEDENTES (shift(1) = on exclut la barre i)
    df["ma"]  = df["close"].rolling(MA_PERIOD).mean().shift(1)
    df["std"] = df["close"].rolling(MA_PERIOD).std().shift(1)
    # RSI(14) sur les closes <= i
    delta = df["close"].diff()
    gain = delta.clip(lower=0).rolling(RSI_PERIOD).mean()
    loss = (-delta.clip(upper=0)).rolling(RSI_PERIOD).mean()
    rs = gain / loss
    df["rsi"] = 100 - 100 / (1 + rs)
    # Pente de la MA sur SLOPE_LOOKBACK barres
    df["slope"] = df["ma"] - df["ma"].shift(SLOPE_LOOKBACK)
    return df


# ============================================================
# UTILITAIRES
# ============================================================
def is_news_window(dt):
    minutes = dt.hour * 60 + dt.minute
    return any(abs(minutes - (h * 60 + m)) <= NEWS_BLOCK_MINUTES for h, m in NEWS_BLOCK_TIMES_UTC)

def is_in_trading_window(dt):
    return TRADING_HOUR_START_UTC <= dt.hour < TRADING_HOUR_END_UTC


# ============================================================
# BACKTEST avec filtres optionnels
# ============================================================
def backtest(df, name, use_zscore=False, use_rsi=False, use_flat_ma=False):
    trades = []
    in_position = False
    pos_direction = entry_price = entry_time = None
    sl_internal = sl_filet = take_profit = None
    last_close_time = None
    daily_trades = defaultdict(int)

    start_i = MA_PERIOD + SLOPE_LOOKBACK + 2
    for i in range(start_i, len(df)):
        row = df.iloc[i]
        t = row["snapshotTime"]
        price = row["close"]
        high  = row["high"]
        low   = row["low"]

        # --- gestion position ouverte ---
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
                pnl = gross - cost
                trades.append({"entry_time": entry_time, "exit_time": t,
                               "pnl_pts": pnl, "pnl_eur": pnl * LOT_SIZE,
                               "duration_min": (t - entry_time).total_seconds() / 60,
                               "reason": reason})
                in_position = False
                last_close_time = t
                continue

        if in_position:                      continue
        if not is_in_trading_window(t):      continue
        if is_news_window(t):                continue
        if last_close_time and (t - last_close_time).total_seconds() < COOLDOWN_BARS * 60:
            continue
        day_key = t.strftime("%Y-%m-%d")
        if daily_trades[day_key] >= MAX_DAILY_TRADES:
            continue

        ma, std, rsi, slope = row["ma"], row["std"], row["rsi"], row["slope"]
        if pd.isna(ma) or pd.isna(std) or pd.isna(rsi) or pd.isna(slope):
            continue

        deviation = price - ma

        # --- signal d'entree (base ou z-score) ---
        if use_zscore:
            if std <= 0:
                continue
            z = deviation / std
            long_sig  = z <= -Z_THRESH
            short_sig = z >=  Z_THRESH
        else:
            long_sig  = deviation < -DEVIATION_PTS
            short_sig = deviation >  DEVIATION_PTS

        # --- filtres additionnels ---
        if use_rsi:
            long_sig  = long_sig  and (rsi <= RSI_OVERSOLD)
            short_sig = short_sig and (rsi >= RSI_OVERBOUGHT)
        if use_flat_ma:
            flat = abs(slope) <= SLOPE_MAX
            long_sig  = long_sig  and flat
            short_sig = short_sig and flat

        if short_sig:
            in_position, pos_direction, entry_price, entry_time = True, "SHORT", price, t
            sl_internal, sl_filet, take_profit = price + SL_POINTS, price + 50.0, price - TP_POINTS
            daily_trades[day_key] += 1
        elif long_sig:
            in_position, pos_direction, entry_price, entry_time = True, "LONG", price, t
            sl_internal, sl_filet, take_profit = price - SL_POINTS, price - 50.0, price + TP_POINTS
            daily_trades[day_key] += 1

    return trades


def stats(trades, name):
    if not trades:
        return {"name": name, "n": 0, "winrate": 0, "total_pts": 0, "expectancy": 0,
                "profit_factor": 0, "max_dd": 0, "best_pts": 0, "worst_pts": 0,
                "avg_duration": 0}
    df = pd.DataFrame(trades)
    n = len(df)
    wins = df[df["pnl_pts"] > 0]
    losses = df[df["pnl_pts"] < 0]
    gross_win = wins["pnl_pts"].sum()
    gross_loss = losses["pnl_pts"].sum()
    cum = df["pnl_pts"].cumsum()
    return {
        "name": name, "n": n, "winrate": len(wins) / n * 100,
        "total_pts": df["pnl_pts"].sum(), "expectancy": df["pnl_pts"].sum() / n,
        "profit_factor": (gross_win / abs(gross_loss)) if gross_loss != 0 else float("inf"),
        "max_dd": (cum - cum.cummax()).min(),
        "best_pts": df["pnl_pts"].max(), "worst_pts": df["pnl_pts"].min(),
        "avg_duration": df["duration_min"].mean(),
    }


def print_comparison(results, days):
    print("\n" + "=" * 96)
    print(f" FILTRES D'ENTREE  --  base B (SL8/TP7), config live, {days} jours")
    print("=" * 96)
    fmt = "{:<22} {:>7} {:>8} {:>7} {:>9} {:>8} {:>9} {:>8}"
    print(fmt.format("Filtre", "Trades", "/jour", "WR%", "PnL pts", "Expect", "PF", "MaxDD"))
    print("-" * 96)
    for r in results:
        pf = "inf" if r["profit_factor"] == float("inf") else f"{r['profit_factor']:.2f}"
        print(fmt.format(
            r["name"], r["n"], f"{r['n']/days:.2f}", f"{r['winrate']:.1f}",
            f"{r['total_pts']:+.1f}", f"{r['expectancy']:+.3f}", pf, f"{r['max_dd']:+.1f}"))
    print("-" * 96)
    print("Lecture : un filtre est credible si PF monte NETTEMENT (>1.3), MaxDD se")
    print("reduit, ET il reste >30-40 trades. Sinon = probablement du hasard.")


# ============================================================
# MAIN
# ============================================================
if __name__ == "__main__":
    print("=" * 70)
    print(" BACKTEST FILTRES D'ENTREE -- base B, config live")
    print("=" * 70)

    headers = connect()
    df = fetch_history(headers, days=BACKTEST_DAYS)
    df = add_indicators(df)
    days = max(1, (df["snapshotTime"].iloc[-1] - df["snapshotTime"].iloc[0]).days)

    print("\nLancement des backtests...")
    scenarios = [
        ("BASE (ecart>5pts)", dict()),
        ("F1 Z-SCORE>=2",     dict(use_zscore=True)),
        ("F2 +RSI",           dict(use_rsi=True)),
        ("F3 +MA plate",      dict(use_flat_ma=True)),
        ("F4 +RSI +MA plate", dict(use_rsi=True, use_flat_ma=True)),
    ]
    results = []
    for name, kw in scenarios:
        trades = backtest(df, name, **kw)
        results.append(stats(trades, name))

    print_comparison(results, days)
    print("\nBacktest termine.")
