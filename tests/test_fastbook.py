import unittest

from polyvote.fastbook import BookPair, LocalBook


class FastBookTests(unittest.TestCase):
    def test_snapshot_and_incremental_update(self):
        book = LocalBook("up")
        accepted = book.apply_snapshot(
            {
                "timestamp": 1000,
                "bids": [{"price": "0.48", "size": "10"}],
                "asks": [{"price": "0.52", "size": "8"}],
            },
            recv_monotonic=10,
        )
        self.assertTrue(accepted)
        self.assertTrue(book.synchronized)
        self.assertEqual(book.best_bid, 0.48)
        self.assertTrue(
            book.apply_change(
                {"side": "BUY", "price": "0.49", "size": "4", "best_bid": "0.49", "best_ask": "0.52"},
                1001,
                recv_monotonic=11,
            )
        )
        self.assertEqual(book.best_bid, 0.49)

    def test_out_of_order_update_is_discarded(self):
        book = LocalBook("up")
        book.apply_snapshot(
            {"timestamp": 1000, "bids": [{"price": ".48", "size": "1"}], "asks": [{"price": ".52", "size": "1"}]},
            recv_monotonic=10,
        )
        self.assertFalse(book.apply_change({"side": "BUY", "price": ".5", "size": "1"}, 999, 11))
        self.assertEqual(book.best_bid, 0.48)
        self.assertEqual(book.out_of_order, 1)

    def test_best_price_mismatch_marks_book_unsynchronized(self):
        pair = BookPair("up", "down")
        pair.apply({"event_type": "book", "asset_id": "up", "timestamp": 1000,
                    "bids": [{"price": ".48", "size": "1"}], "asks": [{"price": ".52", "size": "1"}]}, 10)
        pair.apply({"event_type": "price_change", "timestamp": 1001, "price_changes": [
            {"asset_id": "up", "side": "BUY", "price": ".49", "size": "1", "best_bid": ".47", "best_ask": ".52"}
        ]}, 11)
        self.assertFalse(pair.up.synchronized)
        self.assertEqual(pair.up.mismatch_count, 1)

    def test_best_bid_ask_event_updates_top_without_depth(self):
        pair = BookPair("up", "down")
        pair.apply({
            "event_type": "best_bid_ask",
            "asset_id": "up",
            "timestamp": 1000,
            "best_bid": "0.61",
            "best_ask": "0.62",
        }, 10)
        self.assertTrue(pair.up.synchronized)
        self.assertEqual(pair.up.best_bid, 0.61)
        self.assertEqual(pair.up.best_ask, 0.62)

    def test_crossed_best_event_fails_closed(self):
        book = LocalBook("up")
        self.assertFalse(book.apply_best({
            "timestamp": 1000,
            "best_bid": "0.62",
            "best_ask": "0.61",
        }, 10))
        self.assertFalse(book.synchronized)


if __name__ == "__main__":
    unittest.main()
