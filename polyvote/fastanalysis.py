"""Selection-aware counterfactual replay for settled fast shadow fills."""
from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any, Callable, Iterable


def _load_rows(ledgers: Iterable[tuple[str, Path]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for asset, path in ledgers:
        if not path.exists():
            continue
        with closing(sqlite3.connect(path, timeout=15)) as connection:
            connection.row_factory = sqlite3.Row
            for row in connection.execute(
                """SELECT p.condition_id,p.slug,p.outcome,p.official_outcome,
                          p.opened_ts,p.end_epoch,p.cost_usd,p.pnl_usd,
                          i.fair_probability,i.intended_ask,i.net_edge,
                          f.latency_ms,f.slippage_usd,f.fee_usd
                   FROM positions p
                   JOIN intents i USING(intent_id)
                   JOIN fills f USING(intent_id)
                   WHERE p.run_id=? AND p.status='settled'
                   ORDER BY p.opened_ts""",
                (_active_run(connection),),
            ):
                rows.append({**dict(row), "asset": asset})
    return sorted(rows, key=lambda row: float(row["opened_ts"]))


def _active_run(connection: sqlite3.Connection) -> str:
    row = connection.execute(
        "SELECT value FROM meta WHERE key='active_campaign'"
    ).fetchone()
    return str(row[0])


def _first_per_window(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str]] = set()
    output = []
    for row in rows:
        key = (str(row["asset"]), str(row["condition_id"]))
        if key in seen:
            continue
        seen.add(key)
        output.append(row)
    return output


def _metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    count = len(rows)
    wins = sum(1 for row in rows if float(row["pnl_usd"]) > 0)
    pnl = sum(float(row["pnl_usd"]) for row in rows)
    staked = sum(float(row["cost_usd"]) for row in rows)
    fees = sum(float(row["fee_usd"]) for row in rows)
    return {
        "trades": count,
        "wins": wins,
        "win_rate": round(wins / count, 6) if count else None,
        "pnl_usd": round(pnl, 6),
        "staked_usd": round(staked, 6),
        "roi": round(pnl / staked, 6) if staked else None,
        "fees_usd": round(fees, 6),
        "average_latency_ms": round(
            sum(float(row["latency_ms"]) for row in rows) / count, 3
        ) if count else None,
    }


def _scenario(
    scenario_id: str,
    rows: list[dict[str, Any]],
    predicate: Callable[[dict[str, Any]], bool] | None = None,
    first_only: bool = False,
) -> dict[str, Any]:
    selected = [row for row in rows if predicate is None or predicate(row)]
    if first_only:
        selected = _first_per_window(selected)
    return {"id": scenario_id, **_metrics(selected)}


def build_portfolio_replay(ledgers: Iterable[tuple[str, Path]]) -> dict[str, Any]:
    rows = _load_rows(ledgers)
    scenarios = [
        _scenario("observed_champion", rows),
        _scenario("one_entry_per_asset_window", rows, first_only=True),
        _scenario("edge_gte_0_055", rows, lambda row: float(row["net_edge"]) >= 0.055),
        _scenario("edge_gte_0_060", rows, lambda row: float(row["net_edge"]) >= 0.060),
        _scenario(
            "selected_probability_gte_0_55",
            rows,
            lambda row: float(row["fair_probability"]) >= 0.55,
            first_only=True,
        ),
        _scenario(
            "selected_probability_gte_0_60",
            rows,
            lambda row: float(row["fair_probability"]) >= 0.60,
            first_only=True,
        ),
        _scenario(
            "entry_price_gte_0_50",
            rows,
            lambda row: float(row["intended_ask"]) >= 0.50,
            first_only=True,
        ),
    ]
    repeated = len(rows) - len(_first_per_window(rows))
    champion = scenarios[0]
    strict_edge = next(item for item in scenarios if item["id"] == "edge_gte_0_055")
    favorite = next(
        item for item in scenarios if item["id"] == "selected_probability_gte_0_55"
    )
    return {
        "schema": "fast-portfolio-replay-v1",
        "settled_sample": len(rows),
        "selection_bias_warning": (
            "counterfactuals only filter champion fills; they do not include opportunities "
            "the champion rejected and cannot prove challenger profitability"
        ),
        "scenarios": scenarios,
        "diagnosis": {
            "repeat_entries_same_asset_window": repeated,
            "observed_win_rate": champion["win_rate"],
            "strict_edge_pnl_usd": strict_edge["pnl_usd"],
            "favorite_only_pnl_usd": favorite["pnl_usd"],
            "favorite_only_trades": favorite["trades"],
        },
        "lessons": [
            "repeated entries multiplied correlated five-minute risk",
            "raising the edge threshold alone did not repair the observed sample",
            "cheap-tail selections exposed severe probability miscalibration",
            "favorite-only behavior is a challenger hypothesis, not a promoted strategy",
        ],
        "next_experiment": {
            "id": "favorite-only-observer",
            "parameters": {
                "min_selected_probability": 0.55,
                "max_orders_per_window": 1,
                "max_spread": 0.05,
            },
            "capital_usd": 0,
            "promotion_gate": "new independent observations plus frozen holdout",
        },
    }
