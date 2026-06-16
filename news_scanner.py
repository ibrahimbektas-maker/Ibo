"""
NEWS SCANNER -- depeches financieres + calendrier economique vers Telegram
==========================================================================
Surveille :
  1) Plusieurs flux RSS (Forexlive gold, Forexlive general, Investing.com)
     -> push sur Telegram si le titre matche tes mots-cles (gold/XAU, macro
        Fed/FOMC/CPI/NFP, geopolitique).
  2) Le calendrier Forex Factory (free XML)
     -> push 30 min avant chaque event a FORT IMPACT (NFP, FOMC, CPI...)
        sur les devises qui bougent le gold (USD principalement).

Dedup : les depeches deja vues ne sont pas renvoyees.
Anti-spam : plafond MAX_NEWS_PER_DAY.

Prerequis :
  pip install requests (deja dans requirements.txt)
  Le reste (datetime, xml.etree, json, os, time) est en lib standard.

Usage : python news_scanner.py
"""

import os
import re
import time
import json
import html
import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone


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
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip()
    return env

env = load_env()
TELEGRAM_TOKEN = env.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT  = env.get("TELEGRAM_CHAT", "")
if not all([TELEGRAM_TOKEN, TELEGRAM_CHAT]):
    print("ERREUR : TELEGRAM_TOKEN / TELEGRAM_CHAT manquants dans .env")
    exit(1)


# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────
RSS_FEEDS = [
    ("Forexlive Gold",  "https://www.forexlive.com/tag/gold/feed/"),
    ("Forexlive News",  "https://www.forexlive.com/feed/"),
    ("Investing.com",   "https://www.investing.com/rss/news.rss"),
]

CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.xml"

# Mots-cles pour filtrer les news. Si AU MOINS UN match dans le titre -> on push.
KEYWORDS = [
    # Gold / metaux
    "gold", "xau", "xauusd", "bullion", "precious metal", "silver", "or ",
    # Macro / Fed
    "fed", "powell", "fomc", "cpi", "ppi", "nfp", "non-farm", "nonfarm",
    "jobless", "unemployment", "rate", "rates", "yield", "treasury",
    "inflation", "pce", "gdp", "retail sales", "ism", "pmi",
    # Dollar
    "dollar", "dxy", "greenback",
    # Geopolitique (qui bouge le gold)
    "war", "israel", "iran", "russia", "ukraine", "china", "tariff", "trump",
    "central bank", "ecb", "boj", "boe",
]

CALENDAR_COUNTRIES = ["USD"]      # devises a surveiller (USD = principal moteur du gold)
CALENDAR_IMPACT_LEVELS = ["High"] # niveau minimum d'impact ("Low", "Medium", "High")
ALERT_BEFORE_EVENT_MIN = 30       # alerte 30 min avant l'event

POLL_INTERVAL_NEWS_SEC     = 300  # 5 min
POLL_INTERVAL_CALENDAR_SEC = 1800 # 30 min (le check fin se fait a chaque tick principal)
MAIN_LOOP_TICK_SEC         = 60   # boucle principale toutes les minutes

MAX_NEWS_PER_DAY = 25             # garde-fou anti-spam

LOG_FILE   = "news_scanner_log.txt"
STATE_FILE = "news_scanner_state.json"


# ─────────────────────────────────────────────
# STATE
# ─────────────────────────────────────────────
state = {
    "running":          True,
    "seen_news_ids":    [],   # liste des derniers IDs vus (FIFO, plafond 500)
    "alerted_events":   [],   # liste des IDs d'events deja alertes
    "news_today":       0,
    "today":            None,
    "last_news_poll_iso":     None,
    "last_calendar_poll_iso": None,
    "calendar_events":  [],   # cache des events de la semaine (pour le check chaque minute)
}
SEEN_NEWS_MAX = 500       # plafond memoire


# ─────────────────────────────────────────────
# UTIL
# ─────────────────────────────────────────────
def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
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
        log(f"Erreur sauvegarde: {e}")


