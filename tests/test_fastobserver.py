import tempfile
import unittest
from pathlib import Path

from polyvote.fastobserver import FastObserverLedger


class FastObserverLedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = FastObserverLedger(Path(self.tmp.name) / "observer.db", "BTC")

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def candidate(condition_id: str = "condition-1") -> dict:
        return {
            "condition_id": condition_id,
            "slug": "btc-updown-5m-100",
            "start_epoch": 100,
            "end_epoch": 400,
            "outcome": "UP",
            "fair_probability": 0.62,
            "intended_ask": 0.55,
            "limit_price": 0.56,
            "net_edge": 0.04,
            "target_shares": 8.5,
            "created_ts": 101,
            "due_ts": 101.02,
            "details": {"simulation": "observer"},
        }

    def test_fill_and_official_settlement_are_scored_without_capital(self):
        row = self.ledger.create_candidate(self.candidate())
        self.ledger.fill(row["observation_id"], {
            "executed_ts": 101.03,
            "shares": 8.5,
            "average_price": 0.55,
            "notional_usd": 4.675,
            "fee_usd": 0.1,
            "total_cost_usd": 4.775,
            "slippage_usd": 0,
            "latency_ms": 30,
            "details": {"depth": []},
        })
        settled = self.ledger.settle(row["observation_id"], "UP")
        self.assertAlmostEqual(settled["pnl_usd"], 3.725)
        state = self.ledger.state()
        self.assertEqual(state["capital_usd"], 0)
        self.assertEqual(state["settled"], 1)
        self.assertEqual(state["wins"], 1)
        self.assertEqual(state["accuracy"], 1)

    def test_condition_is_unique_and_pending_restart_fails_closed(self):
        first = self.ledger.create_candidate(self.candidate())
        second = self.ledger.create_candidate(self.candidate())
        self.assertEqual(first["observation_id"], second["observation_id"])
        self.assertEqual(self.ledger.reject_pending_after_restart(), 1)
        state = self.ledger.state()
        self.assertEqual(state["candidates"], 1)
        self.assertEqual(state["rejects"], 1)
        self.assertEqual(self.ledger.reject_pending_after_restart(), 0)


if __name__ == "__main__":
    unittest.main()
