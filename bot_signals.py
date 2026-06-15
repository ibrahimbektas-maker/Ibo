"""
BOT SIGNALS GOLD (Capital.com) -- mode PUSH, SANS execution
============================================================
Scanne le marche en bougies 1 min et envoie une alerte Telegram a chaque
setup detecte. AUCUN ORDRE n'est passe -- tu vois l'alerte, tu decides.

Deux types de signaux :
  MEAN-REV : ecart >= DEV_THRESHOLD pts ET MA90 plate (|pente| <= SLOPE_FLAT_MAX)
             -> potentiel retour a la moyenne dans un range.
  TREND    : pente MA90 forte (>= TREND_SLOPE_MIN sur SLOPE_LOOKBACK barres)
             -> tendance directionnelle a suivre.

Anti-spam : cooldown ALERT_COOLDOWN_MIN entre deux alertes du MEME type,
plafond ALERTS_PER_DAY au total. Filtres news (8h/9h/12h30 UTC).

Commandes Telegram : /status (etat du scanner), /stop (arret).

Usage : python bot_signals.py
"""

import os
import time
import json
import requests
import pandas as pd
from datetime import datetime, date

# ─────────────────────────────────────────────
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
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip()
    return env

env = load_env()
API_KEY        = env.get("CAPITAL_API_KEY", "")
API_SECRET     = env.get("CAPITAL_API_SECRET", "")
ACCOUNT_ID     = env.get("CAPITAL_ACCOUNT_ID", "")
EPIC           = env.get("CAPITAL_EPIC", "GOLD")
BASE_URL       = env.get("CAPITAL_BASE_URL", "https://api-capital.backend-capital.com/api/v1")
TELEGRAM_TOKEN = env.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT  = env.get("TELEGRAM_CHAT", "")

if not all([API_KEY, API_SECRET, ACCOUNT_ID, TELEGRAM_TOKEN, TELEGRAM_CHAT]):
    print("ERREUR : identifiants manquants dans .env")
    exit(1)

# ─────────────────────────────────────────────
# CONFIG SIGNAUX
# ─────────────────────────────────────────────
MA_PERIOD          = 90
SLOPE_LOOKBACK     = 30
DEV_THRESHOLD      = 5.0      # ecart prix-MA mini pour MEAN-REV
SLOPE_FLAT_MAX     = 3.0      # MA "plate" si |pente sur 30 barres| <= ce seuil
TREND_SLOPE_MIN    = 5.0      # pente mini pour TREND
SL_SUGGESTED_PTS   = 8.0      # SL suggere dans l'alerte
TP_MR_SUGGESTED    = 7.0      # TP mean-rev suggere
TP_TREND_SUGGESTED = 20.0     # TP trend suggere (R:R plus large)

# Quels signaux on emet
# Eval 1-2 juin 2026 : MR a fait 16/16 TP, TREND 0/17 (entree systematiquement
# au point d'epuisement de la tendance -> rebond contraire). On desactive TREND.
ENABLE_MR    = True
ENABLE_TREND = False
ENABLE_FVG   = True            # methode Palomatrd (FVG + Fibo + bias 1h)

# Parametres FVG-FIBO (backtest_fvg_v2 : PF 1.05, 2/3 blocs positifs)
FVG_5M_LOOKBACK   = 50    # bougies 5min pour swing/Fibo
FVG_5M_MAX_AGE    = 30    # age max d'un FVG (bougies 5min = 2h30)
FVG_5M_HTF_MA     = 96    # MA bias "1h+" (96 bougies 5min = 8h)
FVG_5M_HTF_SLOPE  = 24    # pente sur 24 bougies 5min (2h)
FVG_5M_SESSION_H  = set(range(8, 16))   # 8-15h UTC (= matin Europe + ouverture NY)
SL_FVG_PTS        = 15.0
TP_FVG_PTS        = 11.0  # TP1 de Palomatrd

# Fenetre de scan (large par defaut : tu filtres avec ton cerveau)
WINDOW_START_HOUR_UTC = 6
WINDOW_END_HOUR_UTC   = 21

