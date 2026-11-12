import unittest
from datetime import date, datetime, time
from decimal import Decimal
from unittest.mock import Mock, patch

import pandas as pd

import us_market as market


def day(value):
    return date.fromisoformat(value)


def valuation(value, hour=23, minute=59):
    return datetime.combine(day(value), time(hour, minute), market.TAIPEI)


def frame(*rows, with_actions=True):
    index = pd.DatetimeIndex([stamp for stamp, _, _ in rows], tz=market.NEW_YORK)
    values = {"Close": [close for _, close, _ in rows]}
    if with_actions:
        values["Stock Splits"] = [split for _, _, split in rows]
    return pd.DataFrame(values, index=index)


class UsMarketTests(unittest.TestCase):
    def start_patch(self, replacement):
        value = replacement.start()
        self.addCleanup(replacement.stop)
        return value

    def setUp(self):
        self.now = self.start_patch(patch.object(market, "_now", return_value=valuation("2026-11-10", 18)))
        self.stock = Mock()
        self.stock.get_history_metadata.return_value = {
            "currency": "USD", "instrumentType": "EQUITY", "exchangeTimezoneName": "America/New_York"
        }
        self.stock.history.return_value = frame(
            ("2026-11-05", 98, 0), ("2026-11-06", 100, 0), ("2026-11-09", 102, 0)
        )
        self.ticker = self.start_patch(patch("yfinance.Ticker", return_value=self.stock))
        self.fx = self.start_patch(patch.object(market, "_fx_history", return_value={
            day("2026-11-05"): Decimal("30.75"),
            day("2026-11-06"): Decimal("31.32"),
            day("2026-11-09"): Decimal("32.5"),
            day("2026-11-10"): Decimal("33.01"),
        }))

    def test_latest_quote_keeps_raw_currency_rate_and_unrounded_twd(self):
        self.stock.history.return_value = frame(
            ("2026-11-05", 98, 0), ("2026-11-06", 100, 0), ("2026-11-09", 102.125, 0)
        )
        quote = market.latest_quote("brk.b")
        self.assertEqual(quote["usd_price"], Decimal("102.125"))
        self.assertEqual(quote["fx_rate"], Decimal("33.01"))
        self.assertEqual(quote["price"], Decimal("3371.14625"))
        self.assertEqual(quote["market_date"], "2026-11-09")
        self.assertEqual(quote["at"], "2026-11-10T05:15:00+08:00")
        self.assertEqual(quote["fx_at"], "2026-11-10T17:00:00+08:00")
        self.ticker.assert_called_once_with("BRK-B")
        self.assertEqual(self.stock.history.call_args.kwargs["interval"], "1d")
        self.assertTrue(self.stock.history.call_args.kwargs["actions"])
        self.assertFalse(self.stock.history.call_args.kwargs["auto_adjust"])

    def test_never_uses_same_day_fx_before_its_publication(self):
        quote = market.latest_quote("VOO", at=valuation("2026-11-10", 16, 59))
        self.assertEqual(quote["market_date"], "2026-11-09")
        self.assertEqual(quote["fx_rate"], Decimal("32.5"))

    def test_taipei_day_values_only_closes_known_at_end_of_that_day(self):
        start, end = day("2026-11-06"), day("2026-11-10")
        result = market.load_history("AAPL", start, end)
        quotes = result["quotes"]
        self.assertEqual(quotes["2026-11-06"]["market_date"], "2026-11-05")
        self.assertEqual(quotes["2026-11-06"]["price"], Decimal("3069.36"))
        self.assertEqual(quotes["2026-11-07"]["market_date"], "2026-11-06")
        self.assertEqual(quotes["2026-11-08"]["price"], Decimal("3132.00"))
        # Monday Taipei: CBC has a new rate, but Monday New York has not closed.
        self.assertEqual(quotes["2026-11-09"]["market_date"], "2026-11-06")
        self.assertEqual(quotes["2026-11-09"]["price"], Decimal("3250.0"))
        self.assertEqual(result["coverage"], [(start, end)])

    def test_current_in_progress_close_is_excluded(self):
        self.now.return_value = valuation("2026-11-10", 3)
        quote = market.latest_quote("AAPL")
        self.assertEqual(quote["market_date"], "2026-11-06")

    def test_closes_follow_dst_and_standard_holidays(self):
        self.assertEqual(market._stock_available(day("2026-07-01")).isoformat(), "2026-07-02T04:15:00+08:00")
        self.assertEqual(market._stock_available(day("2026-01-05")).isoformat(), "2026-01-06T05:15:00+08:00")
        # Good Friday and the Friday observance of July 4 are closed.
        self.assertEqual(market._last_session(valuation("2026-04-04")), day("2026-04-02"))
        self.assertEqual(market._last_session(valuation("2026-07-04")), day("2026-07-02"))
        # New Year's on Saturday does not close the previous year-end Friday.
        self.assertEqual(market._last_session(valuation("2028-01-01")), day("2027-12-31"))

    def test_pre_split_price_restored_even_when_split_is_after_history_range(self):
        self.stock.history.return_value = frame(
            ("2026-11-05", 24.5, 0), ("2026-11-06", 25, 0), ("2026-11-09", 26, 4)
        )
        result = market.load_history("AAPL", day("2026-11-07"), day("2026-11-08"))
        self.assertEqual(result["quotes"]["2026-11-07"]["usd_price"], Decimal("100"))
        self.assertEqual(market.latest_quote("AAPL")["usd_price"], Decimal("26"))
        self.assertEqual(self.stock.history.call_args.kwargs["end"], "2026-11-11")

    def test_reverse_split_is_restored_in_old_share_units(self):
        self.stock.history.return_value = frame(
            ("2026-11-05", 99, 0), ("2026-11-06", 99, 0), ("2026-11-09", 100, 1 / 3)
        )
        quote = market.latest_quote("AAPL", at=valuation("2026-11-08"))
        self.assertEqual(quote["usd_price"], Decimal("33"))

    def test_ambiguous_action_or_missing_intervening_action_cannot_value_earlier_price(self):
        cases = (
            # Contradicts Yahoo's documented adjusted price: do not multiply an already raw pre-split value.
            frame(("2026-11-05", 98, 0), ("2026-11-06", 100, 0), ("2026-11-09", 26, 4)),
            frame(("2026-11-05", 98, 0), ("2026-11-09", 102, 0)),
        )
        for prices in cases:
            with self.subTest(prices=prices):
                self.stock.history.return_value = prices
                self.assertIsNone(market.latest_quote("AAPL", at=valuation("2026-11-06")))
                self.assertIsNotNone(market.latest_quote("AAPL"))

    def test_split_day_before_completed_close_is_not_used_to_restate_prior_bar(self):
        self.now.return_value = valuation("2026-11-10", 3)
        self.stock.history.return_value = frame(
            ("2026-11-05", 24.5, 0), ("2026-11-06", 25, 0), ("2026-11-09", 26, 4)
        )
        self.assertIsNone(market.latest_quote("AAPL"))

    def test_missing_stock_close_leaves_gap_not_previous_session_price(self):
        self.stock.history.return_value = frame(
            ("2026-11-05", 98, 0), ("2026-11-06", float("nan"), 0), ("2026-11-09", 102, 0)
        )
        result = market.load_history("AAPL", day("2026-11-06"), day("2026-11-10"))
        self.assertEqual(list(result["quotes"]), ["2026-11-06", "2026-11-10"])

    def test_malformed_market_data_and_missing_fx_fail_safe(self):
        self.fx.return_value = {day("2026-10-10"): Decimal("31")}
        self.assertIsNone(market.latest_quote("AAPL"))
        self.fx.side_effect = TimeoutError("simulated")
        with self.assertLogs(market.LOG):
            self.assertIsNone(market.latest_quote("AAPL"))
        self.fx.side_effect = None
        self.stock.history.return_value = frame(("2026-11-09", 100, 0), with_actions=False)
        with self.assertLogs(market.LOG):
            self.assertIsNone(market.latest_quote("AAPL"))
        self.stock.history.return_value = frame(("2026-11-09", 100, 0))
        self.stock.get_history_metadata.return_value["currency"] = "CAD"
        with self.assertLogs(market.LOG):
            self.assertIsNone(market.latest_quote("AAPL"))


