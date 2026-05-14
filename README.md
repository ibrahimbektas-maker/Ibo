# Ibo — Robot de trading sur l'or (XAU/USD)

Robot intraday pour l'or qui combine **signaux techniques** et **sentiment macro analysé par un LLM** (Claude). Exécution via l'API **Capital.com**.

> **Avertissement** : ce projet est fourni à des fins éducatives. Le trading sur l'or comporte un risque élevé de perte en capital. Aucune garantie de performance n'est offerte. Toujours tester en mode démo / dry-run avant tout passage en réel.

## Architecture

```
Capital.com API ────► prix XAU/USD M15
yfinance ────────────► DXY, US10Y, VIX, BTC
                           │
                           ▼
                  ┌─────────────────────┐
                  │ Signaux techniques  │  (EMA + RSI + Donchian + ATR + sessions)
                  └────────┬────────────┘
                           ▼
                  ┌─────────────────────┐
                  │ Sentiment LLM       │  (Claude Haiku — JSON structuré)
                  │ veto si extrême     │
                  └────────┬────────────┘
                           ▼
                  ┌─────────────────────┐
                  │ Risk manager        │  (1-2 % par trade, kill switch)
                  └────────┬────────────┘
                           ▼
                  Ordres Capital.com (ou dry-run)
```

## Installation

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Renseigner les clés Capital.com et Anthropic dans .env
```

## Utilisation

**Mode dry-run (par défaut)** — évalue le marché, simule une décision, ne passe pas d'ordre :

```bash
python -m ibo.main run-once
```

**Backtest** sur l'historique récent Capital.com :

```bash
python -m ibo.main backtest --source capital
```

**Backtest** sur un CSV local (colonnes `time, open, high, low, close`) :

```bash
python -m ibo.main backtest --source csv --csv historique.csv
```

**Backtest** sur historique étendu via yfinance (gold futures `GC=F`) — utile pour avoir un échantillon statistiquement significatif :

```bash
# 60 jours en M15 (max yfinance pour <1h) — ~5-6× plus de données que Capital.com
python -m ibo.main backtest --source yfinance

# 12 mois en H1 — recul plus long, granularité moindre
python -m ibo.main backtest --source yfinance --yf-interval 1h --yf-period 365d
```

Note : les futures `GC=F` ne sont pas strictement identiques au spot XAU/USD (contango, roll), mais la corrélation est forte — suffisant pour valider la robustesse de la stratégie.

**Backtest avec filtre sentiment Claude** — chaque entrée potentielle est soumise au LLM, qui peut vétoer selon le contexte macro du jour :

```bash
python -m ibo.main backtest --source yfinance --yf-interval 1h --yf-period 365d --with-sentiment
```

Les appels Claude sont mis en cache par date (UTC) dans `.backtest_sentiment_cache.json` — un rerun n'appelle plus l'API. Pour repartir de zéro, supprimer le fichier.

## Configuration

Tout se passe dans `config.yaml` :

- `instrument` : epic Capital.com et timeframe
- `sessions` : trader uniquement Londres / New York
- `risk` : capital, % par trade, multiples ATR pour SL/TP, kill switch
- `signals` : périodes EMA / RSI, lookback Donchian
- `sentiment` : seuil de véto LLM
- `execution.dry_run` : `true` par défaut. Passer à `false` pour ordres réels.

## Couche IA (sentiment)

Le module `ibo/sentiment.py` appelle Claude Haiku 4.5 avec un schéma JSON imposé (`output_config.format`). Le LLM reçoit l'instantané macro (DXY, US10Y, VIX, BTC) et renvoie un score entre -1 et +1. Si le score est trop défavorable, le signal technique est rejeté (veto).

Modèle par défaut : `claude-haiku-4-5` (rapide, économique). Modifiable via `ANTHROPIC_MODEL`.

## Sécurité

- Les clés API sont chargées depuis `.env` (jamais commité)
- L'API key Capital.com doit avoir **uniquement** les droits de trading, **pas** de retrait (cf. hack 3Commas 2022)
- Le mode `dry_run` est activé par défaut

## Statut

Projet en construction. Étapes restantes : tests unitaires, walk-forward backtest, monitoring temps réel, calibration paramètres.
