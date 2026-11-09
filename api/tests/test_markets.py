"""How many candles a year holds for each kind of market, which sets how Sharpe and Sortino are annualised."""
from django.test import SimpleTestCase

from algo_tracker_backend.markets import PRESETS, parse_market, Market

MINUTE, HOUR, DAY, WEEK = 60, 3600, 86400, 7 * 86400


class PeriodsPerYearTests(SimpleTestCase):
    def test_always_open_market(self):
        market = PRESETS["always_open"]
        self.assertAlmostEqual(market.periods_per_year(MINUTE), 525_600)
        self.assertAlmostEqual(market.periods_per_year(HOUR), 8_760)
        self.assertAlmostEqual(market.periods_per_year(DAY), 365)
        self.assertAlmostEqual(market.periods_per_year(WEEK), 365 / 7)

    def test_us_equity_market(self):
        market = PRESETS["us_equity"]
        self.assertAlmostEqual(market.periods_per_year(MINUTE), 98_280)   # 252 x 6.5 x 60
        self.assertAlmostEqual(market.periods_per_year(HOUR), 1_638)
        self.assertAlmostEqual(market.periods_per_year(DAY), 252)         # one candle a session
        self.assertAlmostEqual(market.periods_per_year(WEEK), 365 / 7)

    def test_an_explicit_market_matches_the_preset_it_spells_out(self):
        self.assertEqual(parse_market("X", "252d6.5h").periods_per_year(MINUTE), PRESETS["us_equity"].periods_per_year(MINUTE))
        self.assertEqual(parse_market("X", "365d24h").periods_per_year(DAY), 365)


class ParseMarketTests(SimpleTestCase):
    def test_names(self):
        self.assertIs(parse_market("AAPL", "us_equity"), PRESETS["us_equity"])
        self.assertIs(parse_market("BTC/USD", " always_open "), PRESETS["always_open"])

    def test_old_number_is_still_read_as_trading_hours_per_year_with_a_warning(self):
        with self.assertLogs("algo_tracker_backend.markets", "WARNING") as logs:
            market = parse_market("BTC/USD", "8760")
        self.assertIn("old trading-hours-per-year number", logs.output[0])
        self.assertIn("BTC/USD", logs.output[0])
        # The old arithmetic is kept, so a number that was right stays right and nothing silently changes.
        self.assertAlmostEqual(market.periods_per_year(DAY), 365)
        self.assertAlmostEqual(market.periods_per_year(MINUTE), 525_600)
        self.assertAlmostEqual(Market("x", 0, 0, legacy_hours_per_year=1638).periods_per_year(DAY), 68.25)

    def test_rubbish_is_refused_with_the_valid_forms(self):
        for bad in ("nyse", "", "0", "-5", "400d6h", "252d25h", "0d6h"):
            with self.subTest(bad=bad), self.assertRaises(ValueError) as caught:
                parse_market("AAPL", bad)
            self.assertIn("AAPL", str(caught.exception))
