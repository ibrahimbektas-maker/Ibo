"""
BOT GOLD MEAN-REVERSION v3.2 -- Patch SL interne + sizing dynamique
====================================================================

CHANGELOG vs v3.1
-----------------
FIX 1 (CRITIQUE) - SL interne fermeture manuelle
  Bug v3.1 : DELETE /positions/{dealId} renvoie HTTP 404 4 minutes apres
  l'ouverture (dealId d'affectedDeals[0] devient invalide en mode hedging
  avec stop garanti). Resultat : SL interne touche a -8pts mais ferme a
  -27pts (3.4x la perte prevue) car le filet broker prend le relais tard.

  Fix : avant chaque tentative de close, on rafraichit GET /positions et
  on matche par symbol + direction + open_price (tolerance 1pt). On utilise
  ce dealId frais pour DELETE. Plus de fallback "ordre inverse" qui en
  hedging ouvre une 2eme position au lieu de fermer.

FIX 2 - Bug f-string Telegram (/status, daily_report)
  Bug v3.1 : Python parsait `f"A" f"B" if cond else ""` comme
  `(f"A" f"B") if cond else ""` -> si balance est defini, on perdait
  tout le reste du message. Reportings tronques.

  Fix : separer les morceaux avec `+` explicites et variables intermediaires.

NEW 1 - Sizing dynamique
  Avant : LOT_SIZE fixe. Avec 160 EUR de capital, 0.5 lot = 4 EUR/SL = 2.5%.
  Si capital baisse, risque % augmente mecaniquement.

  Apres : lot calcule a chaque trade en fonction du capital courant et
  du % de risque cible (RISK_PCT_PER_TRADE).

NEW 2 - Limites de securite en %
  Avant : MAX_DAILY_LOSS = 30 EUR (= 18.75% avec 160 EUR -> trop agressif).
  Apres : tout en % du capital initial.

NEW 3 - Stop equite global
  Si le compte chute de MAX_TOTAL_DD_PCT depuis START_BALANCE, bot s'arrete
  definitivement. Eviter le mode "death spiral".

NEW 4 - Filtre regime F3 + TP=7
  Le signal d'origine (ecart fixe) fade des tendances (PF ~1.11, drawdown -237).
  Ajout d'un filtre : ne trader que si la MA90 est ~plate (|pente sur 30 barres|
  <= SLOPE_MAX). Backtest 30j : PF 1.11 -> 1.43, drawdown -237 -> -64 pts.
  TP passe de 14 a 7 (strategie B, meilleur couple avec le filtre).
  ATTENTION : valide sur 1 mois de backtest seulement, jamais en forward-test.

FIX 3 + FIX 4 (patch suivi de position)
  - get_positions() renvoie None (et non []) sur erreur/timeout/401, pour ne
    plus confondre "erreur API" avec "position fermee" (le bot oubliait sa
    position et arretait le SL interne). Les callers distinguent None de [].
  - PnL calcule sur la variation reelle du solde (balance_at_open) au lieu du
    prix poll, pour que les limites de securite soient exactes.
"""

import requests
import time
import schedule
import pandas as pd
from datetime import datetime, date
import json
import os

# ─────────────────────────────────────────────
# CHARGEMENT .env
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
                key, _, value = line.partition("=")
                env[key.strip()] = value.strip()
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
# STRATEGIE
# ─────────────────────────────────────────────
MA_PERIOD       = 90
DEVIATION_PTS   = 5.0
SL_POINTS       = 8.0
TP_POINTS       = 7.0    # strategie B validee (etait 14 ; 7 = meilleur PF avec filtre F3)
SL_BROKER_PTS   = 50.0   # filet broker, ajuste auto au minimum requis
SL_SAFETY_MARGIN = 2.0
SL_FALLBACK_PTS = 60.0
USE_GUARANTEED_STOP = True
COOLDOWN_BARS   = 10
CANDLES_NEEDED  = 150

# Filtre F3 (regime "MA plate") : ne fader que si la MA90 est ~horizontale,
# pour ne PAS trader contre une tendance. Valide en backtest (PF 1.11 -> 1.43,
# drawdown -237 -> -64). SLOPE_MAX plus petit = plus strict = moins de trades
# mais meilleur PF (1 -> PF 1.57 ; 3 -> PF 1.43). 999 = filtre desactive.
SLOPE_LOOKBACK  = 30
SLOPE_MAX       = 3.0

# ─────────────────────────────────────────────
# SIZING DYNAMIQUE
# ─────────────────────────────────────────────
RISK_PCT_PER_TRADE = 1.0    # 1% du capital par trade
MIN_LOT            = 0.01   # min Capital.com pour gold
MAX_LOT            = 5.0    # plafond de securite
RAMP_UP_DAYS       = 3      # jours en mode "lot minimal" pour valider en live
RAMP_UP_LOT        = 0.01

# ─────────────────────────────────────────────
# FENETRE HORAIRE
# ─────────────────────────────────────────────
TRADING_HOUR_START_UTC = 7
TRADING_HOUR_END_UTC   = 15

NEWS_BLOCK_TIMES_UTC = [(8, 0), (9, 0), (12, 30)]
NEWS_BLOCK_MINUTES   = 5