def load_state():
    if not os.path.exists(STATE_FILE):
        return
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            saved = json.load(f)
        for k, v in saved.items():
            if k in state:
                state[k] = v
        log("Etat precedent restaure")
    except Exception as e:
        log(f"Erreur chargement: {e}")


def telegram(msg):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        r = requests.post(url, json={"chat_id": TELEGRAM_CHAT, "text": msg,
                                     "disable_web_page_preview": True}, timeout=10)
        if r.status_code != 200:
            log(f"Telegram HTTP {r.status_code}: {r.text[:100]}")
    except Exception as e:
        log(f"Erreur Telegram: {e}")


_last_update_id = 0
def telegram_check_commands():
    global _last_update_id
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
        r = requests.get(url, params={"timeout": 1, "offset": _last_update_id + 1}, timeout=5)
        if r.status_code != 200:
            return
        for upd in r.json().get("result", []):
            _last_update_id = max(_last_update_id, upd.get("update_id", 0))
            msg = upd.get("message", {}).get("text", "")
            if msg == "/status":
                lines = [
                    "NEWS SCANNER -- STATUS",
                    f"Heure UTC : {datetime.now(timezone.utc):%H:%M}",
                    f"News du jour : {state['news_today']}/{MAX_NEWS_PER_DAY}",
                    f"Events calendrier en memoire : {len(state['calendar_events'])}",
                    f"Dernier poll news : {state.get('last_news_poll_iso') or 'jamais'}",
                    f"Dernier poll calendrier : {state.get('last_calendar_poll_iso') or 'jamais'}",
                ]
                telegram("\n".join(lines))
            elif msg == "/stop":
                state["running"] = False
                telegram("News scanner arrete.")
    except Exception:
        pass


def reset_daily_if_new_day():
    today = datetime.now(timezone.utc).date().isoformat()
    if state["today"] != today:
        state["today"] = today
        state["news_today"] = 0
        log(f"Nouveau jour : {today}, compteur news reset")


# ─────────────────────────────────────────────
# NEWS RSS
# ─────────────────────────────────────────────
def title_matches_keywords(title):
    t = title.lower()
    for kw in KEYWORDS:
        if kw in t:
            return kw
    return None


def _strip_html(s):
    """Retire tags HTML simples + decode les entites."""
    if not s:
        return ""
    s = re.sub(r"<[^>]+>", "", s)
    return html.unescape(s).strip()


def _findtext_ns(elem, tag):
    """Cherche un tag avec ou sans namespace (ns://...:tag)."""
    found = elem.find(tag)
    if found is not None and found.text:
        return found.text
    # Cherche en parcourant tous les enfants (gere namespaces Atom/RSS)
    for child in elem:
        local = child.tag.split("}", 1)[-1] if "}" in child.tag else child.tag
        if local == tag:
            return child.text or ""
    return ""


def fetch_feed(name, url):
    """Parse RSS 2.0 ou Atom via stdlib. Renvoie [{id, title, summary, link, source}]."""
    try:
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0 NewsScanner/1.0"},
                         timeout=10)
        if r.status_code != 200:
            log(f"Feed {name} HTTP {r.status_code}")
            return []
        root = ET.fromstring(r.content)
        items = []
        # RSS 2.0 : <rss><channel><item>... ; Atom : <feed><entry>...
        # On cherche items ou entries en ignorant les namespaces
        for child in root.iter():
            local = child.tag.split("}", 1)[-1] if "}" in child.tag else child.tag
            if local in ("item", "entry"):
                title   = _findtext_ns(child, "title") or ""
                summary = _findtext_ns(child, "summary") or _findtext_ns(child, "description") or ""
                link    = _findtext_ns(child, "link") or ""
                # Atom: <link href="..."/>  ; certains RSS aussi
                if not link.strip():
                    for c in child:
                        local2 = c.tag.split("}", 1)[-1] if "}" in c.tag else c.tag
                        if local2 == "link":
                            link = c.attrib.get("href", "") or (c.text or "")
                            if link:
                                break
                iid = (_findtext_ns(child, "guid") or _findtext_ns(child, "id")
                       or link or title)
                items.append({
                    "id":      (iid or "").strip(),
                    "title":   _strip_html(title)[:400],
                    "summary": _strip_html(summary)[:300],
                    "link":    (link or "").strip(),
                    "source":  name,
                })
                if len(items) >= 50:
                    break
        return items
    except Exception as e:
        log(f"Erreur feed {name}: {e}")
        return []


