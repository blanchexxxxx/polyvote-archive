"""Independent zero-capital challenger observations for fast markets.

The observer is deliberately separate from the funded shadow ledger.  It records
at most one hypothetical FAK arrival per market window and scores it only after
the official Gamma outcome is final.  The recorded stake is a normalization
unit, not spendable cash and never changes the champion campaign account.
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


class FastObserverLedger:
    def __init__(self, path: str | Path, asset: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.asset = str(asset).upper()
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=15000")
        return connection

    def _init_schema(self) -> None:
        with closing(self._connect()) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS observations (
                    observation_id TEXT PRIMARY KEY,
                    asset TEXT NOT NULL,
                    condition_id TEXT NOT NULL UNIQUE,
                    slug TEXT NOT NULL,
                    start_epoch INTEGER NOT NULL,
                    end_epoch INTEGER NOT NULL,
                    outcome TEXT NOT NULL,
                    fair_probability REAL NOT NULL,
                    intended_ask REAL NOT NULL,
                    limit_price REAL NOT NULL,
                    net_edge REAL NOT NULL,
                    target_shares REAL NOT NULL,
                    created_ts REAL NOT NULL,
                    due_ts REAL NOT NULL,
                    status TEXT NOT NULL,
                    reject_reason TEXT,
                    executed_ts REAL,
                    shares REAL,
                    average_price REAL,
                    notional_usd REAL,
                    fee_usd REAL,
                    total_cost_usd REAL,
                    slippage_usd REAL,
                    latency_ms REAL,
                    official_outcome TEXT,
                    payout_usd REAL,
                    pnl_usd REAL,
                    settled_ts REAL,
                    details_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_observer_status
                    ON observations(status, end_epoch);
                CREATE INDEX IF NOT EXISTS idx_observer_created
                    ON observations(created_ts DESC);
                CREATE TABLE IF NOT EXISTS observer_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            connection.execute(
                "INSERT OR IGNORE INTO observer_meta(key,value) VALUES ('started_ts',?)",
                (str(time.time()),),
            )

    def condition_ids(self) -> set[str]:
        with closing(self._connect()) as connection:
            return {
                str(row["condition_id"])
                for row in connection.execute("SELECT condition_id FROM observations")
            }

    def reject_pending_after_restart(self) -> int:
        """Fail closed when a process died between signal and arrival sampling."""
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """UPDATE observations
                   SET status='rejected',reject_reason='service_restart_before_arrival'
                   WHERE status='submitted'"""
            )
            connection.commit()
            return int(cursor.rowcount or 0)

    def create_candidate(self, row: dict[str, Any]) -> dict[str, Any]:
        now = float(row.get("created_ts") or time.time())
        observation_id = str(row.get("observation_id") or f"obs-{uuid.uuid4().hex}")
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM observations WHERE condition_id=?",
                (str(row["condition_id"]),),
            ).fetchone()
            if existing:
                connection.commit()
                return dict(existing)
            connection.execute(
                """INSERT INTO observations
                   (observation_id,asset,condition_id,slug,start_epoch,end_epoch,
                    outcome,fair_probability,intended_ask,limit_price,net_edge,
                    target_shares,created_ts,due_ts,status,reject_reason,details_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    observation_id,
                    self.asset,
                    str(row["condition_id"]),
                    str(row["slug"]),
                    int(row["start_epoch"]),
                    int(row["end_epoch"]),
                    str(row["outcome"]).upper(),
                    float(row["fair_probability"]),
                    float(row["intended_ask"]),
                    float(row["limit_price"]),
                    float(row["net_edge"]),
                    float(row["target_shares"]),
                    now,
                    float(row["due_ts"]),
                    "submitted",
                    None,
                    _json(row.get("details") or {}),
                ),
            )
            connection.commit()
            saved = connection.execute(
                "SELECT * FROM observations WHERE observation_id=?", (observation_id,)
            ).fetchone()
            return dict(saved)

    def reject(self, observation_id: str, reason: str) -> dict[str, Any]:
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """UPDATE observations SET status='rejected',reject_reason=?
                   WHERE observation_id=? AND status='submitted'""",
                (str(reason), observation_id),
            )
            connection.commit()
            row = connection.execute(
                "SELECT * FROM observations WHERE observation_id=?", (observation_id,)
            ).fetchone()
            return dict(row)

    def fill(self, observation_id: str, fill: dict[str, Any]) -> dict[str, Any]:
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """UPDATE observations
                   SET status='filled',executed_ts=?,shares=?,average_price=?,
                       notional_usd=?,fee_usd=?,total_cost_usd=?,slippage_usd=?,
                       latency_ms=?,details_json=?
                   WHERE observation_id=? AND status='submitted'""",
                (
                    float(fill["executed_ts"]),
                    float(fill["shares"]),
                    float(fill["average_price"]),
                    float(fill["notional_usd"]),
                    float(fill["fee_usd"]),
                    float(fill["total_cost_usd"]),
                    float(fill["slippage_usd"]),
                    float(fill["latency_ms"]),
                    _json(fill.get("details") or {}),
                    observation_id,
                ),
            )
            connection.commit()
            row = connection.execute(
                "SELECT * FROM observations WHERE observation_id=?", (observation_id,)
            ).fetchone()
            return dict(row)

    def open_observations(self) -> list[dict[str, Any]]:
        with closing(self._connect()) as connection:
            return [
                dict(row)
                for row in connection.execute(
                    """SELECT * FROM observations
                       WHERE status='filled' ORDER BY end_epoch,created_ts"""
                )
            ]

    def settle(self, observation_id: str, official_outcome: str) -> dict[str, Any]:
        official = str(official_outcome).upper()
        if official not in {"UP", "DOWN"}:
            raise ValueError(f"invalid official outcome: {official_outcome}")
        now = time.time()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM observations WHERE observation_id=?", (observation_id,)
            ).fetchone()
            if not row:
                connection.rollback()
                raise KeyError(observation_id)
            if row["status"] == "settled":
                connection.commit()
                return dict(row)
            if row["status"] != "filled":
                connection.rollback()
                raise ValueError(f"observation is not fillable: {row['status']}")
            payout = float(row["shares"]) if row["outcome"] == official else 0.0
            pnl = payout - float(row["total_cost_usd"])
            connection.execute(
                """UPDATE observations
                   SET status='settled',official_outcome=?,payout_usd=?,pnl_usd=?,
                       settled_ts=? WHERE observation_id=?""",
                (official, payout, pnl, now, observation_id),
            )
            connection.commit()
            saved = connection.execute(
                "SELECT * FROM observations WHERE observation_id=?", (observation_id,)
            ).fetchone()
            return dict(saved)

    def state(self) -> dict[str, Any]:
        with closing(self._connect()) as connection:
            started_row = connection.execute(
                "SELECT value FROM observer_meta WHERE key='started_ts'"
            ).fetchone()
            counts = connection.execute(
                """SELECT COUNT(*) candidates,
                          SUM(status IN ('filled','settled')) fills,
                          SUM(status='rejected') rejects,
                          SUM(status='filled') open_observations,
                          SUM(status='settled') settled,
                          SUM(status='settled' AND pnl_usd>0) wins,
                          COALESCE(SUM(CASE WHEN status='settled' THEN pnl_usd END),0) pnl_usd,
                          COALESCE(SUM(CASE WHEN status='settled' THEN total_cost_usd END),0) staked_usd,
                          COALESCE(SUM(CASE WHEN status IN ('filled','settled') THEN fee_usd END),0) fees_usd,
                          AVG(CASE WHEN status IN ('filled','settled') THEN latency_ms END) average_latency_ms
                   FROM observations"""
            ).fetchone()
            settled = int(counts["settled"] or 0)
            wins = int(counts["wins"] or 0)
            recent = [
                dict(row)
                for row in connection.execute(
                    """SELECT observation_id,asset,condition_id,slug,outcome,
                              fair_probability,intended_ask,net_edge,created_ts,status,
                              reject_reason,shares,average_price,fee_usd,total_cost_usd,
                              latency_ms,official_outcome,payout_usd,pnl_usd,settled_ts
                       FROM observations ORDER BY created_ts DESC LIMIT 30"""
                )
            ]
            return {
                "capital_usd": 0.0,
                "started_ts": float(started_row["value"]) if started_row else None,
                "candidates": int(counts["candidates"] or 0),
                "fills": int(counts["fills"] or 0),
                "rejects": int(counts["rejects"] or 0),
                "open_observations": int(counts["open_observations"] or 0),
                "settled": settled,
                "wins": wins,
                "accuracy": wins / settled if settled else None,
                "pnl_usd": float(counts["pnl_usd"] or 0.0),
                "staked_usd": float(counts["staked_usd"] or 0.0),
                "fees_usd": float(counts["fees_usd"] or 0.0),
                "average_latency_ms": (
                    float(counts["average_latency_ms"])
                    if counts["average_latency_ms"] is not None else None
                ),
                "recent_observations": recent,
            }
