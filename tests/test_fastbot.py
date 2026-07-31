import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from fastbot import FastBot, PersistentClobProbe, ProbeTimingDiagnostics
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
            {
                "total_ms",
                "queue_wait_ms",
                "socket_ttfb_ms",
                "body_read_ms",
                "worker_started_monotonic",
                "worker_completed_monotonic",
            },
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


class ProbeTimingDiagnosticsTests(unittest.TestCase):
    def test_tracks_latest_phase_and_sliding_ttfb_buckets(self):
        timing = ProbeTimingDiagnostics(size=2)
        timing.add(
            submitted=1.0,
            worker_started=1.1,
            worker_completed=1.2,
            coroutine_completed=1.3,
            ttfb_ms=99.0,
        )
        timing.add(
            submitted=2.0,
            worker_started=2.1,
            worker_completed=2.2,
            coroutine_completed=2.3,
            ttfb_ms=158.0,
        )
        timing.add(
            submitted=3.0,
            worker_started=3.1,
            worker_completed=3.2,
            coroutine_completed=3.3,
            ttfb_ms=600.0,
        )

        state = timing.state()
        self.assertEqual(state["samples"], 2)
        self.assertEqual(state["latest"]["submitted_monotonic"], 3.0)
        self.assertEqual(
            state["ttfb_buckets"],
            {
                "under_100_ms": 0,
                "100_to_140_ms": 0,
                "140_to_180_ms": 1,
                "180_to_500_ms": 0,
                "at_least_500_ms": 1,
            },
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