# Anti-spam
ALERT_COOLDOWN_MIN = 15     # entre 2 alertes du meme type
ALERTS_PER_DAY     = 20

# Filtre news
NEWS_BLOCK_TIMES_UTC = [(8, 0), (9, 0), (12, 30)]
NEWS_BLOCK_MINUTES   = 5

CANDLES_NEEDED = 150
POLL_INTERVAL_SEC = 30

LOG_FILE   = "bot_signals_log.txt"
STATE_FILE = "bot_signals_state.json"

# ─────────────────────────────────────────────
state = {
    "running": True,
    "connected": False,
    "last_mr_alert_iso":    None,
    "last_trend_alert_iso": None,
    "last_fvg_alert_iso":   None,
    "fvg_trades_today":     0,
    "alerts_today":         0,
    "today":                None,
}

headers = {"X-CAP-API-KEY": API_KEY, "Content-Type": "application/json"}
last_update_id = 0


def now_str():
    return datetime.now().strftime("%H:%M:%S")

def now_utc():
    return datetime.utcnow()

def log(msg, level="INFO"):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] [{level}] {msg}"
    print(line)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def save_state():
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, default=str)
    except Exception as e:
        log(f"Erreur sauvegarde: {e}", "ERROR")


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
            for k, v in saved.items():
                if k in state:
                    state[k] = v
            log("Etat precedent restaure")
        except Exception as e:
            log(f"Erreur chargement: {e}", "ERROR")


# ─────────────────────────────────────────────
def telegram(msg):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TELEGRAM_CHAT, "text": msg}, timeout=10)
    except Exception as e:
        log(f"Erreur Telegram: {e}", "ERROR")


def telegram_check_commands():
    global last_update_id
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
        r = requests.get(url, params={"timeout": 1, "offset": last_update_id + 1}, timeout=5)
        if r.status_code != 200:
            return
        for upd in r.json().get("result", []):
            last_update_id = max(last_update_id, upd.get("update_id", 0))
            msg = upd.get("message", {}).get("text", "")
            if msg == "/status":
                lines = ["SCANNER SIGNAUX -- STATUS",
                         f"Heure UTC : {now_utc():%H:%M}",
                         f"Alertes du jour : {state['alerts_today']}/{ALERTS_PER_DAY}",
                         f"FVG du jour     : {state.get('fvg_trades_today', 0)}/2",
                         f"Derniere mean-rev : {state.get('last_mr_alert_iso') or 'aucune'}",
                         f"Derniere FVG-Fibo : {state.get('last_fvg_alert_iso') or 'aucune'}",
                         f"Derniere trend    : {state.get('last_trend_alert_iso') or 'aucune'}"]
                telegram("\n".join(lines))
            elif msg == "/stop":
                state["running"] = False
                telegram("Scanner arrete.")
    except Exception:
        pass


# ─────────────────────────────────────────────
def connect():
    log("Connexion a Capital.com...")
    try:
        r = requests.post(BASE_URL + "/session", headers=headers,
                          json={"identifier": ACCOUNT_ID, "password": API_SECRET}, timeout=15)
        if r.status_code == 200:
            headers["X-SECURITY-TOKEN"] = r.headers.get("X-SECURITY-TOKEN", "")
            headers["CST"]              = r.headers.get("CST", "")
            state["connected"] = True
            log("Connexion reussie")
            return True
        log(f"Erreur connexion: {r.text}", "ERROR")
        return False
    except Exception as e:
        log(f"Erreur reseau: {e}", "ERROR")
        return False


def ensure_connected():
    if not state["connected"]:
        for _ in range(3):
            if connect():
                return True
            time.sleep(5)
        telegram("ALERTE: Reconnexion impossible")
        return False
    return True