# ─────────────────────────────────────────────
# LIMITES SECURITE (toutes en % du capital initial)
# ─────────────────────────────────────────────
MAX_DAILY_TRADES        = 10
MAX_DAILY_LOSS_PCT      = 5.0    # -5% du capital initial sur une journee
MAX_WEEKLY_LOSS_PCT     = 15.0
MAX_CONSECUTIVE_LOSSES  = 3
MAX_TOTAL_DD_PCT        = 25.0   # arret definitif si compte < 75% du depart

# ─────────────────────────────────────────────
# CLOSE
# ─────────────────────────────────────────────
INTERNAL_SL_RETRIES     = 5
INTERNAL_SL_RETRY_DELAY = 2
DEAL_OPEN_SETTLE_DELAY  = 5       # secondes apres ouverture avant 1er close possible
POSITION_MATCH_TOLERANCE = 5.0    # pts de tolerance pour matcher la position par prix (gere le slippage broker)

LOG_FILE   = "bot_meanrev_v3_2_log.txt"
STATE_FILE = "bot_meanrev_v3_2_state.json"

# ─────────────────────────────────────────────
# ETAT
# ─────────────────────────────────────────────
state = {
    "running":               True,
    "connected":             False,
    "current_position":      None,
    "position_id":           None,
    "deal_reference":        None,
    "entry_price":           None,
    "entry_time":            None,
    "open_timestamp":        None,
    "current_lot":           None,
    "balance_at_open":       None,
    "sl_internal_price":     None,
    "sl_broker_price":       None,
    "daily_pnl":             0.0,
    "weekly_pnl":            0.0,
    "daily_trades":          0,
    "daily_wins":            0,
    "daily_losses":          0,
    "consecutive_losses":    0,
    "total_trades":          0,
    "total_pnl":             0.0,
    "start_balance":         None,
    "first_run_date":        None,
    "last_trade_day":        None,
    "last_trade_week":       None,
    "paused_by_safety":      False,
    "pause_reason":          "",
    "last_trade_close_time": None,
}

headers = {
    "X-CAP-API-KEY": API_KEY,
    "Content-Type":  "application/json"
}

trade_log      = []
last_update_id = 0


# ─────────────────────────────────────────────
# UTILITAIRES
# ─────────────────────────────────────────────
def now_str():
    return datetime.now().strftime("%H:%M:%S")


def now_utc_hour():
    return datetime.utcnow().hour


def is_news_window():
    now_utc = datetime.utcnow()
    now_minutes = now_utc.hour * 60 + now_utc.minute
    for h, m in NEWS_BLOCK_TIMES_UTC:
        if abs(now_minutes - (h * 60 + m)) <= NEWS_BLOCK_MINUTES:
            return True, f"{h:02d}h{m:02d} UTC"
    return False, ""


def today_str():
    return date.today().strftime("%d/%m/%Y")


def current_week_id():
    return datetime.now().strftime("%Y-W%U")


def log(msg, level="INFO"):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] [{level}] {msg}"
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
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
            for k, v in saved.items():
                if k in state:
                    state[k] = v
            log("Etat precedent restaure")
    except Exception as e:
        log(f"Erreur chargement: {e}", "ERROR")


def reset_position_state():
    state["current_position"]   = None
    state["position_id"]        = None
    state["deal_reference"]     = None
    state["entry_price"]        = None
    state["entry_time"]         = None
    state["open_timestamp"]     = None
    state["current_lot"]        = None
    state["balance_at_open"]    = None
    state["sl_internal_price"]  = None
    state["sl_broker_price"]    = None
    state["last_trade_close_time"] = datetime.now().isoformat()


def is_in_cooldown():
    last = state.get("last_trade_close_time")
    if not last:
        return False
    try:
        last_dt = datetime.fromisoformat(last)
    except (ValueError, TypeError):
        return False
    elapsed = (datetime.now() - last_dt).total_seconds()
    return elapsed < COOLDOWN_BARS * 60


def days_since_first_run():
    first = state.get("first_run_date")
    if not first:
        return 0
    try:
        first_dt = date.fromisoformat(first)
    except (ValueError, TypeError):
        return 0
    return (date.today() - first_dt).days


# ─────────────────────────────────────────────
# TELEGRAM (f-string bug fixe : on concatene avec +)
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
        for update in r.json().get("result", []):
            last_update_id = max(last_update_id, update.get("update_id", 0))
            msg = update.get("message", {}).get("text", "")

            if msg == "/status":
                pos = state["current_position"] or "Aucune"
                in_window = TRADING_HOUR_START_UTC <= now_utc_hour() < TRADING_HOUR_END_UTC
                balance = get_balance()

                lines = ["STATUS BOT MEAN-REV v3.2"]
                if balance is not None:
                    lines.append(f"Solde: {balance:.2f} EUR")
                    if state["start_balance"]:
                        dd_pct = (balance - state["start_balance"]) / state["start_balance"] * 100
                        lines.append(f"P&L cumul: {dd_pct:+.2f}% (start {state['start_balance']:.2f})")
                lines.append(f"Position: {pos}")
                lines.append(f"Heure UTC: {datetime.utcnow().strftime('%H:%M')} "
                             f"({'TRADING' if in_window else 'HORS FENETRE'})")
                lines.append(f"PnL jour: {state['daily_pnl']:.2f} EUR")
                lines.append(f"PnL semaine: {state['weekly_pnl']:.2f} EUR")
                lines.append(f"Trades jour: {state['daily_trades']}/{MAX_DAILY_TRADES}")
                lines.append(f"Pertes consec: {state['consecutive_losses']}/{MAX_CONSECUTIVE_LOSSES}")
                if state["paused_by_safety"]:
                    lines.append(f"PAUSE: {state['pause_reason']}")
                telegram("\n".join(lines))

            elif msg == "/stop":
                state["running"] = False
                telegram("Bot arrete manuellement.")

            elif msg == "/emergency":
                emergency_close()

            elif msg == "/resume":
                if state["paused_by_safety"]:
                    state["paused_by_safety"] = False
                    state["pause_reason"]     = ""
                    state["consecutive_losses"] = 0
                    telegram("Bot reactive.")
                else:
                    telegram("Bot deja actif.")

            elif msg == "/trades":
                if not trade_log:
                    telegram("Aucun trade aujourd'hui.")
                else:
                    lines = ["TRADES DU JOUR"]
                    for t in trade_log[-10:]:
                        lines.append(f"{t['time']} | {t['direction']} | {t['reason']} | {t['pnl']:.2f} EUR")
                    telegram("\n".join(lines))
    except Exception:
        pass


