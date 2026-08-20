import unittest

from polyvote.fastarb import analyze_complete_sets, fee_per_share, top_margins


def book(*, bids=(), asks=(), timestamp="1000"):
    return {
        "timestamp": timestamp,
        "bids": [{"price": str(price), "size": str(size)} for price, size in bids],
        "asks": [{"price": str(price), "size": str(size)} for price, size in asks],
    }


class CompleteSetArbitrageTests(unittest.TestCase):
    def test_dynamic_fee_curve(self):
        self.assertAlmostEqual(float(fee_per_share(__import__("decimal").Decimal("0.5"), __import__("decimal").Decimal("0.07"))), 0.0175)

    def test_buy_set_walks_equal_depth_and_accounts_for_fees(self):
        first = book(asks=((0.40, 10), (0.43, 20)), bids=((0.39, 10),))
        second = book(asks=((0.50, 5), (0.51, 20)), bids=((0.49, 10),))
        found = analyze_complete_sets(
            "c1", first, second,
            fee_rate=0.0,
            min_order_size=5,
            max_capital_usd=30,
            min_net_profit_usd=0.01,
            min_net_return=0.001,
        )
        buy = next(item for item in found if item.direction == "BUY_SET")
        self.assertEqual(float(buy.shares), 25.0)
        self.assertGreater(float(buy.net_profit_usd), 1.8)

    def test_fee_turns_apparent_crypto_arb_negative(self):
        first = book(asks=((0.48, 100),))
        second = book(asks=((0.49, 100),))
        self.assertGreater(top_margins(first, second, fee_rate=0)["buy_set"], 0)
        self.assertLess(top_margins(first, second, fee_rate=0.07)["buy_set"], 0)
        found = analyze_complete_sets(
            "c2", first, second,
            fee_rate=0.07,
            min_order_size=5,
            max_capital_usd=30,
            min_net_profit_usd=0,
            min_net_return=0,
        )
        self.assertFalse(any(item.direction == "BUY_SET" for item in found))

    def test_sell_set_uses_split_capital_and_equal_shares(self):
        first = book(bids=((0.58, 8), (0.52, 20)))
        second = book(bids=((0.47, 12), (0.45, 20)))
        found = analyze_complete_sets(
            "c3", first, second,
            fee_rate=0,
            min_order_size=5,
            max_capital_usd=30,
            min_net_profit_usd=0.01,
            min_net_return=0.001,
        )
        sell = next(item for item in found if item.direction == "SELL_SET")
        self.assertEqual(float(sell.shares), 8.0)
        self.assertAlmostEqual(float(sell.net_profit_usd), 0.4)

    def test_fixed_cost_and_minimum_order_fail_closed(self):
        first = book(asks=((0.45, 4),))
        second = book(asks=((0.54, 4),))
        found = analyze_complete_sets(
            "c4", first, second,
            fee_rate=0,
            min_order_size=5,
            max_capital_usd=30,
            min_net_profit_usd=0.01,
            min_net_return=0,
            fixed_cost_buffer_usd=0.05,
        )
        self.assertEqual(found, [])


if __name__ == "__main__":
    unittest.main()
