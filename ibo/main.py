from __future__ import annotations

import argparse
import json
import logging
import sys

import pandas as pd

from .backtest import run_backtest
from .capital import CapitalAPIError, CapitalClient
from .config import (
    load_anthropic_settings,
    load_capital_credentials,
    load_yaml_config,
)
from .data import fetch_macro_features, fetch_yfinance_prices, macro_snapshot
from .risk import RiskManager
from .sentiment import SentimentAnalyzer
from .trader import Trader

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("ibo")


def cmd_run_once(cfg: dict) -> int:
    creds = load_capital_credentials()
    if not creds.api_key:
        log.error("CAPITAL_API_KEY manquant. Renseignez .env (voir .env.example).")
        return 2

    capital = CapitalClient(creds)
    try:
        capital.login()
    except CapitalAPIError as e:
        log.error("Echec login Capital.com : %s", e)
        return 3

    prices = capital.get_prices(
        epic=cfg["instrument"]["epic"],
        resolution=cfg["instrument"]["timeframe"],
        max_bars=200,
    )
    if prices.empty:
        log.error("Aucun prix reçu pour %s", cfg["instrument"]["epic"])
        return 4

    macro = fetch_macro_features(lookback_days=15)
    snapshot = macro_snapshot(macro)

    anth = load_anthropic_settings()
    analyzer = (
        SentimentAnalyzer(anth, cache_ttl_seconds=cfg["sentiment"]["cache_ttl_seconds"])
        if anth.api_key and cfg["sentiment"]["enabled"]
        else None
    )

    risk = RiskManager(cfg)
    trader = Trader(cfg, capital, risk, analyzer)

    decision = trader.evaluate_and_trade(prices, snapshot)
    log.info("Décision : %s (%s)", decision.action, decision.reason)
    print(
        json.dumps(
            {
                "action": decision.action,
                "reason": decision.reason,
                "signal_side": decision.technical.side,
                "signal_score": decision.technical.score,
                "atr": decision.technical.atr,
                "details": decision.details,
            },
            indent=2,
            default=str,
        )
    )
    return 0


def cmd_backtest(
    cfg: dict,
    source: str,
    csv: str | None,
    yf_ticker: str = "GC=F",
    yf_interval: str = "15m",
    yf_period: str = "60d",
) -> int:
    if source == "csv":
        if not csv:
            log.error("--csv requis avec --source csv")
            return 2
        df = pd.read_csv(csv, parse_dates=["time"]).set_index("time").sort_index()
    elif source == "capital":
        creds = load_capital_credentials()
        if not creds.api_key:
            log.error("CAPITAL_API_KEY manquant pour source=capital")
            return 2
        capital = CapitalClient(creds)
        capital.login()
        df = capital.get_prices(
            epic=cfg["instrument"]["epic"],
            resolution=cfg["instrument"]["timeframe"],
            max_bars=1000,
        )
    elif source == "yfinance":
        log.info(
            "yfinance: ticker=%s interval=%s period=%s",
            yf_ticker,
            yf_interval,
            yf_period,
        )
        df = fetch_yfinance_prices(
            ticker=yf_ticker, interval=yf_interval, period=yf_period
        )
    else:
        log.error("Source inconnue : %s", source)
        return 2

    if df.empty:
        log.error("Pas de données pour le backtest")
        return 4

    result = run_backtest(df, cfg)
    print(json.dumps(result.metrics, indent=2, default=str))
    if not result.trades.empty:
        log.info("%d trades simulés", len(result.trades))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Robot or XAU/USD intraday")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("run-once", help="Évaluer une fois et exécuter selon la config")

    bt = sub.add_parser("backtest", help="Backtest sur historique")
    bt.add_argument(
        "--source", default="capital", choices=["capital", "csv", "yfinance"]
    )
    bt.add_argument("--csv", help="Chemin CSV (colonnes : time, open, high, low, close)")
    bt.add_argument(
        "--yf-ticker", default="GC=F", help="Ticker yfinance (def: GC=F gold futures)"
    )
    bt.add_argument(
        "--yf-interval",
        default="15m",
        help="Intervalle yfinance : 15m, 30m, 60m, 1h, 1d (def: 15m)",
    )
    bt.add_argument(
        "--yf-period",
        default="60d",
        help="Période yfinance : 60d max pour <1h, 730d max pour 1h (def: 60d)",
    )

    args = parser.parse_args(argv)
    cfg = load_yaml_config()

    if args.cmd == "run-once":
        return cmd_run_once(cfg)
    if args.cmd == "backtest":
        return cmd_backtest(
            cfg,
            args.source,
            args.csv,
            yf_ticker=args.yf_ticker,
            yf_interval=args.yf_interval,
            yf_period=args.yf_period,
        )
    return 1


if __name__ == "__main__":
    sys.exit(main())
