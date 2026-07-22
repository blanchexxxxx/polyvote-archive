"""Deterministic reviews for the autonomous fast-market shadow campaign."""
from __future__ import annotations

import json
import math
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any

from .fastledger import FastLedger
from .faststate import write_state


def _ratio(numerator: float, denominator: float) -> float | None:
    return None if not denominator else numerator / denominator


def _wilson_interval(wins: int, total: int, z: float = 1.96) -> list[float] | None:
    if total <= 0:
        return None
    p = wins / total
    denominator = 1 + z * z / total
    center = (p + z * z / (2 * total)) / denominator
    radius = z * math.sqrt((p * (1 - p) + z * z / (4 * total)) / total) / denominator
    return [max(0.0, center - radius), min(1.0, center + radius)]


def _lesson_plan(
    gates: list[dict[str, Any]],
    issues: list[str],
    fill_count: int,
    settled_count: int,
    pnl: float,
    eligible_for_tuning: bool,
) -> dict[str, Any]:
    """Turn observed failures into bounded experiments, never silent mutation."""
    counts = {str(row["reason"]): int(row["count"]) for row in gates}
    ranked = sorted(counts.items(), key=lambda item: item[1], reverse=True)
    lessons: list[dict[str, Any]] = []
    if "controllable_latency_p95_above_50ms" in issues:
        lessons.append({
            "kind": "code",
            "lesson": "controllable latency is above the 50ms gate",
            "action": "inspect persistent-connection P95, loop lag and reconnects separately",
        })
    if any(name in counts for name in ("chainlink_fresh", "binance_fresh", "books_healthy")):
        lessons.append({
            "kind": "feed",
            "lesson": "feed freshness or orderbook health blocked entries",
            "action": "keep symbol-filtered RTDS snapshots and inactivity watchdogs; never relax freshness to create trades",
        })
    if settled_count < 30:
        lessons.append({
            "kind": "sample",
            "lesson": "too few settled trades to distinguish edge from variance",
            "action": "continue collection without changing the champion",
        })
    elif pnl < 0:
        lessons.append({
            "kind": "strategy",
            "lesson": "settled sample is negative after modeled costs",
            "action": "run a higher-edge challenger; do not average down or increase size",
        })

    proposals = [
        {
            "id": "strict-edge",
            "changes": {"min_net_edge": 0.055},
            "hypothesis": "fewer fills but less fee/slippage erosion",
        },
        {
            "id": "tight-execution",
            "changes": {"max_spread": 0.05, "execution_slippage_ticks": 1},
            "hypothesis": "lower adverse selection and tail slippage",
        },
        {
            "id": "slow-cadence",
            "changes": {"entry_cooldown_seconds": 20},
            "hypothesis": "reduce correlated repeat entries inside one five-minute window",
        },
    ]
    return {
        "top_no_trade_reasons": [
            {"reason": reason, "count": count} for reason, count in ranked[:5]
        ],
        "lessons": lessons,
        "parameter_proposals": proposals,
        "promotion_allowed": eligible_for_tuning,
        "mutation_applied": False,
        "next_review": "hourly; consolidated daily and weekly",
    }


