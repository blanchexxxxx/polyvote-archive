import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import yaml

from fastmulti import load_portfolio, portfolio_risk_loop
from polyvote.fastconfig import FastConfigError


class FastPortfolioTests(unittest.TestCase):
    def test_committed_portfolio_is_four_bots_and_exactly_thirty_dollars(self):
        portfolio, configs = load_portfolio("fast_portfolio.yaml")
        self.assertEqual(portfolio["total_capital_usd"], 30)
        self.assertEqual(portfolio["max_daily_loss_usd"], 6)
        self.assertEqual([cfg.asset for cfg in configs], ["BTC", "ETH", "SOL", "XRP"])
        self.assertEqual(sum(cfg.risk["initial_capital_usd"] for cfg in configs), 30)
        self.assertTrue(all(cfg.risk["max_orders_per_window"] == 1 for cfg in configs))
        self.assertEqual(len({cfg.ledger_path for cfg in configs}), 4)

    def test_allocation_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "portfolio.yaml"
            base = Path("fast_config.yaml").resolve()
            path.write_text(yaml.safe_dump({
                "total_capital_usd": 30,
                "base_config": str(base),
                "bots": [{"asset": "BTC", "allocation_usd": 29}],
            }), encoding="utf-8")
            with self.assertRaises(FastConfigError):
                load_portfolio(path)


class FastPortfolioRiskTests(unittest.IsolatedAsyncioTestCase):
    async def test_portfolio_loss_gate_halts_every_bot_and_records_incident(self):
        with tempfile.TemporaryDirectory() as tmp:
            incident = Path(tmp) / "incident.json"
            bots = [SimpleNamespace(
                cfg=SimpleNamespace(asset=asset),
                account_state={
                    "daily_pnl_usd": -3.5,
                    "equity_usd": 5,
                    "fills": 2,
                    "settled": 2,
                    "open_positions": [],
                },
                external_halt_reason=None,
            ) for asset in ("BTC", "ETH")]
            stopping = asyncio.Event()
            task = asyncio.create_task(portfolio_risk_loop(bots, {
                "max_daily_loss_usd": 6,
                "max_open_positions": 4,
                "incident_path": str(incident),
            }, stopping))
            await asyncio.sleep(0.1)
            stopping.set()
            await task
            self.assertTrue(all(
                bot.external_halt_reason == "portfolio_daily_loss_gate" for bot in bots
            ))
            self.assertEqual(
                json.loads(incident.read_text(encoding="utf-8"))["trigger"],
                "portfolio_daily_loss_gate",
            )


if __name__ == "__main__":
    unittest.main()
