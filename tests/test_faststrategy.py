import unittest

from polyvote.fastbook import BookPair
from polyvote.fastmarket import FastMarket
from polyvote.faststrategy import FastSignalModel, evaluate_entry


STRATEGY = {
    "min_seconds_after_open": 8,
    "min_seconds_to_close": 20,
    "max_seconds_to_close": 270,
    "max_clob_age_ms": 500,
    "max_chainlink_age_ms": 1500,
    "max_binance_age_ms": 1000,
    "max_spread": 0.08,
    "min_net_edge": 0.04,
    "slippage_buffer": 0.01,
    "min_sigma_per_sqrt_second": 0.000035,
}
RISK = {"max_order_usd": 5, "max_window_usd": 5, "max_daily_loss_usd": 15, "max_orders_per_window": 1}


def fixture():
    market = FastMarket(
        "c", "btc-updown-5m-0", "q", "up", "down", 0, 300,
        0.07, 0.2, 0.01, 5, taker_delay_verified=True,
    )
    books = BookPair("up", "down")
    for asset in ("up", "down"):
        books.apply({"event_type": "book", "asset_id": asset, "timestamp": 100,
                     "bids": [{"price": ".50", "size": "20"}], "asks": [{"price": ".54", "size": "20"}]}, 10)
    signal = FastSignalModel(STRATEGY)
    signal.open_price = 100.0
    signal.update_chainlink(101.0, 100, recv_monotonic=10)
    signal.update_binance(101.0, 100, recv_monotonic=10)
    return market, books, signal


class FastStrategyTests(unittest.TestCase):
    def test_fee_aware_candidate(self):
        market, books, signal = fixture()
        decision = evaluate_entry(market, books, signal, 100, STRATEGY, RISK, now_monotonic=10.1)
        self.assertEqual(decision.action, "CANDIDATE")
        self.assertEqual(decision.outcome, "UP")
        self.assertGreater(decision.net_edge, STRATEGY["min_net_edge"])
        self.assertAlmostEqual(decision.fee_per_share, 0.07 * 0.54 * 0.46)

    def test_selected_probability_floor_blocks_tail_bet(self):
        market, books, signal = fixture()
        strategy = {**STRATEGY, "min_selected_probability": 0.999}
        decision = evaluate_entry(
            market, books, signal, 100, strategy, RISK, now_monotonic=10.1
        )
        self.assertEqual(decision.action, "WAIT")
        self.assertIn("selected_probability_below_threshold", decision.reasons)

    def test_stale_book_fails_closed(self):
        market, books, signal = fixture()
        decision = evaluate_entry(market, books, signal, 100, STRATEGY, RISK, now_monotonic=11)
        self.assertEqual(decision.action, "WAIT")
        self.assertFalse(decision.gates["books_healthy"])

    def test_missing_open_is_not_invented(self):
        market, books, signal = fixture()
        signal.open_price = None
        decision = evaluate_entry(market, books, signal, 100, STRATEGY, RISK, now_monotonic=10.1)
        self.assertEqual(decision.action, "WAIT")
        self.assertIn("open_captured", decision.reasons)

    def test_unknown_processing_delay_fails_closed(self):
        market, books, signal = fixture()
        market = FastMarket(
            market.condition_id, market.slug, market.question,
            market.up_token, market.down_token, market.start_epoch, market.end_epoch,
            market.fee_rate, market.maker_rebate, market.tick_size, market.min_order_size,
        )
        decision = evaluate_entry(
            market, books, signal, 100, STRATEGY, RISK, now_monotonic=10.1,
        )
        self.assertEqual(decision.action, "WAIT")
        self.assertIn("processing_delay_known", decision.reasons)

    def test_cumulative_window_exposure_limits_next_tranche(self):
        market, books, signal = fixture()
        multi_risk = {
            **RISK,
            "max_order_usd": 3,
            "max_window_usd": 5,
            "max_orders_per_window": 3,
        }
        decision = evaluate_entry(
            market,
            books,
            signal,
            100,
            STRATEGY,
            multi_risk,
            orders_this_window=1,
            window_committed_usd=4,
            now_monotonic=10.1,
        )
        self.assertEqual(decision.action, "WAIT")
        self.assertIn("minimum_order_exceeds_cap", decision.reasons)

    def test_open_price_is_recovered_from_prewarmed_history(self):
        market = FastMarket("c", "btc-updown-5m-100", "q", "up", "down", 100, 400, 0.07, 0.2, 0.01, 5)
        signal = FastSignalModel(STRATEGY)
        signal.update_chainlink(99.5, 101, recv_monotonic=9.0)
        signal.update_chainlink(100.5, 110, recv_monotonic=9.1)
        self.assertTrue(signal.capture_open(market))
        self.assertEqual(signal.open_price, 99.5)
        self.assertEqual(signal.open_source_ts, 101)

    def test_out_of_order_price_is_rejected(self):
        signal = FastSignalModel(STRATEGY)
        self.assertTrue(signal.update_chainlink(100, 101, recv_monotonic=1))
        self.assertFalse(signal.update_chainlink(99, 100, recv_monotonic=2))
        self.assertEqual(signal.chainlink.value, 100)


if __name__ == "__main__":
    unittest.main()