def emergency_close():
    log("EMERGENCY : fermeture immediate", "WARN")
    if state["current_position"]:
        # Refresh + DELETE pour fermer proprement
        fresh_id = find_position_id(state["current_position"], state["entry_price"])
        if fresh_id:
            close_position_by_delete(fresh_id)
        reset_position_state()
    state["running"] = False
    telegram("EMERGENCY: positions fermees, bot arrete.")
    save_state()


# ─────────────────────────────────────────────
# CONNEXION
# ─────────────────────────────────────────────
def connect():
    log("Connexion a Capital.com...")
    try:
        r = requests.post(
            BASE_URL + "/session", headers=headers,
            json={"identifier": ACCOUNT_ID, "password": API_SECRET}, timeout=15
        )
        if r.status_code == 200:
            headers["X-SECURITY-TOKEN"] = r.headers.get("X-SECURITY-TOKEN", "")
            headers["CST"]              = r.headers.get("CST", "")
            state["connected"] = True
            log("Connexion reussie!")
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


# ─────────────────────────────────────────────
# DONNEES MARCHE
# ─────────────────────────────────────────────
def get_minute_data():
    try:
        r = requests.get(
            BASE_URL + "/prices/" + EPIC, headers=headers,
            params={"resolution": "MINUTE", "max": CANDLES_NEEDED}, timeout=15
        )
        if r.status_code == 200:
            prices = r.json().get("prices", [])
            if not prices:
                return None
            df = pd.DataFrame(prices)
            df["close"] = pd.to_numeric(df["closePrice"].apply(lambda x: x["bid"]))
            df["high"]  = pd.to_numeric(df["highPrice"].apply(lambda x:  x["bid"]))
            df["low"]   = pd.to_numeric(df["lowPrice"].apply(lambda x:   x["bid"]))
            df["open"]  = pd.to_numeric(df["openPrice"].apply(lambda x:  x["bid"]))
            return df
        elif r.status_code == 401:
            state["connected"] = False
            return None
        return None
    except Exception as e:
        log(f"Erreur get_minute_data: {e}", "ERROR")
        return None


def get_balance():
    try:
        r = requests.get(BASE_URL + "/accounts", headers=headers, timeout=15)
        if r.status_code == 200:
            accounts = r.json().get("accounts", [])
            if accounts:
                return float(accounts[0]["balance"]["balance"])
        return None
    except Exception as e:
        log(f"Erreur balance: {e}", "ERROR")
        return None


def get_positions():
    """Retourne la liste des positions, ou None si l'appel a ECHOUE.
    None != [] : [] = aucune position ouverte (info fiable),
    None = appel impossible (timeout/401/erreur) -> on ne doit RIEN conclure.
    """
    try:
        r = requests.get(BASE_URL + "/positions", headers=headers, timeout=15)
        if r.status_code == 200:
            return r.json().get("positions", [])
        if r.status_code == 401:
            state["connected"] = False
        log(f"get_positions HTTP {r.status_code}", "WARN")
        return None
    except Exception as e:
        log(f"Erreur positions: {e}", "ERROR")
        return None


# ─────────────────────────────────────────────
# DISTANCE SL MINIMALE
# ─────────────────────────────────────────────
def get_min_stop_distance(guaranteed=True):
    try:
        r = requests.get(BASE_URL + "/markets/" + EPIC, headers=headers, timeout=10)
        if r.status_code != 200:
            return SL_FALLBACK_PTS
        data  = r.json()
        rules = data.get("dealingRules", {})
        key   = "minControlledRiskStopDistance" if guaranteed else "minStopOrProfitDistance"
        rule  = rules.get(key, {}) or rules.get("minStopOrProfitDistance", {})
        unit  = rule.get("unit", "POINTS")
        value = float(rule.get("value", SL_FALLBACK_PTS))
        if unit == "POINTS":
            return value
        if unit == "PERCENTAGE":
            snap = data.get("snapshot", {})
            mid  = (float(snap.get("bid", 0)) + float(snap.get("offer", 0))) / 2
            if mid > 0:
                return mid * value / 100.0
        return SL_FALLBACK_PTS
    except Exception as e:
        log(f"Erreur get_min_stop_distance: {e}, fallback {SL_FALLBACK_PTS}", "WARN")
        return SL_FALLBACK_PTS


