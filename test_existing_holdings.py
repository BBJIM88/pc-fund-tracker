"""Offline ledger and Streamlit checks for independently tracked existing holdings.

Run: venv\\Scripts\\python.exe -m unittest -v test_existing_holdings.py
All Sheets and market-data calls use fakes; no credentials or live data are read.
"""

import json
import unittest
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import gspread
import pandas as pd
import streamlit as st
from streamlit.testing.v1 import AppTest

import app


def sample_baseline():
    baseline = app.default_data()
    baseline["cash_account"] = {
        "balance": 939,
        "history": [{"date": "2026-01-01", "amount": 1000, "note": "salary"}],
    }
    baseline["target_hardware"]["current_target"] = 5000
    baseline["transactions"] = [{
        "type": "buy", "date": "2026-01-01", "ticker": "2330.TW",
        "shares": 3, "price": 20, "fee": 1,
    }]
    baseline["daily_snapshots"] = {
        "2026-01-01": {"total_assets": 1239.0, "target_price": 5000.0},
    }
    return baseline


class FakeWorksheet:
    def __init__(self, rows=None):
        self.rows = deepcopy(rows or [])

    def acell(self, address):
        assert address == "A1"
        return SimpleNamespace(value=self.rows[0][0] if self.rows else "")

    def get_all_values(self):
        return deepcopy(self.rows)

    def append_row(self, row, **kwargs):
        self.rows.append(deepcopy(row))


class FakeSpreadsheet:
    def __init__(self, baseline=None, events=None):
        self.raw_baseline = json.dumps(baseline or app.default_data(), ensure_ascii=False)
        self.sheet1 = FakeWorksheet([[self.raw_baseline]])
        self.events = FakeWorksheet([[json.dumps(event)] for event in events or []])

    def worksheet(self, title):
        if title != app.EVENTS_NAME:
            raise gspread.WorksheetNotFound(title)
        return self.events

    def add_worksheet(self, title, **kwargs):
        assert title == app.EVENTS_NAME
        return self.events


def event(action, **fields):
    return {"id": "test-event", "action": action, "date": "2026-01-01", **fields}


