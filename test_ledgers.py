"""Multiple-ledger isolation using only in-memory worksheets and mocked quotes."""
import json
import unittest
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal as D
from unittest.mock import patch

import app
import history_backfill
import ledger_books
import snapshot_job
import test_existing_holdings as fixtures


def store_with_two_books():
    sheet = fixtures.FakeSpreadsheet(events=fixtures.funded().events)
    main = app.SheetStore(sheet, baseline_id=42)
    book = ledger_books.create_book(sheet, "第二個帳本")
    return main, main.for_book(book), book


def post(store, action, **fields):
    event, _ = app.create_event(store.read(), action, **fields)
    checked, result = app.write_pending(store, event)
    assert result["status"] == "已入帳"
    return checked, event


class LedgerIsolationTests(unittest.TestCase):
    def setUp(self):
        self.main, self.second, self.book = store_with_two_books()

    def test_creation_keeps_original_data_and_new_book_is_empty(self):
        original = app.load_bundle("", self.main.read().events)
        actual = self.main.read()
        self.assertEqual(actual.state, original.state)
        self.assertEqual(actual.revision, original.revision)
        new = self.second.read()
        self.assertEqual(new.state["balance"], D("0"))
        self.assertEqual(app.holdings(new.state), {})
        self.assertEqual(new.state["existing_holdings"], {})
        self.assertEqual(new.events, [])
        self.assertNotEqual(new.revision, actual.revision)
        self.assertEqual(len(self.main.list_books()), 2)

    def test_deposits_us_fractions_and_existing_holdings_are_isolated(self):
        before = deepcopy(self.main.read().state)
        post(self.second, "deposit", amount="1000")
        new, trade = post(self.second, "buy", ticker="AAPL", shares="0.125", settled_twd="300")
        new, _ = post(self.second, "set_existing_holding", ticker="VOO", shares="0.25")
        self.assertEqual(new.state["balance"], D("700"))
        self.assertEqual(app.holdings(new.state), {"AAPL": D("0.125")})
        self.assertEqual(new.state["existing_holdings"], {"VOO": D("0.25")})
        self.assertEqual(trade["source_ledger_id"], self.book["id"])
        self.assertEqual(self.main.read().state, before)

    def test_pending_write_cannot_cross_ledgers_in_either_direction(self):
        for source, destination in ((self.main, self.second), (self.second, self.main)):
            event, _ = app.create_event(source.read(), "deposit", amount="10")
            before = deepcopy(destination.read().state)
            with self.assertRaises(app.LedgerError):
                app.write_pending(destination, event)
            self.assertEqual(destination.read().state, before)

    def test_unbound_old_pending_operation_only_belongs_to_main(self):
        legacy = app.load_bundle("", self.main.read().events)
        event, _ = app.create_event(legacy, "deposit", amount="10")
        with self.assertRaises(app.LedgerError):
            app.write_pending(self.second, event)
        self.assertEqual(app.write_pending(self.main, event)[0].state["balance"], D("949"))

    def test_copying_another_books_event_is_rejected_on_read(self):
        _, event = post(self.main, "deposit", amount="10")
        self.second.worksheet(app.EVENTS_NAME).append_row([app.canonical(event)])
        with self.assertRaises(app.LedgerError):
            self.second.read()

    def test_sheet_ids_survive_renaming_and_missing_sheet_is_not_empty(self):
        post(self.second, "deposit", amount="25")
        self.second.worksheet(app.EVENTS_NAME).title = "重新命名的交易分頁"
        self.assertEqual(self.second.read().state["balance"], D("25"))
        missing = self.second.worksheet(app.SNAPSHOTS_NAME)
        self.second.spreadsheet.sheets.remove(missing)
        with self.assertRaises(app.LedgerError):
            self.second.read_snapshots()

    def test_unconfigured_original_baseline_does_not_match_new_book(self):
        self.assertEqual(app.SheetStore(self.main.spreadsheet).read().state, self.main.read().state)

    def test_daily_snapshots_and_history_are_separate(self):
        day = (app.datetime.now(app.TZ).date() - timedelta(days=2)).isoformat()
        post(self.second, "deposit", amount="500", date=day)
        quote = {"2330.TW": {"price": D("100"), "at": day}}
        main = app.save_daily_snapshot(self.main, self.main.read(), quote)
        second = app.save_daily_snapshot(self.second, self.second.read(), {})
        self.assertEqual(main["snapshot"]["total_assets"], "1239.00")
        self.assertEqual(second["snapshot"]["total_assets"], "500.00")
        self.assertEqual(second["snapshot"]["ledger_id"], self.book["id"])
        result = history_backfill.backfill_missing_days(self.second, self.second.read())
        self.assertEqual(result["written"], 2)
        self.assertEqual(len(self.main.read_snapshots()), 1)
        self.assertEqual(len(self.second.read_snapshots()), 3)

    def test_named_ledger_backup_restores_to_separate_workbook(self):
        post(self.second, "deposit", amount="123")
        ledger = self.second.read()
        content = app.backup_document(ledger, spreadsheet_id=self.second.spreadsheet.id,
            baseline_worksheet_id=self.second.baseline_id, snapshot_rows=[])
        target = fixtures.FakeSpreadsheet()
        target.id = "empty-restore-target"
        restored = app.restore_to_empty_spreadsheet(self.second, target, content)
        self.assertEqual(restored.ledger_id, self.book["id"])
        self.assertEqual(restored.read().revision, ledger.revision)
        self.assertEqual(restored.read().state, ledger.state)
        self.assertEqual(app.SheetStore(target).read().state["balance"], D("0"))
        self.assertEqual(len(restored.list_books()), 2)

    def test_catalogue_tampering_is_detected(self):
        row = self.main.spreadsheet.worksheet(ledger_books.CATALOGUE).rows[0]
        value = json.loads(row[0])
        value["baseline_id"] = 42
        row[0] = app.canonical(value)
        with self.assertRaises(app.LedgerError):
            self.main.list_books()

    def test_snapshot_from_another_ledger_cannot_be_saved_or_displayed(self):
        snapshot = app.build_daily_snapshot(self.main.read(),
            {"2330.TW": {"price": D("100"), "at": "2026-01-01"}})
        with self.assertRaises(app.LedgerError):
            self.second.append_snapshot(snapshot)
        with self.assertRaises(app.LedgerError):
            self.second.append_snapshots([snapshot])
        self.second.worksheet(app.SNAPSHOTS_NAME).append_row([app.canonical(snapshot)])
        with self.assertRaises(app.LedgerError):
            self.second.read_snapshots()

    def test_catalogue_timeout_after_append_reconciles_and_retry_is_idempotent(self):
        original = fixtures.FakeWorksheet.append_row
        def append(sheet, row, **kwargs):
            original(sheet, row, **kwargs)
            if sheet.title == ledger_books.CATALOGUE:
                raise TimeoutError("offline")
        book_id = "a" * 32
        with patch.object(fixtures.FakeWorksheet, "append_row", new=append):
            book = ledger_books.create_book(self.main.spreadsheet, "第三帳本", operation_id=book_id)
        repeated = ledger_books.create_book(self.main.spreadsheet, "第三帳本", operation_id=book_id)
        self.assertEqual(book, repeated)
        self.assertEqual(len(self.main.list_books()), 3)

    def test_catalogue_timeout_before_append_keeps_same_operation_for_retry(self):
        original = fixtures.FakeWorksheet.append_row
        def append(sheet, row, **kwargs):
            if sheet.title == ledger_books.CATALOGUE:
                raise TimeoutError("offline")
            return original(sheet, row, **kwargs)
        book_id = "b" * 32
        with patch.object(fixtures.FakeWorksheet, "append_row", new=append):
            with self.assertRaises(app.WriteUncertain):
                ledger_books.create_book(self.main.spreadsheet, "第三帳本", operation_id=book_id)
        count = len(self.main.spreadsheet.sheets)
        ledger_books.create_book(self.main.spreadsheet, "第三帳本", operation_id=book_id)
        self.assertEqual(len(self.main.spreadsheet.sheets), count)
        self.assertEqual(len(self.main.list_books()), 3)

    def test_duplicate_or_invalid_name_never_creates_sheets(self):
        count = len(self.main.spreadsheet.sheets)
        for name in ("", " ", "第二個帳本", "x" * 41, "a\nb"):
            with self.subTest(name=name), self.assertRaises(app.LedgerError):
                ledger_books.create_book(self.main.spreadsheet, name)
        self.assertEqual(len(self.main.spreadsheet.sheets), count)

    def test_background_job_saves_each_ledger_and_reuses_quotes(self):
        post(self.second, "deposit", amount="500")
        result = snapshot_job.run_all_snapshot_jobs(self.main,
            quote_loader=lambda _: {"price": D("100"), "at": "2026-01-01"})
        self.assertEqual(result["errors"], [])
        self.assertEqual(len(result["results"]), 2)
        self.assertEqual(len(self.main.read_snapshots()), 1)
        self.assertEqual(len(self.second.read_snapshots()), 1)

    def test_background_failure_in_one_ledger_does_not_stop_the_other(self):
        post(self.second, "deposit", amount="500")
        result = snapshot_job.run_all_snapshot_jobs(self.main, quote_loader=lambda _: None)
        self.assertEqual(result["errors"], ["LedgerError"])
        self.assertEqual(len(result["results"]), 1)
        self.assertEqual(self.main.read_snapshots(), [])
        self.assertEqual(len(self.second.read_snapshots()), 1)