def get_minute_data():
    try:
        r = requests.get(BASE_URL + "/prices/" + EPIC, headers=headers,
                         params={"resolution": "MINUTE", "max": CANDLES_NEEDED}, timeout=15)
        if r.status_code == 200:
            prices = r.json().get("prices", [])
            if not prices:
                return None
            df = pd.DataFrame(prices)
            df["close"] = pd.to_numeric(df["closePrice"].apply(lambda x: x["bid"]))
            return df
        if r.status_code == 401:
            state["connected"] = False
        return None
    except Exception as e:
        log(f"Erreur get_minute_data: {e}", "ERROR")
        return None


# ─────────────────────────────────────────────
def is_news_window():
    n = now_utc()
    cur = n.hour * 60 + n.minute
    for h, m in NEWS_BLOCK_TIMES_UTC:
        if abs(cur - (h * 60 + m)) <= NEWS_BLOCK_MINUTES:
            return True
    return False


def in_window():
    h = now_utc().hour
    return WINDOW_START_HOUR_UTC <= h < WINDOW_END_HOUR_UTC


def reset_daily_if_new_day():
    today = date.today().isoformat()
    if state["today"] != today:
        state["today"] = today
        state["alerts_today"] = 0
        state["fvg_trades_today"] = 0
        log(f"Nouveau jour : {today}, compteurs alertes reset")


def minutes_since(iso_ts):
    if not iso_ts:
        return None
    try:
        dt = datetime.fromisoformat(iso_ts)
    except (ValueError, TypeError):
        return None
    return (datetime.now() - dt).total_seconds() / 60.0


# ─────────────────────────────────────────────
# DETECTION
# ─────────────────────────────────────────────
def compute_features(df):
    if len(df) < MA_PERIOD + SLOPE_LOOKBACK:
        return None
    price = float(df["close"].iloc[-1])
    ma = float(df["close"].iloc[-MA_PERIOD:].mean())
    ma_past = float(df["close"].iloc[-(MA_PERIOD + SLOPE_LOOKBACK):-SLOPE_LOOKBACK].mean())
    slope = ma - ma_past
    return {"price": price, "ma": ma, "slope": slope, "deviation": price - ma}


def detect_mean_rev(f):
    """Mean-rev : ecart prix-MA significatif + MA plate."""
    if abs(f["slope"]) > SLOPE_FLAT_MAX:
        return None
    if f["deviation"] >  DEV_THRESHOLD: return "SELL"   # prix au-dessus -> fade
    if f["deviation"] < -DEV_THRESHOLD: return "BUY"    # prix en dessous -> fade
    return None


def detect_trend(f):
    """Trend : pente MA90 forte."""
    if f["slope"] >=  TREND_SLOPE_MIN: return "BUY"
    if f["slope"] <= -TREND_SLOPE_MIN: return "SELL"
    return None


# ─────────────────────────────────────────────
# FVG-FIBO (methode Palomatrd)
# ─────────────────────────────────────────────
def get_5min_data():
    """Recupere les dernieres bougies 5 minutes (~ derniers ~3 jours utiles)."""
    try:
        r = requests.get(BASE_URL + "/prices/" + EPIC, headers=headers,
                         params={"resolution": "MINUTE_5", "max": 500}, timeout=15)
        if r.status_code == 200:
            prices = r.json().get("prices", [])
            if not prices:
                return None
            df = pd.DataFrame(prices)
            df["close"] = pd.to_numeric(df["closePrice"].apply(lambda x: x["bid"]))
            df["high"]  = pd.to_numeric(df["highPrice"].apply(lambda x:  x["bid"]))
            df["low"]   = pd.to_numeric(df["lowPrice"].apply(lambda x:   x["bid"]))
            df["open"]  = pd.to_numeric(df["openPrice"].apply(lambda x:  x["bid"]))
            df["time"]  = pd.to_datetime(df["snapshotTime"])
            df = df.drop_duplicates(subset="time").sort_values("time").reset_index(drop=True)
            return df
        if r.status_code == 401:
            state["connected"] = False
        return None
    except Exception as e:
        log(f"Erreur get_5min_data: {e}", "ERROR")
        return None


