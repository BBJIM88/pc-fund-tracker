"""Offline regression tests for the v2 ledger and separate existing holdings.

Run: python -m unittest -v test_existing_holdings
Google Sheets, credentials and market data are all replaced with fakes.
"""
import json
import unittest
from copy import deepcopy
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import gspread
import pandas as pd
import streamlit as st
from streamlit.testing.v1 import AppTest

import app


class FakeWorksheet:
    def __init__(self, title, sheet_id, rows=None):
        self.title, self.id = title, sheet_id
        self.rows = deepcopy(rows or [])

    def acell(self, address):
        assert address == "A1"
        return SimpleNamespace(value=self.rows[0][0] if self.rows else "")

    def get_all_values(self):
        return deepcopy(self.rows)

    def append_row(self, row, **kwargs):
        self.rows.append(deepcopy(row))


class FakeSpreadsheet:
    def __init__(self, raw="", events=None):
        self.id = "test-book"
        self.sheets = [FakeWorksheet("原帳本", 42, [[raw]] if raw else []),
                       FakeWorksheet(app.EVENTS_NAME, 43, [[app.canonical(e)] for e in events or []])]

    def worksheet(self, title):
        for sheet in self.sheets:
            if sheet.title == title:
                return sheet
        raise gspread.WorksheetNotFound(title)

    def worksheets(self):
        return self.sheets[:]

    def get_worksheet_by_id(self, sheet_id):
        return next((s for s in self.sheets if s.id == sheet_id), None)

    def add_worksheet(self, title, **kwargs):
        sheet = FakeWorksheet(title, max(s.id for s in self.sheets) + 1)
        self.sheets.append(sheet)
        return sheet


def advance(ledger, action, **fields):
    event, updated = app.create_event(ledger, action, **fields)
    return updated, event


def funded():
    ledger, _ = advance(app.load_bundle("", []), "deposit", amount="1000", date="2026-01-01")
    return advance(ledger, "buy", ticker="2330.TW", shares=3, price="20", fee="1", date="2026-01-02")[0]


