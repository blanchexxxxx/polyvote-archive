#!/usr/bin/env python3
"""Finite-cash holdout follower for independent fast-market observations.

The process opens no feeds and submits no real orders.  It follows observer
records created after a frozen timestamp, applies a fixed pre-declared filter,
and mirrors realistic arrival fills into separate crash-safe cash ledgers.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import signal
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from polyvote.fastledger import FastLedger
from polyvote.faststate import write_state


log = logging.getLogger("polyvote.fastchallenger")
HALTED_POLL_SECONDS = 10.0


@dataclass(frozen=True)
class AssetSpec:
    asset: str
    allocation_usd: float
    max_order_usd: float
    max_daily_loss_usd: float
    minimum_cash_reserve_usd: float


def _timestamp(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def _poll_delay(base_seconds: float, status: str) -> float:
    """Back off disk-heavy bookkeeping after the challenger is latched.

    The incremental reader retains its rowid cursor, so a slower halted cycle
    still consumes every observer record while avoiding repeated full account
    summaries and state rewrites for a book that can no longer place fills.
    """
    if status == "HALTED":
        return max(base_seconds, HALTED_POLL_SECONDS)
    return base_seconds


class CashChallenger:
    def __init__(self, config_path: str | Path):
        self.config_path = Path(config_path).resolve()
        self.root = self.config_path.parent
        self.config = yaml.safe_load(
            self.config_path.read_text(encoding="utf-8")
        ) or {}
        if self.config.get("schema") != "fast-cash-challenger-v1":
            raise ValueError("unsupported challenger schema")
        self.experiment_id = str(self.config["experiment_id"])
        self.frozen_at_utc = str(self.config["frozen_at_utc"])
        self.frozen_ts = _timestamp(self.frozen_at_utc)
        self.rules = dict(self.config.get("rules") or {})
        self.promotion_gate = dict(self.config.get("promotion_gate") or {})
        self.source_virtual_order_usd = float(
            self.config.get("source_virtual_order_usd", 5.0)
        )
        if self.source_virtual_order_usd <= 0:
            raise ValueError("source_virtual_order_usd must be positive")
        self.specs: dict[str, AssetSpec] = {}
        for row in self.config.get("assets") or []:
            spec = AssetSpec(
                asset=str(row["asset"]).upper(),
                allocation_usd=float(row["allocation_usd"]),
                max_order_usd=float(row["max_order_usd"]),
                max_daily_loss_usd=float(row["max_daily_loss_usd"]),
                minimum_cash_reserve_usd=float(
                    row.get("minimum_cash_reserve_usd", 0)
                ),
            )
            if spec.asset in self.specs:
                raise ValueError(f"duplicate challenger asset: {spec.asset}")
            self.specs[spec.asset] = spec
        if not self.specs:
            raise ValueError("challenger must define assets")
        allocated = sum(spec.allocation_usd for spec in self.specs.values())
        expected = float(self.config["total_capital_usd"])
        if abs(allocated - expected) > 1e-8:
            raise ValueError(
                f"challenger allocations total ${allocated:.2f}, expected ${expected:.2f}"
            )
        source_pattern = str(
            self.config.get("source_observer_pattern", "fast_observer_{asset}.db")
        )
        ledger_pattern = str(
            self.config.get(
                "ledger_pattern", "fast_challenger_favorite_v1_{asset}.db"
            )
        )
        self.source_paths = {
            asset: self.root / source_pattern.format(asset=asset.lower())
            for asset in self.specs
        }
        self.ledgers = {
            asset: FastLedger(
                self.root / ledger_pattern.format(asset=asset.lower()),
                spec.allocation_usd,
            )
            for asset, spec in self.specs.items()
        }
        self.state_path = self.root / str(
            self.config.get("state_path", "fast_challenger_state.json")
        )
        self.incident_path = self.root / str(
            self.config.get("incident_path", "fast_challenger_incident.json")
        )
        self.done: set[str] = set()
        self.source_rowids = {asset: 0 for asset in self.specs}
        self.pending = {asset: set() for asset in self.specs}

    def _source_rows(self) -> list[dict[str, Any]]:
        rows: dict[str, dict[str, Any]] = {}
        for asset, path in self.source_paths.items():
            if not path.exists():
                continue
            uri = f"{path.resolve().as_uri()}?mode=ro"
            with closing(sqlite3.connect(uri, uri=True, timeout=15)) as connection:
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA query_only=ON")
                for row in connection.execute(
                    """SELECT rowid source_rowid,* FROM observations
                       WHERE created_ts>=? AND rowid>?
                       ORDER BY rowid""",
                    (self.frozen_ts, self.source_rowids[asset]),
                ):
                    item = {**dict(row), "asset": asset}
                    rows[str(item["observation_id"])] = item
                    self.source_rowids[asset] = max(
                        self.source_rowids[asset], int(item["source_rowid"])
                    )
                pending = sorted(self.pending[asset])
                for start in range(0, len(pending), 200):
                    batch = pending[start:start + 200]
                    placeholders = ",".join("?" for _ in batch)
                    for row in connection.execute(
                        f"""SELECT rowid source_rowid,* FROM observations
                            WHERE observation_id IN ({placeholders})""",
                        batch,
                    ):
                        item = {**dict(row), "asset": asset}
                        rows[str(item["observation_id"])] = item
        return sorted(rows.values(), key=lambda row: float(row["created_ts"]))

    def _intent_id(self, row: dict[str, Any]) -> str:
        ledger = self.ledgers[str(row["asset"])]
        return (
            f"{ledger.run_id}-{self.experiment_id}-"
            f"{str(row['observation_id']).removeprefix('obs-')}"
        )

    def _global_open_positions(self) -> int:
        return sum(
            len(ledger.state().get("open_positions") or [])
            for ledger in self.ledgers.values()
        )

    def _pre_reject_reason(
        self,
        row: dict[str, Any],
        spec: AssetSpec,
        target_shares: float,
    ) -> str | None:
        if self.incident_path.exists():
            return "challenger_portfolio_incident_latched"
        if float(row["intended_ask"]) < float(
            self.rules.get("min_entry_price", 0)
        ):
            return "challenger_entry_price_below_floor"
        if float(row["fair_probability"]) < float(
            self.rules.get("min_selected_probability", 0)
        ):
            return "challenger_selected_probability_below_threshold"
        if float(row["net_edge"]) < float(self.rules.get("min_net_edge", 0)):
            return "challenger_net_edge_below_threshold"
        account = self.ledgers[spec.asset].state()
        if float(account.get("daily_pnl_usd") or 0) <= -spec.max_daily_loss_usd:
            return "challenger_asset_daily_loss_gate"
        if self._global_open_positions() >= int(
            self.config.get("max_open_positions", len(self.specs))
        ):
            return "challenger_portfolio_open_position_gate"
        if target_shares + 1e-9 < float(
            self.rules.get("minimum_order_shares", 5)
        ):
            return "challenger_minimum_order_exceeds_cash_cap"
        return None

    def _ensure_intent(self, row: dict[str, Any]) -> dict[str, Any]:
        asset = str(row["asset"])
        spec = self.specs[asset]
        ledger = self.ledgers[asset]
        ledger.ensure_window(
            {
                "condition_id": str(row["condition_id"]),
                "slug": str(row["slug"]),
                "start_epoch": int(row["start_epoch"]),
                "end_epoch": int(row["end_epoch"]),
            }
        )
        source_target = float(row["target_shares"])
        inferred_limit_unit_cost = (
            self.source_virtual_order_usd / source_target
            if source_target > 0
            else float(row["limit_price"])
        )
        source_filled_shares = float(row["shares"] or 0)
        if source_filled_shares > 0 and row["total_cost_usd"] is not None:
            inferred_limit_unit_cost = max(
                inferred_limit_unit_cost,
                float(row["total_cost_usd"]) / source_filled_shares,
            )
        target_shares = (
            math.floor(
                min(
                    source_target,
                    spec.max_order_usd / inferred_limit_unit_cost,
                )
                * 10_000
            )
            / 10_000
            if source_target > 0 and inferred_limit_unit_cost > 0
            else 0.0
        )
        locked = target_shares * inferred_limit_unit_cost
        pre_reject = self._pre_reject_reason(row, spec, target_shares)
        return ledger.create_intent(
            {
                "intent_id": self._intent_id(row),
                "condition_id": str(row["condition_id"]),
                "created_ts": float(row["created_ts"]),
                "due_ts": float(row["due_ts"]),
                "outcome": str(row["outcome"]),
                "fair_probability": float(row["fair_probability"]),
                "intended_ask": float(row["intended_ask"]),
                "limit_price": float(row["limit_price"]),
                "net_edge": float(row["net_edge"]),
                "target_shares": target_shares,
                "locked_usd": locked,
                "platform_delay_ms": 0.0,
                "outbound_delay_ms": max(
                    0.0,
                    (float(row["due_ts"]) - float(row["created_ts"])) * 1000,
                ),
                "decision_compute_ms": 0.0,
                "pre_reject_reason": pre_reject,
                "details": {
                    "simulation": "finite-cash-observer-follower",
                    "experiment_id": self.experiment_id,
                    "frozen_at_utc": self.frozen_at_utc,
                    "source_observation_id": row["observation_id"],
                    "frozen_rules": self.rules,
                },
            },
            spec.minimum_cash_reserve_usd,
        )

    def _mirror_fill(
        self, row: dict[str, Any], intent: dict[str, Any]
    ) -> dict[str, Any] | None:
        ledger = self.ledgers[str(row["asset"])]
        intent_id = str(intent["intent_id"])
        if intent["status"] != "submitted":
            return None
        if row["status"] == "rejected":
            ledger.finalize_fill(
                intent_id,
                None,
                f"source_observer_{row['reject_reason'] or 'rejected'}",
            )
            return None
        if row["status"] not in {"filled", "settled"}:
            return None
        source_shares = float(row["shares"] or 0)
        shares = min(float(intent["target_shares"]), source_shares)
        minimum = float(self.rules.get("minimum_order_shares", 5))
        if source_shares <= 0 or shares + 1e-9 < minimum:
            ledger.finalize_fill(
                intent_id, None, "source_observer_depth_below_challenger_minimum"
            )
            return None
        ratio = shares / source_shares
        fill = {
            "executed_ts": float(row["executed_ts"]),
            "shares": shares,
            "average_price": float(row["average_price"]),
            "notional_usd": float(row["notional_usd"]) * ratio,
            "fee_usd": float(row["fee_usd"]) * ratio,
            "total_cost_usd": float(row["total_cost_usd"]) * ratio,
            "slippage_usd": float(row["slippage_usd"]) * ratio,
            "latency_ms": float(row["latency_ms"]),
            "depth": [{
                "source": "independent-observer-arrival-fill",
                "source_observation_id": row["observation_id"],
                "source_shares": source_shares,
                "challenger_shares": shares,
            }],
        }
        return ledger.finalize_fill(intent_id, fill)

    def _process_row(self, row: dict[str, Any]) -> bool:
        source_id = str(row["observation_id"])
        if source_id in self.done:
            return True
        intent = self._ensure_intent(row)
        if intent["status"] == "rejected":
            self.done.add(source_id)
            return True
        self._mirror_fill(row, intent)
        if row["status"] == "settled":
            self.ledgers[str(row["asset"])].settle_position(
                f"pos-{intent['intent_id']}",
                str(row["official_outcome"]),
                settled_ts=float(row["settled_ts"] or time.time()),
            )
            self.done.add(source_id)
            return True
        if row["status"] == "rejected":
            self.done.add(source_id)
            return True
        return False

    def _maybe_latch_incident(self) -> dict[str, Any] | None:
        if self.incident_path.exists():
            return json.loads(self.incident_path.read_text(encoding="utf-8"))
        states = {
            asset: ledger.state() for asset, ledger in self.ledgers.items()
        }
        daily_pnl = sum(
            float(state.get("daily_pnl_usd") or 0) for state in states.values()
        )
        limit = float(self.config.get("max_daily_loss_usd", 6))
        if daily_pnl > -limit:
            return None
        incident = {
            "schema": "fast-challenger-incident-v1",
            "experiment_id": self.experiment_id,
            "triggered_at": time.time(),
            "trigger": "challenger_portfolio_daily_loss_gate",
            "daily_pnl_usd": round(daily_pnl, 6),
            "daily_loss_limit_usd": limit,
            "action": "latch this challenger only; champion and observer unchanged",
        }
        if bool(self.config.get("latch_on_daily_loss", True)):
            write_state(self.incident_path, incident)
        return incident

    def state(self) -> dict[str, Any]:
        accounts = {
            asset: ledger.state() for asset, ledger in self.ledgers.items()
        }
        numeric = (
            "initial_capital_usd", "cash_usd", "locked_usd", "market_value_usd",
            "equity_usd", "realized_pnl_usd", "daily_pnl_usd", "fees_usd",
            "max_drawdown_usd", "staked_usd",
        )
        totals = {
            key: round(
                sum(float(account.get(key) or 0) for account in accounts.values()),
                6,
            )
            for key in numeric
        }
        for key in ("fills", "settled", "wins", "rejects"):
            totals[key] = sum(
                int(account.get(key) or 0) for account in accounts.values()
            )
        recent: list[dict[str, Any]] = []
        for asset, account in accounts.items():
            recent.extend(
                {**trade, "asset": asset, "book": "challenger"}
                for trade in account.get("recent_trades") or []
            )
        totals["recent_trades"] = sorted(
            recent,
            key=lambda trade: float(trade.get("created_ts") or 0),
            reverse=True,
        )[:50]
        totals["pnl_usd"] = totals["realized_pnl_usd"]
        totals["win_rate"] = (
            round(totals["wins"] / totals["settled"], 6)
            if totals["settled"] else None
        )
        totals["roi"] = (
            round(totals["pnl_usd"] / totals["staked_usd"], 6)
            if totals["staked_usd"] else None
        )
        runtime_hours = max(0.0, (time.time() - self.frozen_ts) / 3600)
        incident = self._maybe_latch_incident()
        minimum_settled = int(
            self.promotion_gate.get("minimum_settled", 500)
        )
        minimum_hours = float(
            self.promotion_gate.get("minimum_runtime_hours", 336)
        )
        return {
            "schema": "fast-cash-challenger-state-v1",
            "ready": all(path.exists() for path in self.source_paths.values()),
            "mode": "SHADOW",
            "experiment_id": self.experiment_id,
            "hypothesis": self.config.get("hypothesis"),
            "frozen_at_utc": self.frozen_at_utc,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "status": "HALTED" if incident else "COLLECTING",
            "rules": self.rules,
            **totals,
            "open_positions": sum(
                len(account.get("open_positions") or [])
                for account in accounts.values()
            ),
            "accounts": accounts,
            "incident": incident,
            "promotion_gate": {
                **self.promotion_gate,
                "runtime_hours": round(runtime_hours, 3),
                "settled": totals["settled"],
                "eligible": (
                    not incident
                    and totals["settled"] >= minimum_settled
                    and runtime_hours >= minimum_hours
                    and totals["pnl_usd"] > 0
                ),
            },
        }

    def run_once(self) -> dict[str, Any]:
        for row in self._source_rows():
            asset = str(row["asset"])
            source_id = str(row["observation_id"])
            if self._process_row(row):
                self.pending[asset].discard(source_id)
            else:
                self.pending[asset].add(source_id)
        state = self.state()
        write_state(self.state_path, state)
        return state


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Finite-cash fast-market holdout challenger"
    )
    parser.add_argument("--config", default="fast_challenger.yaml")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    challenger = CashChallenger(args.config)
    if args.once:
        print(json.dumps(challenger.run_once(), ensure_ascii=False))
        return
    stopping = False

    def stop(*_args: Any) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    interval = max(0.2, float(challenger.config.get("poll_seconds", 1)))
    log.info(
        "starting %s finite-cash holdout at %s",
        challenger.experiment_id,
        challenger.frozen_at_utc,
    )
    while not stopping:
        delay = interval
        try:
            state = challenger.run_once()
            delay = _poll_delay(interval, str(state.get("status") or ""))
        except Exception:  # noqa: BLE001
            log.exception("challenger cycle failed closed")
        time.sleep(delay)


if __name__ == "__main__":
    main()
