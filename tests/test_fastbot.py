import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from fastbot import FastBot, PersistentClobProbe
from polyvote.fastconfig import load_fast_config


class PersistentClobProbeTests(unittest.TestCase):
    @patch("fastbot.http.client.HTTPSConnection")
    def test_measure_preserves_total_and_splits_queue_ttfb_and_body(self, connection_cls):
        response = Mock(status=200)
        response.read.return_value = b"ok"
        connection_cls.return_value.getresponse.return_value = response
        probe = PersistentClobProbe()

        sample = probe.measure(time.perf_counter() - 0.001)

        self.assertEqual(
            set(sample),
            {"total_ms", "queue_wait_ms", "socket_ttfb_ms", "body_read_ms"},
        )
        self.assertGreaterEqual(sample["queue_wait_ms"], 1.0)
        self.assertGreaterEqual(sample["socket_ttfb_ms"], 0.0)
        self.assertGreaterEqual(sample["body_read_ms"], 0.0)
        self.assertAlmostEqual(
            sample["total_ms"],
            sample["socket_ttfb_ms"] + sample["body_read_ms"],
            places=6,
        )
        connection_cls.return_value.request.assert_called_once_with(
            "GET",
            "/time",
            headers={"Connection": "keep-alive"},
        )


class FastBotFeedTests(unittest.TestCase):
    def test_filtered_rtds_history_snapshot_is_replayed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = load_fast_config("fast_config.yaml")
            cfg = replace(cfg, storage={
                "state": str(root / "state.json"),
                "decisions": str(root / "decisions.jsonl"),
                "ledger": str(root / "ledger.db"),
                "reviews": str(root / "reviews.json"),
            })
            bot = FastBot(cfg)
            now = time.time()
            changed = bot._handle_price_message({
                "topic": "crypto_prices_chainlink",
                "payload": {
                    "symbol": "btc/usd",
                    "data": [
                        {"timestamp": (now - 120) * 1000, "value": 60000},
                        {"timestamp": (now - 1) * 1000, "value": 60100},
                    ],
                },
            })
            self.assertTrue(changed)
            self.assertEqual(bot.signal.chainlink.value, 60100)
            self.assertEqual(len(bot.signal._chainlink_history), 2)


if __name__ == "__main__":
    unittest.main()