def _fib_zone(swing_low, swing_high, is_long):
    """Zone Fibo 0.5-0.786 d'un retracement."""
    rng = swing_high - swing_low
    if is_long:
        zone_high = swing_low + 0.50  * rng
        zone_low  = swing_low + 0.214 * rng
    else:
        zone_low  = swing_high - 0.50  * rng
        zone_high = swing_high - 0.214 * rng
    return (zone_low, zone_high)


def _overlaps(a_lo, a_hi, b_lo, b_hi):
    return not (a_hi < b_lo or b_hi < a_lo)


def detect_fvg_signal():
    """Retourne un dict de setup FVG-Fibo ou None.
    Filtres : FVG bullish/bearish, Fibo 0.5-0.786, biais HTF (MA8h), session UTC."""
    # Session
    h_utc = now_utc().hour
    if h_utc not in FVG_5M_SESSION_H:
        return None
    # Plafond journalier
    if state.get("fvg_trades_today", 0) >= 2:
        return None

    df = get_5min_data()
    if df is None or len(df) < FVG_5M_HTF_MA + FVG_5M_HTF_SLOPE + FVG_5M_LOOKBACK:
        return None

    n = len(df)
    price       = float(df["close"].iloc[-1])
    high_now    = float(df["high"].iloc[-1])
    low_now     = float(df["low"].iloc[-1])
    ma          = float(df["close"].iloc[-FVG_5M_HTF_MA:].mean())
    ma_past     = float(df["close"].iloc[-(FVG_5M_HTF_MA + FVG_5M_HTF_SLOPE):-FVG_5M_HTF_SLOPE].mean())
    slope       = ma - ma_past
    if price > ma and slope > 0:
        htf = "bull"
    elif price < ma and slope < 0:
        htf = "bear"
    else:
        return None       # pas de bias clair -> pas de setup

    swing_high = float(df["high"].iloc[-FVG_5M_LOOKBACK:].max())
    swing_low  = float(df["low"].iloc[-FVG_5M_LOOKBACK:].min())
    if swing_high <= swing_low:
        return None

    # FVG actifs : detectes dans les FVG_5M_MAX_AGE dernieres bougies (sauf la courante)
    start_scan = max(2, n - 1 - FVG_5M_MAX_AGE)
    for i in range(start_scan, n - 1):
        c1_h, c1_l = float(df["high"].iloc[i-2]), float(df["low"].iloc[i-2])
        c3_h, c3_l = float(df["high"].iloc[i]),   float(df["low"].iloc[i])
        # BULL FVG : c1.high < c3.low (gap haut)
        if c1_h < c3_l and htf == "bull":
            fvg_top, fvg_bottom = c3_l, c1_h
            fib_lo, fib_hi = _fib_zone(swing_low, swing_high, is_long=True)
            if not _overlaps(fvg_bottom, fvg_top, fib_lo, fib_hi):
                continue
            # Le prix doit avoir touche le FVG dans la bougie courante
            if low_now > fvg_top:
                continue
            return {"direction": "BUY", "kind": "BULL",
                    "fvg_top": fvg_top, "fvg_bottom": fvg_bottom,
                    "swing_low": swing_low, "swing_high": swing_high,
                    "fib_low": fib_lo, "fib_high": fib_hi,
                    "htf": htf, "price": price, "ma": ma, "slope": slope}
        # BEAR FVG
        if c1_l > c3_h and htf == "bear":
            fvg_top, fvg_bottom = c1_l, c3_h
            fib_lo, fib_hi = _fib_zone(swing_low, swing_high, is_long=False)
            if not _overlaps(fvg_bottom, fvg_top, fib_lo, fib_hi):
                continue
            if high_now < fvg_bottom:
                continue
            return {"direction": "SELL", "kind": "BEAR",
                    "fvg_top": fvg_top, "fvg_bottom": fvg_bottom,
                    "swing_low": swing_low, "swing_high": swing_high,
                    "fib_low": fib_lo, "fib_high": fib_hi,
                    "htf": htf, "price": price, "ma": ma, "slope": slope}
    return None


