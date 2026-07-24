import tempfile
import unittest
from pathlib import Path

import yaml

from fastchallenger import CashChallenger, _poll_delay
from polyvote.fastobserver import FastObserverLedger


class CashChallengerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config_path = self.root / "challenger.yaml"
        self.config_path.write_text(yaml.safe_dump({
            "schema": "fast-cash-challenger-v1",
            "experiment_id": "test-holdout",
            "frozen_at_utc": "1970-01-01T00:01:40Z",
            "hypothesis": "test",
            "source_observer_pattern": "observer_{asset}.db",
            "source_virtual_order_usd": 5,
            "ledger_pattern": "challenger_{asset}.db",
            "state_path": "state.json",
            "incident_path": "incident.json",
            "total_capital_usd": 10,
            "max_daily_loss_usd": 5,
            "max_open_positions": 1,
            "rules": {
                "min_entry_price": 0.6,
                "min_selected_probability": 0.55,
                "min_net_edge": 0.04,
                "minimum_order_shares": 5,
            },
            "promotion_gate": {
                "minimum_settled": 500,
                "minimum_runtime_hours": 336,
            },
            "assets": [{
                "asset": "BTC",
                "allocation_usd": 10,
                "max_order_usd": 4,
                "max_daily_loss_usd": 3,
                "minimum_cash_reserve_usd": 1,
            }],
        }), encoding="utf-8")
        self.observer = FastObserverLedger(self.root / "observer_btc.db", "BTC")

    def tearDown(self):
        self.tmp.cleanup()

    def add_observation(self, condition: str, ask: float, created: float):
        row = self.observer.create_candidate({
            "condition_id": condition,
            "slug": f"btc-updown-5m-{int(created)}",
            "start_epoch": int(created),
            "end_epoch": int(created + 300),
            "outcome": "UP",
            "fair_probability": 0.72,
            "intended_ask": ask,
            "limit_price": ask + 0.01,
            "net_edge": 0.06,
            "target_shares": 7.8,
            "created_ts": created,
            "due_ts": created + 0.05,
            "details": {},
        })
        self.observer.fill(row["observation_id"], {
            "executed_ts": created + 0.06,
            "shares": 7.8,
            "average_price": ask,
            "notional_usd": 7.8 * ask,
            "fee_usd": 0.1,
            "total_cost_usd": 7.8 * ask + 0.1,
            "slippage_usd": 0,
            "latency_ms": 60,
            "details": {},
        })
        return self.observer.settle(row["observation_id"], "UP")

    def add_pending_observation(self, condition: str, ask: float, created: float):
        return self.observer.create_candidate({
            "condition_id": condition,
            "slug": f"btc-updown-5m-{int(created)}",
            "start_epoch": int(created),
            "end_epoch": int(created + 300),
            "outcome": "UP",
            "fair_probability": 0.72,
            "intended_ask": ask,
            "limit_price": ask + 0.01,
            "net_edge": 0.06,
            "target_shares": 7.4,
            "created_ts": created,
            "due_ts": created + 0.05,
            "details": {},
        })

    def test_only_post_freeze_price_floor_trade_uses_finite_cash(self):
        self.add_observation("before", 0.65, 99)
        self.add_observation("cheap", 0.55, 101)
        self.add_observation("qualifying", 0.65, 102)
        state = CashChallenger(self.config_path).run_once()
        self.assertEqual(state["fills"], 1)
        self.assertEqual(state["settled"], 1)
        self.assertEqual(state["wins"], 1)
        self.assertEqual(state["rejects"], 1)
        self.assertGreater(state["staked_usd"], 0)
        self.assertAlmostEqual(state["cash_usd"], state["equity_usd"])
        self.assertGreater(state["equity_usd"], 10)
        self.assertFalse(state["promotion_gate"]["eligible"])

    def test_allocation_mismatch_fails_closed(self):
        config = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))
        config["total_capital_usd"] = 11
        self.config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
        with self.assertRaises(ValueError):
            CashChallenger(self.config_path)

    def test_incremental_reader_tracks_pending_status_changes(self):
        source = self.add_pending_observation("pending", 0.65, 101)
        challenger = CashChallenger(self.config_path)
        first = challenger.run_once()
        self.assertEqual(first["fills"], 0)
        self.assertIn(source["observation_id"], challenger.pending["BTC"])
        self.assertEqual(challenger._source_rows()[0]["status"], "submitted")

        self.observer.fill(source["observation_id"], {
            "executed_ts": 101.06,
            "shares": 7.4,
            "average_price": 0.65,
            "notional_usd": 4.81,
            "fee_usd": 0.1,
            "total_cost_usd": 4.91,
            "slippage_usd": 0,
            "latency_ms": 60,
            "details": {},
        })
        self.observer.settle(source["observation_id"], "UP")
        second = challenger.run_once()
        self.assertEqual(second["fills"], 1)
        self.assertEqual(second["settled"], 1)
        self.assertNotIn(source["observation_id"], challenger.pending["BTC"])
        self.assertEqual(challenger._source_rows(), [])

    def test_halted_polling_backs_off_without_slowing_collection(self):
        self.assertEqual(_poll_delay(2.0, "COLLECTING"), 2.0)
        self.assertEqual(_poll_delay(2.0, "HALTED"), 10.0)
        self.assertEqual(_poll_delay(15.0, "HALTED"), 15.0)


if __name__ == "__main__":
    unittest.main()