def build_review(
    ledger: FastLedger,
    review_type: str,
    period_start: float,
    period_end: float,
    runtime: dict[str, Any],
) -> dict[str, Any]:
    with closing(sqlite3.connect(ledger.path, timeout=15)) as connection:
        connection.row_factory = sqlite3.Row
        intent_rows = connection.execute(
            """SELECT status,COUNT(*) count FROM intents
               WHERE run_id=? AND created_ts>=? AND created_ts<? GROUP BY status""",
            (ledger.run_id, period_start, period_end),
        ).fetchall()
        intents = {str(row["status"]): int(row["count"]) for row in intent_rows}
        fills = connection.execute(
            """SELECT COUNT(*) count,COALESCE(SUM(shares),0) shares,
                      COALESCE(SUM(notional_usd),0) notional,
                      COALESCE(SUM(fee_usd),0) fees,
                      COALESCE(SUM(slippage_usd),0) slippage,
                      AVG(latency_ms) avg_latency,MAX(latency_ms) max_latency
               FROM fills WHERE executed_ts>=? AND executed_ts<?""",
            (period_start, period_end),
        ).fetchone()
        settled = connection.execute(
            """SELECT COUNT(*) count,
                      COALESCE(SUM(CASE WHEN p.pnl_usd>0 THEN 1 ELSE 0 END),0) wins,
                      COALESCE(SUM(p.pnl_usd),0) pnl,
                      AVG((i.fair_probability-
                          CASE WHEN p.outcome=p.official_outcome THEN 1.0 ELSE 0.0 END) *
                          (i.fair_probability-
                          CASE WHEN p.outcome=p.official_outcome THEN 1.0 ELSE 0.0 END)) brier
               FROM positions p JOIN intents i USING(intent_id)
               WHERE p.run_id=? AND p.status='settled'
                 AND p.settled_ts>=? AND p.settled_ts<?""",
            (ledger.run_id, period_start, period_end),
        ).fetchone()
        gates = [dict(row) for row in connection.execute(
            """SELECT reason,SUM(count) count FROM gate_counts
               WHERE run_id=? AND updated_ts>=? AND updated_ts<?
               GROUP BY reason ORDER BY count DESC""",
            (ledger.run_id, period_start, period_end),
        ).fetchall()]
        windows = int(connection.execute(
            """SELECT COUNT(*) FROM windows
               WHERE run_id=? AND start_epoch>=? AND start_epoch<?""",
            (ledger.run_id, period_start, period_end),
        ).fetchone()[0])
        account = dict(connection.execute(
            "SELECT * FROM account WHERE run_id=?", (ledger.run_id,)
        ).fetchone())
        campaign_started = float(connection.execute(
            "SELECT started_ts FROM campaigns WHERE run_id=?", (ledger.run_id,)
        ).fetchone()[0])
        overdue = int(connection.execute(
            """SELECT COUNT(*) FROM intents
               WHERE run_id=? AND status='submitted' AND due_ts<?""",
            (ledger.run_id, time.time() - 2),
        ).fetchone()[0])

    total_intents = sum(intents.values())
    fill_count = int(fills["count"])
    settled_count = int(settled["count"])
    invariant_ok = (
        float(account["cash_usd"]) >= -1e-8
        and float(account["locked_usd"]) >= -1e-8
        and overdue == 0
    )
    issues: list[str] = []
    if not invariant_ok:
        issues.append("ledger_invariant_or_overdue_intent")
    network = runtime.get("network") or {}
    if not network.get("under_50ms", False):
        issues.append("controllable_latency_p95_above_50ms")
    if sum((network.get("reconnects") or {}).values()) > 0:
        issues.append("feed_reconnect_observed")
    if runtime.get("state_age_seconds", 0) > 2:
        issues.append("state_stale")

    all_time = ledger.state()
    eligible_for_tuning = (
        all_time["fills"] >= 500
        and time.time() - campaign_started >= 14 * 86400
        and invariant_ok
    )
    challenger: dict[str, Any] = {
        "eligible": eligible_for_tuning,
        "champion_mutated": False,
        "reason": (
            "requires >=14 complete days, >=500 realistic fills and healthy ledger"
            if not eligible_for_tuning
            else "eligible for a parallel challenger; promotion still requires frozen holdout"
        ),
        "allowed_parameter_bounds": {
            "min_net_edge": [0.03, 0.10],
            "execution_slippage_ticks": [0, 3],
            "max_spread": [0.03, 0.08],
        },
        "forbidden": [
            "fees",
            "platform_delay",
            "initial_capital",
            "cash_reserve",
            "daily_loss_limit",
            "live_mode",
        ],
    }
    lesson_plan = _lesson_plan(
        gates,
        issues,
        fill_count,
        settled_count,
        float(settled["pnl"]),
        eligible_for_tuning,
    )
    observer = runtime.get("observer") or {}
    observer_settled = int(observer.get("settled") or 0)
    observer_wins = int(observer.get("wins") or 0)
    observer_pnl = float(observer.get("pnl_usd") or 0.0)
    observer_started = float(observer.get("started_ts") or time.time())
    observer_runtime_days = max(0.0, (time.time() - observer_started) / 86400)
    observer_gate_passed = (
        observer_settled >= 500 and observer_runtime_days >= 14
    )
    if observer_settled < 30:
        observer_verdict = "COLLECT_INDEPENDENT_DATA"
        observer_action = "keep parameters frozen; no inference from tiny samples"
    elif observer_pnl <= 0:
        observer_verdict = "HYPOTHESIS_NOT_SUPPORTED"
        observer_action = "do not promote; propose a separate bounded challenger"
    elif not observer_gate_passed:
        observer_verdict = "PROMISING_BUT_UNDER_GATE"
        observer_action = "continue frozen holdout until 14 days and 500 settlements"
    else:
        observer_verdict = "ELIGIBLE_FOR_FROZEN_HOLDOUT_REVIEW"
        observer_action = "manual review only; no automatic funded promotion"
    observer_review = {
        "capital_usd": 0.0,
        "sample": {
            "candidates": int(observer.get("candidates") or 0),
            "fills": int(observer.get("fills") or 0),
            "settled": observer_settled,
            "wins": observer_wins,
        },
        "performance": {
            "accuracy": _ratio(observer_wins, observer_settled),
            "accuracy_wilson_95": _wilson_interval(
                observer_wins, observer_settled
            ),
            "normalized_pnl_usd": observer_pnl,
            "staked_usd": float(observer.get("staked_usd") or 0.0),
            "fees_usd": float(observer.get("fees_usd") or 0.0),
            "average_arrival_latency_ms": observer.get("average_latency_ms"),
        },
        "promotion_gate": {
            "minimum_settled": 500,
            "minimum_runtime_days": 14,
            "runtime_days": observer_runtime_days,
            "passed": observer_gate_passed,
        },
        "verdict": observer_verdict,
        "automatic_action": observer_action,
        "code_mutated": False,
    }
    return {
        "schema": "fast-review-v2",
        "run_id": ledger.run_id,
        "review_type": review_type,
        "period_start": period_start,
        "period_end": period_end,
        "generated_at": time.time(),
        "sample": {
            "windows": windows,
            "intents": intents,
            "fills": fill_count,
            "settled": settled_count,
        },
        "execution": {
            "fill_rate": _ratio(fill_count, total_intents),
            "shares": float(fills["shares"]),
            "notional_usd": float(fills["notional"]),
            "fees_usd": float(fills["fees"]),
            "slippage_usd": float(fills["slippage"]),
            "average_latency_ms": fills["avg_latency"],
            "max_latency_ms": fills["max_latency"],
        },
        "performance": {
            "wins": int(settled["wins"]),
            "win_rate": _ratio(int(settled["wins"]), settled_count),
            "pnl_usd": float(settled["pnl"]),
            "brier_score": settled["brier"],
            "all_time_equity_usd": all_time["equity_usd"],
            "all_time_max_drawdown_usd": all_time["max_drawdown_usd"],
        },
        "no_trade_reasons": gates,
        "health": {
            "ledger_invariant_ok": invariant_ok,
            "overdue_intents": overdue,
            "network": network,
            "feed_rejects": runtime.get("feed_rejects") or {},
            "issues": issues,
        },
        "challenger": challenger,
        "independent_observer": observer_review,
        "lesson_plan": lesson_plan,
        "verdict": (
            "HALT_AND_INVESTIGATE"
            if not invariant_ok
            else "COLLECT_MORE_DATA"
            if not eligible_for_tuning
            else "CHALLENGER_MAY_RUN_IN_PARALLEL"
        ),
    }


