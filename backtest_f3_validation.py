"""
VALIDATION DU FILTRE F3 (MA plate)
==================================
F3 a donne un beau resultat (PF 1.43, drawdown -64) MAIS sur une seule fenetre
de 28 jours et avec un seuil SLOPE_MAX=3 choisi a la main. Avant d'y mettre du
vrai argent, on verifie 2 choses :

  PARTIE 1 -- Sensibilite au parametre :
    On fait varier SLOPE_MAX. Si le PF reste >1.3 sur une PLAGE de valeurs
    (ex. 2 a 5), l'edge est robuste. Si seul 3.0 marche -> overfit.

  PARTIE 2 -- Consistance dans le temps :
    On decoupe la periode par semaine et on regarde le PF/PnL de chaque
    semaine. Si la plupart des semaines sont positives -> robuste. Si tout
    le profit vient d'une seule semaine -> fragile.

Base : strategie B (SL8/TP7) + filtre MA plate, config live, couts inclus.
Usage : python backtest_f3_validation.py
"""

import os
import requests
import pandas as pd
from datetime import datetime, timedelta
from collections import defaultdict

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
# PARAMETRES (base B + config live)
# ============================================================
MA_PERIOD       = 90
DEVIATION_PTS   = 5.0
SL_POINTS       = 8.0
TP_POINTS       = 7.0
TRADING_HOUR_START_UTC = 7
TRADING_HOUR_END_UTC   = 15
NEWS_BLOCK_TIMES_UTC = [(8, 0), (9, 0), (12, 30)]
NEWS_BLOCK_MINUTES   = 5
MAX_DAILY_TRADES = 10
COOLDOWN_BARS    = 10
BACKTEST_DAYS    = 30

SLOPE_LOOKBACK = 30
SLOPE_MAX_CHOSEN = 3.0           # valeur retenue dans backtest_filters
SLOPE_MAX_GRID = [1, 2, 3, 4, 5, 6, 8, 999]   # 999 ~ pas de filtre (= base B)

SPREAD_POINTS       = 0.4
SLIPPAGE_PTS        = 0.2
GUARANTEED_STOP_FEE = 0.5
LOT_SIZE            = 0.01


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
    all_prices, cursor = [], start
    print(f"Telechargement {days} jours de bougies 1min...")
    while cursor < end:
        chunk_end = min(cursor + timedelta(hours=16), end)
        params = {"resolution": "MINUTE",
                  "from": cursor.strftime("%Y-%m-%dT%H:%M:%S"),
                  "to":   chunk_end.strftime("%Y-%m-%dT%H:%M:%S"), "max": 1000}
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
    df["ma"]    = df["close"].rolling(MA_PERIOD).mean().shift(1)
    df["slope"] = df["ma"] - df["ma"].shift(SLOPE_LOOKBACK)
    return df


def is_news_window(dt):
    minutes = dt.hour * 60 + dt.minute
    return any(abs(minutes - (h * 60 + m)) <= NEWS_BLOCK_MINUTES for h, m in NEWS_BLOCK_TIMES_UTC)

def is_in_trading_window(dt):
    return TRADING_HOUR_START_UTC <= dt.hour < TRADING_HOUR_END_UTC


# ============================================================
# Backtest F3 : base B + filtre MA plate (|pente| <= slope_max)
# ============================================================
def backtest_f3(df, slope_max):
    trades = []
    in_position = False
    pos_direction = entry_price = entry_time = None
    sl_internal = sl_filet = take_profit = None
    last_close_time = None
    daily_trades = defaultdict(int)

    start_i = MA_PERIOD + SLOPE_LOOKBACK + 2
    for i in range(start_i, len(df)):
        row = df.iloc[i]
        t, price, high, low = row["snapshotTime"], row["close"], row["high"], row["low"]

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
        if last_close_time and (t - last_close_time).total_seconds() < COOLDOWN_BARS * 60:
            continue
        day_key = t.strftime("%Y-%m-%d")
        if daily_trades[day_key] >= MAX_DAILY_TRADES:
            continue

        ma, slope = row["ma"], row["slope"]
        if pd.isna(ma) or pd.isna(slope):
            continue
        if abs(slope) > slope_max:     # filtre MA plate
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


# ============================================================
if __name__ == "__main__":
    print("=" * 70)
    print(" VALIDATION FILTRE F3 (MA plate)")
    print("=" * 70)
    headers = connect()
    df = fetch_history(headers, days=BACKTEST_DAYS)
    df = add_indicators(df)

    # ---- PARTIE 1 : sensibilite a SLOPE_MAX ----
    print("\n" + "=" * 78)
    print(" PARTIE 1 -- Sensibilite au seuil SLOPE_MAX (sur toute la periode)")
    print("=" * 78)
    fmt = "{:>10} {:>8} {:>8} {:>10} {:>9} {:>8} {:>10}"
    print(fmt.format("SLOPE_MAX", "Trades", "WR%", "PnL pts", "Expect", "PF", "MaxDD"))
    print("-" * 78)
    for sm in SLOPE_MAX_GRID:
        s = stats(backtest_f3(df, sm))
        label = "sans (base)" if sm >= 999 else f"{sm}"
        print(fmt.format(label, s["n"], f"{s['winrate']:.1f}", f"{s['total']:+.1f}",
                         f"{s['expectancy']:+.3f}", pf_str(s["pf"]), f"{s['dd']:+.1f}"))
    print("-" * 78)
    print("ROBUSTE si le PF reste >1.3 sur une plage (ex. 2 a 5), pas juste a 3.0.")

    # ---- PARTIE 2 : consistance par semaine ----
    print("\n" + "=" * 78)
    print(f" PARTIE 2 -- Consistance par semaine (SLOPE_MAX={SLOPE_MAX_CHOSEN})")
    print("=" * 78)
    trades = backtest_f3(df, SLOPE_MAX_CHOSEN)
    by_week = defaultdict(list)
    for tr in trades:
        wk = tr["entry_time"].strftime("%Y-W%U")
        by_week[wk].append(tr)

    fmt2 = "{:>10} {:>8} {:>8} {:>10} {:>8}"
    print(fmt2.format("Semaine", "Trades", "WR%", "PnL pts", "PF"))
    print("-" * 78)
    pos_weeks = 0
    for wk in sorted(by_week):
        s = stats(by_week[wk])
        if s["total"] > 0:
            pos_weeks += 1
        print(fmt2.format(wk, s["n"], f"{s['winrate']:.1f}", f"{s['total']:+.1f}", pf_str(s["pf"])))
    print("-" * 78)
    n_weeks = len(by_week)
    print(f"Semaines positives : {pos_weeks}/{n_weeks}")
    print("ROBUSTE si la majorite des semaines sont positives (pas 1 seule qui porte tout).")
    print("\nValidation terminee.")
