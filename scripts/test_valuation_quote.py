import unittest

import pandas as pd

from scripts.valuation_pipeline import PipelineError, current_market_quote


class FakeMarketService:
    def __init__(self, date="2026-07-22", close=68.58):
        self.date = date
        self.close = close

    def get_daily_bars(self, codes, as_of, minimum_days):
        code = codes[0][0]
        frame = pd.DataFrame([{"date": self.date, "close": self.close, "source": "test"}])
        return {code: frame}, {code: {"source": "database", "error": None}}


class CurrentMarketQuoteTest(unittest.TestCase):
    def test_uses_latest_close_with_audit_metadata(self):
        quote = current_market_quote("300442", "润泽科技", "2026-07-22", FakeMarketService())
        self.assertEqual(quote, {"current_price": 68.58, "price_date": "2026-07-22", "price_source": "test"})

    def test_rejects_stale_quote(self):
        with self.assertRaisesRegex(PipelineError, "行情过期"):
            current_market_quote("300442", "润泽科技", "2026-07-22", FakeMarketService("2026-07-21"))

    def test_rejects_non_positive_price(self):
        with self.assertRaisesRegex(PipelineError, "必须大于零"):
            current_market_quote("300442", "润泽科技", "2026-07-22", FakeMarketService(close=0))


if __name__ == "__main__":
    unittest.main()