def check_news():
    if state["news_today"] >= MAX_NEWS_PER_DAY:
        return
    new_pushes = 0
    for name, url in RSS_FEEDS:
        if state["news_today"] >= MAX_NEWS_PER_DAY:
            break
        items = fetch_feed(name, url)
        for item in items:
            if item["id"] in state["seen_news_ids"]:
                continue
            state["seen_news_ids"].append(item["id"])
            # plafond memoire
            if len(state["seen_news_ids"]) > SEEN_NEWS_MAX:
                state["seen_news_ids"] = state["seen_news_ids"][-SEEN_NEWS_MAX:]

            matched = title_matches_keywords(item["title"])
            if matched is None:
                continue

            msg = (
                f"NEWS [{item['source']}]\n"
                f"{item['title']}\n"
                f"\n(matched: {matched})\n"
                + (f"\n{item['link']}" if item["link"] else "")
            )
            telegram(msg)
            log(f"PUSH news: {item['title'][:80]}")
            state["news_today"] += 1
            new_pushes += 1
            if state["news_today"] >= MAX_NEWS_PER_DAY:
                break
    state["last_news_poll_iso"] = datetime.now().isoformat()
    if new_pushes > 0:
        save_state()


# ─────────────────────────────────────────────
# CALENDRIER ECONOMIQUE
# ─────────────────────────────────────────────
def parse_ff_datetime(date_str, time_str):
    """Forex Factory : date 'MM-DD-YYYY', time '2:00pm' ou 'All Day'.
    La feed est en EST (UTC-5) sans DST automatique. On retourne datetime UTC ou None."""
    if not date_str or not time_str:
        return None
    if "all day" in time_str.lower() or "tentative" in time_str.lower():
        return None
    try:
        # Parse date MM-DD-YYYY
        dt_date = datetime.strptime(date_str.strip(), "%m-%d-%Y")
        # Parse time '2:00pm' or '12:30am'
        t = time_str.strip().lower().replace(" ", "")
        # gere ex: 2:00pm, 12:30am, 8:30am
        dt_time = datetime.strptime(t, "%I:%M%p").time()
        dt_local = datetime.combine(dt_date.date(), dt_time)
        # Forex Factory est en EST/EDT. On approxime a EST (-5h).
        # (Pas critique car on alerte 30 min avant : meme un decalage d'1h reste utile.)
        dt_utc = dt_local + timedelta(hours=5)
        return dt_utc.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def fetch_calendar():
    """Renvoie la liste des events HIGH USD a venir cette semaine."""
    try:
        r = requests.get(CALENDAR_URL,
                         headers={"User-Agent": "Mozilla/5.0 NewsScanner/1.0"}, timeout=15)
        if r.status_code != 200:
            log(f"Calendar HTTP {r.status_code}")
            return []
        root = ET.fromstring(r.content)
        events = []
        for ev in root.findall("event"):
            title  = (ev.findtext("title") or "").strip()
            ctry   = (ev.findtext("country") or "").strip()
            date_s = (ev.findtext("date") or "").strip()
            time_s = (ev.findtext("time") or "").strip()
            impact = (ev.findtext("impact") or "").strip()
            if ctry not in CALENDAR_COUNTRIES:
                continue
            if impact not in CALENDAR_IMPACT_LEVELS:
                continue
            dt_utc = parse_ff_datetime(date_s, time_s)
            if dt_utc is None:
                continue
            eid = f"{ctry}-{title}-{date_s}-{time_s}"
            events.append({"id": eid, "title": title, "country": ctry,
                           "impact": impact, "dt_utc": dt_utc.isoformat()})
        return events
    except Exception as e:
        log(f"Erreur calendar: {e}")
        return []