# ─────────────────────────────────────────────
# DEAL CONFIRMATION
# ─────────────────────────────────────────────
def get_deal_id_from_reference(deal_reference):
    try:
        time.sleep(1)
        r = requests.get(BASE_URL + "/confirms/" + deal_reference, headers=headers, timeout=15)
        if r.status_code != 200:
            return None
        data = r.json()
        if data.get("dealStatus", "") != "ACCEPTED":
            log(f"Deal non accepte: {data}", "WARN")
            return None
        affected = data.get("affectedDeals", [])
        if affected and isinstance(affected, list):
            first = affected[0]
            if isinstance(first, dict) and first.get("dealId"):
                return first["dealId"]
        return data.get("dealId")
    except Exception as e:
        log(f"Erreur get_deal_id_from_reference: {e}", "ERROR")
        return None


# ─────────────────────────────────────────────
# CLOSE (FIX CRITIQUE v3.2)
# ─────────────────────────────────────────────
def find_position_id(direction, entry_price):
    """
    Cherche dans /positions courant la position qui correspond a notre trade.
    Retourne le dealId frais (utilisable pour DELETE), ou None si introuvable.

    Match strict : epic + direction + open_level (a +/- POSITION_MATCH_TOLERANCE pts).
    Capital.com retourne 404 sur le dealId stocke a l'ouverture (en mode hedging
    avec stop garanti, ce dealId devient invalide quelques minutes plus tard).
    Il faut rafraichir avant chaque close.
    """
    positions = get_positions()
    if not positions:  # None (erreur API) ou [] (aucune position) -> introuvable
        return None
    api_dir = "BUY" if direction == "LONG" else "SELL"
    strict_candidates = []
    loose_candidates  = []
    for p in positions:
        market   = p.get("market", {})
        pos_data = p.get("position", {})
        if market.get("epic") != EPIC:
            continue
        if pos_data.get("direction") != api_dir:
            continue
        open_level = float(pos_data.get("level", 0))
        deal_id = pos_data.get("dealId")
        if not deal_id:
            continue
        loose_candidates.append((deal_id, open_level))
        if abs(open_level - entry_price) <= POSITION_MATCH_TOLERANCE:
            strict_candidates.append((deal_id, open_level))
    if strict_candidates:
        strict_candidates.sort(key=lambda c: abs(c[1] - entry_price))
        return strict_candidates[0][0]
    # Fallback : si une seule position avec bonne epic+direction, on la prend
    # (slippage broker > tolerance, mais pas d'ambiguite possible)
    if len(loose_candidates) == 1:
        log(f"Match large (slippage > {POSITION_MATCH_TOLERANCE}pts): "
            f"level={loose_candidates[0][1]:.2f} vs entry={entry_price:.2f}", "WARN")
        return loose_candidates[0][0]
    return None


def close_position_by_delete(deal_id):
    """Tentative simple DELETE, retourne True/False."""
    if not deal_id:
        return False
    try:
        r = requests.delete(BASE_URL + "/positions/" + deal_id, headers=headers, timeout=15)
        if r.status_code in [200, 201]:
            log(f"Position {deal_id[:12]}... fermee via DELETE")
            return True
        log(f"DELETE echoue ({r.status_code}): {r.text[:150]}", "WARN")
        return False
    except Exception as e:
        log(f"Erreur DELETE: {e}", "ERROR")
        return False


def close_position_manual(direction, entry_price):
    """
    Ferme la position en mode robuste pour hedging :
      1. Attend que la position soit "settle" cote broker (si trop tot apres open)
      2. A chaque tentative : refresh GET /positions, retrouve le vrai dealId
      3. DELETE avec ce dealId
      4. Si tous les retries echouent, on ne tente PAS d'ordre inverse (qui en
         hedging ouvre une 2eme position au lieu de fermer)
    """
    # Attente "settle" si on est trop tot apres ouverture
    open_ts = state.get("open_timestamp")
    if open_ts:
        try:
            open_dt = datetime.fromisoformat(open_ts)
            elapsed = (datetime.now() - open_dt).total_seconds()
            if elapsed < DEAL_OPEN_SETTLE_DELAY:
                wait = DEAL_OPEN_SETTLE_DELAY - elapsed
                log(f"Attente settle broker : {wait:.1f}s")
                time.sleep(wait)
        except (ValueError, TypeError):
            pass

    for attempt in range(1, INTERNAL_SL_RETRIES + 1):
        log(f"Fermeture manuelle tentative {attempt}/{INTERNAL_SL_RETRIES}")
        fresh_id = find_position_id(direction, entry_price)
        if not fresh_id:
            log("Position introuvable dans /positions (peut-etre deja fermee broker?)", "WARN")
            # On ne conclut "fermee" QUE si l'appel a reussi ET renvoie 0 position EPIC.
            # Si get_positions() renvoie None (erreur/timeout), on NE conclut RIEN -> retry.
            positions = get_positions()
            if positions is None:
                log("get_positions indisponible -> on ne conclut pas a une fermeture", "WARN")
            elif not [p for p in positions if p.get("market", {}).get("epic") == EPIC]:
                log("Aucune position EPIC ouverte -> consideree fermee", "INFO")
                return True
        else:
            log(f"DealId frais: {fresh_id[:16]}...")
            if close_position_by_delete(fresh_id):
                time.sleep(2)
                return True
        if attempt < INTERNAL_SL_RETRIES:
            time.sleep(INTERNAL_SL_RETRY_DELAY)

    log("Fermeture manuelle ABANDONNEE -- on laisse le filet broker", "ERROR")
    telegram(
        "ALERTE: SL interne fermeture manuelle echouee apres "
        f"{INTERNAL_SL_RETRIES} retries.\n"
        f"Le filet broker (-{SL_BROKER_PTS:.0f}pts) reste actif. "
        "VERIFIER LA POSITION DANS L'APP."
    )
    return False