class LedgerCompatibilityTests(unittest.TestCase):
    def test_existing_v2_financial_records_and_amendments_keep_their_results(self):
        ledger = funded()
        ledger, sale = advance(ledger, "sell", ticker="2330.TW", shares=1, price="100", fee="1", date="2026-01-03")
        ledger, _ = advance(ledger, "amend", target_id=sale["id"], replacement={**sale, "price": "110"}, reason="更正")
        original = deepcopy(ledger.state)
        imported, event = advance(ledger, "set_existing_holding", ticker=" 2330.tw ", shares=20)
        self.assertEqual(event["schema_version"], 2)
        self.assertIn("expected_revision", event)
        self.assertIn("event_hash", event)
        self.assertEqual(imported.state["existing_holdings"], {"2330.TW": 20})
        self.assertEqual({k: v for k, v in imported.state.items() if k != "existing_holdings"},
                         {k: v for k, v in original.items() if k != "existing_holdings"})
        self.assertEqual(imported.state["balance"], D("1048.00"))
        self.assertEqual(imported.state["sells"][0]["realized"], D("88.67"))

    def test_id_based_connection_accepts_both_supported_secret_formats(self):
        for raw in ('{"fake": "test"}', {"fake": "test"}):
            client = Mock()
            ui = SimpleNamespace(secrets={"GCP_KEY_JSON": raw, "SPREADSHEET_ID": "test-book", "BASELINE_WORKSHEET_ID": 42})
            with patch("gspread.service_account_from_dict", return_value=client) as auth:
                store = app.connect_store(ui)
            self.assertEqual(auth.call_args.args[0], {"fake": "test"})
            client.open_by_key.assert_called_once_with("test-book")
            client.open.assert_not_called()
            self.assertEqual(store.baseline_id, 42)

    def test_reordered_worksheets_and_pinned_marker_keep_same_a1(self):
        ledger = funded()
        book = FakeSpreadsheet(events=ledger.events)
        marker = {"id": "marker", "action": "baseline", "baseline_hash": app.text_hash(""), "baseline_worksheet_id": 42}
        book.worksheet(app.EVENTS_NAME).append_row([app.canonical(marker)])
        book.sheets.reverse()
        self.assertEqual(app.SheetStore(book).read().state, ledger.state)

    def test_old_feature_rows_and_snapshots_after_v2_are_read_without_rewriting(self):
        ledger = funded()
        additions = [
            {"id": "import", "action": "set_existing_holding", "date": "2026-10-21", "ticker": "2330.TW", "shares": 20},
            {"id": "snapshot", "action": "snapshot", "day": "2026-10-21", "total_assets": "1239", "target_price": "5000"},
        ]
        events = ledger.events + additions
        saved = deepcopy(events)
        restored = app.load_bundle("", events)
        self.assertEqual(events, saved)
        self.assertEqual(restored.state["existing_holdings"], {"2330.TW": 20})
        self.assertEqual(restored.state["balance"], ledger.state["balance"])
        later, _ = advance(restored, "deposit", amount="1", date="2026-01-04")
        self.assertEqual(later.state["balance"], D("940.00"))
        self.assertEqual(later.state["existing_holdings"], {"2330.TW": 20})

    def test_legacy_financial_writer_after_v2_still_rejected(self):
        ledger = funded()
        event = {"id": "unsafe-old-writer", "action": "deposit", "date": "2026-10-21", "amount": "1"}
        with self.assertRaisesRegex(app.LedgerError, "舊版寫入"):
            app.load_bundle("", ledger.events + [event])

    def test_backdated_financial_replay_preserves_latest_position_quantity(self):
        ledger = funded()
        ledger, _ = advance(ledger, "set_existing_holding", ticker="0050.TW", shares=10)
        ledger, _ = advance(ledger, "deposit", amount="2", date="2026-01-06")
        ledger, _ = advance(ledger, "set_existing_holding", ticker="0050.TW", shares=7)
        ledger, _ = advance(ledger, "deposit", amount="3", date="2026-01-05")
        self.assertEqual(ledger.state["existing_holdings"], {"0050.TW": 7})
        self.assertEqual(ledger.state["balance"], D("944.00"))

    def test_update_remove_retry_and_amendment_preserve_financial_ledger(self):
        ledger = funded()
        original = deepcopy(ledger.state)
        ledger, imported = advance(ledger, "set_existing_holding", ticker="6488.TWO", shares=20)
        ledger = app.load_bundle("", ledger.events + [deepcopy(imported)])
        ledger, _ = advance(ledger, "set_existing_holding", ticker="6488.TWO", shares=9)
        self.assertEqual(ledger.state["existing_holdings"], {"6488.TWO": 9})
        ledger, removal = advance(ledger, "remove_existing_holding", ticker="6488.TWO")
        ledger, _ = advance(ledger, "remove_existing_holding", ticker="6488.TWO")
        self.assertEqual(ledger.state, original)
        ledger, _ = advance(ledger, "amend", target_id=removal["id"], replacement=None, reason="保留追蹤")
        self.assertEqual(ledger.state, original)  # The second removal remains effective.

    def test_stale_position_write_is_rejected_and_duplicate_id_not_repeated(self):
        book = FakeSpreadsheet(events=funded().events)
        store = app.SheetStore(book, baseline_id=42)
        ledger = store.read()
        first, _ = app.create_event(ledger, "set_existing_holding", ticker="0050.TW", shares=8)
        stale, _ = app.create_event(ledger, "set_existing_holding", ticker="0050.TW", shares=3)
        app.write_pending(store, first)
        before = deepcopy(book.worksheet(app.EVENTS_NAME).rows)
        _, status = app.write_pending(store, stale)
        self.assertEqual(status["status"], "衝突未入帳")
        checked, status = app.write_pending(store, first)
        self.assertEqual(status["status"], "已入帳")
        self.assertEqual(checked.state["existing_holdings"], {"0050.TW": 8})
        self.assertEqual(book.worksheet(app.EVENTS_NAME).rows, before)

    def test_invalid_positions_rejected_before_any_write(self):
        ledger = funded()
        for shares in (0, -1, 1.5, True, "NaN", "Infinity", None, 10**12 + 1):
            with self.subTest(shares=shares), self.assertRaises(app.LedgerError):
                app.create_event(ledger, "set_existing_holding", ticker="2330.TW", shares=shares)
        for ticker in ("2330", "AAPL", "2330.US", "<script>.TW", ""):
            with self.subTest(ticker=ticker), self.assertRaises(app.LedgerError):
                app.create_event(ledger, "set_existing_holding", ticker=ticker, shares=1)
        self.assertEqual(ledger.state["existing_holdings"], {})

    def test_same_symbol_valuations_and_missing_quotes_are_independent(self):
        ledger, _ = advance(funded(), "set_existing_holding", ticker="2330.TW", shares=20)
        q = {"2330.TW": {"price": D("100.00")}}
        self.assertEqual(app.valuation(ledger.state, q), (D("300.00"), D("1239.00")))
        self.assertEqual(app.existing_valuation(ledger.state, q), D("2000.00"))
        self.assertEqual(app.profit_parts(ledger.state, q)["total"], D("239.00"))
        ledger, _ = advance(ledger, "set_existing_holding", ticker="0050.TW", shares=2)
        self.assertIsNone(app.existing_valuation(ledger.state, q))
        self.assertEqual(app.valuation(ledger.state, q)[1], D("1239.00"))
        self.assertIsNone(app.valuation(ledger.state, {"2330.TW": None})[1])
        self.assertEqual(app.existing_valuation(app.make_state(app.default_data()), {}), D("0.00"))

    def test_existing_only_shares_cannot_be_consumed_by_original_sell(self):
        ledger, _ = advance(funded(), "set_existing_holding", ticker="2330.TW", shares=20)
        with self.assertRaises(app.LedgerError):
            app.create_event(ledger, "sell", ticker="2330.TW", shares=4, price="100", fee="0")
        sold, _ = advance(ledger, "sell", ticker="2330.TW", shares=3, price="100", fee="0")
        self.assertEqual(app.holdings(sold.state), {})
        self.assertEqual(sold.state["existing_holdings"], {"2330.TW": 20})

    def test_complete_backup_roundtrip_contains_existing_positions(self):
        ledger, _ = advance(funded(), "set_existing_holding", ticker="0050.TW", shares=8)
        document = app.backup_document(ledger, spreadsheet_id="test-book", baseline_worksheet_id=42)
        self.assertEqual(app.validate_backup(json.loads(json.dumps(document))).state, ledger.state)

    def test_connection_failure_diagnostic_never_displays_exception_contents(self):
        ui = SimpleNamespace(error=Mock(), caption=Mock())
        app.show_failure(ui, PermissionError("fake-secret-sentinel"), "測試讀取")
        displayed = str(ui.error.call_args) + str(ui.caption.call_args)
        self.assertIn("PermissionError", displayed)
        self.assertNotIn("fake-secret-sentinel", displayed)