class ExistingHoldingsLedgerTests(unittest.TestCase):
    def setUp(self):
        self.state = app.make_state(sample_baseline())

    def test_old_baseline_starts_with_empty_existing_holdings(self):
        self.assertEqual(self.state["existing_holdings"], {})
        self.assertEqual(app.holdings(self.state), {"2330.TW": 3})

    def test_add_update_remove_leave_original_ledger_unchanged(self):
        original = deepcopy(self.state)
        app.apply_event(self.state, event("set_existing_holding", ticker=" 2330.tw ", shares=20))
        app.apply_event(self.state, event("set_existing_holding", ticker="2330.TW", shares=7))
        self.assertEqual(self.state["existing_holdings"], {"2330.TW": 7})
        self.assertEqual(app.holdings(self.state), {"2330.TW": 3})
        self.assertEqual({k: v for k, v in self.state.items() if k != "existing_holdings"},
                         {k: v for k, v in original.items() if k != "existing_holdings"})
        app.apply_event(self.state, event("remove_existing_holding", ticker=" 2330.tw "))
        self.assertEqual(self.state, original)

    def test_tw_and_two_are_separate_normalized_symbols(self):
        app.apply_event(self.state, event("set_existing_holding", ticker=" 6488.two ", shares=11))
        app.apply_event(self.state, event("set_existing_holding", ticker="0050.tw", shares=8))
        self.assertEqual(self.state["existing_holdings"], {"6488.TWO": 11, "0050.TW": 8})

    def test_invalid_shares_are_rejected_without_partial_mutation(self):
        for shares in (0, -1, 1.9, "1.1", True, False, float("nan"), float("inf"),
                       "NaN", "Infinity", None, "abc", 2**53):
            with self.subTest(shares=shares):
                original = deepcopy(self.state)
                with self.assertRaises(ValueError):
                    app.apply_event(self.state, event("set_existing_holding", ticker="2330.TW", shares=shares))
                self.assertEqual(self.state, original)

    def test_invalid_tickers_are_rejected(self):
        for ticker in ("2330", "AAPL", "2330.US", "<script>.TW", "", None):
            with self.subTest(ticker=ticker), self.assertRaises(ValueError):
                app.apply_event(self.state, event("set_existing_holding", ticker=ticker, shares=1))
        self.assertEqual(self.state["existing_holdings"], {})

    def test_removing_missing_ticker_leaves_other_holdings_unchanged(self):
        app.apply_event(self.state, event("set_existing_holding", ticker="0050.TW", shares=8))
        original = deepcopy(self.state)
        app.apply_event(self.state, event("remove_existing_holding", ticker="2330.TW"))
        self.assertEqual(self.state, original)

    def test_empty_valuation_needs_no_quotes(self):
        with patch.object(app, "quote") as quote:
            self.assertEqual(app.price_existing_holdings(self.state), ({}, Decimal("0.00")))
            quote.assert_not_called()

    def test_same_ticker_valued_independently_from_original_finances(self):
        app.apply_event(self.state, event("set_existing_holding", ticker="2330.TW", shares=20))
        with patch.object(app, "quote", return_value={"price": Decimal("100.00"), "at": "2026-01-01"}):
            prices, portfolio, assets = app.price_all(self.state)
            _, existing = app.price_existing_holdings(self.state)
        self.assertEqual((portfolio, assets, existing),
                         (Decimal("300.00"), Decimal("1239.00"), Decimal("2000.00")))
        self.assertEqual(app.investment_profit(self.state, prices), Decimal("239.00"))
        self.assertEqual(app.cumulative_principal(self.state), Decimal("1000.00"))

    def test_missing_existing_quote_does_not_disable_original_valuation(self):
        app.apply_event(self.state, event("set_existing_holding", ticker="0050.TW", shares=2))
        quotes = {"2330.TW": {"price": Decimal("100.00"), "at": "2026-01-01"}, "0050.TW": None}
        with patch.object(app, "quote", side_effect=quotes.get):
            prices, _, assets = app.price_all(self.state)
            existing_prices, existing = app.price_existing_holdings(self.state)
        self.assertEqual(assets, Decimal("1239.00"))
        self.assertEqual(app.investment_profit(self.state, prices), Decimal("239.00"))
        self.assertEqual(existing_prices, {"0050.TW": None})
        self.assertIsNone(existing)

    def test_missing_original_quote_does_not_disable_existing_valuation(self):
        app.apply_event(self.state, event("set_existing_holding", ticker="0050.TW", shares=2))
        quotes = {"2330.TW": None, "0050.TW": {"price": Decimal("123.45"), "at": "2026-01-01"}}
        with patch.object(app, "quote", side_effect=quotes.get):
            _, portfolio, assets = app.price_all(self.state)
            _, existing = app.price_existing_holdings(self.state)
        self.assertIsNone(portfolio)
        self.assertIsNone(assets)
        self.assertEqual(existing, Decimal("246.90"))

    def test_missing_quote_never_presents_partial_sum_as_total(self):
        for ticker in ("0050.TW", "6488.TWO"):
            app.apply_event(self.state, event("set_existing_holding", ticker=ticker, shares=3))
        quotes = {"0050.TW": {"price": Decimal("12.34"), "at": "2026-01-01"}, "6488.TWO": None}
        with patch.object(app, "quote", side_effect=quotes.get):
            prices, total = app.price_existing_holdings(self.state)
        self.assertEqual(set(prices), set(quotes))
        self.assertIsNone(total)
        quotes["6488.TWO"] = {"price": Decimal("56.78"), "at": "2026-01-01"}
        with patch.object(app, "quote", side_effect=quotes.get):
            self.assertEqual(app.price_existing_holdings(self.state)[1], Decimal("207.36"))

    def test_existing_only_shares_cannot_fund_original_sell_or_dividend(self):
        app.apply_event(self.state, event("set_existing_holding", ticker="0050.TW", shares=20))
        original = deepcopy(self.state)
        with self.assertRaises(ValueError):
            app.apply_event(self.state, event("sell", ticker="0050.TW", shares=1, price="100", fee="0"))
        with self.assertRaises(ValueError):
            app.apply_event(self.state, event("dividend", ticker="0050.TW", amount="10"))
        self.assertEqual(self.state, original)

    def test_original_sale_cannot_consume_existing_shares_of_same_symbol(self):
        app.apply_event(self.state, event("set_existing_holding", ticker="2330.TW", shares=20))
        original = deepcopy(self.state)
        with self.assertRaises(ValueError):
            app.apply_event(self.state, event("sell", ticker="2330.TW", shares=4, price="100", fee="0"))
        self.assertEqual(self.state, original)
        app.apply_event(self.state, event("sell", ticker="2330.TW", shares=3, price="100", fee="0"))
        self.assertEqual(app.holdings(self.state), {})
        self.assertEqual(self.state["existing_holdings"], {"2330.TW": 20})
        self.assertEqual(self.state["balance"], Decimal("1239.00"))

    def test_invalid_input_does_not_append_to_sheets(self):
        worksheet = FakeWorksheet()
        for action, fields in (
            ("set_existing_holding", {"ticker": "2330.TW", "shares": 1.5}),
            ("set_existing_holding", {"ticker": "bad", "shares": 1}),
            ("remove_existing_holding", {"ticker": "bad"}),
        ):
            with self.subTest(action=action, fields=fields), self.assertRaises(ValueError):
                app.commit(worksheet, self.state, action, **fields)
        self.assertEqual(worksheet.rows, [])

    def test_persisted_add_update_remove_roundtrip_and_duplicate_id_replay(self):
        book = FakeSpreadsheet(sample_baseline())
        original_a1 = deepcopy(book.sheet1.rows)
        with patch.object(app, "connection", return_value=book), patch.object(app.st, "rerun"):
            state, worksheet = app.load_ledger()
            app.commit(worksheet, state, "set_existing_holding", ticker="2330.TW", shares=20)
            state, _ = app.load_ledger()
            self.assertEqual(state["existing_holdings"], {"2330.TW": 20})
            app.commit(worksheet, state, "set_existing_holding", ticker="2330.TW", shares=9)
            state, _ = app.load_ledger()
            self.assertEqual(state["existing_holdings"], {"2330.TW": 9})
            app.commit(worksheet, state, "remove_existing_holding", ticker="2330.TW")
            worksheet.rows.append(deepcopy(worksheet.rows[-1]))
            state, _ = app.load_ledger()
            self.assertEqual(state["existing_holdings"], {})
        self.assertEqual(book.sheet1.rows, original_a1)
        self.assertEqual(state, app.make_state(sample_baseline()))

    def test_distinct_retry_ids_do_not_accumulate_shares_or_break_removal_replay(self):
        actions = [
            {**event("set_existing_holding", ticker="2330.TW", shares=20), "id": "save-one"},
            {**event("set_existing_holding", ticker="2330.TW", shares=20), "id": "save-two"},
        ]
        book = FakeSpreadsheet(sample_baseline(), events=actions)
        with patch.object(app, "connection", return_value=book):
            state, worksheet = app.load_ledger()
            self.assertEqual(state["existing_holdings"], {"2330.TW": 20})
            for entry_id in ("remove-one", "remove-two"):
                removal = {**event("remove_existing_holding", ticker="2330.TW"), "id": entry_id}
                worksheet.append_row([json.dumps(removal)])
            state, _ = app.load_ledger()
        self.assertEqual(state, app.make_state(sample_baseline()))


