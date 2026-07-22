"""Discovery and fee math for Polymarket's short crypto Up/Down markets."""
from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def window_start_epoch(now_epoch: float, window_seconds: int = 300) -> int:
    return int(now_epoch // window_seconds) * window_seconds


def market_slug(asset: str, start_epoch: int, window_seconds: int = 300) -> str:
    minutes = window_seconds // 60
    return f"{asset.lower()}-updown-{minutes}m-{start_epoch}"


def crypto_fee_per_share(price: float, fee_rate: float) -> float:
    """Current crypto fee curve: C * fee_rate * p * (1-p), per share."""
    p = min(1.0, max(0.0, float(price)))
    return float(fee_rate) * p * (1.0 - p)


@dataclass(frozen=True)
class FastMarket:
    condition_id: str
    slug: str
    question: str
    up_token: str
    down_token: str
    start_epoch: int
    end_epoch: int
    fee_rate: float
    maker_rebate: float
    tick_size: float
    min_order_size: float
    taker_delay_enabled: bool = False
    taker_delay_ms: int = 0
    taker_delay_verified: bool = False

    @property
    def seconds_total(self) -> int:
        return self.end_epoch - self.start_epoch

    @property
    def url(self) -> str:
        return f"https://polymarket.com/event/{self.slug}"

    def token_for(self, outcome: str) -> str:
        return self.up_token if outcome.upper() == "UP" else self.down_token

    def to_dict(self) -> dict[str, Any]:
        return {
            "condition_id": self.condition_id,
            "slug": self.slug,
            "question": self.question,
            "up_token": self.up_token,
            "down_token": self.down_token,
            "start_epoch": self.start_epoch,
            "end_epoch": self.end_epoch,
            "fee_rate": self.fee_rate,
            "maker_rebate": self.maker_rebate,
            "tick_size": self.tick_size,
            "min_order_size": self.min_order_size,
            "taker_delay_ms": self.taker_delay_ms,
            "itode": self.taker_delay_enabled,
            "taker_delay_verified": self.taker_delay_verified,
            "url": self.url,
        }


def parse_fast_market(raw: dict[str, Any], fallback_start: int, window_seconds: int = 300) -> FastMarket:
    outcomes = [str(x).lower() for x in _json_list(raw.get("outcomes"))]
    tokens = [str(x) for x in _json_list(raw.get("clobTokenIds"))]
    if len(outcomes) != 2 or len(tokens) != 2:
        raise ValueError("market does not expose two outcome tokens")
    try:
        up = tokens[outcomes.index("up")]
        down = tokens[outcomes.index("down")]
    except ValueError as exc:
        raise ValueError(f"expected Up/Down outcomes, got {outcomes}") from exc

    slug = str(raw.get("slug") or "")
    try:
        start = int(slug.rsplit("-", 1)[1])
    except (IndexError, ValueError):
        start = fallback_start
    end = start + window_seconds
    if raw.get("endDate"):
        try:
            dt = datetime.fromisoformat(str(raw["endDate"]).replace("Z", "+00:00"))
            end = int(dt.astimezone(timezone.utc).timestamp())
        except ValueError:
            pass

    rewards = raw.get("rewards") or {}
    delay_value = raw.get("secondsDelay")
    if delay_value is None:
        delay_value = raw.get("seconds_delay")
    try:
        taker_delay_ms = max(0, round(float(delay_value) * 1000))
        taker_delay_verified = True
    except (TypeError, ValueError):
        taker_delay_ms = 0
        taker_delay_verified = False
    return FastMarket(
        condition_id=str(raw.get("conditionId") or raw.get("condition_id") or raw.get("id") or ""),
        slug=slug,
        question=str(raw.get("question") or ""),
        up_token=up,
        down_token=down,
        start_epoch=start,
        end_epoch=end,
        fee_rate=float(raw.get("feeRate") or raw.get("fee_rate") or 0.07),
        maker_rebate=float(raw.get("makerRebate") or rewards.get("maker_rebate") or 0.20),
        tick_size=float(raw.get("orderPriceMinTickSize") or raw.get("minimumTickSize") or 0.01),
        min_order_size=float(raw.get("orderMinSize") or raw.get("minimumOrderSize") or 5.0),
        taker_delay_ms=taker_delay_ms,
        taker_delay_verified=taker_delay_verified,
    )


def apply_clob_market_info(market: FastMarket, raw: dict[str, Any]) -> FastMarket:
    """Merge latency-critical CLOB metadata into a Gamma-discovered market.

    The compact `itode` field only says whether the delay feature is enabled.
    It does not contain a duration, so it must never be converted into a guessed
    number of milliseconds. The documented numeric source is `seconds_delay`
    from the public CLOB market response.
    """
    try:
        tick_size = float(raw.get("mts") or market.tick_size)
    except (TypeError, ValueError):
        tick_size = market.tick_size
    try:
        min_order_size = float(raw.get("mos") or market.min_order_size)
    except (TypeError, ValueError):
        min_order_size = market.min_order_size
    return replace(
        market,
        tick_size=tick_size,
        min_order_size=min_order_size,
        taker_delay_enabled=bool(raw.get("itode", market.taker_delay_enabled)),
    )


def apply_market_processing_info(market: FastMarket, raw: dict[str, Any]) -> FastMarket:
    """Apply the documented numeric order-processing delay from CLOB market data."""
    delay_value = raw.get("seconds_delay")
    if delay_value is None:
        delay_value = raw.get("secondsDelay")
    try:
        delay_ms = max(0, round(float(delay_value) * 1000))
    except (TypeError, ValueError):
        return replace(market, taker_delay_verified=False)
    return replace(market, taker_delay_ms=delay_ms, taker_delay_verified=True)


def select_market_payload(payload: Any) -> dict[str, Any] | None:
    """Accept both Gamma /events?slug= and /markets?slug= response shapes."""
    rows = payload if isinstance(payload, list) else [payload]
    for row in rows:
        if not isinstance(row, dict):
            continue
        markets = row.get("markets")
        if isinstance(markets, list):
            for market in markets:
                if isinstance(market, dict) and not market.get("closed", False):
                    return market
        elif row.get("clobTokenIds"):
            return row
    return None


def select_any_market_payload(payload: Any) -> dict[str, Any] | None:
    """Return a market even after it closes (used for official settlement)."""
    rows = payload if isinstance(payload, list) else [payload]
    for row in rows:
        if not isinstance(row, dict):
            continue
        markets = row.get("markets")
        if isinstance(markets, list):
            for market in markets:
                if isinstance(market, dict) and market.get("clobTokenIds"):
                    return market
        elif row.get("clobTokenIds"):
            return row
    return None


def resolved_fast_outcome(raw: dict[str, Any]) -> str | None:
    """Read a final Up/Down winner from Gamma's outcome prices.

    Settlement is deliberately strict: an outcome must be at least 0.99 and the
    other at most 0.01.  Anything less remains pending instead of guessing.
    """
    outcomes = [str(item).upper() for item in _json_list(raw.get("outcomes"))]
    prices_raw = _json_list(raw.get("outcomePrices"))
    try:
        prices = [float(item) for item in prices_raw]
    except (TypeError, ValueError):
        return None
    if len(outcomes) != 2 or len(prices) != 2 or set(outcomes) != {"UP", "DOWN"}:
        return None
    winner = max(range(2), key=prices.__getitem__)
    loser = 1 - winner
    if prices[winner] < 0.99 or prices[loser] > 0.01:
        return None
    return outcomes[winner]
