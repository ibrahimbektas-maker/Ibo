from __future__ import annotations

from dataclasses import dataclass


@dataclass
class TradeSizing:
    size: float
    stop_distance: float
    profit_distance: float
    risk_amount: float


class RiskManager:
    def __init__(self, cfg: dict):
        self._cfg = cfg["risk"]
        self._daily_pnl = 0.0
        self._open_positions = 0

    def reset_day(self) -> None:
        self._daily_pnl = 0.0

    def register_close(self, pnl: float) -> None:
        self._daily_pnl += pnl
        self._open_positions = max(0, self._open_positions - 1)

    def register_open(self) -> None:
        self._open_positions += 1

    def kill_switch_triggered(self) -> bool:
        max_loss = self._cfg["capital_eur"] * self._cfg["max_daily_loss_pct"] / 100.0
        return self._daily_pnl <= -max_loss

    def can_open_new(self) -> bool:
        if self.kill_switch_triggered():
            return False
        return self._open_positions < self._cfg["max_open_positions"]

    def size_trade(self, atr: float, price: float) -> TradeSizing | None:
        if atr <= 0 or price <= 0:
            return None
        sl_dist = atr * self._cfg["sl_atr_multiple"]
        tp_dist = atr * self._cfg["tp_atr_multiple"]
        risk_amount = self._cfg["capital_eur"] * self._cfg["risk_per_trade_pct"] / 100.0
        size = risk_amount / sl_dist
        if size <= 0:
            return None
        return TradeSizing(
            size=round(size, 4),
            stop_distance=round(sl_dist, 4),
            profit_distance=round(tp_dist, 4),
            risk_amount=risk_amount,
        )
