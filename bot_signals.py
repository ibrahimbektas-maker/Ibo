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
                         f"Derniere mean-rev : {state.get('last_mr_alert_iso') or 'aucune'}",
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
        log(f"Nouveau jour : {today}, compteur alertes reset")


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
    sig_mr = detect_mean_rev(f)
    if sig_mr is not None:
        mins = minutes_since(state.get("last_mr_alert_iso"))
        if mins is None or mins >= ALERT_COOLDOWN_MIN:
            send_signal("MEAN-REV", sig_mr, f)
        else:
            log(f"Mean-rev {sig_mr} mais cooldown ({mins:.1f}/{ALERT_COOLDOWN_MIN}min)")

    # Trend (avec cooldown)
    sig_tr = detect_trend(f)
    if sig_tr is not None:
        mins = minutes_since(state.get("last_trend_alert_iso"))
        if mins is None or mins >= ALERT_COOLDOWN_MIN:
            send_signal("TREND", sig_tr, f)
        else:
            log(f"Trend {sig_tr} mais cooldown ({mins:.1f}/{ALERT_COOLDOWN_MIN}min)")


# ─────────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 70)
    print(" BOT SIGNALS GOLD  --  mode push, AUCUNE execution")
    print(f" Fenetre scan : {WINDOW_START_HOUR_UTC}h-{WINDOW_END_HOUR_UTC}h UTC")
    print(f" Mean-rev : ecart >= {DEV_THRESHOLD}pts ET MA plate (<= {SLOPE_FLAT_MAX})")
    print(f" Trend    : pente MA90 >= {TREND_SLOPE_MIN}pts sur {SLOPE_LOOKBACK} barres")
    print(f" Anti-spam: cooldown {ALERT_COOLDOWN_MIN}min, max {ALERTS_PER_DAY}/jour")
    print("=" * 70)

    load_state()
    if not connect():
        print("ERREUR connexion."); exit(1)

    telegram("SCANNER SIGNAUX GOLD demarre.\n"
             f"Fenetre {WINDOW_START_HOUR_UTC}-{WINDOW_END_HOUR_UTC}h UTC | "
             f"max {ALERTS_PER_DAY} alertes/j | cooldown {ALERT_COOLDOWN_MIN}min\n"
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
