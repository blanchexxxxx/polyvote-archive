import json
import unittest

from polyvote.fastmarket import (
    apply_clob_market_info,
    apply_market_processing_info,
    crypto_fee_per_share,
    market_slug,
    parse_fast_market,
    resolved_fast_outcome,
    select_any_market_payload,
    select_market_payload,
    window_start_epoch,
)


class FastMarketTests(unittest.TestCase):
    def test_window_slug(self):
        self.assertEqual(window_start_epoch(1_000.2, 300), 900)
        self.assertEqual(market_slug("BTC", 900), "btc-updown-5m-900")

    def test_crypto_fee_curve(self):
        self.assertAlmostEqual(crypto_fee_per_share(0.5, 0.07), 0.0175)
        self.assertAlmostEqual(crypto_fee_per_share(0.9, 0.07), 0.0063)

    def test_parse_gamma_market_and_outcome_mapping(self):
        raw = {
            "conditionId": "condition",
            "slug": "btc-updown-5m-900",
            "question": "Bitcoin Up or Down?",
            "outcomes": json.dumps(["Down", "Up"]),
            "clobTokenIds": json.dumps(["down-token", "up-token"]),
            "feeRate": 0.07,
            "orderMinSize": 5,
            "orderPriceMinTickSize": 0.01,
            "secondsDelay": 0,
        }
        market = parse_fast_market(raw, 900)
        self.assertEqual(market.up_token, "up-token")
        self.assertEqual(market.down_token, "down-token")
        self.assertEqual(market.end_epoch, 1200)
        self.assertEqual(market.taker_delay_ms, 0)
        self.assertTrue(market.taker_delay_verified)
        self.assertIs(select_market_payload([{"markets": [raw]}]), raw)

    def test_delay_flag_is_not_converted_to_a_guessed_duration(self):
        raw = {
            "conditionId": "condition",
            "slug": "btc-updown-5m-900",
            "question": "Bitcoin Up or Down?",
            "outcomes": ["Up", "Down"],
            "clobTokenIds": ["up-token", "down-token"],
        }
        market = apply_clob_market_info(
            parse_fast_market(raw, 900),
            {"itode": True, "mts": "0.001", "mos": "7", "fd": {"r": "0.04"}},
        )
        self.assertTrue(market.taker_delay_enabled)
        self.assertEqual(market.taker_delay_ms, 0)
        self.assertFalse(market.taker_delay_verified)
        self.assertEqual(market.tick_size, 0.001)
        self.assertEqual(market.min_order_size, 7)
        self.assertEqual(market.fee_rate, 0.04)

        market = apply_market_processing_info(market, {"seconds_delay": 0})
        self.assertEqual(market.taker_delay_ms, 0)
        self.assertTrue(market.taker_delay_verified)

    def test_documented_processing_delay_is_converted_from_seconds(self):
        raw = {
            "conditionId": "condition",
            "slug": "btc-updown-5m-900",
            "question": "Bitcoin Up or Down?",
            "outcomes": ["Up", "Down"],
            "clobTokenIds": ["up-token", "down-token"],
        }
        market = apply_market_processing_info(
            parse_fast_market(raw, 900),
            {"seconds_delay": 3},
        )
        self.assertEqual(market.taker_delay_ms, 3000)
        self.assertTrue(market.taker_delay_verified)

    def test_official_resolution_requires_final_prices(self):
        raw = {
            "clobTokenIds": ["up", "down"],
            "outcomes": ["Up", "Down"],
            "outcomePrices": ["1", "0"],
            "closed": True,
        }
        self.assertIs(select_any_market_payload([raw]), raw)
        self.assertEqual(resolved_fast_outcome(raw), "UP")
        raw["outcomePrices"] = ["0.7", "0.3"]
        self.assertIsNone(resolved_fast_outcome(raw))


if __name__ == "__main__":
    unittest.main()
