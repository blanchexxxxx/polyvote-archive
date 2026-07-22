"""Deterministic local order book with stale and out-of-order protection."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _ts_ms(value: Any) -> int:
    try:
        n = int(float(value))
    except (TypeError, ValueError):
        return int(time.time() * 1000)
    return n * 1000 if n < 10_000_000_000 else n


@dataclass
class LocalBook:
    asset_id: str
    bids: dict[float, float] = field(default_factory=dict)
    asks: dict[float, float] = field(default_factory=dict)
    event_ts_ms: int = 0
    recv_monotonic: float = 0.0
    synchronized: bool = False
    out_of_order: int = 0
    mismatch_count: int = 0
    update_count: int = 0

    @property
    def best_bid(self) -> float | None:
        return max(self.bids) if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return min(self.asks) if self.asks else None

    @property
    def spread(self) -> float | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return self.best_ask - self.best_bid

    def age_ms(self, now_monotonic: float | None = None) -> float | None:
        if not self.recv_monotonic:
            return None
        now = time.monotonic() if now_monotonic is None else now_monotonic
        return max(0.0, (now - self.recv_monotonic) * 1000)

    def apply_snapshot(self, message: dict[str, Any], recv_monotonic: float | None = None) -> bool:
        ts = _ts_ms(message.get("timestamp"))
        if self.event_ts_ms and ts < self.event_ts_ms:
            self.out_of_order += 1
            return False
        self.bids = self._levels(message.get("bids"))
        self.asks = self._levels(message.get("asks"))
        self.event_ts_ms = ts
        self.recv_monotonic = recv_monotonic or time.monotonic()
        self.synchronized = bool(self.bids and self.asks and self.best_bid < self.best_ask)
        self.update_count += 1
        return True

    def apply_change(
        self,
        change: dict[str, Any],
        timestamp: Any,
        recv_monotonic: float | None = None,
        validate_expected: bool = True,
    ) -> bool:
        ts = _ts_ms(timestamp)
        if self.event_ts_ms and ts < self.event_ts_ms:
            self.out_of_order += 1
            return False
        price, size = _f(change.get("price"), -1), _f(change.get("size"), -1)
        side = str(change.get("side") or "").upper()
        if price <= 0 or size < 0 or side not in {"BUY", "SELL"}:
            return False
        levels = self.bids if side == "BUY" else self.asks
        if size == 0:
            levels.pop(price, None)
        else:
            levels[price] = size
        self.event_ts_ms = ts
        self.recv_monotonic = recv_monotonic or time.monotonic()
        self.update_count += 1

        if not validate_expected:
            return True
        return self.validate_best(change.get("best_bid"), change.get("best_ask"))

    def apply_best(self, message: dict[str, Any], recv_monotonic: float | None = None) -> bool:
        """Apply the lightweight custom top-of-book event.

        Depth is intentionally discarded here.  The shadow executor fetches a
        fresh REST snapshot only when an intent reaches its simulated arrival
        time, which keeps the always-on 512 MB service below its CPU quota.
        """
        ts = _ts_ms(message.get("timestamp"))
        if self.event_ts_ms and ts < self.event_ts_ms:
            self.out_of_order += 1
            return False
        bid = _f(message.get("best_bid"), -1)
        ask = _f(message.get("best_ask"), -1)
        if not 0 < bid < ask < 1:
            self.mismatch_count += 1
            self.synchronized = False
            return False
        self.bids = {bid: self.bids.get(bid, 0.0)}
        self.asks = {ask: self.asks.get(ask, 0.0)}
        self.event_ts_ms = ts
        self.recv_monotonic = recv_monotonic or time.monotonic()
        self.synchronized = True
        self.update_count += 1
        return True

    def validate_best(self, expected_bid: Any, expected_ask: Any) -> bool:
        mismatch = (
            expected_bid is not None
            and self.best_bid is not None
            and abs(self.best_bid - _f(expected_bid)) > 1e-9
        ) or (
            expected_ask is not None
            and self.best_ask is not None
            and abs(self.best_ask - _f(expected_ask)) > 1e-9
        )
        if mismatch:
            self.mismatch_count += 1
            self.synchronized = False
        return not mismatch

    @staticmethod
    def _levels(rows: Any) -> dict[float, float]:
        out: dict[float, float] = {}
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            price, size = _f(row.get("price"), -1), _f(row.get("size"), -1)
            if price > 0 and size > 0:
                out[price] = size
        return out

    def to_dict(self, now_monotonic: float | None = None) -> dict[str, Any]:
        return {
            "best_bid": self.best_bid,
            "best_ask": self.best_ask,
            "spread": self.spread,
            "age_ms": self.age_ms(now_monotonic),
            "synchronized": self.synchronized,
            "out_of_order": self.out_of_order,
            "mismatch_count": self.mismatch_count,
            "updates": self.update_count,
        }


class BookPair:
    def __init__(self, up_token: str, down_token: str):
        self.books = {
            up_token: LocalBook(up_token),
            down_token: LocalBook(down_token),
        }
        self.up_token = up_token
        self.down_token = down_token
        self.last_touch_monotonic = 0.0

    @property
    def up(self) -> LocalBook:
        return self.books[self.up_token]

    @property
    def down(self) -> LocalBook:
        return self.books[self.down_token]

    @property
    def initialized(self) -> bool:
        return all(book.update_count > 0 and book.synchronized for book in self.books.values())

    def apply(self, message: dict[str, Any], recv_monotonic: float | None = None) -> None:
        event = str(message.get("event_type") or message.get("type") or "")
        if event == "book":
            asset = str(message.get("asset_id") or "")
            if asset in self.books:
                self.books[asset].apply_snapshot(message, recv_monotonic)
            return
        if event == "price_change":
            final_expected: dict[str, tuple[Any, Any]] = {}
            for change in message.get("price_changes") or []:
                asset = str(change.get("asset_id") or "")
                if asset in self.books:
                    self.books[asset].apply_change(
                        change,
                        message.get("timestamp"),
                        recv_monotonic,
                        validate_expected=False,
                    )
                    final_expected[asset] = (change.get("best_bid"), change.get("best_ask"))
            # A price_change event is a batch.  Validate only after the complete
            # batch, otherwise an intermediate level can look falsely desynced.
            for asset, expected in final_expected.items():
                self.books[asset].validate_best(*expected)
            return
        if event == "best_bid_ask":
            asset = str(message.get("asset_id") or "")
            if asset in self.books:
                self.books[asset].apply_best(message, recv_monotonic)

    def touch(self, recv_monotonic: float | None = None) -> None:
        """Record channel liveness when an ignored delta did not change top."""
        recv = recv_monotonic or time.monotonic()
        if recv - self.last_touch_monotonic < 0.1:
            return
        self.last_touch_monotonic = recv
        for book in self.books.values():
            if book.synchronized:
                book.recv_monotonic = recv

    def healthy(self, max_age_ms: float, max_spread: float, now_monotonic: float | None = None) -> bool:
        for book in (self.up, self.down):
            age = book.age_ms(now_monotonic)
            if not book.synchronized or age is None or age > max_age_ms:
                return False
            if (
                book.best_bid is None
                or book.best_ask is None
                or not 0 < book.best_bid < book.best_ask < 1
                or book.spread is None
                or book.spread > max_spread
            ):
                return False
        return True