def send_fvg_signal(sig):
    """Envoie l'alerte FVG-Fibo sur Telegram."""
    is_long = sig["direction"] == "BUY"
    entry = sig["fvg_top"] if is_long else sig["fvg_bottom"]
    sl    = entry - SL_FVG_PTS if is_long else entry + SL_FVG_PTS
    tp    = entry + TP_FVG_PTS if is_long else entry - TP_FVG_PTS
    arrow = "↑" if is_long else "↓"

    msg = (
        f"SIGNAL FVG-FIBO {sig['direction']} {arrow}\n"
        f"Heure  : {now_utc():%H:%M} UTC\n"
        f"Prix   : {sig['price']:.2f}\n"
        f"FVG {sig['kind']} : {sig['fvg_bottom']:.2f} - {sig['fvg_top']:.2f}\n"
        f"Dans zone Fibo 0.5-0.786 ({sig['fib_low']:.2f} - {sig['fib_high']:.2f})\n"
        f"Swing : {sig['swing_low']:.2f} -> {sig['swing_high']:.2f}\n"
        f"Bias HTF (MA8h) : {sig['htf']} (pente {sig['slope']:+.1f})\n"
        f"Entree suggeree : {entry:.2f}\n"
        f"SL : {sl:.2f}  (-{SL_FVG_PTS:.0f}pts)\n"
        f"TP : {tp:.2f}  (+{TP_FVG_PTS:.0f}pts)\n"
        f"\nMethode Palomatrd. Backtest 90j : PF 1.05, 2/3 blocs +. Tu decides."
    )
    log(msg)
    telegram(msg)
    state["alerts_today"] += 1
    state["fvg_trades_today"] += 1
    state["last_fvg_alert_iso"] = datetime.now().isoformat()
    save_state()


# ─────────────────────────────────────────────
def send_signal(sig_type, direction, f):
    is_long = direction == "BUY"
    if sig_type == "MEAN-REV":
        tp = f["price"] + TP_MR_SUGGESTED if is_long else f["price"] - TP_MR_SUGGESTED
        sl = f["price"] - SL_SUGGESTED_PTS if is_long else f["price"] + SL_SUGGESTED_PTS
        reason = (f"Ecart {f['deviation']:+.1f}pts vs MA90, pente {f['slope']:+.1f}pts (plate)")
    else:  # TREND
        tp = f["price"] + TP_TREND_SUGGESTED if is_long else f["price"] - TP_TREND_SUGGESTED
        sl = f["price"] - SL_SUGGESTED_PTS if is_long else f["price"] + SL_SUGGESTED_PTS
        reason = f"Pente MA90 {f['slope']:+.1f}pts sur {SLOPE_LOOKBACK} barres (forte)"

    arrow = "↑" if is_long else "↓"
    msg = (
        f"SIGNAL {sig_type} {direction} {arrow}\n"
        f"Heure  : {now_utc():%H:%M} UTC\n"
        f"Prix   : {f['price']:.2f}\n"
        f"MA90   : {f['ma']:.2f}\n"
        f"{reason}\n"
        f"SL suggere : {sl:.2f} (-{SL_SUGGESTED_PTS:.0f}pts)\n"
        f"TP suggere : {tp:.2f} ({tp - f['price']:+.0f}pts)\n"
        f"\nTu decides. (Aucun ordre passe par le bot)"
    )
    log(msg)
    telegram(msg)
    state["alerts_today"] += 1
    if sig_type == "MEAN-REV":
        state["last_mr_alert_iso"] = datetime.now().isoformat()
    else:
        state["last_trend_alert_iso"] = datetime.now().isoformat()
    save_state()


