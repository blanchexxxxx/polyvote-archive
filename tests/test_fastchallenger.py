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

    def add_observation(
        self,
        condition: str,
        ask: float,
        created: float,
        fair_probability: float = 0.72,
        net_edge: float = 0.06,
    ):
        row = self.observer.create_candidate({
            "condition_id": condition,
            "slug": f"btc-updown-5m-{int(created)}",
            "start_epoch": int(created),
            "end_epoch": int(created + 300),
            "outcome": "UP",
            "fair_probability": fair_probability,
            "intended_ask": ask,
            "limit_price": ask + 0.01,
            "net_edge": net_edge,
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
        self.assertAlmostEqual(state["research"]["brier"], 0.0784)
        self.assertAlmostEqual(state["research"]["calibration_gap_pp"], 28.0)

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

    def test_frozen_calibration_recomputes_probability_and_net_edge(self):
        config = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))
        config["rules"]["min_entry_price"] = 0
        config["probability_calibration"] = {
            "schema": "fixed-bin-calibration-v1",
            "training_cutoff_utc": "1970-01-01T00:01:39Z",
            "bins": [{
                "min_inclusive": 0.5,
                "max_exclusive": 1.0,
                "samples": 100,
                "wins": 64,
                "calibrated_probability": 0.64,
            }],
        }
        self.config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
        self.add_observation(
            "calibrated", 0.65, 101, fair_probability=0.72, net_edge=0.14
        )
        challenger = CashChallenger(self.config_path)
        intent = challenger._ensure_intent(challenger._source_rows()[0])
        self.assertAlmostEqual(intent["fair_probability"], 0.64)
        self.assertAlmostEqual(intent["net_edge"], 0.06)
        self.assertEqual(intent["status"], "submitted")

    def test_calibration_rejects_raw_edge_that_disappears(self):
        config = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))
        config["rules"]["min_entry_price"] = 0
        config["probability_calibration"] = {
            "schema": "fixed-bin-calibration-v1",
            "training_cutoff_utc": "1970-01-01T00:01:39Z",
            "bins": [{
                "min_inclusive": 0.5,
                "max_exclusive": 1.0,
                "samples": 100,
                "wins": 60,
                "calibrated_probability": 0.60,
            }],
        }
        self.config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
        self.add_observation(
            "false-edge", 0.65, 101, fair_probability=0.72, net_edge=0.10
        )
        state = CashChallenger(self.config_path).run_once()
        self.assertEqual(state["fills"], 0)
        self.assertEqual(state["rejects"], 1)
        recent = state["accounts"]["BTC"]["recent_trades"]
        self.assertEqual(
            recent[0]["reject_reason"],
            "challenger_net_edge_below_threshold",
        )

    def test_calibration_training_must_precede_holdout(self):
        config = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))
        config["probability_calibration"] = {
            "schema": "fixed-bin-calibration-v1",
            "training_cutoff_utc": "1970-01-01T00:01:41Z",
            "bins": [{
                "min_inclusive": 0.5,
                "max_exclusive": 1.0,
                "samples": 1,
                "wins": 1,
                "calibrated_probability": 0.75,
            }],
        }
        self.config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
        with self.assertRaises(ValueError):
            CashChallenger(self.config_path)


if __name__ == "__main__":
    unittest.main()
