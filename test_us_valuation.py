"""Offline integration checks for fractional US valuations and snapshots."""
from datetime import date
from decimal import Decimal as D
import unittest
from unittest.mock import patch

import app
import history_backfill
import snapshot_job
import test_history_backfill as fixtures


def add(ledger, action, **fields):
    return app.create_event(ledger, action, **fields)[1]


def usd_quote(usd="100", fx="31.25", *, market="2026-10-21",
              at="2026-10-22T04:20:00+08:00", fx_at="2026-10-22T17:00:00+08:00"):
    return {"price": D(usd) * D(fx), "usd_price": D(usd), "fx_rate": D(fx),
            "at": at, "market_date": market, "fx_at": fx_at, "source": "test"}


class USValuationTests(unittest.TestCase):
    def setUp(self):
        ledger = add(app.load_bundle("", []), "deposit", amount="10000", date="2026-10-20")
        ledger = add(ledger, "buy", ticker="2330.TW", shares=2, price="100", fee="2", date="2026-10-20")
        self.ledger = add(ledger, "buy", ticker="AAPL", shares="0.375", settled_twd="1200",
                          date="2026-10-21")
        self.at = "2026-10-22T18:17:00+08:00"
        self.tw = {"price": D("100"), "at": "2026-10-22"}

    def test_snapshot_preserves_fraction_and_does_not_round_converted_unit_price(self):
        quote = usd_quote("0.014", "31.111")
        snapshot = app.build_daily_snapshot(self.ledger, {"2330.TW": self.tw, "AAPL": quote},
                                            recorded_at=self.at)
        self.assertEqual(snapshot["holdings"], {"2330.TW": 2, "AAPL": "0.375"})
        self.assertEqual(snapshot["cash_balance"], "8598.00")
        self.assertEqual(snapshot["market_value"], "200.16")
        self.assertEqual(snapshot["total_assets"], "8798.16")
        self.assertEqual(snapshot["profit"], "-1201.84")
        self.assertEqual(snapshot["quotes"]["AAPL"]["price"], "0.435554")
        self.assertEqual(snapshot["quotes"]["AAPL"]["fx_rate"], "31.111")
        self.assertEqual(app.parse_snapshots([[app.canonical(snapshot)]])[0], [snapshot])

    def test_snapshot_rejects_missing_fx_future_quote_and_inconsistent_conversion(self):
        no_fx = {k: v for k, v in usd_quote().items() if k != "fx_rate"}
        for quote in (None, no_fx, usd_quote(fx_at="2026-10-22T19:00:00+08:00"),
                      usd_quote(at="2026-10-22T19:00:00+08:00"), {**usd_quote(), "price": D("100")}):
            with self.subTest(quote=quote), self.assertRaises(app.LedgerError):
                app.build_daily_snapshot(self.ledger, {"2330.TW": self.tw, "AAPL": quote},
                                         recorded_at=self.at)

    def test_backfill_uses_each_valuation_days_fx_for_the_same_us_close(self):
        usd_days = {
            "2026-10-21": usd_quote(fx="30", market="2026-10-20",
                                   at="2026-10-21T04:20:00+08:00", fx_at="2026-10-21T17:00:00+08:00"),
            "2026-10-22": usd_quote(fx="32", market="2026-10-20", at="2026-10-21T04:20:00+08:00"),
        }
        def history(ticker, start, end):
            if ticker == "AAPL":
                return {"quotes": usd_days}
            return {"quotes": {"2026-10-20": {"price": D("100"), "at": "2026-10-20"}},
                    "coverage": [(start, end)]}

        store = fixtures.BatchStore(self.ledger)
        result = history_backfill.backfill_missing_days(store, self.ledger, history_loader=history,
                                                       at="2026-10-23T18:00:00+08:00")
        snapshots = app.merged_snapshots(self.ledger, result["rows"])[0]
        self.assertEqual(result["written"], 3)
        self.assertEqual(snapshots["2026-10-20"]["total_assets"], "9998.00")
        self.assertEqual(snapshots["2026-10-21"]["total_assets"], "9923.00")
        self.assertEqual(snapshots["2026-10-22"]["total_assets"], "9998.00")
        self.assertEqual(snapshots["2026-10-22"]["holdings"]["AAPL"], "0.375")

    def test_split_waits_for_a_close_on_new_share_basis(self):
        split = add(self.ledger, "split", ticker="AAPL", numerator=4, denominator=1, date="2026-10-22")
        quote = usd_quote()
        self.assertIsNone(history_backfill.quote_for_day(
            {"quotes": {"2026-10-22": quote}}, "AAPL", date(2026, 10, 22), split.state))
        with self.assertRaises(app.LedgerError):
            app.build_daily_snapshot(split, {"2330.TW": self.tw, "AAPL": quote}, recorded_at=self.at)

    def test_job_saves_mixed_market_and_skips_write_when_fx_is_unavailable(self):
        store = fixtures.BatchStore(self.ledger)
        with patch.object(store, "append_snapshot", create=True, side_effect=lambda s: store.append_snapshots([s])):
            result = snapshot_job.run_snapshot_job(store, quote_loader=lambda t: usd_quote() if t == "AAPL" else self.tw)
            self.assertTrue(result["written"])
            self.assertEqual(result["snapshot"]["total_assets"], "9969.88")
            with self.assertRaises(app.LedgerError):
                snapshot_job.run_snapshot_job(store, quote_loader=lambda t: None if t == "AAPL" else self.tw)
        self.assertEqual(len(store.read_snapshots()), 1)

    def test_separate_us_existing_holdings_stay_out_of_total_and_history(self):
        ledger = add(self.ledger, "set_existing_holding", ticker="VOO", shares="0.123456789012")
        quotes = {"2330.TW": self.tw, "AAPL": usd_quote(), "VOO": usd_quote("500", "32")}
        snapshot = app.build_daily_snapshot(ledger, quotes, recorded_at=self.at)
        self.assertNotIn("VOO", snapshot["holdings"])
        self.assertEqual(snapshot["total_assets"], "9969.88")
        self.assertEqual(app.existing_valuation(ledger.state, quotes), D("1975.31"))


if __name__ == "__main__":
    unittest.main()
