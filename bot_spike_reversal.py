import requests
import time
import schedule
import pandas as pd
from datetime import datetime, date
import json
import os

# ─────────────────────────────────────────────
# IDENTIFIANTS
# ─────────────────────────────────────────────
API_KEY        = "BUdgl9r8B1oh1g4E"
API_SECRET     = "Gusa1270"
ACCOUNT_ID     = "ibrahimbektas@live.fr"
TELEGRAM_TOKEN = "8612124775:AAHBXEbvNmT62CaMjxbbJEaTZv2KhOCjvmA"
TELEGRAM_CHAT  = "6346548096"
EPIC           = "GOLD"
BASE_URL       = "https://api-capital.backend-capital.com/api/v1"

# ─────────────────────────────────────────────
# PARAMÈTRES STRATÉGIE SPIKE REVERSAL
# ─────────────────────────────────────────────
SPIKE_PCT        = 0.3    # Seuil du spike en %
SPIKE_WINDOW     = 5      # Durée du spike en minutes
CALM_WINDOW      = 15     # Fenêtre calme avant spike en minutes
CALM_MAX_RANGE   = 8.0    # Range max pour marché calme (points)
RANGE_WINDOW     = 120    # Fenêtre range 2h en minutes (filtre cassure)
LOT_SIZE         = 1.0    # Taille fixe en lots
SL_POINTS        = 10.0   # Stop loss en points
TP_POINTS        = 5.0    # Take profit en points
MAX_DAILY_TRADES = 10     # Max trades par jour
MAX_DAILY_LOSS   = 60.0   # Perte max journalière en EUR
CANDLES_NEEDED   = 150    # Bougies 1min à récupérer