def run_due_reviews(
    ledger: FastLedger,
    review_path: str | Path,
    runtime: dict[str, Any],
    now: float | None = None,
) -> list[dict[str, Any]]:
    now = now or time.time()
    existing = ledger.recent_reviews(200)
    keys = {(item["review_type"], int(item["period_end"])) for item in existing}
    periods: list[tuple[str, float, float]] = []
    if not any(item["review_type"] == "startup" for item in existing):
        periods.append(("startup", now - 60, now))

    hour_end = math.floor(now / 3600) * 3600
    if hour_end > 0:
        periods.append(("hourly", hour_end - 3600, hour_end))
    day_end = math.floor(now / 86400) * 86400
    if day_end > 0:
        periods.append(("daily", day_end - 86400, day_end))
    week_end = day_end - ((time.gmtime(day_end).tm_wday) * 86400)
    if week_end > 0:
        periods.append(("weekly", week_end - 7 * 86400, week_end))

    created: list[dict[str, Any]] = []
    for review_type, start, end in periods:
        if (review_type, int(end)) in keys:
            continue
        report = build_review(ledger, review_type, start, end, runtime)
        ledger.record_review(review_type, start, end, report)
        created.append(report)

    bundle = {
        "schema": "fast-review-bundle-v2",
        "run_id": ledger.run_id,
        "updated_at": now,
        "reviews": ledger.recent_reviews(50),
    }
    write_state(Path(review_path), bundle)
    return created
