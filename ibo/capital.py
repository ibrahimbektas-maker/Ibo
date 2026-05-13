from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd
import requests

from .config import CapitalCredentials


class CapitalAPIError(RuntimeError):
    pass


@dataclass
class Session:
    cst: str
    x_security_token: str


class CapitalClient:
    def __init__(self, creds: CapitalCredentials, timeout: float = 10.0):
        self._creds = creds
        self._timeout = timeout
        self._session: Session | None = None
        self._http = requests.Session()
        self._http.headers.update({"X-CAP-API-KEY": creds.api_key})

    def login(self) -> Session:
        url = f"{self._creds.base_url}/api/v1/session"
        payload = {
            "identifier": self._creds.identifier,
            "password": self._creds.password,
        }
        r = self._http.post(url, json=payload, timeout=self._timeout)
        if r.status_code != 200:
            raise CapitalAPIError(f"Login failed ({r.status_code}): {r.text}")
        self._session = Session(
            cst=r.headers["CST"],
            x_security_token=r.headers["X-SECURITY-TOKEN"],
        )
        return self._session

    def _auth_headers(self) -> dict[str, str]:
        if self._session is None:
            self.login()
        assert self._session is not None
        return {
            "CST": self._session.cst,
            "X-SECURITY-TOKEN": self._session.x_security_token,
        }

    def search_market(self, query: str) -> list[dict[str, Any]]:
        url = f"{self._creds.base_url}/api/v1/markets"
        r = self._http.get(
            url,
            headers=self._auth_headers(),
            params={"searchTerm": query},
            timeout=self._timeout,
        )
        r.raise_for_status()
        return r.json().get("markets", [])

    def get_prices(
        self,
        epic: str,
        resolution: str = "MINUTE_15",
        max_bars: int = 200,
    ) -> pd.DataFrame:
        url = f"{self._creds.base_url}/api/v1/prices/{epic}"
        r = self._http.get(
            url,
            headers=self._auth_headers(),
            params={"resolution": resolution, "max": max_bars},
            timeout=self._timeout,
        )
        if r.status_code != 200:
            raise CapitalAPIError(f"get_prices failed ({r.status_code}): {r.text}")
        prices = r.json().get("prices", [])
        rows = []
        for p in prices:
            rows.append(
                {
                    "time": p["snapshotTimeUTC"],
                    "open": (p["openPrice"]["bid"] + p["openPrice"]["ask"]) / 2,
                    "high": (p["highPrice"]["bid"] + p["highPrice"]["ask"]) / 2,
                    "low": (p["lowPrice"]["bid"] + p["lowPrice"]["ask"]) / 2,
                    "close": (p["closePrice"]["bid"] + p["closePrice"]["ask"]) / 2,
                    "volume": p.get("lastTradedVolume", 0),
                }
            )
        df = pd.DataFrame(rows)
        if df.empty:
            return df
        df["time"] = pd.to_datetime(df["time"], utc=True)
        df = df.set_index("time").sort_index()
        return df

    def place_market_order(
        self,
        epic: str,
        direction: str,
        size: float,
        stop_distance: float | None = None,
        profit_distance: float | None = None,
    ) -> dict[str, Any]:
        url = f"{self._creds.base_url}/api/v1/positions"
        payload: dict[str, Any] = {
            "epic": epic,
            "direction": direction.upper(),
            "size": size,
        }
        if stop_distance is not None:
            payload["stopDistance"] = stop_distance
        if profit_distance is not None:
            payload["profitDistance"] = profit_distance
        r = self._http.post(
            url, headers=self._auth_headers(), json=payload, timeout=self._timeout
        )
        if r.status_code not in (200, 201):
            raise CapitalAPIError(
                f"place_market_order failed ({r.status_code}): {r.text}"
            )
        return r.json()

    def get_open_positions(self) -> list[dict[str, Any]]:
        url = f"{self._creds.base_url}/api/v1/positions"
        r = self._http.get(url, headers=self._auth_headers(), timeout=self._timeout)
        r.raise_for_status()
        return r.json().get("positions", [])

    def close_position(self, deal_id: str) -> dict[str, Any]:
        url = f"{self._creds.base_url}/api/v1/positions/{deal_id}"
        r = self._http.delete(
            url, headers=self._auth_headers(), timeout=self._timeout
        )
        if r.status_code not in (200, 201):
            raise CapitalAPIError(f"close_position failed ({r.status_code}): {r.text}")
        return r.json()