# ─────────────────────────────────────────────
# DETECTION MEAN-REVERSION
# ─────────────────────────────────────────────
def detect_mean_reversion_signal(df):
    if len(df) < MA_PERIOD + SLOPE_LOOKBACK:
        return None, 0.0
    price_now = df["close"].iloc[-1]
    ma = df["close"].iloc[-MA_PERIOD:].mean()
    deviation = price_now - ma

    # Filtre F3 : pente de la MA = MA actuelle - MA il y a SLOPE_LOOKBACK barres.
    # On ne trade que si |pente| <= SLOPE_MAX (marche en range, pas en tendance).
    ma_past = df["close"].iloc[-(MA_PERIOD + SLOPE_LOOKBACK):-SLOPE_LOOKBACK].mean()
    slope = ma - ma_past

    log(f"Prix: {price_now:.2f} | MA{MA_PERIOD}: {ma:.2f} | Ecart: {deviation:+.2f}pts "
        f"(seuil: +/-{DEVIATION_PTS}) | Pente: {slope:+.2f}pts/{SLOPE_LOOKBACK}b "
        f"(max plat: {SLOPE_MAX})")

    if abs(slope) > SLOPE_MAX:
        log(f"Signal ignore -- MA en tendance (|pente| {abs(slope):.2f} > {SLOPE_MAX}pts)")
        return None, 0.0

    if deviation > DEVIATION_PTS:
        return "SELL", abs(deviation)
    if deviation < -DEVIATION_PTS:
        return "BUY", abs(deviation)
    return None, 0.0


# ─────────────────────────────────────────────
# SIZING DYNAMIQUE
# ─────────────────────────────────────────────
def compute_lot_size(balance):
    """
    Lot tel que perte au SL_POINTS = balance * RISK_PCT_PER_TRADE/100.
    Mode ramp-up : pendant les RAMP_UP_DAYS premiers jours, lot = RAMP_UP_LOT
    (minimum broker) pour valider en live a risque tres reduit.
    """
    if days_since_first_run() < RAMP_UP_DAYS:
        log(f"Ramp-up actif (jour {days_since_first_run()}/{RAMP_UP_DAYS}) -> lot {RAMP_UP_LOT}")
        return RAMP_UP_LOT

    risk_eur = balance * RISK_PCT_PER_TRADE / 100.0
    lot = risk_eur / SL_POINTS
    lot = round(lot, 2)
    lot = max(MIN_LOT, min(MAX_LOT, lot))
    return lot


# ─────────────────────────────────────────────
# LIMITES SECURITE
# ─────────────────────────────────────────────
def reset_daily_if_new_day():
    today = date.today().isoformat()
    if state["last_trade_day"] != today:
        state["daily_pnl"]      = 0.0
        state["daily_trades"]   = 0
        state["daily_wins"]     = 0
        state["daily_losses"]   = 0
        state["last_trade_day"] = today
        log(f"Nouveau jour : {today}, compteurs daily reset")


def reset_weekly_if_new_week():
    week = current_week_id()
    if state["last_trade_week"] != week:
        state["weekly_pnl"]      = 0.0
        state["last_trade_week"] = week
        log(f"Nouvelle semaine : {week}, compteur weekly reset")


def check_safety_limits():
    if state["paused_by_safety"]:
        return False

    balance = get_balance()
    start = state.get("start_balance")

    # Stop equite global
    if balance is not None and start:
        total_dd_pct = (balance - start) / start * 100
        if total_dd_pct <= -MAX_TOTAL_DD_PCT:
            msg = (f"STOP TOTAL DD: {total_dd_pct:.2f}% "
                   f"(limite -{MAX_TOTAL_DD_PCT}%, solde {balance:.2f}/{start:.2f})")
            log(msg, "WARN")
            telegram(msg + "\nBot arrete pour proteger le capital.")
            state["paused_by_safety"] = True
            state["pause_reason"]     = "max_total_drawdown"
            return False

    if state["daily_trades"] >= MAX_DAILY_TRADES:
        return False

    if start:
        if state["daily_pnl"] <= -start * MAX_DAILY_LOSS_PCT / 100:
            msg = (f"STOP DAILY: {state['daily_pnl']:.2f} EUR "
                   f"(limite -{MAX_DAILY_LOSS_PCT}% = -{start * MAX_DAILY_LOSS_PCT / 100:.2f})")
            log(msg, "WARN"); telegram(msg)
            state["paused_by_safety"] = True
            state["pause_reason"]     = "daily_loss"
            return False

        if state["weekly_pnl"] <= -start * MAX_WEEKLY_LOSS_PCT / 100:
            msg = (f"STOP WEEKLY: {state['weekly_pnl']:.2f} EUR "
                   f"(limite -{MAX_WEEKLY_LOSS_PCT}% = -{start * MAX_WEEKLY_LOSS_PCT / 100:.2f})")
            log(msg, "WARN"); telegram(msg)
            state["paused_by_safety"] = True
            state["pause_reason"]     = "weekly_loss"
            return False

    if state["consecutive_losses"] >= MAX_CONSECUTIVE_LOSSES:
        msg = f"STOP: {state['consecutive_losses']} pertes consecutives"
        log(msg, "WARN")
        telegram(msg + "\nUtilise /resume pour reactiver.")
        state["paused_by_safety"] = True
        state["pause_reason"]     = "consecutive_losses"
        return False

    return True


