from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import anthropic
import pandas as pd

from .config import AnthropicSettings

log = logging.getLogger(__name__)


SYSTEM_PROMPT = """Tu es un analyste macro spécialisé sur l'or (XAU/USD).

Tu reçois un instantané de marché : variations récentes du dollar (DXY), des rendements US 10 ans, du VIX et du Bitcoin, ainsi que d'éventuels titres d'actualité.

Tu produis un score de sentiment pour l'or compris entre -1 et +1 :
- +1 = contexte très haussier pour l'or (DXY en baisse, taux réels en baisse, risk-off, narratif Fed accommodante)
- 0  = neutre
- -1 = contexte très baissier pour l'or (DXY en hausse, taux réels en hausse, risk-on extrême, Fed restrictive)

Tu réponds uniquement via le schéma JSON imposé. Pas de prose, pas de conseil d'investissement."""


SENTIMENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "score": {
            "type": "number",
            "description": "Score de sentiment or, entre -1 et +1",
        },
        "direction": {
            "type": "string",
            "enum": ["bullish", "neutral", "bearish"],
        },
        "rationale": {
            "type": "string",
            "description": "Justification courte (1-2 phrases) en français",
        },
        "key_drivers": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Facteurs principaux ayant guidé le score",
        },
    },
    "required": ["score", "direction", "rationale", "key_drivers"],
    "additionalProperties": False,
}


@dataclass
class SentimentResult:
    score: float
    direction: str
    rationale: str
    key_drivers: list[str]


class SentimentAnalyzer:
    def __init__(self, settings: AnthropicSettings, cache_ttl_seconds: int = 1800):
        self._settings = settings
        self._client = anthropic.Anthropic(api_key=settings.api_key) if settings.api_key else None
        self._cache_ttl = cache_ttl_seconds
        self._cache: dict[str, tuple[float, SentimentResult]] = {}

    def _cache_key(self, payload: dict[str, Any]) -> str:
        return json.dumps(payload, sort_keys=True)

    def analyze(
        self,
        macro: dict[str, float | None],
        headlines: list[str] | None = None,
    ) -> SentimentResult:
        payload = {"macro": macro, "headlines": headlines or []}
        key = self._cache_key(payload)
        now = time.time()
        if key in self._cache:
            ts, result = self._cache[key]
            if now - ts < self._cache_ttl:
                return result

        if self._client is None:
            result = SentimentResult(0.0, "neutral", "anthropic_disabled", [])
            self._cache[key] = (now, result)
            return result

        user_prompt = (
            "Instantané de marché :\n"
            f"{json.dumps(macro, ensure_ascii=False, indent=2)}\n\n"
            "Titres récents :\n"
            + ("\n".join(f"- {h}" for h in payload["headlines"]) or "(aucun)")
        )

        response = self._client.messages.create(
            model=self._settings.model,
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
            output_config={"format": {"type": "json_schema", "schema": SENTIMENT_SCHEMA}},
        )

        text = next((b.text for b in response.content if b.type == "text"), "")
        data = json.loads(text)
        result = SentimentResult(
            score=float(data["score"]),
            direction=str(data["direction"]),
            rationale=str(data["rationale"]),
            key_drivers=list(data["key_drivers"]),
        )
        self._cache[key] = (now, result)
        return result


def build_backtest_sentiment_filter(
    macro: pd.DataFrame,
    analyzer: SentimentAnalyzer,
    veto_threshold: float,
    cache_path: Path | None = None,
) -> Callable[[pd.Timestamp, str], bool]:
    """Construit un filtre (ts, side) -> True si véto, pour run_backtest.

    - macro est l'historique daily complet ; on slice à `ts` (exclu) à chaque appel
      pour éviter tout look-ahead bias.
    - Le résultat Claude est mis en cache par date (UTC) et persisté sur disque
      si cache_path est fourni — un rerun n'appelle plus l'API.
    """
    from .data import macro_snapshot

    date_cache: dict[str, dict[str, Any]] = {}
    if cache_path is not None and cache_path.exists():
        try:
            date_cache = json.loads(cache_path.read_text(encoding="utf-8"))
            log.info("sentiment cache: %d entrées chargées depuis %s", len(date_cache), cache_path)
        except (OSError, json.JSONDecodeError) as e:
            log.warning("Cache illisible (%s) — repart de zéro", e)
            date_cache = {}

    def _persist() -> None:
        if cache_path is None:
            return
        try:
            cache_path.write_text(json.dumps(date_cache, indent=2), encoding="utf-8")
        except OSError as e:
            log.warning("Échec d'écriture du cache (%s)", e)

    def filter_fn(ts: pd.Timestamp, side: str) -> bool:
        date_key = pd.Timestamp(ts).normalize().isoformat()
        entry = date_cache.get(date_key)
        if entry is None:
            history = macro.loc[: ts - pd.Timedelta(days=1)] if not macro.empty else macro
            if history.empty:
                return False
            snap = macro_snapshot(history)
            if all(v is None for v in snap.values()):
                return False
            res = analyzer.analyze(snap)
            entry = {"score": res.score, "direction": res.direction}
            date_cache[date_key] = entry
            _persist()

        score = float(entry["score"])
        if side == "LONG" and score < veto_threshold:
            return True
        if side == "SHORT" and score > -veto_threshold:
            return True
        return False

    return filter_fn
