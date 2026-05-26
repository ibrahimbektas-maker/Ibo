"""
ANALYSE GOLD 5 MIN -- recherche de regularites (lit gold_5m.csv)
================================================================
Cherche des patterns qui se repetent dans le gold sur 3 mois :
  1) Profil par HEURE UTC : volatilite, biais directionnel, tendance a
     mean-revert (autocorrelation) -> OU et QUAND l'edge existe
  2) Profil par JOUR de semaine
  3) A quelle HEURE se forment le plus haut et le plus bas du jour
  4) Heures les plus actives / biais notables

Tourne hors-ligne. Usage : python analyse_gold_5m.py
"""

import os
import pandas as pd

CSV_IN = "gold_5m.csv"
JOURS = ["Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi", "Samedi", "Dimanche"]


def bar(v, scale, width=18):
    """Petite barre visuelle proportionnelle a v (pour lire d'un coup d'oeil)."""
    n = int(round(min(abs(v) / scale, 1.0) * width)) if scale > 0 else 0
    return ("#" * n).ljust(width)


if __name__ == "__main__":
    if not os.path.exists(CSV_IN):
        print(f"ERREUR : {CSV_IN} introuvable. Lance d'abord : python fetch_gold_5m.py")
        exit(1)

    df = pd.read_csv(CSV_IN, parse_dates=["time"])
    df = df.drop_duplicates(subset="time").sort_values("time").reset_index(drop=True)

    df["ret_pts"]  = df["close"].diff()                  # variation 5 min en points
    df["range_pts"]= df["high"] - df["low"]              # amplitude de la bougie
    df["hour"]     = df["time"].dt.hour                  # heure UTC
    df["weekday"]  = df["time"].dt.dayofweek
    df["date"]     = df["time"].dt.date
    df["ret_prev"] = df["ret_pts"].shift(1)

    # ---- Resume general ----
    span = (df["time"].iloc[-1] - df["time"].iloc[0]).days
    print("=" * 80)
    print(f" ANALYSE GOLD 5 MIN -- {len(df)} bougies, {span} jours "
          f"({df['time'].iloc[0]:%Y-%m-%d} -> {df['time'].iloc[-1]:%Y-%m-%d})")
    print("=" * 80)
    print(f" Prix : {df['close'].iloc[0]:.1f} -> {df['close'].iloc[-1]:.1f} "
          f"({df['close'].iloc[-1] - df['close'].iloc[0]:+.1f} pts sur la periode)")
    print(f" Amplitude moyenne d'une bougie 5min : {df['range_pts'].mean():.2f} pts")
    print(f" Volatilite (ecart-type des variations 5min) : {df['ret_pts'].std():.2f} pts")

    # ---- 1) Profil par HEURE UTC ----
    print("\n" + "=" * 80)
    print(" 1) PROFIL PAR HEURE (UTC)")
    print("=" * 80)
    g = df.groupby("hour")
    hourly = pd.DataFrame({
        "n":         g.size(),
        "ret_moyen": g["ret_pts"].mean(),
        "volat":     g["ret_pts"].std(),
        "range_moy": g["range_pts"].mean(),
        "pct_haus":  g["ret_pts"].apply(lambda s: (s > 0).mean() * 100),
    })
    # autocorrelation lag-1 par heure : <0 = mean-reverting, >0 = trending
    autoc = {h: sub["ret_pts"].corr(sub["ret_prev"]) for h, sub in df.groupby("hour")}
    hourly["autocorr"] = pd.Series(autoc)

    rng_scale = hourly["range_moy"].max()
    print(f"{'H':>3} {'n':>5} {'ret_moy':>8} {'volat':>7} {'range':>7} {'%haus':>6} "
          f"{'autocor':>8}  activite (range)")
    print("-" * 80)
    for h, r in hourly.iterrows():
        flag = " <-- fenetre bot" if 7 <= h < 15 else ""
        print(f"{h:>3} {int(r['n']):>5} {r['ret_moyen']:>+8.3f} {r['volat']:>7.2f} "
              f"{r['range_moy']:>7.2f} {r['pct_haus']:>5.1f}% {r['autocorr']:>+8.3f}  "
              f"{bar(r['range_moy'], rng_scale, 14)}{flag}")
    print("\n Lecture : 'autocor' negatif = le prix a tendance a REVENIR (bon pour"
          " mean-rev) ;\n positif = il CONTINUE (tendance, mauvais pour fader)."
          " 'range' = activite/volatilite.")

    # ---- 2) Profil par JOUR de semaine ----
    print("\n" + "=" * 80)
    print(" 2) PROFIL PAR JOUR DE SEMAINE")
    print("=" * 80)
    gw = df.groupby("weekday")
    print(f"{'Jour':>10} {'n':>6} {'ret_moy':>9} {'volat':>7} {'range_moy':>10} {'%haus':>6}")
    print("-" * 60)
    for wd, sub in gw:
        print(f"{JOURS[wd]:>10} {len(sub):>6} {sub['ret_pts'].mean():>+9.3f} "
              f"{sub['ret_pts'].std():>7.2f} {sub['range_pts'].mean():>10.2f} "
              f"{(sub['ret_pts'] > 0).mean()*100:>5.1f}%")

    # ---- 3) Heure du PLUS HAUT / PLUS BAS du jour ----
    print("\n" + "=" * 80)
    print(" 3) A QUELLE HEURE (UTC) SE FORME LE HAUT / LE BAS DU JOUR ?")
    print("=" * 80)
    hi_rows = df.loc[df.groupby("date")["high"].idxmax()]
    lo_rows = df.loc[df.groupby("date")["low"].idxmin()]
    hi_hours = hi_rows["hour"].value_counts().sort_index()
    lo_hours = lo_rows["hour"].value_counts().sort_index()
    ndays = df["date"].nunique()
    print(f"(sur {ndays} jours)  HAUT du jour | BAS du jour")
    print(f"{'H':>3} {'#haut':>6} {'%':>5}   {'#bas':>6} {'%':>5}")
    print("-" * 40)
    for h in range(24):
        nh = int(hi_hours.get(h, 0)); nl = int(lo_hours.get(h, 0))
        if nh == 0 and nl == 0:
            continue
        print(f"{h:>3} {nh:>6} {nh/ndays*100:>4.0f}%   {nl:>6} {nl/ndays*100:>4.0f}%")

    # ---- 4) Synthese ----
    print("\n" + "=" * 80)
    print(" 4) SYNTHESE")
    print("=" * 80)
    h_volat = hourly["range_moy"].idxmax()
    h_calme = hourly["range_moy"].idxmin()
    h_revert = hourly["autocorr"].idxmin()   # plus negatif = plus mean-reverting
    h_trend  = hourly["autocorr"].idxmax()
    print(f" Heure la plus VOLATILE       : {h_volat}h UTC (range {hourly.loc[h_volat,'range_moy']:.2f} pts)")
    print(f" Heure la plus CALME          : {h_calme}h UTC (range {hourly.loc[h_calme,'range_moy']:.2f} pts)")
    print(f" Heure la plus MEAN-REVERTING : {h_revert}h UTC (autocorr {hourly.loc[h_revert,'autocorr']:+.3f})")
    print(f" Heure la plus TENDANCIELLE   : {h_trend}h UTC (autocorr {hourly.loc[h_trend,'autocorr']:+.3f})")
    win = hourly.loc[7:14]
    print(f"\n Dans ta fenetre 7-15 UTC : autocorr moyen {win['autocorr'].mean():+.3f} "
          f"(negatif = favorable au mean-rev)")
    print("\nNOTE : 90 jours = un seul regime (gold en forte hausse en 2026).")
    print("Les biais directionnels (%haus, ret_moy) sont influences par cette tendance ;")
    print("la volatilite par heure et les heures de haut/bas sont plus robustes.")
