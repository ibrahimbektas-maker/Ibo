"""
RECUPERATION BOUGIES 5 MIN GOLD (Capital.com) -> CSV
====================================================
Telecharge les bougies 5 minutes du GOLD sur DAYS jours et les sauve dans
un fichier CSV reutilisable (gold_5m.csv), pour backtester sans re-telecharger.

Usage : python fetch_gold_5m.py
Sortie : gold_5m.csv  (colonnes : time, open, high, low, close)

NOTE : l'API Capital.com limite la profondeur d'historique. Le script affiche
la plage REELLEMENT recuperee a la fin. Si tu obtiens moins de 3 mois, c'est la
limite du broker (pas un bug) -- on travaillera avec ce qu'on a.
"""

import os
import time
import requests
import pandas as pd
from datetime import datetime, timedelta

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
# PARAMETRES
# ============================================================
RESOLUTION  = "MINUTE_5"   # bougies 5 minutes
DAYS        = 90           # ~3 mois
MAX_PER_REQ = 1000         # plafond Capital.com par requete
CHUNK_HOURS = 80           # 80h * 12 bougies/h = 960 bougies (< 1000) par requete
CSV_OUT     = "gold_5m.csv"
SLEEP_SEC   = 0.3          # petite pause entre requetes (respect du rate limit)


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


def fetch(headers):
    end = datetime.utcnow()
    start = end - timedelta(days=DAYS)
    all_prices = []
    cursor = start
    chunk = timedelta(hours=CHUNK_HOURS)

    print(f"Telechargement {RESOLUTION} sur {DAYS} jours (par tranches de {CHUNK_HOURS}h)...")
    n_req = 0
    while cursor < end:
        chunk_end = min(cursor + chunk, end)
        params = {
            "resolution": RESOLUTION,
            "from": cursor.strftime("%Y-%m-%dT%H:%M:%S"),
            "to":   chunk_end.strftime("%Y-%m-%dT%H:%M:%S"),
            "max":  MAX_PER_REQ,
        }
        try:
            r = requests.get(BASE_URL + "/prices/" + EPIC, headers=headers, params=params, timeout=20)
        except Exception as e:
            print(f"  {cursor:%Y-%m-%d %H:%M} -> erreur reseau ({e}), on passe")
            cursor = chunk_end
            continue

        if r.status_code == 200:
            prices = r.json().get("prices", [])
            all_prices.extend(prices)
            print(f"  {cursor:%Y-%m-%d %H:%M} -> {len(prices)} bougies")
        else:
            print(f"  {cursor:%Y-%m-%d %H:%M} -> HTTP {r.status_code}: {r.text[:120]}")

        n_req += 1
        cursor = chunk_end
        time.sleep(SLEEP_SEC)

    print(f"\n{n_req} requetes envoyees, {len(all_prices)} bougies brutes recuperees.")
    return all_prices


def to_dataframe(prices):
    if not prices:
        print("Aucune donnee recuperee. (Verifie EPIC, identifiants, ou la profondeur dispo.)")
        exit(1)
    df = pd.DataFrame(prices)
    # Capital.com : chaque prix a {bid, ask} ; on prend le bid (comme les autres scripts).
    df["open"]  = pd.to_numeric(df["openPrice"].apply(lambda x:  x["bid"]))
    df["high"]  = pd.to_numeric(df["highPrice"].apply(lambda x:  x["bid"]))
    df["low"]   = pd.to_numeric(df["lowPrice"].apply(lambda x:   x["bid"]))
    df["close"] = pd.to_numeric(df["closePrice"].apply(lambda x: x["bid"]))
    df["time"]  = pd.to_datetime(df["snapshotTime"])
    df = df[["time", "open", "high", "low", "close"]]
    df = df.drop_duplicates(subset="time").sort_values("time").reset_index(drop=True)
    return df


if __name__ == "__main__":
    print("=" * 60)
    print(f" FETCH {RESOLUTION} GOLD -> {CSV_OUT}")
    print("=" * 60)
    headers = connect()
    prices = fetch(headers)
    df = to_dataframe(prices)
    df.to_csv(CSV_OUT, index=False)

    span_days = (df["time"].iloc[-1] - df["time"].iloc[0]).days
    print("\n" + "=" * 60)
    print(f"Sauve : {CSV_OUT}")
    print(f"Bougies : {len(df)}")
    print(f"Plage   : {df['time'].iloc[0]} -> {df['time'].iloc[-1]}  ({span_days} jours)")
    if span_days < DAYS - 5:
        print(f"/!\\ Seulement {span_days} jours obtenus (demande {DAYS}). "
              f"C'est la limite de profondeur de l'API Capital.com pour le 5 min.")
    print("=" * 60)