# ─────────────────────────────────────────────
# ENREGISTREMENT TRADE
# ─────────────────────────────────────────────
def realized_pnl(direction, entry, exit_price, lot_used):
    """PnL REEL via la variation de solde (capture frais, swap, slippage broker).
    Comme on n'a qu'une position a la fois, balance_now - balance_at_open = PnL realise.
    Fallback sur l'estimation par prix (poll) si le solde est indisponible."""
    bal = get_balance()
    bo  = state.get("balance_at_open")
    if bal is not None and bo is not None:
        return bal - bo
    price_diff = exit_price - entry if direction == "LONG" else entry - exit_price
    log("Solde indisponible -> PnL estime par prix (approximatif)", "WARN")
    return price_diff * lot_used


def record_trade(direction, entry, exit_price, pnl, reason, lot_used):
    state["daily_pnl"]    += pnl
    state["weekly_pnl"]   += pnl
    state["total_pnl"]    += pnl
    state["daily_trades"] += 1
    state["total_trades"] += 1

    if pnl > 0:
        state["daily_wins"]         += 1
        state["consecutive_losses"]  = 0
        result = "WIN"
    else:
        state["daily_losses"]       += 1
        state["consecutive_losses"] += 1
        result = "LOSS"

    trade_log.append({
        "time": now_str(), "direction": direction,
        "entry": entry, "exit": exit_price,
        "pnl": round(pnl, 2), "result": result, "reason": reason, "lot": lot_used
    })

    msg = (
        f"POSITION FERMEE [{reason}]\n"
        f"Direction: {direction} ({lot_used} lot)\n"
        f"Entree: {entry:.2f} | Sortie: {exit_price:.2f}\n"
        f"Resultat: {result} | PnL: {pnl:.2f} EUR\n"
        f"PnL jour: {state['daily_pnl']:.2f}\n"
        f"Pertes consec: {state['consecutive_losses']}/{MAX_CONSECUTIVE_LOSSES}"
    )
    log(msg); telegram(msg)


# ─────────────────────────────────────────────
# GESTION POSITION OUVERTE
# ─────────────────────────────────────────────
def manage_open_position(current_price):
    entry       = state["entry_price"]
    pos         = state["current_position"]
    sl_internal = state.get("sl_internal_price")
    lot_used    = state.get("current_lot") or 0

    positions = get_positions()
    if positions is None:
        # Erreur API/timeout : on ne sait PAS si la position est ouverte ou fermee.
        # On ne touche a rien ce tour (sinon on "oublie" une position bien reelle).
        log("get_positions indisponible -> position laissee en l'etat ce tour", "WARN")
        return
    gold_pos = [p for p in positions if p.get("market", {}).get("epic") == EPIC]

    # Cas 1 : position fermee par Capital.com (TP / SL filet / manuel)
    if not gold_pos:
        pnl = realized_pnl(pos, entry, current_price, lot_used)
        record_trade(pos, entry, current_price, pnl, "BROKER", lot_used)
        reset_position_state()
        save_state()
        return

    if sl_internal is None:
        return

    sl_hit = (current_price <= sl_internal) if pos == "LONG" else (current_price >= sl_internal)
    if not sl_hit:
        return

    log(f"SL INTERNE TOUCHE : prix={current_price:.2f}, sl_interne={sl_internal:.2f}, "
        f"position {pos}", "WARN")
    success = close_position_manual(pos, entry)

    if success:
        # Le close a reussi, ou le broker avait deja ferme.
        pnl = realized_pnl(pos, entry, current_price, lot_used)
        record_trade(pos, entry, current_price, pnl, "SL_INTERNE", lot_used)
        reset_position_state()
        save_state()
    else:
        # Echec total : on reste en pause de securite, le filet broker prendra le relais
        state["paused_by_safety"] = True
        state["pause_reason"]     = "sl_interne_close_echec"
        save_state()
        telegram(
            "BOT EN PAUSE : SL interne non ferme.\n"
            f"Le filet broker -{SL_BROKER_PTS:.0f}pts reste actif.\n"
            "Une fois resolu, faire /resume sur Telegram."
        )


