import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path

from fastbot import FastBot
from polyvote.fastconfig import load_fast_config


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
