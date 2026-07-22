"""Fee-aware signal model and fail-closed entry gates."""
from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

from .fastbook import BookPair
from .fastmarket import FastMarket, crypto_fee_per_share


@dataclass
class PricePoint:
    value: float
    source_ts: float
    recv_monotonic: float

    def age_ms(self, now_monotonic: float | None = None) -> float:
        now = time.monotonic() if now_monotonic is None else now_monotonic
        return max(0.0, (now - self.recv_monotonic) * 1000)


@dataclass(frozen=True)
class Decision:
    action: str
    outcome: str | None
    fair_probability: float | None
    ask: float | None
    fee_per_share: float | None
    net_edge: float | None
    order_usd: float
    reasons: tuple[str, ...]
    gates: dict[str, bool]

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "outcome": self.outcome,
            "fair_probability": self.fair_probability,
            "ask": self.ask,
            "fee_per_share": self.fee_per_share,
            "net_edge": self.net_edge,
            "order_usd": self.order_usd,
            "reasons": list(self.reasons),
            "gates": self.gates,
        }


class FastSignalModel:
    def __init__(self, strategy: dict[str, Any]):
        self.cfg = strategy
        self.open_price: float | None = None
        self.open_source_ts: float | None = None
        self.chainlink: PricePoint | None = None
        self.binance: PricePoint | None = None
        self.basis = 0.0
        self.variance_per_second = float(strategy.get("min_sigma_per_sqrt_second", 0.000035)) ** 2
        self._last_binance: PricePoint | None = None
        self._chainlink_history: deque[PricePoint] = deque(maxlen=64)

    def reset_window(self) -> None:
        self.open_price = None
        self.open_source_ts = None

    def update_chainlink(self, price: float, source_ts: float,
                         recv_monotonic: float | None = None) -> bool:
        if self.chainlink and float(source_ts) <= self.chainlink.source_ts:
            return False
        recv = recv_monotonic or time.monotonic()
        self.chainlink = PricePoint(float(price), float(source_ts), recv)
        self._chainlink_history.append(self.chainlink)
        if self.binance and abs(source_ts - self.binance.source_ts) <= 3:
            observed = self.chainlink.value - self.binance.value
            alpha = float(self.cfg.get("basis_alpha", 0.08))
            self.basis = alpha * observed + (1 - alpha) * self.basis
        return True

    def update_binance(self, price: float, source_ts: float,
                       recv_monotonic: float | None = None) -> bool:
        if self.binance and float(source_ts) <= self.binance.source_ts:
            return False
        recv = recv_monotonic or time.monotonic()
        point = PricePoint(float(price), float(source_ts), recv)
        previous = self._last_binance
        if previous and point.source_ts > previous.source_ts and previous.value > 0:
            dt = max(0.001, point.source_ts - previous.source_ts)
            ret = math.log(point.value / previous.value)
            sample = min(0.01, (ret * ret) / dt)
            alpha = float(self.cfg.get("variance_alpha", 0.08))
            floor = float(self.cfg.get("min_sigma_per_sqrt_second", 0.000035)) ** 2
            self.variance_per_second = max(floor, alpha * sample + (1 - alpha) * self.variance_per_second)
        self.binance = point
        self._last_binance = point
        return True

    def capture_open(self, market: FastMarket) -> bool:
        if self.open_price is not None:
            return self.open_price is not None
        # Market discovery and the book subscription can finish after the first
        # Chainlink print.  Keep a short source-timestamped ring buffer so the
        # exact opening print is still captured without inventing a midpoint.
        point = next(
            (
                item
                for item in reversed(self._chainlink_history)
                if market.start_epoch <= item.source_ts <= market.start_epoch + 5
            ),
            None,
        )
        if point:
            self.open_price = point.value
            self.open_source_ts = point.source_ts
            return True
        return False

    def estimated_resolution_price(self, now_monotonic: float | None = None) -> float | None:
        now = time.monotonic() if now_monotonic is None else now_monotonic
        max_binance_age = float(self.cfg.get("max_binance_age_ms", 1000))
        if self.binance and self.binance.age_ms(now) <= max_binance_age:
            return self.binance.value + self.basis
        return self.chainlink.value if self.chainlink else None

    def fair_up(self, remaining_seconds: float, now_monotonic: float | None = None) -> float | None:
        estimate = self.estimated_resolution_price(now_monotonic)
        if not self.open_price or not estimate or estimate <= 0:
            return None
        sigma = math.sqrt(max(1e-12, self.variance_per_second) * max(1.0, remaining_seconds))
        z = math.log(estimate / self.open_price) / sigma
        p = 0.5 * (1 + math.erf(z / math.sqrt(2)))
        return min(0.98, max(0.02, p))

    def feed_state(self, now_monotonic: float | None = None) -> dict[str, Any]:
        now = time.monotonic() if now_monotonic is None else now_monotonic
        return {
            "open_price": self.open_price,
            "open_source_ts": self.open_source_ts,
            "chainlink": None if not self.chainlink else {
                "price": self.chainlink.value,
                "source_ts": self.chainlink.source_ts,
                "age_ms": self.chainlink.age_ms(now),
            },
            "binance": None if not self.binance else {
                "price": self.binance.value,
                "source_ts": self.binance.source_ts,
                "age_ms": self.binance.age_ms(now),
            },
            "basis_usd": self.basis,
            "sigma_per_sqrt_second": math.sqrt(self.variance_per_second),
            "estimated_resolution_price": self.estimated_resolution_price(now),
        }


