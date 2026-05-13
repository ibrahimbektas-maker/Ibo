from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml
from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class CapitalCredentials:
    api_key: str
    identifier: str
    password: str
    demo: bool

    @property
    def base_url(self) -> str:
        return (
            "https://demo-api-capital.backend-capital.com"
            if self.demo
            else "https://api-capital.backend-capital.com"
        )


@dataclass(frozen=True)
class AnthropicSettings:
    api_key: str
    model: str


def load_capital_credentials() -> CapitalCredentials:
    return CapitalCredentials(
        api_key=os.environ.get("CAPITAL_API_KEY", ""),
        identifier=os.environ.get("CAPITAL_IDENTIFIER", ""),
        password=os.environ.get("CAPITAL_PASSWORD", ""),
        demo=os.environ.get("CAPITAL_DEMO", "true").lower() == "true",
    )


def load_anthropic_settings() -> AnthropicSettings:
    return AnthropicSettings(
        api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
        model=os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5"),
    )


def load_yaml_config(path: str | Path = "config.yaml") -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)
