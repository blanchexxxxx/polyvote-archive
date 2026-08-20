"""Fee-aware complete-set arbitrage math for shadow observation.

The unity-constraint detector is adapted from the MIT-licensed ``cryptuon/polybot``
arbitrage strategy.  The implementation here deliberately replaces its fixed fee
estimate and top-of-book sizing with Polymarket's dynamic fee curve and an
equal-share walk through both books.

This module only evaluates snapshots.  It cannot make a two-leg CLOB submission
atomic, so a positive result is an *opportunity*, not a guaranteed executable
profit.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Any, Iterable


D = Decimal


@dataclass(frozen=True)
class Level:
    price: Decimal
    size: Decimal


@dataclass(frozen=True)
class CompleteSetOpportunity:
    condition_id: str
    direction: str
    shares: Decimal
    outcome_prices: tuple[Decimal, Decimal]
    raw_value_usd: Decimal
    fees_usd: Decimal
    execution_buffer_usd: Decimal
    fixed_cost_buffer_usd: Decimal
    capital_usd: Decimal
    gross_profit_usd: Decimal
    net_profit_usd: Decimal
    net_return: Decimal

    def to_dict(self) -> dict[str, Any]:
        return {
            key: float(value) if isinstance(value, Decimal) else value
            for key, value in asdict(self).items()
        } | {"outcome_prices": [float(value) for value in self.outcome_prices]}


def fee_per_share(price: Decimal, fee_rate: Decimal) -> Decimal:
    """Polymarket taker fee curve: ``rate * p * (1-p)`` per share."""
    price = min(D("1"), max(D("0"), price))
    return fee_rate * price * (D("1") - price)


def parse_levels(rows: Iterable[dict[str, Any]], *, descending: bool) -> list[Level]:
    levels: list[Level] = []
    for row in rows:
        try:
            price = D(str(row.get("price")))
            size = D(str(row.get("size")))
        except (ValueError, TypeError, ArithmeticError):
            continue
        if D("0") < price < D("1") and size > 0:
            levels.append(Level(price, size))
    levels.sort(key=lambda level: level.price, reverse=descending)
    return levels


def top_margins(
    first_book: dict[str, Any],
    second_book: dict[str, Any],
    *,
    fee_rate: float,
    execution_buffer_per_set: float = 0.0,
) -> dict[str, float | None]:
    """Return fee-adjusted marginal profit per complete set at top of book."""
    asks = [
        parse_levels(book.get("asks") or [], descending=False)
        for book in (first_book, second_book)
    ]
    bids = [
        parse_levels(book.get("bids") or [], descending=True)
        for book in (first_book, second_book)
    ]
    rate = D(str(fee_rate))
    buffer = D(str(execution_buffer_per_set))
    buy = None
    sell = None
    if all(asks):
        prices = (asks[0][0].price, asks[1][0].price)
        buy = D("1") - sum(prices) - sum(fee_per_share(p, rate) for p in prices) - buffer
    if all(bids):
        prices = (bids[0][0].price, bids[1][0].price)
        sell = sum(prices) - sum(fee_per_share(p, rate) for p in prices) - D("1") - buffer
    return {
        "buy_set": float(buy) if buy is not None else None,
        "sell_set": float(sell) if sell is not None else None,
    }


def analyze_complete_sets(
    condition_id: str,
    first_book: dict[str, Any],
    second_book: dict[str, Any],
    *,
    fee_rate: float,
    min_order_size: float,
    max_capital_usd: float,
    min_net_profit_usd: float,
    min_net_return: float,
    execution_buffer_per_set: float = 0.0,
    fixed_cost_buffer_usd: float = 0.0,
) -> list[CompleteSetOpportunity]:
    """Walk equal quantities through both books and return viable set trades.

    BUY_SET buys both outcomes then merges them for one collateral unit.
    SELL_SET starts from a split complete set and sells both outcomes.  Both are
    evaluated because they share the same unity constraint, but neither result
    claims atomic two-leg execution.
    """
    rate = D(str(fee_rate))
    minimum = D(str(min_order_size))
    capital_limit = D(str(max_capital_usd))
    profit_floor = D(str(min_net_profit_usd))
    return_floor = D(str(min_net_return))
    execution_buffer = D(str(execution_buffer_per_set))
    fixed_buffer = D(str(fixed_cost_buffer_usd))
    opportunities: list[CompleteSetOpportunity] = []

    pairs = (
        (
            "BUY_SET",
            parse_levels(first_book.get("asks") or [], descending=False),
            parse_levels(second_book.get("asks") or [], descending=False),
        ),
        (
            "SELL_SET",
            parse_levels(first_book.get("bids") or [], descending=True),
            parse_levels(second_book.get("bids") or [], descending=True),
        ),
    )
    for direction, first_levels, second_levels in pairs:
        if not first_levels or not second_levels or capital_limit <= 0:
            continue
        i = j = 0
        remaining_first = first_levels[0].size
        remaining_second = second_levels[0].size
        shares = D("0")
        leg_values = [D("0"), D("0")]
        fees = D("0")
        gross_profit = D("0")
        buffer_total = D("0")
        capital = D("0")

        while i < len(first_levels) and j < len(second_levels):
            prices = (first_levels[i].price, second_levels[j].price)
            tranche_fees = sum(fee_per_share(price, rate) for price in prices)
            if direction == "BUY_SET":
                unit_capital = sum(prices) + tranche_fees
                unit_gross = D("1") - sum(prices)
            else:
                unit_capital = D("1")
                unit_gross = sum(prices) - D("1")
            unit_net = unit_gross - tranche_fees - execution_buffer
            # Deeper asks can only rise and deeper bids can only fall.  Once the
            # marginal set is non-positive, no later tranche can repair it.
            if unit_net <= 0 or unit_capital <= 0:
                break
            capacity = (capital_limit - capital) / unit_capital
            quantity = min(remaining_first, remaining_second, capacity)
            if quantity <= 0:
                break
            shares += quantity
            leg_values[0] += prices[0] * quantity
            leg_values[1] += prices[1] * quantity
            fees += tranche_fees * quantity
            gross_profit += unit_gross * quantity
            buffer_total += execution_buffer * quantity
            capital += unit_capital * quantity
            remaining_first -= quantity
            remaining_second -= quantity
            if remaining_first <= 0:
                i += 1
                if i < len(first_levels):
                    remaining_first = first_levels[i].size
            if remaining_second <= 0:
                j += 1
                if j < len(second_levels):
                    remaining_second = second_levels[j].size
            if capital >= capital_limit:
                break

        net_profit = gross_profit - fees - buffer_total - fixed_buffer
        if shares < minimum or capital <= 0:
            continue
        net_return = net_profit / capital
        if net_profit < profit_floor or net_return < return_floor:
            continue
        opportunities.append(CompleteSetOpportunity(
            condition_id=condition_id,
            direction=direction,
            shares=shares,
            outcome_prices=(leg_values[0] / shares, leg_values[1] / shares),
            raw_value_usd=sum(leg_values),
            fees_usd=fees,
            execution_buffer_usd=buffer_total,
            fixed_cost_buffer_usd=fixed_buffer,
            capital_usd=capital,
            gross_profit_usd=gross_profit,
            net_profit_usd=net_profit,
            net_return=net_return,
        ))
    return opportunities
