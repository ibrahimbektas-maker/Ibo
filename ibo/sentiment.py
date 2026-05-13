from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

import anthropic

from .config import AnthropicSettings


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