# ─────────────────────────────────────────────
# OUVERTURE POSITION
# ─────────────────────────────────────────────
def open_position(signal, price, pct):
    balance = get_balance()
    if balance is None:
        log("Impossible d'obtenir le solde, ouverture annulee", "ERROR")
        return

    lot_size = compute_lot_size(balance)
    log(f"Sizing: balance={balance:.2f}, risque={RISK_PCT_PER_TRADE}%, lot={lot_size}")

    min_dist = get_min_stop_distance(guaranteed=True)
    sl_broker_pts = max(SL_BROKER_PTS, min_dist + SL_SAFETY_MARGIN)
    if sl_broker_pts > SL_BROKER_PTS:
        log(f"SL filet ajuste: {SL_BROKER_PTS} -> {sl_broker_pts:.2f} pts "
            f"(min broker: {min_dist:.2f})")

    if signal == "BUY":
        sl_broker_price   = round(price - sl_broker_pts, 2)
        sl_internal_price = round(price - SL_POINTS, 2)
        take_profit       = round(price + TP_POINTS, 2)
        direction         = "LONG"
    else:
        sl_broker_price   = round(price + sl_broker_pts, 2)
        sl_internal_price = round(price + SL_POINTS, 2)
        take_profit       = round(price - TP_POINTS, 2)
        direction         = "SHORT"

    order = {
        "epic":           EPIC,
        "direction":      signal,
        "size":           lot_size,
        "guaranteedStop": USE_GUARANTEED_STOP,
        "stopLevel":      sl_broker_price,
        "profitLevel":    take_profit,
    }

    # Tentative ouverture avec auto-correction du SL si trop serre
    for attempt in range(3):
        try:
            r = requests.post(BASE_URL + "/positions", headers=headers, json=order, timeout=15)
            if r.status_code in [200, 201]:
                break

            err_text = r.text.lower()
            log(f"Erreur ordre (try {attempt+1}/3): {r.text[:200]}", "WARN")

            # Auto-correction si SL trop serre OU stop garanti requis avec distance plus grande
            if "stop" in err_text and attempt < 2:
                new_sl_pts = sl_broker_pts + 10
                if signal == "BUY":
                    sl_broker_price = round(price - new_sl_pts, 2)
                else:
                    sl_broker_price = round(price + new_sl_pts, 2)
                sl_broker_pts = new_sl_pts
                order["stopLevel"] = sl_broker_price
                log(f"Retry avec SL filet={sl_broker_price} (-{new_sl_pts}pts)")
                continue
            break
        except Exception as e:
            log(f"Exception place_order: {e}", "ERROR")
            return

    if r.status_code not in [200, 201]:
        telegram(f"ERREUR ORDRE apres retries: {r.text[:200]}")
        return

    deal_ref = r.json().get("dealReference")
    deal_id  = get_deal_id_from_reference(deal_ref)
    if not deal_id:
        log("Deal non confirme", "ERROR")
        telegram("ERREUR: Deal non confirme par Capital.com")
        return

    state["current_position"]   = direction
    state["position_id"]        = deal_id  # informatif, on rafraichit avant close
    state["deal_reference"]     = deal_ref
    state["entry_price"]        = price
    state["entry_time"]         = now_str()
    state["open_timestamp"]     = datetime.now().isoformat()
    state["current_lot"]        = lot_size
    state["balance_at_open"]    = balance
    state["sl_internal_price"]  = sl_internal_price
    state["sl_broker_price"]    = sl_broker_price

    risk_internal_eur = SL_POINTS * lot_size
    risk_filet_eur    = sl_broker_pts * lot_size
    reward_eur        = TP_POINTS * lot_size
    msg = (
        f"MEAN-REV {direction} [meanrev_v3.2]\n"
        f"Lot: {lot_size} | Solde: {balance:.2f} EUR\n"
        f"Ecart MA: {pct:+.2f}pts\n"
        f"Heure UTC: {datetime.utcnow().strftime('%H:%M')}\n"
        f"Prix entree: {price:.2f}\n"
        f"SL interne:  {sl_internal_price:.2f} (-{SL_POINTS:.0f}pts = -{risk_internal_eur:.2f}E) [bot]\n"
        f"SL filet:    {sl_broker_price:.2f} (-{sl_broker_pts:.0f}pts = -{risk_filet_eur:.2f}E) [broker]\n"
        f"Take Profit: {take_profit:.2f} (+{TP_POINTS:.0f}pts = +{reward_eur:.2f}E) [broker]\n"
        f"Trade #{state['daily_trades'] + 1}/{MAX_DAILY_TRADES} du jour"
    )
    log(msg); telegram(msg)
    save_state()


# ─────────────────────────────────────────────
# BOUCLE PRINCIPALE
# ─────────────────────────────────────────────
def analyse():
    if not state["running"]:
        return
    if not ensure_connected():
        return

    telegram_check_commands()
    reset_daily_if_new_day()
    reset_weekly_if_new_week()

    h = now_utc_hour()
    if not (TRADING_HOUR_START_UTC <= h < TRADING_HOUR_END_UTC):
        if state["current_position"] is not None:
            df = get_minute_data()
            if df is not None and len(df) > 0:
                manage_open_position(float(df["close"].iloc[-1]))
        return

    if not check_safety_limits():
        return

    df = get_minute_data()
    if df is None or len(df) < CANDLES_NEEDED - 10:
        log("Donnees insuffisantes")
        return

    current_price = float(df["close"].iloc[-1])

    if state["current_position"] is not None:
        manage_open_position(current_price)
        return

    signal, pct = detect_mean_reversion_signal(df)
    if signal is None:
        return

    is_news, news_label = is_news_window()
    if is_news:
        log(f"Signal {signal} ignore -- fenetre news ({news_label})")
        return

    if is_in_cooldown():
        log(f"Signal {signal} ignore -- en cooldown ({COOLDOWN_BARS}min)")
        return

    log(f">>> SIGNAL MEAN-REVERSION {signal} | Ecart {pct:+.2f}pts <<<")
    open_position(signal, current_price, pct)
    save_state()