class ExistingHoldingsUITests(unittest.TestCase):
    PASSWORD = "offline-test-password"

    def setUp(self):
        st.cache_data.clear()
        self.book = FakeSpreadsheet(events=funded().events)
        self.prices = {"2330.TW": 100.0, "0050.TW": 50.0}
        self.client = SimpleNamespace(open_by_key=lambda _: self.book)
        self.patches = [patch("gspread.service_account_from_dict", return_value=self.client),
                        patch("yfinance.Ticker", side_effect=self.fake_ticker),
                        patch("urllib.request.urlopen", side_effect=TimeoutError("offline test"))]
        for replacement in self.patches:
            replacement.start()
            self.addCleanup(replacement.stop)
        self.addCleanup(st.cache_data.clear)

    def fake_ticker(self, ticker):
        def history(**kwargs):
            price = self.prices.get(ticker)
            return (pd.DataFrame({"Close": [price]}, index=pd.DatetimeIndex(["2026-10-21"], tz="Asia/Taipei"))
                    if price is not None else pd.DataFrame())
        return SimpleNamespace(history=history)

    def open_app(self):
        at = AppTest.from_file(str(Path(__file__).with_name("app.py")), default_timeout=15)
        at.secrets.update(APP_PASSWORD=self.PASSWORD, GCP_KEY_JSON="{}", SPREADSHEET_ID="test-book", BASELINE_WORKSHEET_ID=42)
        at.run()
        at.text_input[0].set_value(self.PASSWORD)
        self.button(at, "登入").click().run()
        self.assertFalse(at.exception)
        return at

    @staticmethod
    def button(at, label):
        return next(b for b in at.button if b.label == label)

    def assert_metric(self, at, label, value):
        self.assertEqual([m.value for m in at.metric if m.label == label], [value])

    def save(self, at, ticker, shares):
        at.text_input(key="existing_ticker").set_value(ticker)
        at.number_input(key="existing_shares").set_value(shares)
        self.button(at, "預覽既有庫存").click().run()
        self.assertFalse(at.exception)
        self.button(at, "確認寫入").click().run()
        self.assertFalse(at.exception)

    def test_preview_confirm_update_remove_and_new_session_keep_a1_and_assets(self):
        original_a1 = deepcopy(self.book.get_worksheet_by_id(42).rows)
        at = self.open_app()
        self.assertEqual(len(at.tabs), 6)
        self.save(at, " 2330.tw ", 20)
        self.assert_metric(at, "既有庫存總市值", "NT$ 2,000.00")
        self.assert_metric(at, "目前總資產", "NT$ 1,239.00")
        self.save(at, "2330.TW", 9)
        reopened = self.open_app()
        self.assert_metric(reopened, "既有庫存總市值", "NT$ 900.00")
        self.button(reopened, "預覽移除既有庫存").click().run()
        self.button(reopened, "確認寫入").click().run()
        self.assertFalse(reopened.exception)
        self.assert_metric(reopened, "既有庫存總市值", "NT$ 0.00")
        self.assert_metric(reopened, "目前總資產", "NT$ 1,239.00")
        self.assertEqual(self.book.get_worksheet_by_id(42).rows, original_a1)

    def test_missing_existing_quote_does_not_disable_original_assets(self):
        self.prices["0050.TW"] = None
        at = self.open_app()
        self.save(at, "0050.TW", 15)
        self.assert_metric(at, "既有庫存總市值", "報價不完整")
        self.assert_metric(at, "目前總資產", "NT$ 1,239.00")

    def test_missing_original_quote_does_not_disable_existing_value(self):
        self.prices["2330.TW"] = None
        at = self.open_app()
        self.save(at, "0050.TW", 15)
        self.assert_metric(at, "既有庫存總市值", "NT$ 750.00")
        self.assert_metric(at, "目前總資產", "報價不完整")

    def test_pending_operation_disables_existing_position_forms(self):
        at = self.open_app()
        at.query_params["pending"] = "unknown-operation"
        at.run()
        self.assertTrue(self.button(at, "預覽既有庫存").disabled)


if __name__ == "__main__":
    unittest.main()