def refresh_calendar():
    events = fetch_calendar()
    state["calendar_events"] = events
    state["last_calendar_poll_iso"] = datetime.now().isoformat()
    save_state()
    log(f"Calendar refresh : {len(events)} events HIGH USD cette semaine")


def check_upcoming_events():
    """Pour chaque event en cache : si on est a moins de ALERT_BEFORE_EVENT_MIN
    de son debut ET pas encore alerte -> push."""
    now_utc = datetime.now(timezone.utc)
    for ev in state["calendar_events"]:
        if ev["id"] in state["alerted_events"]:
            continue
        try:
            dt = datetime.fromisoformat(ev["dt_utc"])
        except ValueError:
            continue
        delta_min = (dt - now_utc).total_seconds() / 60.0
        if 0 <= delta_min <= ALERT_BEFORE_EVENT_MIN:
            msg = (
                f"ATTENTION EVENT MACRO -- DANS {int(delta_min)} MIN\n"
                f"{ev['country']} -- {ev['title']}\n"
                f"Impact : {ev['impact']}\n"
                f"Heure UTC : {dt:%H:%M}\n\n"
                f"Pas de trade 15 min avant -> 30 min apres."
            )
            telegram(msg)
            log(f"PUSH event: {ev['title']}")
            state["alerted_events"].append(ev["id"])
            # plafond memoire alerted_events
            if len(state["alerted_events"]) > 200:
                state["alerted_events"] = state["alerted_events"][-200:]
            save_state()


def minutes_since(iso_ts):
    if not iso_ts:
        return None
    try:
        dt = datetime.fromisoformat(iso_ts)
    except (ValueError, TypeError):
        return None
    return (datetime.now() - dt).total_seconds() / 60.0


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 70)
    print(" NEWS SCANNER -- depeches RSS + calendrier economique -> Telegram")
    print(f" Flux RSS surveilles : {len(RSS_FEEDS)}")
    print(f" Mots-cles : {len(KEYWORDS)} (gold, macro, fed, geopol...)")
    print(f" Calendrier : alertes HIGH IMPACT {CALENDAR_COUNTRIES} -- "
          f"{ALERT_BEFORE_EVENT_MIN}min avant")
    print(f" Plafonds : {MAX_NEWS_PER_DAY} news/jour")
    print("=" * 70)

    load_state()
    telegram("NEWS SCANNER demarre.\n"
             f"Flux : {', '.join(n for n, _ in RSS_FEEDS)}\n"
             f"Calendrier : alertes {ALERT_BEFORE_EVENT_MIN}min avant events USD/HIGH\n"
             "Commandes : /status /stop")
    log("Scanner actif")

    # Premier refresh calendrier
    refresh_calendar()

    while state["running"]:
        try:
            telegram_check_commands()
            reset_daily_if_new_day()

            # Check news (toutes les POLL_INTERVAL_NEWS_SEC)
            mins_news = minutes_since(state.get("last_news_poll_iso"))
            if mins_news is None or mins_news * 60 >= POLL_INTERVAL_NEWS_SEC:
                check_news()

            # Refresh calendrier (toutes les POLL_INTERVAL_CALENDAR_SEC)
            mins_cal = minutes_since(state.get("last_calendar_poll_iso"))
            if mins_cal is None or mins_cal * 60 >= POLL_INTERVAL_CALENDAR_SEC:
                refresh_calendar()

            # Check events imminents (chaque minute)
            check_upcoming_events()

            save_state()
            time.sleep(MAIN_LOOP_TICK_SEC)
        except KeyboardInterrupt:
            log("Arret manuel")
            telegram("News scanner arrete (Ctrl+C).")
            save_state()
            break
        except Exception as e:
            log(f"Erreur boucle: {e}")
            time.sleep(30)