class LedgerUITests(unittest.TestCase):
    PASSWORD = "multiple-ledger-offline"
    setUp = fixtures.ExistingHoldingsUITests.setUp
    fake_ticker = fixtures.ExistingHoldingsUITests.fake_ticker
    open_app = fixtures.ExistingHoldingsUITests.open_app
    button = staticmethod(fixtures.ExistingHoldingsUITests.button)
    assert_metric = fixtures.ExistingHoldingsUITests.assert_metric

    def create_second(self, at):
        at.text_input(key="new_book_name").set_value("第二個帳本")
        self.button(at, "建立空白帳本").click().run()
        self.assertFalse(at.exception)
        return at.selectbox(key="ledger_picker").value

    def test_create_switch_and_reopen_keep_financial_totals_separate(self):
        at = self.open_app()
        self.assert_metric(at, "目前總資產", "NT$ 1,239.00")
        at.session_state["backup_bytes"] = b"old-ledger-backup"
        second_id = self.create_second(at)
        self.assertNotEqual(second_id, "main")
        self.assertNotIn("backup_bytes", at.session_state)
        self.assert_metric(at, "目前總資產", "NT$ 0.00")
        at.selectbox(key="entry_action").select("deposit").run()
        self.button(at, "預覽此筆操作").click().run()
        self.button(at, "確認寫入").click().run()
        self.assertFalse(at.exception)
        self.assert_metric(at, "目前總資產", "NT$ 1,000.00")
        at.selectbox(key="ledger_picker").select("main").run()
        self.assertFalse(at.exception)
        self.assert_metric(at, "目前總資產", "NT$ 1,239.00")
        at.selectbox(key="ledger_picker").select(second_id).run()
        self.assertFalse(at.exception)
        self.assert_metric(at, "目前總資產", "NT$ 1,000.00")

    def test_pending_transaction_disables_switch_and_creation(self):
        at = self.open_app()
        self.create_second(at)
        at.selectbox(key="entry_action").select("deposit").run()
        self.button(at, "預覽此筆操作").click().run()
        self.assertTrue(at.selectbox(key="ledger_picker").disabled)
        self.assertTrue(self.button(at, "建立空白帳本").disabled)
        self.button(at, "取消此操作").click().run()
        self.assertFalse(at.selectbox(key="ledger_picker").disabled)

    def test_refresh_with_ledger_url_keeps_selected_book(self):
        at = self.open_app()
        second_id = self.create_second(at)
        at.run()
        self.assertFalse(at.exception)
        self.assertEqual(at.query_params["ledger"], [second_id])
        self.assertEqual(at.selectbox(key="ledger_picker").value, second_id)

    def test_duplicate_name_has_clear_error_and_no_extra_book(self):
        at = self.open_app()
        self.create_second(at)
        at.text_input(key="new_book_name").set_value("第二個帳本")
        self.button(at, "建立空白帳本").click().run()
        self.assertFalse(at.exception)
        self.assertTrue(any("同名帳本" in e.value for e in at.error))
        self.assertEqual(len(ledger_books.list_books(self.book)), 2)

    def test_uncertain_create_retries_the_same_id_without_duplicate_sheets(self):
        at = self.open_app()
        at.text_input(key="new_book_name").set_value("第二個帳本")
        original = fixtures.FakeWorksheet.append_row
        def append(sheet, row, **kwargs):
            if sheet.title == ledger_books.CATALOGUE:
                raise TimeoutError("offline")
            return original(sheet, row, **kwargs)
        with patch.object(fixtures.FakeWorksheet, "append_row", new=append):
            self.button(at, "建立空白帳本").click().run()
        self.assertFalse(at.exception)
        operation_id = at.session_state["create_book_pending"]["id"]
        count = len(self.book.sheets)
        self.button(at, "建立空白帳本").click().run()
        self.assertFalse(at.exception)
        self.assertEqual(at.selectbox(key="ledger_picker").value, operation_id)
        self.assertEqual(len(self.book.sheets), count)
        self.assertEqual(len(ledger_books.list_books(self.book)), 2)


if __name__ == "__main__":
    unittest.main()
