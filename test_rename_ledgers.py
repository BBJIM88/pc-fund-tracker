"""Rename metadata without changing ledger identity, events, or snapshots."""
import unittest
import json
from copy import deepcopy
from decimal import Decimal as D
from unittest.mock import patch

import app
import ledger_books
import test_existing_holdings as fixtures
from test_ledgers import post, store_with_two_books


class RenameLedgerTests(unittest.TestCase):
    def setUp(self):
        self.main, self.second, self.book = store_with_two_books()

    def test_named_book_rename_keeps_ids_events_revision_and_snapshots(self):
        before, _ = post(self.second, "deposit", amount="456")
        app.save_daily_snapshot(self.second, before, {})
        events = deepcopy(self.second.worksheet(app.EVENTS_NAME).rows)
        snapshots = self.second.read_snapshots()
        original_catalogue = deepcopy(self.main.spreadsheet.worksheet(ledger_books.CATALOGUE).rows)
        renamed = ledger_books.rename_book(self.main.spreadsheet, self.book["id"], "長期投資")
        for key in ("id", "baseline_id", "events_id", "snapshots_id"):
            self.assertEqual(renamed[key], self.book[key])
        self.assertEqual(renamed["name"], "長期投資")
        current = self.second.for_book(renamed)
        self.assertEqual(current.read().revision, before.revision)
        self.assertEqual(current.read().state, before.state)
        self.assertEqual(current.read().ledger_name, "長期投資")
        self.assertEqual(current.worksheet(app.EVENTS_NAME).rows, events)
        self.assertEqual(current.read_snapshots(), snapshots)
        self.assertEqual(self.main.spreadsheet.worksheet(ledger_books.CATALOGUE).rows[:1], original_catalogue)

    def test_original_book_can_be_renamed_without_replacing_its_baseline(self):
        before = self.main.read()
        baseline = deepcopy(self.main.spreadsheet.get_worksheet_by_id(42).rows)
        renamed = ledger_books.rename_book(self.main.spreadsheet, "main", "我的股票")
        self.assertEqual(renamed, {"id": "main", "name": "我的股票"})
        self.main.for_book(renamed)
        self.assertEqual(self.main.read().ledger_name, "我的股票")
        self.assertEqual(self.main.read().revision, before.revision)
        self.assertEqual(self.main.spreadsheet.get_worksheet_by_id(42).rows, baseline)
        self.assertEqual(app.SheetStore(self.main.spreadsheet).read().revision, before.revision)

    def test_same_name_is_a_noop_invalid_and_duplicate_names_are_rejected(self):
        rows = deepcopy(self.main.spreadsheet.worksheet(ledger_books.CATALOGUE).rows)
        self.assertEqual(ledger_books.rename_book(self.main.spreadsheet, self.book["id"], "第二個帳本"), self.book)
        for name in ("", " ", "x" * 41, "a\nb", "原本帳本"):
            with self.subTest(name=name), self.assertRaises(app.LedgerError):
                ledger_books.rename_book(self.main.spreadsheet, self.book["id"], name)
        self.assertEqual(self.main.spreadsheet.worksheet(ledger_books.CATALOGUE).rows, rows)

    def test_stale_name_revision_cannot_overwrite_a_newer_name(self):
        initial = ledger_books.book_revision(self.book)
        ledger_books.rename_book(self.main.spreadsheet, self.book["id"], "已更新")
        with self.assertRaises(app.LedgerError):
            ledger_books.rename_book(self.main.spreadsheet, self.book["id"], "過期操作",
                                     expected_revision=initial)
        self.assertEqual(self.main.list_books()[1]["name"], "已更新")

    def test_concurrent_rename_of_same_book_keeps_first_committed_name(self):
        original = fixtures.FakeWorksheet.append_row
        raced = []
        def append(sheet, row, **kwargs):
            value = json.loads(row[0])
            if sheet.title == ledger_books.CATALOGUE and value.get("name") == "較晚完成" and not raced:
                raced.append(True)
                ledger_books.rename_book(self.main.spreadsheet, self.book["id"], "先完成")
            return original(sheet, row, **kwargs)
        with patch.object(fixtures.FakeWorksheet, "append_row", new=append):
            with self.assertRaises(app.LedgerError):
                ledger_books.rename_book(self.main.spreadsheet, self.book["id"], "較晚完成")
        self.assertEqual(self.main.list_books()[1]["name"], "先完成")
        self.assertEqual(len(self.main.list_books()), 2)

    def test_concurrent_duplicate_names_do_not_break_the_catalogue(self):
        original = fixtures.FakeWorksheet.append_row
        raced = []
        def append(sheet, row, **kwargs):
            value = json.loads(row[0])
            if sheet.title == ledger_books.CATALOGUE and value.get("action") == "rename" and not raced:
                raced.append(True)
                ledger_books.rename_book(self.main.spreadsheet, "main", "共同名稱")
            return original(sheet, row, **kwargs)
        with patch.object(fixtures.FakeWorksheet, "append_row", new=append):
            with self.assertRaises(app.LedgerError):
                ledger_books.rename_book(self.main.spreadsheet, self.book["id"], "共同名稱")
        self.assertEqual([b["name"] for b in self.main.list_books()], ["共同名稱", "第二個帳本"])

    def test_timeout_after_append_confirms_without_a_second_append(self):
        original = fixtures.FakeWorksheet.append_row
        def append(sheet, row, **kwargs):
            original(sheet, row, **kwargs)
            if sheet.title == ledger_books.CATALOGUE:
                raise TimeoutError("offline")
        with patch.object(fixtures.FakeWorksheet, "append_row", new=append):
            renamed = ledger_books.rename_book(self.main.spreadsheet, self.book["id"], "投資帳本",
                                              operation_id="a" * 32)
        repeated = ledger_books.rename_book(self.main.spreadsheet, self.book["id"], "投資帳本",
                                            operation_id="a" * 32)
        self.assertEqual(repeated, renamed)
        self.assertEqual(len(self.main.spreadsheet.worksheet(ledger_books.CATALOGUE).rows), 2)

    def test_timeout_before_append_retries_the_same_operation(self):
        original = fixtures.FakeWorksheet.append_row
        def append(sheet, row, **kwargs):
            if sheet.title == ledger_books.CATALOGUE:
                raise TimeoutError("offline")
            return original(sheet, row, **kwargs)
        expected = ledger_books.book_revision(self.book)
        with patch.object(fixtures.FakeWorksheet, "append_row", new=append):
            with self.assertRaises(app.WriteUncertain):
                ledger_books.rename_book(self.main.spreadsheet, self.book["id"], "投資帳本",
                                          operation_id="b" * 32, expected_revision=expected)
        renamed = ledger_books.rename_book(self.main.spreadsheet, self.book["id"], "投資帳本",
                                          operation_id="b" * 32, expected_revision=expected)
        self.assertEqual(renamed["name"], "投資帳本")
        self.assertEqual(len(self.main.spreadsheet.worksheet(ledger_books.CATALOGUE).rows), 2)

    def test_duplicate_rename_rows_are_idempotent_and_changed_payload_is_rejected(self):
        ledger_books.rename_book(self.main.spreadsheet, self.book["id"], "投資帳本", operation_id="c" * 32)
        ws = self.main.spreadsheet.worksheet(ledger_books.CATALOGUE)
        ws.append_row(deepcopy(ws.rows[-1]))
        self.assertEqual(self.main.list_books()[1]["name"], "投資帳本")
        with self.assertRaises(app.LedgerError):
            ledger_books.rename_book(self.main.spreadsheet, self.book["id"], "另一個名稱",
                                      operation_id="c" * 32)

    def test_creation_retry_after_rename_uses_original_creation_name(self):
        ledger_books.rename_book(self.main.spreadsheet, self.book["id"], "投資帳本")
        repeated = ledger_books.create_book(self.main.spreadsheet, "第二個帳本", operation_id=self.book["id"])
        self.assertEqual(repeated["id"], self.book["id"])
        self.assertEqual(repeated["name"], "投資帳本")
        self.assertEqual(len(self.main.list_books()), 2)

    def test_original_name_is_preserved_in_backup_and_restore(self):
        before = self.main.read()
        renamed = ledger_books.rename_book(self.main.spreadsheet, "main", "主要投資")
        self.main.for_book(renamed)
        content = app.backup_document(self.main.read(), spreadsheet_id=self.main.spreadsheet.id,
                                      baseline_worksheet_id=self.main.baseline_id)
        target = fixtures.FakeSpreadsheet()
        target.id = "restored-test-book"
        restored = app.restore_to_empty_spreadsheet(self.main, target, content)
        self.assertEqual(restored.read().revision, before.revision)
        self.assertEqual(restored.ledger_name, "主要投資")
        self.assertEqual(restored.list_books()[0]["name"], "主要投資")