class CentralBankRateTests(unittest.TestCase):
    def test_parses_verified_daily_table_and_pagination(self):
        markup = '<table><tr><th>標題(日期)</th><th>NTD/USD</th></tr>' \
                 '<tr><td><a href="/tw/x">2026/11/12</a></td><td>32.889</td></tr>' \
                 '<tr><td>2026/11/11</td><td>32.860</td></tr></table>' \
                 '<a href="/tw/lp-645-1-57-60.html">最後一頁</a>'
        rows, last_page = market._parse_fx_page(markup)
        self.assertEqual(last_page, 57)
        self.assertEqual(rows, ((day("2026-11-12"), Decimal("32.889")),
                                (day("2026-11-11"), Decimal("32.860"))))
        with self.assertRaises(ValueError):
            market._parse_fx_page(markup.replace("NTD/USD", "USD/NTD"))
        with self.assertRaises(ValueError):
            market._parse_fx_page(markup.replace("32.860", "NaN"))

    def test_finds_older_fx_pages_without_inserting_future_rates(self):
        pages = {
            1: ((day("2026-11-12"), Decimal("32")), (day("2026-11-03"), Decimal("31"))),
            2: ((day("2026-11-02"), Decimal("30")), (day("2026-10-15"), Decimal("29"))),
            3: ((day("2026-10-14"), Decimal("28")), (day("2026-10-05"), Decimal("27"))),
        }
        with patch.object(market, "_fx_page", side_effect=lambda page, _: (pages[page], 3)) as load:
            rates = market._fx_history(day("2026-10-08"), day("2026-10-26"))
        self.assertEqual(rates[day("2026-10-05")], Decimal("27"))
        self.assertNotIn(day("2026-11-12"), rates)
        self.assertEqual([call.args[0] for call in load.call_args_list], [1, 2, 3])


if __name__ == "__main__":
    unittest.main()
