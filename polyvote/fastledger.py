"""Crash-safe SQLite ledger for the fast-market shadow campaign.

The ledger models the same cash lifecycle as a real FAK order: funds are locked
when an intent is submitted, unused funds are returned on fill/cancel, and only
an actual fill creates a position.  Every mutation is one SQLite transaction so
restarts cannot manufacture buying power or duplicate a position.
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any


EPSILON = 1e-8


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


class LedgerError(RuntimeError):
    pass


class FastLedger:
    def __init__(self, path: str | Path, initial_capital_usd: float = 10.0):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initial_capital_usd = float(initial_capital_usd)
        if self.initial_capital_usd <= 0:
            raise LedgerError("initial capital must be positive")
        self._init_schema()
        self.run_id = self._ensure_campaign()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=15000")
        return connection

    def _init_schema(self) -> None:
        with closing(self._connect()) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS campaigns (
                    run_id TEXT PRIMARY KEY,
                    started_ts REAL NOT NULL,
                    initial_capital_usd REAL NOT NULL,
                    status TEXT NOT NULL,
                    label TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS account (
                    run_id TEXT PRIMARY KEY REFERENCES campaigns(run_id),
                    cash_usd REAL NOT NULL,
                    locked_usd REAL NOT NULL,
                    realized_pnl_usd REAL NOT NULL,
                    fees_usd REAL NOT NULL,
                    equity_peak_usd REAL NOT NULL,
                    max_drawdown_usd REAL NOT NULL,
                    updated_ts REAL NOT NULL,
                    CHECK (cash_usd >= -0.00000001),
                    CHECK (locked_usd >= -0.00000001)
                );
                CREATE TABLE IF NOT EXISTS windows (
                    condition_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES campaigns(run_id),
                    slug TEXT NOT NULL,
                    start_epoch INTEGER NOT NULL,
                    end_epoch INTEGER NOT NULL,
                    open_price REAL,
                    open_source_ts REAL,
                    status TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    created_ts REAL NOT NULL,
                    updated_ts REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS intents (
                    intent_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES campaigns(run_id),
                    condition_id TEXT NOT NULL REFERENCES windows(condition_id),
                    created_ts REAL NOT NULL,
                    due_ts REAL NOT NULL,
                    outcome TEXT NOT NULL,
                    fair_probability REAL NOT NULL,
                    intended_ask REAL NOT NULL,
                    limit_price REAL NOT NULL,
                    net_edge REAL NOT NULL,
                    target_shares REAL NOT NULL,
                    locked_usd REAL NOT NULL,
                    platform_delay_ms REAL NOT NULL,
                    outbound_delay_ms REAL NOT NULL,
                    decision_compute_ms REAL NOT NULL,
                    status TEXT NOT NULL,
                    reject_reason TEXT,
                    details_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS fills (
                    fill_id TEXT PRIMARY KEY,
                    intent_id TEXT NOT NULL UNIQUE REFERENCES intents(intent_id),
                    executed_ts REAL NOT NULL,
                    shares REAL NOT NULL,
                    average_price REAL NOT NULL,
                    notional_usd REAL NOT NULL,
                    fee_usd REAL NOT NULL,
                    total_cost_usd REAL NOT NULL,
                    slippage_usd REAL NOT NULL,
                    latency_ms REAL NOT NULL,
                    status TEXT NOT NULL,
                    depth_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS positions (
                    position_id TEXT PRIMARY KEY,
                    intent_id TEXT NOT NULL UNIQUE REFERENCES intents(intent_id),
                    run_id TEXT NOT NULL REFERENCES campaigns(run_id),
                    condition_id TEXT NOT NULL REFERENCES windows(condition_id),
                    slug TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    shares REAL NOT NULL,
                    average_price REAL NOT NULL,
                    fee_usd REAL NOT NULL,
                    cost_usd REAL NOT NULL,
                    opened_ts REAL NOT NULL,
                    end_epoch INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    provisional_outcome TEXT,
                    official_outcome TEXT,
                    payout_usd REAL,
                    pnl_usd REAL,
                    settled_ts REAL
                );
                CREATE TABLE IF NOT EXISTS account_events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    run_id TEXT NOT NULL REFERENCES campaigns(run_id),
                    ts REAL NOT NULL,
                    event_type TEXT NOT NULL,
                    cash_after_usd REAL NOT NULL,
                    locked_after_usd REAL NOT NULL,
                    amount_usd REAL NOT NULL,
                    reference_id TEXT,
                    details_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS gate_counts (
                    run_id TEXT NOT NULL REFERENCES campaigns(run_id),
                    condition_id TEXT NOT NULL REFERENCES windows(condition_id),
                    reason TEXT NOT NULL,
                    count INTEGER NOT NULL,
                    updated_ts REAL NOT NULL,
                    PRIMARY KEY (run_id, condition_id, reason)
                );
                CREATE TABLE IF NOT EXISTS reviews (
                    review_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES campaigns(run_id),
                    review_type TEXT NOT NULL,
                    period_start REAL NOT NULL,
                    period_end REAL NOT NULL,
                    created_ts REAL NOT NULL,
                    report_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_positions_status
                    ON positions(run_id, status, end_epoch);
                CREATE INDEX IF NOT EXISTS idx_intents_status
                    ON intents(run_id, status, due_ts);
                CREATE INDEX IF NOT EXISTS idx_events_ts
                    ON account_events(run_id, ts);
                """
            )
            # Early V2 builds marked an open position at zero when a background
            # review did not supply a live quote, creating a fake drawdown.  A
            # one-time migration repairs only that known accounting artifact.
            repaired = connection.execute(
                "SELECT 1 FROM meta WHERE key='cost_basis_mark_fallback_v2'"
            ).fetchone()
            if not repaired:
                for account in connection.execute("SELECT * FROM account").fetchall():
                    basis = float(connection.execute(
                        """SELECT COALESCE(SUM(shares*average_price),0)
                           FROM positions WHERE run_id=? AND status='open'""",
                        (account["run_id"],),
                    ).fetchone()[0])
                    equity = float(account["cash_usd"]) + float(account["locked_usd"]) + basis
                    drawdown = max(0.0, float(account["equity_peak_usd"]) - equity)
                    connection.execute(
                        "UPDATE account SET max_drawdown_usd=? WHERE run_id=?",
                        (drawdown, account["run_id"]),
                    )
                connection.execute(
                    "INSERT INTO meta(key,value) VALUES ('cost_basis_mark_fallback_v2','applied')"
                )

    def _ensure_campaign(self) -> str:
        now = time.time()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT value FROM meta WHERE key='active_campaign'"
            ).fetchone()
            if row:
                run_id = str(row["value"])
                campaign = connection.execute(
                    "SELECT initial_capital_usd FROM campaigns WHERE run_id=?", (run_id,)
                ).fetchone()
                if not campaign:
                    raise LedgerError("active campaign metadata is corrupt")
                if abs(float(campaign["initial_capital_usd"]) - self.initial_capital_usd) > EPSILON:
                    raise LedgerError(
                        "configured initial capital differs from the active campaign; "
                        "start a new campaign instead of rewriting history"
                    )
                connection.commit()
                return run_id

            run_id = f"fast-v4-{uuid.uuid4().hex[:12]}"
            label = f"FAST-V5 ${self.initial_capital_usd:g} SHADOW"
            connection.execute(
                "INSERT INTO campaigns VALUES (?,?,?,?,?)",
                (run_id, now, self.initial_capital_usd, "active", label),
            )
            connection.execute(
                "INSERT INTO account VALUES (?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    self.initial_capital_usd,
                    0.0,
                    0.0,
                    0.0,
                    self.initial_capital_usd,
                    0.0,
                    now,
                ),
            )
            connection.execute(
                "INSERT INTO meta(key,value) VALUES ('active_campaign',?)", (run_id,)
            )
            self._event(
                connection,
                run_id,
                "campaign_started",
                self.initial_capital_usd,
                None,
                {"initial_capital_usd": self.initial_capital_usd},
                now,
            )
            connection.commit()
            return run_id

    @staticmethod
    def _event(
        connection: sqlite3.Connection,
        run_id: str,
        event_type: str,
        amount_usd: float,
        reference_id: str | None,
        details: dict[str, Any],
        ts: float | None = None,
    ) -> None:
        account = connection.execute(
            "SELECT cash_usd,locked_usd FROM account WHERE run_id=?", (run_id,)
        ).fetchone()
        connection.execute(
            """INSERT INTO account_events
               (event_id,run_id,ts,event_type,cash_after_usd,locked_after_usd,
                amount_usd,reference_id,details_json)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                uuid.uuid4().hex,
                run_id,
                ts or time.time(),
                event_type,
                float(account["cash_usd"]),
                float(account["locked_usd"]),
                float(amount_usd),
                reference_id,
                _json(details),
            ),
        )

    def ensure_window(self, market: dict[str, Any], open_price: float | None = None,
                      open_source_ts: float | None = None) -> None:
        now = time.time()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """INSERT INTO windows
                   (condition_id,run_id,slug,start_epoch,end_epoch,open_price,
                    open_source_ts,status,metadata_json,created_ts,updated_ts)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(condition_id) DO UPDATE SET
                     open_price=COALESCE(excluded.open_price,windows.open_price),
                     open_source_ts=COALESCE(excluded.open_source_ts,windows.open_source_ts),
                     metadata_json=excluded.metadata_json,
                     updated_ts=excluded.updated_ts""",
                (
                    market["condition_id"],
                    self.run_id,
                    market["slug"],
                    int(market["start_epoch"]),
                    int(market["end_epoch"]),
                    open_price,
                    open_source_ts,
                    "active",
                    _json(market),
                    now,
                    now,
                ),
            )
            connection.commit()

    def create_intent(self, intent: dict[str, Any], minimum_reserve_usd: float) -> dict[str, Any]:
        """Persist an intent and lock its maximum cost, atomically and idempotently."""
        intent_id = str(intent["intent_id"])
        lock = float(intent["locked_usd"])
        now = float(intent["created_ts"])
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM intents WHERE intent_id=?",
                (intent_id,),
            ).fetchone()
            if existing:
                connection.commit()
                return dict(existing)
            account = connection.execute(
                "SELECT * FROM account WHERE run_id=?", (self.run_id,)
            ).fetchone()
            available = max(0.0, float(account["cash_usd"]) - float(minimum_reserve_usd))
            pre_reject = intent.get("pre_reject_reason")
            status = (
                "submitted"
                if pre_reject is None and lock <= available + EPSILON
                else "rejected"
            )
            reason = (
                None
                if status == "submitted"
                else str(pre_reject or "insufficient_cash_or_reserve")
            )
            connection.execute(
                """INSERT INTO intents
                   (intent_id,run_id,condition_id,created_ts,due_ts,outcome,
                    fair_probability,intended_ask,limit_price,net_edge,target_shares,
                    locked_usd,platform_delay_ms,outbound_delay_ms,decision_compute_ms,
                    status,reject_reason,details_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    intent_id,
                    self.run_id,
                    intent["condition_id"],
                    now,
                    float(intent["due_ts"]),
                    intent["outcome"],
                    float(intent["fair_probability"]),
                    float(intent["intended_ask"]),
                    float(intent["limit_price"]),
                    float(intent["net_edge"]),
                    float(intent["target_shares"]),
                    lock,
                    float(intent["platform_delay_ms"]),
                    float(intent["outbound_delay_ms"]),
                    float(intent["decision_compute_ms"]),
                    status,
                    reason,
                    _json(intent.get("details") or {}),
                ),
            )
            if status == "submitted":
                connection.execute(
                    """UPDATE account SET cash_usd=cash_usd-?, locked_usd=locked_usd+?,
                       updated_ts=? WHERE run_id=?""",
                    (lock, lock, now, self.run_id),
                )
                self._event(
                    connection,
                    self.run_id,
                    "intent_submitted",
                    -lock,
                    intent_id,
                    {"locked_usd": lock, "available_before_usd": float(account["cash_usd"])},
                    now,
                )
            else:
                self._event(
                    connection,
                    self.run_id,
                    "intent_rejected",
                    0.0,
                    intent_id,
                    {"reason": reason, "required_usd": lock, "available_usd": available},
                    now,
                )
            connection.commit()
            return dict(
                connection.execute("SELECT * FROM intents WHERE intent_id=?", (intent_id,)).fetchone()
            )

    def finalize_fill(self, intent_id: str, fill: dict[str, Any] | None,
                      reject_reason: str | None = None) -> dict[str, Any] | None:
        """Release locked cash and create at most one fill/position."""
        now = float((fill or {}).get("executed_ts") or time.time())
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            intent = connection.execute(
                "SELECT * FROM intents WHERE intent_id=? AND run_id=?",
                (intent_id, self.run_id),
            ).fetchone()
            if not intent:
                connection.rollback()
                raise LedgerError(f"unknown intent {intent_id}")
            if intent["status"] != "submitted":
                existing = connection.execute(
                    "SELECT * FROM fills WHERE intent_id=?", (intent_id,)
                ).fetchone()
                connection.commit()
                return dict(existing) if existing else None

            locked = float(intent["locked_usd"])
            if not fill or float(fill.get("shares") or 0) <= 0:
                connection.execute(
                    "UPDATE intents SET status='rejected',reject_reason=? WHERE intent_id=?",
                    (reject_reason or "fak_no_liquidity", intent_id),
                )
                connection.execute(
                    """UPDATE account SET cash_usd=cash_usd+?,locked_usd=locked_usd-?,
                       updated_ts=? WHERE run_id=?""",
                    (locked, locked, now, self.run_id),
                )
                self._event(
                    connection,
                    self.run_id,
                    "fill_rejected",
                    locked,
                    intent_id,
                    {"reason": reject_reason or "fak_no_liquidity"},
                    now,
                )
                connection.commit()
                return None

            total_cost = float(fill["total_cost_usd"])
            if total_cost > locked + EPSILON:
                connection.rollback()
                raise LedgerError("fill cost exceeds locked funds")
            shares = float(fill["shares"])
            status = "filled" if shares + EPSILON >= float(intent["target_shares"]) else "partial"
            fill_id = f"fill-{intent_id}"
            position_id = f"pos-{intent_id}"
            connection.execute(
                """INSERT INTO fills
                   (fill_id,intent_id,executed_ts,shares,average_price,notional_usd,
                    fee_usd,total_cost_usd,slippage_usd,latency_ms,status,depth_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    fill_id,
                    intent_id,
                    now,
                    shares,
                    float(fill["average_price"]),
                    float(fill["notional_usd"]),
                    float(fill["fee_usd"]),
                    total_cost,
                    float(fill["slippage_usd"]),
                    float(fill["latency_ms"]),
                    status,
                    _json(fill.get("depth") or []),
                ),
            )
            window = connection.execute(
                "SELECT slug,end_epoch FROM windows WHERE condition_id=?",
                (intent["condition_id"],),
            ).fetchone()
            connection.execute(
                """INSERT INTO positions
                   (position_id,intent_id,run_id,condition_id,slug,outcome,shares,
                    average_price,fee_usd,cost_usd,opened_ts,end_epoch,status)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    position_id,
                    intent_id,
                    self.run_id,
                    intent["condition_id"],
                    window["slug"],
                    intent["outcome"],
                    shares,
                    float(fill["average_price"]),
                    float(fill["fee_usd"]),
                    total_cost,
                    now,
                    int(window["end_epoch"]),
                    "open",
                ),
            )
            connection.execute(
                "UPDATE intents SET status=? WHERE intent_id=?", (status, intent_id)
            )
            refund = locked - total_cost
            connection.execute(
                """UPDATE account SET cash_usd=cash_usd+?,locked_usd=locked_usd-?,
                   fees_usd=fees_usd+?,updated_ts=? WHERE run_id=?""",
                (refund, locked, float(fill["fee_usd"]), now, self.run_id),
            )
            self._event(
                connection,
                self.run_id,
                "fill_created",
                refund,
                fill_id,
                {"cost_usd": total_cost, "shares": shares, "status": status},
                now,
            )
            connection.commit()
            return dict(connection.execute("SELECT * FROM fills WHERE fill_id=?", (fill_id,)).fetchone())

    def cancel_pending_intents(self, reason: str = "restart_recovery_cancel") -> int:
        pending = self.pending_intents()
        for row in pending:
            self.finalize_fill(str(row["intent_id"]), None, reason)
        return len(pending)

    def pending_intents(self) -> list[dict[str, Any]]:
        with closing(self._connect()) as connection:
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM intents WHERE run_id=? AND status='submitted' ORDER BY due_ts",
                    (self.run_id,),
                ).fetchall()
            ]

    def open_positions(self) -> list[dict[str, Any]]:
        with closing(self._connect()) as connection:
            return [
                dict(row)
                for row in connection.execute(
                    """SELECT p.*,w.open_price,w.open_source_ts
                       FROM positions p JOIN windows w USING(condition_id)
                       WHERE p.run_id=? AND p.status='open' ORDER BY p.opened_ts""",
                    (self.run_id,),
                ).fetchall()
            ]

    def set_provisional_outcome(self, position_id: str, outcome: str) -> None:
        with closing(self._connect()) as connection:
            connection.execute(
                """UPDATE positions SET provisional_outcome=?
                   WHERE position_id=? AND run_id=? AND status='open'""",
                (outcome, position_id, self.run_id),
            )

    def settle_position(self, position_id: str, official_outcome: str,
                        settled_ts: float | None = None) -> dict[str, Any]:
        official = official_outcome.upper()
        if official not in {"UP", "DOWN"}:
            raise LedgerError(f"invalid outcome {official_outcome}")
        now = settled_ts or time.time()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            position = connection.execute(
                "SELECT * FROM positions WHERE position_id=? AND run_id=?",
                (position_id, self.run_id),
            ).fetchone()
            if not position:
                connection.rollback()
                raise LedgerError(f"unknown position {position_id}")
            if position["status"] == "settled":
                connection.commit()
                return dict(position)
            payout = float(position["shares"]) if position["outcome"] == official else 0.0
            pnl = payout - float(position["cost_usd"])
            connection.execute(
                """UPDATE positions SET status='settled',official_outcome=?,payout_usd=?,
                   pnl_usd=?,settled_ts=? WHERE position_id=?""",
                (official, payout, pnl, now, position_id),
            )
            connection.execute(
                """UPDATE account SET cash_usd=cash_usd+?,realized_pnl_usd=realized_pnl_usd+?,
                   updated_ts=? WHERE run_id=?""",
                (payout, pnl, now, self.run_id),
            )
            self._event(
                connection,
                self.run_id,
                "position_settled",
                payout,
                position_id,
                {"official_outcome": official, "pnl_usd": pnl},
                now,
            )
            connection.execute(
                "UPDATE windows SET status='settled',updated_ts=? WHERE condition_id=?",
                (now, position["condition_id"]),
            )
            connection.commit()
            return dict(
                connection.execute("SELECT * FROM positions WHERE position_id=?", (position_id,)).fetchone()
            )

    def flush_gate_counts(self, condition_id: str, counts: dict[str, int]) -> None:
        if not counts:
            return
        now = time.time()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            for reason, count in counts.items():
                connection.execute(
                    """INSERT INTO gate_counts(run_id,condition_id,reason,count,updated_ts)
                       VALUES (?,?,?,?,?)
                       ON CONFLICT(run_id,condition_id,reason) DO UPDATE SET
                         count=gate_counts.count+excluded.count,
                         updated_ts=excluded.updated_ts""",
                    (self.run_id, condition_id, reason, int(count), now),
                )
            connection.commit()

    def state(self, marks: dict[str, float] | None = None) -> dict[str, Any]:
        marks = marks or {}
        with closing(self._connect()) as connection:
            account = dict(
                connection.execute("SELECT * FROM account WHERE run_id=?", (self.run_id,)).fetchone()
            )
            campaign = connection.execute(
                "SELECT started_ts,label FROM campaigns WHERE run_id=?", (self.run_id,)
            ).fetchone()
            campaign_started = float(campaign["started_ts"])
            positions = [dict(row) for row in connection.execute(
                "SELECT * FROM positions WHERE run_id=? ORDER BY opened_ts DESC LIMIT 100",
                (self.run_id,),
            ).fetchall()]
            open_positions = [row for row in positions if row["status"] == "open"]
            market_value = sum(
                float(row["shares"]) * max(
                    0.0,
                    min(
                        1.0,
                        float(marks.get(row["condition_id"], row["average_price"])),
                    ),
                )
                for row in open_positions
            )
            equity = float(account["cash_usd"]) + float(account["locked_usd"]) + market_value
            peak = max(float(account["equity_peak_usd"]), equity)
            drawdown = max(0.0, peak - equity)
            max_drawdown = max(float(account["max_drawdown_usd"]), drawdown)
            connection.execute(
                "UPDATE account SET equity_peak_usd=?,max_drawdown_usd=?,updated_ts=? WHERE run_id=?",
                (peak, max_drawdown, time.time(), self.run_id),
            )
            aggregate = dict(connection.execute(
                """SELECT COUNT(*) fills,
                          COALESCE(SUM(CASE WHEN status='settled' THEN 1 ELSE 0 END),0) settled,
                          COALESCE(SUM(CASE WHEN pnl_usd>0 THEN 1 ELSE 0 END),0) wins,
                          COALESCE(SUM(CASE WHEN status='settled' THEN pnl_usd ELSE 0 END),0) pnl,
                          COALESCE(SUM(cost_usd),0) staked
                   FROM positions WHERE run_id=?""",
                (self.run_id,),
            ).fetchone())
            rejects = int(connection.execute(
                "SELECT COUNT(*) FROM intents WHERE run_id=? AND status='rejected'",
                (self.run_id,),
            ).fetchone()[0])
            day_start = int(time.time() // 86400) * 86400
            daily_pnl = float(connection.execute(
                """SELECT COALESCE(SUM(pnl_usd),0) FROM positions
                   WHERE run_id=? AND status='settled' AND settled_ts>=?""",
                (self.run_id, day_start),
            ).fetchone()[0])
            recent = [dict(row) for row in connection.execute(
                """SELECT i.intent_id,i.created_ts,i.outcome,i.intended_ask,i.limit_price,
                          i.status intent_status,i.reject_reason,f.executed_ts,f.shares,
                          f.average_price,f.fee_usd,f.slippage_usd,f.latency_ms,p.status,
                          p.official_outcome,p.pnl_usd,p.slug
                   FROM intents i
                   LEFT JOIN fills f ON f.intent_id=i.intent_id
                   LEFT JOIN positions p ON p.intent_id=i.intent_id
                   WHERE i.run_id=? ORDER BY i.created_ts DESC LIMIT 30""",
                (self.run_id,),
            ).fetchall()]
            settled_rows = [dict(row) for row in connection.execute(
                """SELECT settled_ts,pnl_usd,slug,outcome,official_outcome
                   FROM positions
                   WHERE run_id=? AND status='settled'
                   ORDER BY settled_ts ASC LIMIT 1000""",
                (self.run_id,),
            ).fetchall()]
        running_equity = float(self.initial_capital_usd)
        equity_curve = [{
            "ts": campaign_started,
            "equity_usd": round(running_equity, 6),
            "pnl_usd": 0.0,
            "slug": "campaign-start",
        }]
        for row in settled_rows:
            realized = float(row["pnl_usd"] or 0.0)
            running_equity += realized
            equity_curve.append({
                "ts": float(row["settled_ts"]),
                "equity_usd": round(running_equity, 6),
                "pnl_usd": round(realized, 6),
                "slug": row["slug"],
                "outcome": row["outcome"],
                "official_outcome": row["official_outcome"],
            })
        settled_count = int(aggregate["settled"])
        fill_count = int(aggregate["fills"])
        staked = float(aggregate["staked"])
        pnl = float(aggregate["pnl"])
        return {
            "run_id": self.run_id,
            "label": str(campaign["label"]),
            "started_ts": campaign_started,
            "initial_capital_usd": self.initial_capital_usd,
            "cash_usd": round(float(account["cash_usd"]), 6),
            "locked_usd": round(float(account["locked_usd"]), 6),
            "market_value_usd": round(market_value, 6),
            "equity_usd": round(equity, 6),
            "realized_pnl_usd": round(float(account["realized_pnl_usd"]), 6),
            "daily_pnl_usd": round(daily_pnl, 6),
            "fees_usd": round(float(account["fees_usd"]), 6),
            "equity_peak_usd": round(peak, 6),
            "drawdown_usd": round(drawdown, 6),
            "max_drawdown_usd": round(max_drawdown, 6),
            "fills": fill_count,
            "settled": settled_count,
            "wins": int(aggregate["wins"]),
            "win_rate": round(int(aggregate["wins"]) / settled_count, 6)
            if settled_count else None,
            "staked_usd": round(staked, 6),
            "pnl_usd": round(pnl, 6),
            "roi": round(pnl / staked, 6) if staked else None,
            "rejects": rejects,
            "rejection_rate": round(rejects / (fill_count + rejects), 6)
            if fill_count + rejects else None,
            "open_positions": open_positions,
            "recent_trades": recent,
            "equity_curve": equity_curve,
        }

    def record_review(self, review_type: str, period_start: float, period_end: float,
                      report: dict[str, Any]) -> str:
        review_id = f"review-{review_type}-{int(period_end)}"
        with closing(self._connect()) as connection:
            connection.execute(
                """INSERT OR IGNORE INTO reviews
                   (review_id,run_id,review_type,period_start,period_end,created_ts,report_json)
                   VALUES (?,?,?,?,?,?,?)""",
                (
                    review_id,
                    self.run_id,
                    review_type,
                    period_start,
                    period_end,
                    time.time(),
                    _json(report),
                ),
            )
        return review_id

    def recent_reviews(self, limit: int = 10) -> list[dict[str, Any]]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """SELECT review_id,review_type,period_start,period_end,created_ts,report_json
                   FROM reviews WHERE run_id=? ORDER BY created_ts DESC LIMIT ?""",
                (self.run_id, int(limit)),
            ).fetchall()
        output = []
        for row in rows:
            item = dict(row)
            item["report"] = json.loads(item.pop("report_json"))
            output.append(item)
        return output