def evaluate_entry(
    market: FastMarket,
    books: BookPair,
    signal: FastSignalModel,
    now_epoch: float,
    strategy: dict[str, Any],
    risk: dict[str, Any],
    orders_this_window: int = 0,
    daily_pnl_usd: float = 0.0,
    window_committed_usd: float = 0.0,
    now_monotonic: float | None = None,
) -> Decision:
    now_mono = time.monotonic() if now_monotonic is None else now_monotonic
    elapsed = now_epoch - market.start_epoch
    remaining = market.end_epoch - now_epoch
    chain_age = signal.chainlink.age_ms(now_mono) if signal.chainlink else math.inf
    binance_age = signal.binance.age_ms(now_mono) if signal.binance else math.inf
    chain_source_age = (
        max(0.0, (now_epoch - signal.chainlink.source_ts) * 1000)
        if signal.chainlink else math.inf
    )
    binance_source_age = (
        max(0.0, (now_epoch - signal.binance.source_ts) * 1000)
        if signal.binance else math.inf
    )
    source_skew = (
        abs(signal.chainlink.source_ts - signal.binance.source_ts)
        if signal.chainlink and signal.binance else math.inf
    )
    max_window_usd = float(risk.get("max_window_usd", 5))
    window_remaining_usd = max(0.0, max_window_usd - float(window_committed_usd))
    gates = {
        "processing_delay_known": market.taker_delay_verified,
        "open_captured": signal.open_price is not None,
        "inside_time_window": (
            elapsed >= float(strategy.get("min_seconds_after_open", 8))
            and remaining >= float(strategy.get("min_seconds_to_close", 20))
            and remaining <= float(strategy.get("max_seconds_to_close", 270))
        ),
        "chainlink_fresh": (
            chain_age <= float(strategy.get("max_chainlink_age_ms", 1500))
            and chain_source_age <= float(strategy.get("max_chainlink_source_age_ms", 3000))
        ),
        "binance_fresh": (
            binance_age <= float(strategy.get("max_binance_age_ms", 1000))
            and binance_source_age <= float(strategy.get("max_binance_source_age_ms", 2000))
        ),
        "source_alignment": source_skew <= float(strategy.get("max_source_skew_seconds", 3)),
        "books_healthy": books.healthy(
            float(strategy.get("max_clob_age_ms", 500)),
            float(strategy.get("max_spread", 0.08)),
            now_mono,
        ),
        "window_order_budget": (
            orders_this_window < int(risk.get("max_orders_per_window", 1))
            and window_remaining_usd > 0
        ),
        "daily_loss_budget": daily_pnl_usd > -float(risk.get("max_daily_loss_usd", 15)),
    }
    failed = [name for name, passed in gates.items() if not passed]
    fair_up = signal.fair_up(remaining, now_mono)
    if failed or fair_up is None:
        return Decision("WAIT", None, fair_up, None, None, None, 0.0, tuple(failed or ["no_fair_price"]), gates)

    candidates: list[tuple[float, str, float, float, float]] = []
    for outcome, fair, book in (("UP", fair_up, books.up), ("DOWN", 1 - fair_up, books.down)):
        if book.best_ask is None:
            continue
        fee = crypto_fee_per_share(book.best_ask, market.fee_rate)
        edge = fair - book.best_ask - fee - float(strategy.get("slippage_buffer", 0.01))
        candidates.append((edge, outcome, fair, book.best_ask, fee))
    if not candidates:
        return Decision("WAIT", None, fair_up, None, None, None, 0.0, ("no_ask",), gates)

    edge, outcome, fair, ask, fee = max(candidates)
    min_selected_probability = float(strategy.get("min_selected_probability", 0.0))
    if fair < min_selected_probability:
        gates["selected_probability"] = False
        return Decision(
            "WAIT", outcome, fair, ask, fee, edge, 0.0,
            ("selected_probability_below_threshold",), gates,
        )
    gates["selected_probability"] = True
    min_edge = float(strategy.get("min_net_edge", 0.04))
    if edge < min_edge:
        gates["net_edge"] = False
        return Decision("WAIT", outcome, fair, ask, fee, edge, 0.0, ("net_edge_below_threshold",), gates)
    gates["net_edge"] = True
    max_order = min(float(risk.get("max_order_usd", 5)), window_remaining_usd)
    # Include fees when checking whether the exchange's five-share minimum fits.
    minimum_total_cost = market.min_order_size * (ask + fee)
    if minimum_total_cost > max_order:
        gates["minimum_order_fits"] = False
        return Decision("WAIT", outcome, fair, ask, fee, edge, 0.0, ("minimum_order_exceeds_cap",), gates)
    gates["minimum_order_fits"] = True
    return Decision("CANDIDATE", outcome, fair, ask, fee, edge, max_order, (), gates)