class RenameLedgerUITests(unittest.TestCase):
    PASSWORD = "rename-ledgers-offline"
    setUp = fixtures.ExistingHoldingsUITests.setUp
    fake_ticker = fixtures.ExistingHoldingsUITests.fake_ticker
    open_app = fixtures.ExistingHoldingsUITests.open_app
    button = staticmethod(fixtures.ExistingHoldingsUITests.button)
    assert_metric = fixtures.ExistingHoldingsUITests.assert_metric

    def rename(self, at, name):
        book_id = at.selectbox(key="ledger_picker").value
        at.text_input(key="rename_book_name_" + book_id).set_value(name)
        self.button(at, "儲存帳本名稱").click().run()
        self.assertFalse(at.exception)
        return book_id

    def test_rename_original_survives_reopen_and_preserves_the_current_trade_form(self):
        at = self.open_app()
        at.number_input(key="entry_buy_shares").set_value(12).run()
        events = deepcopy(self.book.worksheet(app.EVENTS_NAME).rows)
        snapshots = deepcopy(self.book.worksheet(app.SNAPSHOTS_NAME).rows)
        self.assertEqual(self.rename(at, "主要投資"), "main")
        self.assertEqual(at.selectbox(key="ledger_picker").value, "main")
        self.assertTrue(any(c.value == "目前帳本：主要投資" for c in at.caption))
        self.assert_metric(at, "目前總資產", "NT$ 1,239.00")
        self.assertEqual(at.number_input(key="entry_buy_shares").value, 12)
        self.assertEqual(self.book.worksheet(app.EVENTS_NAME).rows, events)
        self.assertEqual(self.book.worksheet(app.SNAPSHOTS_NAME).rows, snapshots)
        reopened = self.open_app()
        self.assertTrue(any(c.value == "目前帳本：主要投資" for c in reopened.caption))

    def test_rename_second_book_then_switch_back_has_independent_labels_and_data(self):
        at = self.open_app()
        at.text_input(key="new_book_name").set_value("第二個帳本")
        self.button(at, "建立空白帳本").click().run()
        book_id = self.rename(at, "長期投資")
        self.assertNotEqual(book_id, "main")
        self.assert_metric(at, "目前總資產", "NT$ 0.00")
        at.selectbox(key="ledger_picker").select("main").run()
        self.assertFalse(at.exception)
        self.assert_metric(at, "目前總資產", "NT$ 1,239.00")
        at.selectbox(key="ledger_picker").select(book_id).run()
        self.assertFalse(at.exception)
        self.assertTrue(any(c.value == "目前帳本：長期投資" for c in at.caption))

    def test_rename_is_disabled_during_a_pending_financial_operation(self):
        at = self.open_app()
        at.selectbox(key="entry_action").select("deposit").run()
        self.button(at, "預覽此筆操作").click().run()
        self.assertTrue(self.button(at, "儲存帳本名稱").disabled)

    def test_ui_retries_uncertain_rename_without_changing_ledger(self):
        at = self.open_app()
        at.text_input(key="rename_book_name_main").set_value("主要投資")
        original = fixtures.FakeWorksheet.append_row
        def append(sheet, row, **kwargs):
            if sheet.title == ledger_books.CATALOGUE:
                raise TimeoutError("offline")
            return original(sheet, row, **kwargs)
        with patch.object(fixtures.FakeWorksheet, "append_row", new=append):
            self.button(at, "儲存帳本名稱").click().run()
        operation = deepcopy(at.session_state["rename_book_pending"])
        self.button(at, "儲存帳本名稱").click().run()
        self.assertFalse(at.exception)
        self.assertNotIn("rename_book_pending", at.session_state)
        self.assertEqual(at.selectbox(key="ledger_picker").value, operation["book_id"])
        self.assertTrue(any(c.value == "目前帳本：主要投資" for c in at.caption))


if __name__ == "__main__":
    unittest.main()
