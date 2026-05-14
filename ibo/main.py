from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from .backtest import run_backtest
from .capital import CapitalAPIError, CapitalClient
from .config import (
    load_anthropic_settings,
    load_capital_credentials,
    load_yaml_config,
)
from .data import (
    fetch_macro_features,
    fetch_macro_features_range,
    fetch_yfinance_prices,
    macro_snapshot,
)
from .risk import RiskManager
from .sentiment import SentimentAnalyzer, build_backtest_sentiment_sizer
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


def _seconds_until_next_bar(interval_minutes: int) -> float:
    """Renvoie le nombre de secondes jusqu'au prochain alignement (UTC).
    Ex: pour interval=15, attend la prochaine borne :00, :15, :30 ou :45."""
    now = datetime.now(timezone.utc)
    minute = now.minute - (now.minute % interval_minutes) + interval_minutes
    next_bar = now.replace(minute=0, second=0, microsecond=0) + timedelta(minutes=minute)
    return max(1.0, (next_bar - now).total_seconds())


def cmd_run_loop(cfg: dict, interval_minutes: int = 15) -> int:
    """Boucle infinie : évalue le marché toutes les `interval_minutes`, aligné sur
    les bornes UTC (00:00, 00:15, 00:30, ...). Ctrl+C pour arrêter."""
    log.info(
        "run-loop démarré (intervalle=%d min, dry_run=%s). Ctrl+C pour stopper.",
        interval_minutes,
        cfg["execution"]["dry_run"],
    )
    try:
        while True:
            try:
                cmd_run_once(cfg)
            except Exception as e:  # noqa: BLE001 — on log et on continue
                log.exception("Erreur dans run-once : %s", e)
            wait = _seconds_until_next_bar(interval_minutes)
            log.info("Prochaine évaluation dans %.0fs", wait)
            time.sleep(wait)
    except KeyboardInterrupt:
        log.info("Arrêt demandé (Ctrl+C). Bye.")
        return 0


def cmd_backtest(
    cfg: dict,
    source: str,
    csv: str | None,
    yf_ticker: str = "GC=F",
    yf_interval: str = "15m",
    yf_period: str = "60d",
    with_sentiment: bool = False,
    sentiment_cache: str = ".backtest_sentiment_cache.json",
    dump_dir: str | None = None,
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

    sentiment_sizer = None
    sentiment_decisions: list[dict] | None = None
    if with_sentiment:
        anth = load_anthropic_settings()
        if not anth.api_key:
            log.error("ANTHROPIC_API_KEY manquant pour --with-sentiment")
            return 2
        start = df.index.min().to_pydatetime() - timedelta(days=10)
        end = df.index.max().to_pydatetime() + timedelta(days=1)
        log.info("sentiment: téléchargement macro daily du %s au %s", start.date(), end.date())
        macro = fetch_macro_features_range(start, end)
        if macro.empty:
            log.error("Macro vide — sentiment désactivé")
        else:
            analyzer = SentimentAnalyzer(
                anth, cache_ttl_seconds=cfg["sentiment"]["cache_ttl_seconds"]
            )
            sentiment_sizer, sentiment_decisions = build_backtest_sentiment_sizer(
                macro=macro,
                analyzer=analyzer,
                sentiment_cfg=cfg["sentiment"],
                cache_path=Path(sentiment_cache),
            )

    result = run_backtest(df, cfg, sentiment_sizer=sentiment_sizer)
    print(json.dumps(result.metrics, indent=2, default=str))
    if not result.trades.empty:
        log.info("%d trades simulés", len(result.trades))

    if dump_dir is not None:
        out = Path(dump_dir)
        out.mkdir(parents=True, exist_ok=True)
        trades_path = out / "backtest_trades.csv"
        if not result.trades.empty:
            result.trades.to_csv(trades_path, index=False)
            log.info("Trades écrits : %s", trades_path)
        if sentiment_decisions:
            sent_df = pd.DataFrame(sentiment_decisions)
            sent_df["key_drivers"] = sent_df["key_drivers"].apply(
                lambda xs: "; ".join(xs) if isinstance(xs, list) else ""
            )
            sent_path = out / "backtest_sentiment.csv"
            sent_df.to_csv(sent_path, index=False)
            counts = sent_df["action"].value_counts().to_dict()
            log.info("Décisions sentiment écrites : %s (%s)", sent_path, counts)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Robot or XAU/USD intraday")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("run-once", help="Évaluer une fois et exécuter selon la config")

    rl = sub.add_parser(
        "run-loop",
        help="Évaluer en continu (aligné sur les bornes M15 UTC). Ctrl+C pour arrêter",
    )
    rl.add_argument(
        "--interval-minutes",
        type=int,
        default=15,
        help="Intervalle entre évaluations en minutes (def: 15)",
    )

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
    bt.add_argument(
        "--with-sentiment",
        action="store_true",
        help="Filtre les trades via le sentiment Claude (appels API, cache disque par date)",
    )
    bt.add_argument(
        "--sentiment-cache",
        default=".backtest_sentiment_cache.json",
        help="Fichier de cache du sentiment (def: .backtest_sentiment_cache.json)",
    )
    bt.add_argument(
        "--dump",
        nargs="?",
        const="backtest_results",
        default=None,
        help="Dump trades + décisions sentiment en CSV (def dir si flag nu: backtest_results/)",
    )

    args = parser.parse_args(argv)
    cfg = load_yaml_config()

    if args.cmd == "run-once":
        return cmd_run_once(cfg)
    if args.cmd == "run-loop":
        return cmd_run_loop(cfg, interval_minutes=args.interval_minutes)
    if args.cmd == "backtest":
        return cmd_backtest(
            cfg,
            args.source,
            args.csv,
            yf_ticker=args.yf_ticker,
            yf_interval=args.yf_interval,
            yf_period=args.yf_period,
            with_sentiment=args.with_sentiment,
            sentiment_cache=args.sentiment_cache,
            dump_dir=args.dump,
        )
    return 1


if __name__ == "__main__":
    sys.exit(main())