class ExistingHoldingsUITests(unittest.TestCase):
    PASSWORD = "offline-test-password"

    def setUp(self):
        st.cache_data.clear()
        self.book = FakeSpreadsheet(sample_baseline())
        self.prices = {"2330.TW": 100.0, "0050.TW": 50.0}
        client = SimpleNamespace(open=lambda name: self.book)
        self.gspread_patch = patch("gspread.service_account_from_dict", return_value=client)
        self.yfinance_patch = patch("yfinance.Ticker", side_effect=self.fake_ticker)
        self.gspread_patch.start()
        self.yfinance_patch.start()
        self.addCleanup(self.gspread_patch.stop)
        self.addCleanup(self.yfinance_patch.stop)
        self.addCleanup(st.cache_data.clear)

    def fake_ticker(self, ticker):
        def history(**kwargs):
            price = self.prices.get(ticker)
            if price is None:
                return pd.DataFrame()
            return pd.DataFrame({"Close": [price]}, index=pd.DatetimeIndex(["2026-01-01"], tz="Asia/Taipei"))
        return SimpleNamespace(history=history)

    def open_app(self):
        at = AppTest.from_file(str(Path(__file__).with_name("app.py")), default_timeout=15)
        at.secrets["APP_PASSWORD"] = self.PASSWORD
        at.secrets["GCP_KEY_JSON"] = "{}"
        at.run()
        at.text_input[0].set_value(self.PASSWORD)
        self.button(at, "登入").click().run()
        self.assertFalse(at.exception)
        return at

    @staticmethod
    def button(at, label):
        return next(button for button in at.button if button.label == label)

    def assert_metric(self, at, label, expected):
        values = [metric.value for metric in at.metric if metric.label == label]
        self.assertTrue(values, f"Missing metric: {label}")
        self.assertTrue(all(value == expected for value in values), (label, values))

    def save(self, at, ticker, shares):
        at.text_input(key="existing_ticker").set_value(ticker)
        at.number_input(key="existing_shares").set_value(shares)
        self.button(at, "儲存既有庫存").click().run()
        self.assertFalse(at.exception)

    def test_add_update_remove_and_new_session_reload_keep_original_assets(self):
        original_a1 = deepcopy(self.book.sheet1.rows)
        at = self.open_app()
        self.assert_metric(at, "既有庫存總市值", "NT$ 0.00")
        self.save(at, " 2330.tw ", 20)
        self.assert_metric(at, "既有庫存總市值", "NT$ 2,000.00")
        self.assert_metric(at, "目前總資產", "NT$ 1,239.00")
        self.assert_metric(at, "投資損益", "NT$ 239.00")
        self.save(at, "2330.TW", 9)
        self.assert_metric(at, "既有庫存總市值", "NT$ 900.00")
        reopened = self.open_app()
        self.assert_metric(reopened, "既有庫存總市值", "NT$ 900.00")
        reopened.selectbox(key="remove_existing_ticker").select("2330.TW")
        self.button(reopened, "移除此檔既有庫存").click().run()
        self.assertFalse(reopened.exception)
        self.assert_metric(reopened, "既有庫存總市值", "NT$ 0.00")
        self.assert_metric(reopened, "目前總資產", "NT$ 1,239.00")
        self.assertEqual(self.book.sheet1.rows, original_a1)
        with patch.object(app, "connection", return_value=self.book):
            state, _ = app.load_ledger()
        self.assertEqual(state["existing_holdings"], {})
        self.assertEqual(state["snapshots"]["2026-01-01"], {"total_assets": 1239.0, "target_price": 5000.0})
        self.assertTrue(all(snapshot["total_assets"] == 1239.0 for snapshot in state["snapshots"].values()))

    def test_unavailable_existing_quote_can_be_saved_without_affecting_original_assets(self):
        self.prices["0050.TW"] = None
        at = self.open_app()
        self.save(at, "0050.TW", 15)
        self.assert_metric(at, "既有庫存總市值", "無法計算")
        self.assert_metric(at, "目前總資產", "NT$ 1,239.00")
        self.assertTrue(any("0050.TW" in warning.value for warning in at.warning))
        with patch.object(app, "connection", return_value=self.book):
            state, _ = app.load_ledger()
        self.assertEqual(state["existing_holdings"], {"0050.TW": 15})

    def test_missing_original_quote_still_renders_existing_total(self):
        self.prices["2330.TW"] = None
        at = self.open_app()
        self.save(at, "0050.TW", 15)
        self.assert_metric(at, "既有庫存總市值", "NT$ 750.00")
        self.assert_metric(at, "目前總資產", "無法計算")

    def test_invalid_ticker_does_not_write_an_event(self):
        at = self.open_app()
        before = deepcopy(self.book.events.rows)
        self.save(at, "AAPL", 15)
        self.assertTrue(at.error)
        self.assertEqual(self.book.events.rows, before)


if __name__ == "__main__":
    unittest.main()
