import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path

from polyvote.fastledger import FastLedger
from polyvote.fastreview import build_review, run_due_reviews


def market():
    return {
        "condition_id": "condition-1",
        "slug": "btc-updown-5m-100",
        "start_epoch": 100,
        "end_epoch": 400,
        "taker_delay_ms": 250,
    }


def intent(intent_id="intent-1", lock=5.0, target=9.0):
    return {
        "intent_id": intent_id,
        "condition_id": "condition-1",
        "created_ts": 120.0,
        "due_ts": 120.28,
        "outcome": "UP",
        "fair_probability": 0.7,
        "intended_ask": 0.5,
        "limit_price": 0.51,
        "net_edge": 0.1,
        "target_shares": target,
        "locked_usd": lock,
        "platform_delay_ms": 250,
        "outbound_delay_ms": 15,
        "decision_compute_ms": 0.1,
        "details": {"test": True},
    }


class FastLedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "ledger.db"
        self.ledger = FastLedger(self.path, 10)
        self.ledger.ensure_window(market(), 100.0, 100.0)

    def tearDown(self):
        self.tmp.cleanup()

    def test_unchanged_window_does_not_refresh_updated_timestamp(self):
        with closing(sqlite3.connect(self.path)) as connection:
            before = connection.execute(
                "SELECT updated_ts FROM windows WHERE condition_id='condition-1'"
            ).fetchone()[0]

        time.sleep(0.01)
        self.ledger.ensure_window(market(), 100.0, 100.0)

        with closing(sqlite3.connect(self.path)) as connection:
            unchanged = connection.execute(
                "SELECT updated_ts FROM windows WHERE condition_id='condition-1'"
            ).fetchone()[0]
        self.assertEqual(unchanged, before)

        changed_market = market()
        changed_market["taker_delay_ms"] = 0
        self.ledger.ensure_window(changed_market, 101.0, 101.0)
        with closing(sqlite3.connect(self.path)) as connection:
            changed = connection.execute(
                """SELECT open_price,open_source_ts,metadata_json,updated_ts
                   FROM windows WHERE condition_id='condition-1'"""
            ).fetchone()
        self.assertEqual(changed[:2], (101.0, 101.0))
        self.assertIn('"taker_delay_ms":0', changed[2])
        self.assertGreater(changed[3], unchanged)

        with closing(sqlite3.connect(self.path)) as connection:
            account_before = connection.execute(
                "SELECT updated_ts FROM account WHERE run_id=?", (self.ledger.run_id,)
            ).fetchone()[0]
        time.sleep(0.01)
        self.ledger.state()
        with closing(sqlite3.connect(self.path)) as connection:
            account_unchanged = connection.execute(
                "SELECT updated_ts FROM account WHERE run_id=?", (self.ledger.run_id,)
            ).fetchone()[0]
        self.assertEqual(account_unchanged, account_before)

    def test_lock_fill_settle_and_restart_are_conservative(self):
        created = self.ledger.create_intent(intent(), minimum_reserve_usd=1)
        self.assertEqual(created["status"], "submitted")
        locked = self.ledger.state()
        self.assertAlmostEqual(locked["cash_usd"], 5)
        self.assertAlmostEqual(locked["locked_usd"], 5)

        duplicate = self.ledger.create_intent(intent(), minimum_reserve_usd=1)
        self.assertEqual(duplicate["intent_id"], created["intent_id"])
        self.assertAlmostEqual(self.ledger.state()["locked_usd"], 5)

        fee = 5 * 0.07 * 0.5 * 0.5
        saved = self.ledger.finalize_fill("intent-1", {
            "executed_ts": 120.28,
            "shares": 5,
            "average_price": 0.5,
            "notional_usd": 2.5,
            "fee_usd": fee,
            "total_cost_usd": 2.5 + fee,
            "slippage_usd": 0,
            "latency_ms": 280,
            "depth": [{"price": 0.5, "filled": 5}],
        })
        self.assertEqual(saved["status"], "partial")
        after_fill = self.ledger.state({"condition-1": 0.5})
        self.assertAlmostEqual(after_fill["cash_usd"], 10 - 2.5 - fee)
        self.assertAlmostEqual(after_fill["locked_usd"], 0)
        self.assertAlmostEqual(after_fill["equity_usd"], 10 - fee)

        position_id = after_fill["open_positions"][0]["position_id"]
        settled = self.ledger.settle_position(position_id, "UP", 401)
        self.assertAlmostEqual(settled["pnl_usd"], 5 - 2.5 - fee)
        final = self.ledger.state()
        self.assertAlmostEqual(final["cash_usd"], 15 - 2.5 - fee)
        self.assertAlmostEqual(final["realized_pnl_usd"], 5 - 2.5 - fee)
        self.assertAlmostEqual(final["win_rate"], 1.0)
        self.assertAlmostEqual(
            final["roi"], (5 - 2.5 - fee) / (2.5 + fee), places=6
        )
        self.assertEqual(len(final["equity_curve"]), 2)
        self.assertAlmostEqual(
            final["equity_curve"][-1]["equity_usd"],
            10 + 5 - 2.5 - fee,
        )

        reopened = FastLedger(self.path, 10)
        self.assertEqual(reopened.run_id, self.ledger.run_id)
        self.assertAlmostEqual(reopened.state()["cash_usd"], final["cash_usd"])

    def test_reserve_rejects_without_moving_cash(self):
        rejected = self.ledger.create_intent(
            intent(lock=9.5, target=10),
            minimum_reserve_usd=1,
        )
        self.assertEqual(rejected["status"], "rejected")
        state = self.ledger.state()
        self.assertAlmostEqual(state["cash_usd"], 10)
        self.assertAlmostEqual(state["locked_usd"], 0)
        self.assertEqual(state["rejects"], 1)
        self.assertAlmostEqual(state["rejection_rate"], 1.0)

    def test_multiple_intents_can_share_one_fast_market(self):
        first = self.ledger.create_intent(
            intent("intent-1", lock=2.0, target=5.0),
            minimum_reserve_usd=1,
        )
        second = self.ledger.create_intent(
            intent("intent-2", lock=2.0, target=5.0),
            minimum_reserve_usd=1,
        )
        self.assertEqual(first["status"], "submitted")
        self.assertEqual(second["status"], "submitted")
        state = self.ledger.state()
        self.assertAlmostEqual(state["cash_usd"], 6.0)
        self.assertAlmostEqual(state["locked_usd"], 4.0)

    def test_pending_intent_is_released_on_recovery(self):
        self.ledger.create_intent(intent(), minimum_reserve_usd=1)
        self.assertEqual(self.ledger.cancel_pending_intents(), 1)
        state = self.ledger.state()
        self.assertAlmostEqual(state["cash_usd"], 10)
        self.assertAlmostEqual(state["locked_usd"], 0)
        self.assertEqual(self.ledger.cancel_pending_intents(), 0)

    def test_review_freezes_tuning_until_sample_gate(self):
        report = build_review(
            self.ledger,
            "hourly",
            0,
            500,
            {
                "network": {"under_50ms": True, "reconnects": {}},
                "feed_rejects": {},
            },
        )
        self.assertEqual(report["verdict"], "COLLECT_MORE_DATA")
        self.assertFalse(report["challenger"]["eligible"])
        self.assertFalse(report["lesson_plan"]["mutation_applied"])
        self.assertEqual(len(report["lesson_plan"]["parameter_proposals"]), 3)
        path = Path(self.tmp.name) / "reviews.json"
        created = run_due_reviews(
            self.ledger,
            path,
            {"network": {"under_50ms": True, "reconnects": {}}},
            now=10_000,
        )
        self.assertTrue(created)
        self.assertTrue(path.exists())
        before_mtime = path.stat().st_mtime_ns
        before_contents = path.read_bytes()

        time.sleep(0.01)
        created = run_due_reviews(
            self.ledger,
            path,
            {"network": {"under_50ms": True, "reconnects": {}}},
            now=10_030,
        )
        self.assertFalse(created)
        self.assertEqual(path.stat().st_mtime_ns, before_mtime)
        self.assertEqual(path.read_bytes(), before_contents)


if __name__ == "__main__":
    unittest.main()
