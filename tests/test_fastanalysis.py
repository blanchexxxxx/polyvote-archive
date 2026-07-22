import tempfile
import unittest
from pathlib import Path

from polyvote.fastanalysis import build_portfolio_replay
from polyvote.fastledger import FastLedger


class FastAnalysisTests(unittest.TestCase):
    def test_replay_reports_observed_and_bounded_scenarios(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.db"
            ledger = FastLedger(path, 10)
            ledger.ensure_window({
                "condition_id": "c1", "slug": "btc-updown-5m-100",
                "start_epoch": 100, "end_epoch": 400,
            })
            ledger.create_intent({
                "intent_id": "i1", "condition_id": "c1", "created_ts": 120,
                "due_ts": 120.02, "outcome": "UP", "fair_probability": 0.7,
                "intended_ask": 0.5, "limit_price": 0.51, "net_edge": 0.1,
                "target_shares": 5, "locked_usd": 5,
                "platform_delay_ms": 0, "outbound_delay_ms": 10,
                "decision_compute_ms": 0.1, "details": {},
            }, minimum_reserve_usd=1)
            ledger.finalize_fill("i1", {
                "executed_ts": 120.02, "shares": 5, "average_price": 0.5,
                "notional_usd": 2.5, "fee_usd": 0.0875,
                "total_cost_usd": 2.5875, "slippage_usd": 0,
                "latency_ms": 20, "depth": [],
            })
            position = ledger.state()["open_positions"][0]
            ledger.settle_position(position["position_id"], "UP", 401)
            report = build_portfolio_replay([("BTC", path)])
            self.assertEqual(report["settled_sample"], 1)
            self.assertEqual(report["scenarios"][0]["trades"], 1)
            self.assertEqual(report["scenarios"][0]["wins"], 1)
            self.assertFalse(report["next_experiment"]["capital_usd"])


if __name__ == "__main__":
    unittest.main()
