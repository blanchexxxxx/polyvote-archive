import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import fastdash_server


class FastDashboardServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.old_html = fastdash_server.HTML_PATH
        self.old_state = fastdash_server.STATE_PATH
        self.old_state_dir = fastdash_server.STATE_DIR
        fastdash_server.HTML_PATH = root / "fast_dashboard.html"
        fastdash_server.STATE_PATH = root / "fast_state.json"
        fastdash_server.STATE_DIR = None
        fastdash_server.HTML_PATH.write_text("<h1>fast only</h1>", encoding="utf-8")
        fastdash_server.STATE_PATH.write_text(
            json.dumps({"ready": True, "updated_at": "2026-01-01T00:00:00Z"}),
            encoding="utf-8",
        )
        self.server = fastdash_server.ThreadingHTTPServer(
            ("127.0.0.1", 0), fastdash_server.Handler
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        fastdash_server.HTML_PATH = self.old_html
        fastdash_server.STATE_PATH = self.old_state
        fastdash_server.STATE_DIR = self.old_state_dir
        self.tmp.cleanup()

    def test_fast_routes_are_read_only_and_slow_is_gone(self):
        with urlopen(self.base + "/", timeout=2) as response:
            self.assertEqual(response.status, 200)
            self.assertIn(b"fast only", response.read())
        with urlopen(self.base + "/api/fast/state", timeout=2) as response:
            self.assertTrue(json.load(response)["ready"])
        for path in ("/slow", "/api/state"):
            with self.assertRaises(HTTPError) as error:
                urlopen(self.base + path, timeout=2)
            self.assertEqual(error.exception.code, 410)
        with self.assertRaises(HTTPError) as error:
            urlopen(Request(self.base + "/", data=b"{}", method="POST"), timeout=2)
        self.assertEqual(error.exception.code, 405)

    def test_portfolio_aggregation_counts_bots_markets_and_positions(self):
        now = time.time()
        states = []
        for asset, capital, positions in (("BTC", 20, 2), ("ETH", 10, 1)):
            states.append({
                "asset": asset,
                "ready": True,
                "updated_at": fastdash_server.datetime.fromtimestamp(
                    now, fastdash_server.timezone.utc
                ).isoformat(),
                "market": {"slug": f"{asset.lower()}-updown-5m-1"},
                "network": {"under_50ms": True},
                "feeds": {
                    "open_price": 100,
                    "estimated_resolution_price": 100.1,
                    "basis_usd": 0.02,
                    "chainlink": {"source_ts": now},
                    "binance": {"price": 100.08, "source_ts": now - 0.1},
                },
                "books": {
                    "up": {"best_bid": 0.51, "best_ask": 0.52, "age_ms": 20},
                    "down": {"best_bid": 0.48, "best_ask": 0.49, "age_ms": 21},
                },
                "account": {
                    "initial_capital_usd": capital,
                    "cash_usd": capital - positions,
                    "market_value_usd": positions,
                    "equity_usd": capital,
                    "fills": positions,
                    "settled": 0,
                    "wins": 0,
                    "open_positions": [{} for _ in range(positions)],
                    "recent_trades": [],
                },
                "observer": {
                    "enabled": True,
                    "decision": {
                        "action": "CANDIDATE",
                        "outcome": "UP",
                        "fair_probability": 0.56,
                        "net_edge": 0.03,
                        "reasons": [],
                    },
                    "candidates": 2,
                    "fills": 1,
                    "settled": 1,
                    "wins": 1 if asset == "BTC" else 0,
                    "pnl_usd": 0.5 if asset == "BTC" else -0.5,
                    "staked_usd": 4,
                    "average_latency_ms": 30,
                    "recent_observations": [],
                },
            })
        result = fastdash_server.aggregate_states(states)
        self.assertTrue(result["ready"])
        self.assertEqual(result["portfolio"]["bot_count"], 2)
        self.assertEqual(result["portfolio"]["execution_engine_count"], 2)
        self.assertEqual(result["portfolio"]["strategy_lane_count"], 8)
        self.assertEqual(result["portfolio"]["active_market_count"], 2)
        self.assertEqual(result["portfolio"]["open_positions"], 3)
        self.assertEqual(result["portfolio"]["initial_capital_usd"], 30)
        self.assertEqual(result["observer"]["capital_usd"], 0)
        self.assertEqual(result["observer"]["settled"], 2)
        self.assertEqual(result["observer"]["wins"], 1)
        self.assertEqual(result["observer"]["accuracy"], 0.5)
        self.assertEqual(
            [lane["id"] for lane in result["bots"][0]["strategy_lanes"]],
            ["EDGE", "MOM", "BASIS", "MICRO"],
        )
        self.assertTrue(result["bots"][0]["strategy_lanes"][0]["tracked_execution"])
        self.assertEqual(result["bots"][0]["diagnostics"]["cvd"]["status"], "NO_FEED")



if __name__ == "__main__":
    unittest.main()
