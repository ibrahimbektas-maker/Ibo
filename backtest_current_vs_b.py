"""
BACKTEST CURRENT vs B -- Config LIVE
====================================
Compare uniquement les 2 strategies robustes (les seules a survivre aux couts) :
  CURRENT : SL -8 / TP +14, pas de time exit  (= ce qui tourne en live)
  B       : SL -8 / TP +7,  pas de time exit  (= live avec TP reduit)

Difference vs backtest_meanrev_v2.py :
  - Config alignee sur le BOT LIVE : MAX_DAILY_TRADES=10, COOLDOWN=10min
  - Periode plus longue (BACKTEST_DAYS) pour un echantillon plus credible
  - Couts de transaction (spread/slippage/prime stop garanti)
  - SL interne modelise comme fermeture sur poll (base close), comme le bot
  - Metriques de ROBUSTESSE : drawdown max, profit factor, esperance/trade

Usage : python backtest_current_vs_b.py
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
# PARAMETRES STRATEGIE (alignes sur bot_meanrev_v3_2.py)
# ============================================================
MA_PERIOD       = 90
DEVIATION_PTS   = 5.0
SL_POINTS       = 8.0
TRADING_HOUR_START_UTC = 7
TRADING_HOUR_END_UTC   = 15
NEWS_BLOCK_TIMES_UTC = [(8, 0), (9, 0), (12, 30)]
NEWS_BLOCK_MINUTES   = 5

# === CONFIG LIVE (identique au bot) ===
MAX_DAILY_TRADES = 10     # bot live = 10 (pas 40)
COOLDOWN_BARS    = 10     # bot live = 10 min (pas 5)

# Periode : augmente si l'API Capital.com a l'historique minute disponible.
BACKTEST_DAYS = 30

# ============================================================
# COUTS DE TRANSACTION
# ============================================================
SPREAD_POINTS       = 0.4   # spread aller-retour du GOLD (a ajuster selon ton broker)
SLIPPAGE_PTS        = 0.2   # slippage sur ordre au marche
GUARANTEED_STOP_FEE = 0.5   # prime stop garanti, si le filet broker se declenche

LOT_SIZE = 0.01


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
# BACKTEST (couts + SL interne base close, comme le bot live)
# ============================================================
def backtest(df, name, tp_pts):
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

    for i in range(MA_PERIOD, len(df)):
        row = df.iloc[i]
        t = row["snapshotTime"]
        price = row["close"]
        high  = row["high"]
        low   = row["low"]

        if in_position:
            exit_price = None
            reason = None
            elapsed_min = (t - entry_time).total_seconds() / 60

            # Filet broker (garanti) = intrabar ; TP (limite) = intrabar ;
            # SL interne = fermeture manuelle sur poll -> base sur le close.
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

            if exit_price is not None:
                gross_pts = (exit_price - entry_price) if pos_direction == "LONG" else (entry_price - exit_price)
                cost_pts = SPREAD_POINTS + SLIPPAGE_PTS         # spread + slippage entree
                if reason == "SL_INTERNE":
                    cost_pts += SLIPPAGE_PTS                    # sortie au marche
                elif reason == "SL_FILET":
                    cost_pts += GUARANTEED_STOP_FEE
                pnl_pts = gross_pts - cost_pts
                trades.append({
                    "entry_time": entry_time, "exit_time": t,
                    "direction": pos_direction, "entry": entry_price, "exit": exit_price,
                    "pnl_pts": pnl_pts, "pnl_eur": pnl_pts * LOT_SIZE,
                    "duration_min": elapsed_min, "reason": reason,
                })
                in_position = False
                last_close_time = t
                continue

        if in_position:
            continue
        if not is_in_trading_window(t):
            continue
        if is_news_window(t):
            continue
        if last_close_time and (t - last_close_time).total_seconds() < COOLDOWN_BARS * 60:
            continue

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
    df = pd.DataFrame(trades)
    n = len(df)
    wins = df[df["pnl_pts"] > 0]
    losses = df[df["pnl_pts"] < 0]
    gross_win = wins["pnl_pts"].sum()
    gross_loss = losses["pnl_pts"].sum()  # negatif
    total_pts = df["pnl_pts"].sum()

    # Drawdown sur la courbe d'equite (trades dans l'ordre chronologique)
    cum = df["pnl_pts"].cumsum()
    peak = cum.cummax()
    max_dd = (cum - peak).min()

    profit_factor = (gross_win / abs(gross_loss)) if gross_loss != 0 else float("inf")

    return {
        "name": name, "n": n,
        "wins": len(wins), "losses": len(losses),
        "winrate": len(wins) / n * 100,
        "total_pts": total_pts, "total_eur": total_pts * LOT_SIZE,
        "expectancy": total_pts / n,
        "profit_factor": profit_factor,
        "max_dd": max_dd,
        "best_pts": df["pnl_pts"].max(),
        "worst_pts": df["pnl_pts"].min(),
        "avg_duration": df["duration_min"].mean(),
        "reasons": df["reason"].value_counts().to_dict(),
    }


def print_comparison(results, days):
    print("\n" + "=" * 92)
    print(f" CURRENT vs B  --  CONFIG LIVE (MAX_DAILY_TRADES={MAX_DAILY_TRADES}, COOLDOWN={COOLDOWN_BARS}min)")
    print("=" * 92)
    for r in results:
        if r is None:
            continue
        print(f"\n--- {r['name']} ---")
        print(f"  Trades        : {r['n']}  ({r['n']/days:.2f}/jour)")
        print(f"  Winrate       : {r['winrate']:.1f}%  ({r['wins']}W / {r['losses']}L)")
        print(f"  PnL total     : {r['total_pts']:+.1f} pts  ({r['total_eur']:+.2f} EUR @ {LOT_SIZE} lot)")
        print(f"  Esperance     : {r['expectancy']:+.3f} pts/trade")
        print(f"  Profit factor : {r['profit_factor']:.2f}   (>1 = rentable, >1.3 = solide)")
        print(f"  Drawdown max  : {r['max_dd']:+.1f} pts   (pire creux de la courbe)")
        print(f"  Duree moyenne : {r['avg_duration']:.1f} min")
        print(f"  Best / Worst  : {r['best_pts']:+.1f} / {r['worst_pts']:+.1f} pts")
        print(f"  Sorties       : {r['reasons']}")

    # Mini-tableau recap
    print("\n" + "-" * 92)
    fmt = "{:<28} {:>7} {:>7} {:>9} {:>8} {:>8} {:>9}"
    print(fmt.format("Strategie", "Trades", "WR%", "PnL pts", "Expect", "PF", "MaxDD"))
    print("-" * 92)
    for r in results:
        if r is None:
            continue
        print(fmt.format(
            r["name"], r["n"], f"{r['winrate']:.1f}",
            f"{r['total_pts']:+.1f}", f"{r['expectancy']:+.3f}",
            f"{r['profit_factor']:.2f}", f"{r['max_dd']:+.1f}",
        ))


# ============================================================
# MAIN
# ============================================================
if __name__ == "__main__":
    print("=" * 70)
    print(" BACKTEST CURRENT vs B -- config live (10 trades/j, cooldown 10min)")
    print("=" * 70)

    headers = connect()
    df = fetch_history(headers, days=BACKTEST_DAYS)
    days = max(1, (df["snapshotTime"].iloc[-1] - df["snapshotTime"].iloc[0]).days)

    print("\nLancement des backtests...")
    scenarios = [
        ("CURRENT (SL8/TP14)", 14.0),
        ("B (SL8/TP7)",         7.0),
    ]
    results = []
    for name, tp in scenarios:
        trades = backtest(df, name, tp_pts=tp)
        results.append(stats(trades, name))
        if trades:
            tag = name.split()[0].lower()
            pd.DataFrame(trades).to_csv(f"bt_{tag}_live.csv", index=False)

    print_comparison(results, days)
    print("\nFichiers CSV : bt_current_live.csv, bt_b_live.csv")
    print("Backtest termine.")