# ─────────────────────────────────────────────
def scan():
    if not state["running"]:
        return
    if not ensure_connected():
        return
    telegram_check_commands()
    reset_daily_if_new_day()

    if not in_window():
        return
    if is_news_window():
        return
    if state["alerts_today"] >= ALERTS_PER_DAY:
        return

    df = get_minute_data()
    if df is None or len(df) < CANDLES_NEEDED - 10:
        return

    f = compute_features(df)
    if f is None:
        return

    log(f"Prix {f['price']:.2f} | MA90 {f['ma']:.2f} | ecart {f['deviation']:+.2f} | "
        f"pente {f['slope']:+.2f}")

    # Mean-rev (avec cooldown)
    if ENABLE_MR:
        sig_mr = detect_mean_rev(f)
        if sig_mr is not None:
            mins = minutes_since(state.get("last_mr_alert_iso"))
            if mins is None or mins >= ALERT_COOLDOWN_MIN:
                send_signal("MEAN-REV", sig_mr, f)
            else:
                log(f"Mean-rev {sig_mr} mais cooldown ({mins:.1f}/{ALERT_COOLDOWN_MIN}min)")

    # Trend (avec cooldown)
    if ENABLE_TREND:
        sig_tr = detect_trend(f)
        if sig_tr is not None:
            mins = minutes_since(state.get("last_trend_alert_iso"))
            if mins is None or mins >= ALERT_COOLDOWN_MIN:
                send_signal("TREND", sig_tr, f)
            else:
                log(f"Trend {sig_tr} mais cooldown ({mins:.1f}/{ALERT_COOLDOWN_MIN}min)")

    # FVG-Fibo (methode Palomatrd, plafond 2/jour gere dans detect_fvg_signal)
    if ENABLE_FVG:
        sig_fvg = detect_fvg_signal()
        if sig_fvg is not None:
            mins = minutes_since(state.get("last_fvg_alert_iso"))
            if mins is None or mins >= ALERT_COOLDOWN_MIN:
                send_fvg_signal(sig_fvg)
            else:
                log(f"FVG {sig_fvg['direction']} mais cooldown ({mins:.1f}/{ALERT_COOLDOWN_MIN}min)")


# ─────────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 70)
    print(" BOT SIGNALS GOLD  --  mode push, AUCUNE execution")
    print(f" Fenetre scan       : {WINDOW_START_HOUR_UTC}h-{WINDOW_END_HOUR_UTC}h UTC")
    print(f" Mean-rev (1min)    : ecart >= {DEV_THRESHOLD}pts ET MA plate (<= {SLOPE_FLAT_MAX})"
          f"   [{'ON' if ENABLE_MR else 'OFF'}]")
    print(f" Trend (1min)       : pente MA90 >= {TREND_SLOPE_MIN}pts sur {SLOPE_LOOKBACK} barres"
          f"   [{'ON' if ENABLE_TREND else 'OFF'}]")
    print(f" FVG-Fibo (5min)    : FVG + Fibo 0.5-0.786 + bias 1h, sessions {sorted(FVG_5M_SESSION_H)}"
          f" UTC   [{'ON' if ENABLE_FVG else 'OFF'}]")
    print(f" Anti-spam : cooldown {ALERT_COOLDOWN_MIN}min, max {ALERTS_PER_DAY}/jour (FVG max 2/jour)")
    print("=" * 70)

    load_state()
    if not connect():
        print("ERREUR connexion."); exit(1)

    enabled = []
    if ENABLE_MR:    enabled.append("MEAN-REV")
    if ENABLE_TREND: enabled.append("TREND")
    if ENABLE_FVG:   enabled.append("FVG-FIBO")
    telegram("SCANNER SIGNAUX GOLD demarre.\n"
             f"Fenetre {WINDOW_START_HOUR_UTC}-{WINDOW_END_HOUR_UTC}h UTC | "
             f"max {ALERTS_PER_DAY} alertes/j | cooldown {ALERT_COOLDOWN_MIN}min\n"
             f"Signaux actifs : {', '.join(enabled)}\n"
             "Commandes: /status /stop")

    log("Scanner actif")
    while state["running"]:
        try:
            scan()
            time.sleep(POLL_INTERVAL_SEC)
        except KeyboardInterrupt:
            log("Arret manuel")
            telegram("Scanner arrete (Ctrl+C).")
            save_state()
            break
        except Exception as e:
            log(f"Erreur boucle: {e}", "ERROR")
            time.sleep(30)
            ensure_connected()