def daily_report():
    winrate = 0
    if state["daily_trades"] > 0:
        winrate = state["daily_wins"] / state["daily_trades"] * 100
    balance = get_balance()

    lines = [f"RAPPORT JOURNALIER MEAN-REV v3.2 - {today_str()}"]
    if balance is not None:
        lines.append(f"Solde: {balance:.2f} EUR")
        if state["start_balance"]:
            dd = (balance - state["start_balance"]) / state["start_balance"] * 100
            lines.append(f"P&L cumul: {dd:+.2f}% depuis start")
    lines.append(f"Trades: {state['daily_trades']} | Wins: {state['daily_wins']} | "
                 f"Losses: {state['daily_losses']}")
    lines.append(f"Winrate: {winrate:.1f}%")
    lines.append(f"PnL jour: {state['daily_pnl']:.2f} EUR")
    lines.append(f"PnL semaine: {state['weekly_pnl']:.2f} EUR")
    lines.append(f"PnL total bot: {state['total_pnl']:.2f} EUR")
    msg = "\n".join(lines)
    log(msg); telegram(msg)
    trade_log.clear()


# ─────────────────────────────────────────────
# ENTREE
# ─────────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 70)
    print("   BOT GOLD MEAN-REV v3.2 -- Sizing dynamique + Fix SL interne")
    print(f"   Fenetre : {TRADING_HOUR_START_UTC}h-{TRADING_HOUR_END_UTC}h UTC")
    print(f"   Risque  : {RISK_PCT_PER_TRADE}% du capital par trade")
    print(f"   SL/TP   : {SL_POINTS}/{TP_POINTS} pts | stop "
          f"{'garanti' if USE_GUARANTEED_STOP else 'normal'}")
    print(f"   Ramp-up : {RAMP_UP_DAYS} premiers jours en lot {RAMP_UP_LOT}")
    print(f"   Securite: -{MAX_DAILY_LOSS_PCT}%/j | -{MAX_WEEKLY_LOSS_PCT}%/s | "
          f"DD max -{MAX_TOTAL_DD_PCT}%")
    print(f"   Filtre news : +/- {NEWS_BLOCK_MINUTES}min autour de "
          f"{', '.join(f'{h:02d}h{m:02d}' for h, m in NEWS_BLOCK_TIMES_UTC)} UTC")
    print("=" * 70)

    load_state()
    if not connect():
        print("ERREUR: Impossible de se connecter.")
        exit(1)

    balance = get_balance()
    if balance is None:
        print("ERREUR: Impossible d'obtenir le solde.")
        exit(1)

    # Premiere execution : on enregistre balance de depart + date
    if state.get("start_balance") is None:
        state["start_balance"] = balance
        log(f"Premier demarrage : start_balance = {balance:.2f} EUR")
    if state.get("first_run_date") is None:
        state["first_run_date"] = date.today().isoformat()
        log(f"first_run_date = {state['first_run_date']}")
    save_state()

    print(f"\nSolde actuel  : {balance:.2f} EUR")
    print(f"Solde start   : {state['start_balance']:.2f} EUR")
    if state["start_balance"]:
        total_pct = (balance - state["start_balance"]) / state["start_balance"] * 100
        print(f"P&L cumul     : {total_pct:+.2f}%")
    print(f"Jour de run   : {days_since_first_run()}/{RAMP_UP_DAYS} (ramp-up)")

    lines = [
        f"BOT MEAN-REV v3.2 DEMARRE",
        f"Solde: {balance:.2f} EUR (start {state['start_balance']:.2f})",
        f"Mode ramp-up: jour {days_since_first_run()}/{RAMP_UP_DAYS}",
        f"Strategie: MEAN-REVERSION",
        f"Fenetre  : {TRADING_HOUR_START_UTC}h-{TRADING_HOUR_END_UTC}h UTC",
        f"SL/TP    : {SL_POINTS}/{TP_POINTS} pts | risque {RISK_PCT_PER_TRADE}%/trade",
        f"Securite : -{MAX_DAILY_LOSS_PCT}%/j | -{MAX_WEEKLY_LOSS_PCT}%/s | DD -{MAX_TOTAL_DD_PCT}%",
        f"Commandes: /status /stop /resume /trades /emergency",
    ]
    telegram("\n".join(lines))

    schedule.every(30).seconds.do(analyse)
    schedule.every().day.at("22:00").do(daily_report)
    schedule.every(5).minutes.do(save_state)

    log("Bot MEAN-REV v3.2 actif")
    print("\nBot actif -- CTRL+C pour arreter")

    while True:
        try:
            schedule.run_pending()
            time.sleep(5)
        except KeyboardInterrupt:
            log("Arret manuel")
            telegram("Bot MEAN-REV v3.2 arrete manuellement.")
            save_state()
            break
        except Exception as e:
            log(f"Erreur: {e} -- Redemarrage dans 30s", "ERROR")
            telegram(f"ERREUR: {e}")
            time.sleep(30)
            ensure_connected()