# ─────────────────────────────────────────────
# ÉTAT
# ─────────────────────────────────────────────
state = {
    "running":          True,
    "connected":        False,
    "current_position": None,
    "position_id":      None,
    "deal_reference":   None,
    "entry_price":      None,
    "entry_time":       None,
    "daily_pnl":        0.0,
    "daily_trades":     0,
    "daily_wins":       0,
    "daily_losses":     0,
    "total_trades":     0,
    "total_pnl":        0.0,
    "start_balance":    None,
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

def today_str():
    return date.today().strftime("%d/%m/%Y")

def log(msg, level="INFO"):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] [{level}] {msg}"
    print(line)
    try:
        with open("bot_spike_log.txt", "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass

def save_state():
    try:
        with open("bot_spike_state.json", "w", encoding="utf-8") as f:
            json.dump(state, f, default=str)
    except Exception as e:
        log(f"Erreur sauvegarde: {e}", "ERROR")

def load_state():
    try:
        if os.path.exists("bot_spike_state.json"):
            with open("bot_spike_state.json", "r", encoding="utf-8") as f:
                saved = json.load(f)
            for k, v in saved.items():
                if k in state:
                    state[k] = v
            log("Etat precedent restaure")
    except Exception as e:
        log(f"Erreur chargement: {e}", "ERROR")

def reset_position_state():
    state["current_position"] = None
    state["position_id"]      = None
    state["deal_reference"]   = None
    state["entry_price"]      = None
    state["entry_time"]       = None

# ─────────────────────────────────────────────
# TELEGRAM
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
        r   = requests.get(url, params={"timeout": 1, "offset": last_update_id + 1}, timeout=5)
        if r.status_code != 200:
            return
        for update in r.json().get("result", []):
            last_update_id = max(last_update_id, update.get("update_id", 0))
            msg = update.get("message", {}).get("text", "")
            if msg == "/status":
                pos = state["current_position"] or "Aucune"
                telegram(
                    f"STATUS SPIKE BOT\n"
                    f"Position: {pos}\n"
                    f"PnL jour: {state['daily_pnl']:.2f} EUR\n"
                    f"Trades: {state['daily_trades']}/{MAX_DAILY_TRADES}\n"
                    f"Bot actif: {state['running']}"
                )
            elif msg == "/stop":
                state["running"] = False
                telegram("Bot arrete manuellement.")
            elif msg == "/trades":
                if not trade_log:
                    telegram("Aucun trade aujourd'hui.")
                else:
                    lines = ["TRADES DU JOUR"]
                    for t in trade_log[-10:]:
                        lines.append(
                            f"{t['time']} | {t['direction']} | {t['reason']} | {t['pnl']:.2f} EUR"
                        )
                    telegram("\n".join(lines))
            elif msg == "/performance":
                winrate = 0
                if state["daily_trades"] > 0:
                    winrate = state["daily_wins"] / state["daily_trades"] * 100
                telegram(
                    f"PERFORMANCE\n"
                    f"Trades: {state['daily_trades']}/{MAX_DAILY_TRADES}\n"
                    f"Wins: {state['daily_wins']} | Losses: {state['daily_losses']}\n"
                    f"Winrate: {winrate:.1f}%\n"
                    f"PnL: {state['daily_pnl']:.2f} EUR"
                )
    except Exception:
        pass

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
# DONNÉES MARCHÉ
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
    try:
        r = requests.get(BASE_URL + "/positions", headers=headers, timeout=15)
        if r.status_code == 200:
            return r.json().get("positions", [])
        return []
    except Exception as e:
        log(f"Erreur positions: {e}", "ERROR")
        return []

# ─────────────────────────────────────────────
# DEAL CONFIRMATION
# ─────────────────────────────────────────────
def get_deal_id(deal_reference):
    try:
        time.sleep(1)
        r = requests.get(BASE_URL + "/confirms/" + deal_reference, headers=headers, timeout=15)
        if r.status_code == 200:
            data    = r.json()
            deal_id = data.get("dealId")
            status  = data.get("dealStatus", "")
            if deal_id and status == "ACCEPTED":
                log(f"Deal confirme: {deal_id}")
                return deal_id
            log(f"Deal non accepte: {data}", "WARN")
        return None
    except Exception as e:
        log(f"Erreur get_deal_id: {e}", "ERROR")
        return None

# ─────────────────────────────────────────────
# FERMETURE ROBUSTE
# ─────────────────────────────────────────────
def close_position(position_id, direction, size):
    if position_id:
        try:
            r = requests.delete(BASE_URL + "/positions/" + position_id,
                                headers=headers, timeout=15)
            if r.status_code in [200, 201]:
                log("Position fermee via DELETE")
                time.sleep(2)
                return True
            log(f"DELETE echoue ({r.status_code}): {r.text}", "WARN")
        except Exception as e:
            log(f"Erreur DELETE: {e}", "ERROR")

    close_dir = "SELL" if direction == "LONG" else "BUY"
    try:
        order = {"epic": EPIC, "direction": close_dir, "size": size, "orderType": "MARKET"}
        r2 = requests.post(BASE_URL + "/positions", headers=headers, json=order, timeout=15)
        if r2.status_code in [200, 201]:
            log("Position fermee via ordre inverse")
            time.sleep(2)
            return True
        log(f"Ordre inverse echoue ({r2.status_code}): {r2.text}", "WARN")
    except Exception as e:
        log(f"Erreur ordre inverse: {e}", "ERROR")
    return False

def confirm_position_closed():
    positions = get_positions()
    gold_pos  = [p for p in positions if p.get("market", {}).get("epic") == EPIC]
    return len(gold_pos) == 0

# ─────────────────────────────────────────────
# DÉTECTION SPIKE
# Spike = hausse ou baisse de ±SPIKE_PCT% en SPIKE_WINDOW minutes
# Spike haussier → SELL (on joue le retour)
# Spike baissier → BUY  (on joue le rebond)
# ─────────────────────────────────────────────
def detect_spike(df):
    price_now    = df["close"].iloc[-1]
    price_before = df["close"].iloc[-(SPIKE_WINDOW + 1)]
    pct          = (price_now - price_before) / price_before * 100

    log(f"Variation {SPIKE_WINDOW}min: {pct:+.3f}% | Prix: {price_now:.2f}")

    if pct >= SPIKE_PCT:
        return "SELL", pct
    if pct <= -SPIKE_PCT:
        return "BUY", abs(pct)
    return None, 0.0

# ─────────────────────────────────────────────
# FILTRE 1 — MARCHÉ CALME AVANT LE SPIKE
# Les CALM_WINDOW minutes qui précèdent le spike doivent être calmes
# ─────────────────────────────────────────────
def is_market_calm(df):
    # df.iloc[-(CALM_WINDOW+SPIKE_WINDOW+1):-(SPIKE_WINDOW+1)]
    # = les 15 minutes juste avant la fenêtre de spike
    calm      = df.iloc[-(CALM_WINDOW + SPIKE_WINDOW + 1):-(SPIKE_WINDOW + 1)]
    range_pts = calm["high"].max() - calm["low"].min()
    log(f"Range calme ({CALM_WINDOW}min avant spike): {range_pts:.2f} pts (max: {CALM_MAX_RANGE})")
    return range_pts < CALM_MAX_RANGE

# ─────────────────────────────────────────────
# FILTRE 2 — PAS DE CASSURE DE RANGE 2H
# Le prix après spike doit rester dans la range des 2h précédentes
# ─────────────────────────────────────────────
def no_breakout(df, signal):
    rng       = df.iloc[-(RANGE_WINDOW + SPIKE_WINDOW + 1):-(SPIKE_WINDOW + 1)]
    rng_high  = rng["high"].max()
    rng_low   = rng["low"].min()
    price_now = df["close"].iloc[-1]
    log(f"Range {RANGE_WINDOW}min: [{rng_low:.2f} - {rng_high:.2f}] | Prix: {price_now:.2f}")
    if signal == "SELL":
        return price_now <= rng_high
    return price_now >= rng_low

# ─────────────────────────────────────────────
# LIMITES JOURNALIÈRES
# ─────────────────────────────────────────────
def check_limits(balance):
    if state["start_balance"] is None:
        state["start_balance"] = balance
        return True

    if state["daily_trades"] >= MAX_DAILY_TRADES:
        log(f"Limite trades atteinte ({MAX_DAILY_TRADES}/jour)")
        return False

    if state["daily_pnl"] <= -MAX_DAILY_LOSS:
        msg = f"STOP: Perte journaliere {state['daily_pnl']:.2f} EUR (limite: {MAX_DAILY_LOSS} EUR)"
        log(msg, "WARN")
        telegram(msg)
        state["running"] = False
        return False

    return True

# ─────────────────────────────────────────────
# ENREGISTREMENT TRADE
# ─────────────────────────────────────────────
def record_trade(direction, entry, exit_price, pnl, reason):
    state["daily_pnl"]    += pnl
    state["total_pnl"]    += pnl
    state["daily_trades"] += 1
    state["total_trades"] += 1
    result = "WIN" if pnl > 0 else "LOSS"
    if pnl > 0:
        state["daily_wins"] += 1
    else:
        state["daily_losses"] += 1

    trade_log.append({
        "time": now_str(), "direction": direction,
        "entry": entry, "exit": exit_price,
        "pnl": round(pnl, 2), "result": result, "reason": reason
    })
    msg = (
        f"POSITION FERMEE [{reason}]\n"
        f"Direction: {direction}\n"
        f"Entree: {entry:.2f} | Sortie: {exit_price:.2f}\n"
        f"Resultat: {result} | PnL: {pnl:.2f} EUR\n"
        f"Trades jour: {state['daily_trades']}/{MAX_DAILY_TRADES}"
    )
    log(msg)
    telegram(msg)

# ─────────────────────────────────────────────
# GESTION POSITION OUVERTE
# Capital.com gère le SL/TP — on surveille juste la fermeture
# ─────────────────────────────────────────────
def manage_open_position(current_price):
    entry = state["entry_price"]
    pos   = state["current_position"]

    positions = get_positions()
    gold_pos  = [p for p in positions if p.get("market", {}).get("epic") == EPIC]

    if not gold_pos:
        pnl = current_price - entry if pos == "LONG" else entry - current_price
        record_trade(pos, entry, current_price, pnl, "SL/TP")
        reset_position_state()
        save_state()

# ─────────────────────────────────────────────
# OUVERTURE POSITION
# ─────────────────────────────────────────────
def open_position(signal, price, pct):
    if signal == "BUY":
        stop_loss   = round(price - SL_POINTS, 2)
        take_profit = round(price + TP_POINTS, 2)
        direction   = "LONG"
    else:
        stop_loss   = round(price + SL_POINTS, 2)
        take_profit = round(price - TP_POINTS, 2)
        direction   = "SHORT"

    try:
        order = {
            "epic":          EPIC,
            "direction":     signal,
            "size":          LOT_SIZE,
            "guaranteedStop": True,
            "stopLevel":     stop_loss,
            "profitLevel":   take_profit,
        }
        r = requests.post(BASE_URL + "/positions", headers=headers, json=order, timeout=15)
        if r.status_code not in [200, 201]:
            log(f"Erreur ordre: {r.text}", "ERROR")
            return
        deal_ref = r.json().get("dealReference")
    except Exception as e:
        log(f"Erreur place_order: {e}", "ERROR")
        return

    deal_id = get_deal_id(deal_ref)
    if not deal_id:
        log("Deal non confirme — position ignoree", "ERROR")
        telegram("ERREUR: Deal non confirme par Capital.com")
        return

    state["current_position"] = direction
    state["position_id"]      = deal_id
    state["deal_reference"]   = deal_ref
    state["entry_price"]      = price
    state["entry_time"]       = now_str()

    msg = (
        f"SPIKE REVERSAL [{direction}]\n"
        f"Spike: {pct:+.3f}% en {SPIKE_WINDOW}min\n"
        f"Prix entree:  {price:.2f}\n"
        f"Stop Loss:    {stop_loss:.2f} (-{SL_POINTS} pts)\n"
        f"Take Profit:  {take_profit:.2f} (+{TP_POINTS} pts)\n"
        f"Taille: {LOT_SIZE} lot\n"
        f"Trade #{state['daily_trades'] + 1}/{MAX_DAILY_TRADES}"
    )
    log(msg)
    telegram(msg)
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

    df = get_minute_data()
    if df is None or len(df) < CANDLES_NEEDED - 10:
        log("Donnees insuffisantes")
        return

    current_price = float(df["close"].iloc[-1])
    balance       = get_balance()
    if balance is None:
        return
    if not check_limits(balance):
        return

    if state["current_position"] is not None:
        manage_open_position(current_price)
        return

    signal, pct = detect_spike(df)
    if signal is None:
        return

    if not is_market_calm(df):
        log(f"Signal {signal} ignore — marche non calme avant le spike")
        return

    if not no_breakout(df, signal):
        log(f"Signal {signal} ignore — cassure de la range {RANGE_WINDOW}min")
        return

    log(f">>> SIGNAL {signal} | Spike {pct:+.3f}% en {SPIKE_WINDOW}min <<<")
    open_position(signal, current_price, pct)
    save_state()

def daily_report():
    winrate = 0
    if state["daily_trades"] > 0:
        winrate = state["daily_wins"] / state["daily_trades"] * 100
    msg = (
        f"RAPPORT JOURNALIER - {today_str()}\n"
        f"Trades: {state['daily_trades']} | Wins: {state['daily_wins']} | Losses: {state['daily_losses']}\n"
        f"Winrate: {winrate:.1f}%\n"
        f"PnL jour:  {state['daily_pnl']:.2f} EUR\n"
        f"PnL total: {state['total_pnl']:.2f} EUR"
    )
    log(msg)
    telegram(msg)
    state["daily_pnl"]     = 0.0
    state["daily_trades"]  = 0
    state["daily_wins"]    = 0
    state["daily_losses"]  = 0
    state["start_balance"] = None
    state["running"]       = True
    trade_log.clear()

# ─────────────────────────────────────────────
# ENTRÉE
# ─────────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 55)
    print("   BOT GOLD — SPIKE REVERSAL")
    print(f"   Seuil : ±{SPIKE_PCT}% en {SPIKE_WINDOW}min")
    print(f"   Filtres : calme {CALM_WINDOW}min + range {RANGE_WINDOW}min")
    print(f"   SL : {SL_POINTS} pts | TP : {TP_POINTS} pts | {LOT_SIZE} lot")
    print(f"   Max : {MAX_DAILY_TRADES} trades/jour | Perte max : {MAX_DAILY_LOSS} EUR")
    print("=" * 55)

    load_state()
    if not connect():
        print("ERREUR: Impossible de se connecter.")
        exit(1)

    telegram(
        f"BOT SPIKE REVERSAL DEMARRE\n"
        f"Strategie : retournement apres spike ±{SPIKE_PCT}%\n"
        f"Filtres   : marche calme {CALM_WINDOW}min + range {RANGE_WINDOW}min\n"
        f"SL : {SL_POINTS} pts | TP : {TP_POINTS} pts | {LOT_SIZE} lot\n"
        f"Limites   : {MAX_DAILY_TRADES} trades/jour | stop -{MAX_DAILY_LOSS} EUR\n"
        f"Commandes : /status /stop /trades /performance"
    )

    analyse()
    schedule.every(30).seconds.do(analyse)
    schedule.every().day.at("22:00").do(daily_report)
    schedule.every(5).minutes.do(save_state)

    log("Bot actif — analyse toutes les 30 secondes")
    print("Bot actif — CTRL+C pour arreter")

    while True:
        try:
            schedule.run_pending()
            time.sleep(5)
        except KeyboardInterrupt:
            log("Arret manuel")
            telegram("Bot arrete manuellement.")
            save_state()
            break
        except Exception as e:
            log(f"Erreur: {e} — Redemarrage dans 30s", "ERROR")
            telegram(f"ERREUR: {e}")
            time.sleep(30)
            ensure_connected()
