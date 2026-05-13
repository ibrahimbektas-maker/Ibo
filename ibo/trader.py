from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import pandas as pd

from .capital import CapitalClient
from .risk import RiskManager
from .sentiment import SentimentAnalyzer, SentimentResult
from .technical import TechnicalSignal, evaluate

log = logging.getLogger(__name__)


@dataclass
class Decision:
    action: str
    reason: str
    technical: TechnicalSignal
    sentiment: SentimentResult | None
    details: dict[str, Any]


class Trader:
    def __init__(
        self,
        cfg: dict,
        capital: CapitalClient,
        risk: RiskManager,
        sentiment: SentimentAnalyzer | None,
    ):
        self._cfg = cfg
        self._capital = capital
        self._risk = risk
        self._sentiment = sentiment

    def evaluate_and_trade(
        self,
        prices: pd.DataFrame,
        macro_snapshot: dict[str, float | None],
        headlines: list[str] | None = None,
    ) -> Decision:
        signal = evaluate(prices, self._cfg)
        details: dict[str, Any] = {"signal_reason": signal.reason}

        if signal.side == "NONE":
            return Decision("HOLD", signal.reason, signal, None, details)

        if not self._risk.can_open_new():
            return Decision("HOLD", "risk_blocked", signal, None, details)

        sent: SentimentResult | None = None
        if self._cfg["sentiment"]["enabled"] and self._sentiment is not None:
            sent = self._sentiment.analyze(macro_snapshot, headlines)
            details["sentiment_score"] = sent.score
            details["sentiment_direction"] = sent.direction

            if signal.side == "LONG" and sent.score < self._cfg["sentiment"]["veto_threshold"]:
                return Decision("HOLD", "sentiment_veto_long", signal, sent, details)
            if signal.side == "SHORT" and sent.score > -self._cfg["sentiment"]["veto_threshold"]:
                return Decision("HOLD", "sentiment_veto_short", signal, sent, details)

        last_price = float(prices["close"].iloc[-1])
        sizing = self._risk.size_trade(signal.atr, last_price)
        if sizing is None:
            return Decision("HOLD", "sizing_invalid", signal, sent, details)

        details["sizing"] = sizing.__dict__

        if self._cfg["execution"]["dry_run"]:
            self._risk.register_open()
            return Decision("DRY_RUN_OPEN", signal.side, signal, sent, details)

        order = self._capital.place_market_order(
            epic=self._cfg["instrument"]["epic"],
            direction="BUY" if signal.side == "LONG" else "SELL",
            size=sizing.size,
            stop_distance=sizing.stop_distance,
            profit_distance=sizing.profit_distance,
        )
        details["order"] = order
        self._risk.register_open()
        return Decision("OPEN", signal.side, signal, sent, details)
