"""Offline UI coverage for automatic historical backfill and its retry controls."""
import unittest
from datetime import date
from decimal import Decimal as D
from unittest.mock import patch

import app
import test_existing_holdings as fixtures


class BackfillUITests(unittest.TestCase):
    PASSWORD = "backfill-offline-ui"
    fake_ticker = fixtures.ExistingHoldingsUITests.fake_ticker
    open_app = fixtures.ExistingHoldingsUITests.open_app
    button = staticmethod(fixtures.ExistingHoldingsUITests.button)

    def setUp(self):
        fixtures.ExistingHoldingsUITests.setUp(self)
        ledger = app.load_bundle("", [])
        ledger, _ = fixtures.advance(ledger, "deposit", amount="1000", date="2026-10-18")
        ledger, _ = fixtures.advance(ledger, "buy", ticker="2330.TW", shares=3,
                                      price="20", fee="1", date="2026-10-19")
        self.book.worksheet(app.EVENTS_NAME).rows = [[app.canonical(event)] for event in ledger.events]
        self.history_calls = []
        self.available = True

        def history(ticker, start, end):
            self.history_calls.append((ticker, start, end))
            return {"quotes": ({"2026-10-19": {"price": D("90"), "at": "2026-10-19", "source": "offline official"},
                                "2026-10-20": {"price": D("95"), "at": "2026-10-20", "source": "offline official"}}
                               if self.available else {}), "coverage": [(start, end)] if self.available else []}

        replacement = patch("historical_prices.load_history", new=history)
        replacement.start()
        self.addCleanup(replacement.stop)

    def rows(self):
        return app.SheetStore(self.book, baseline_id=42).read_snapshots()

    def test_login_fills_missing_days_and_distinguishes_sources(self):
        at = self.open_app()
        self.assertEqual(len(self.rows()), 4)
        ledger = app.SheetStore(self.book, baseline_id=42).read()
        snapshots, _ = app.merged_snapshots(ledger, self.rows())
        self.assertEqual(snapshots["2026-10-18"]["total_assets"], "1000.00")
        self.assertEqual(snapshots["2026-10-19"]["total_assets"], "1209.00")
        self.assertEqual(snapshots["2026-10-20"]["total_assets"], "1224.00")
        self.assertEqual(snapshots["2026-10-21"]["source"], "app")
        self.assertTrue(any("3 天「歷史補算」" in c.value for c in at.caption))
        self.assertTrue(any(e.label == "查看每日資料來源" for e in at.expander))

    def test_widget_rerun_and_manual_retry_do_not_duplicate_complete_days(self):
        at = self.open_app()
        before_calls = len(self.history_calls)
        at.selectbox(key="entry_action").select("withdrawal").run()
        self.button(at, "補算缺漏日期").click().run()
        self.assertFalse(at.exception)
        self.assertEqual(len(self.rows()), 4)
        self.assertEqual(len(self.history_calls), before_calls)

    def test_missing_history_warns_keeps_today_and_cash_only_day(self):
        self.available = False
        at = self.open_app()
        self.assertEqual(len(self.rows()), 2)
        self.assertTrue(any("2 天尚未補齊" in w.value for w in at.warning))
        self.assertTrue(any(m.label == "最新資產快照時間" and "2026/10/21" in m.value for m in at.metric))
        self.assertTrue(self.button(at, "補算缺漏日期"))

    def test_existing_observation_is_not_replaced_by_later_backfill(self):
        ledger = app.SheetStore(self.book, baseline_id=42).read()
        snapshot = app.build_daily_snapshot(ledger,
            {"2330.TW": {"price": D("80"), "at": "2026-10-19"}},
            recorded_at="2026-10-19T18:00:00+08:00")
        self.book.add_worksheet(app.SNAPSHOTS_NAME).append_row([app.canonical(snapshot)])
        at = self.open_app()
        self.assertFalse(at.exception)
        merged, _ = app.merged_snapshots(ledger, self.rows())
        self.assertEqual(merged["2026-10-19"]["id"], snapshot["id"])

    def test_pending_operation_disables_backfill_and_does_not_call_api_again(self):
        at = self.open_app()
        previous = len(self.history_calls)
        at.query_params["pending"] = "unknown-operation"
        at.run()
        self.assertFalse(at.exception)
        self.assertTrue(self.button(at, "補算缺漏日期").disabled)
        self.assertEqual(len(self.history_calls), previous)

    def test_partial_batch_timeout_can_be_reconciled_on_manual_retry(self):
        original = fixtures.FakeWorksheet.append_rows
        failed = []

        def partial(sheet, rows, **kwargs):
            if sheet.title == app.SNAPSHOTS_NAME and not failed:
                failed.append(True)
                original(sheet, rows[:1], **kwargs)
                raise TimeoutError("offline partial")
            return original(sheet, rows, **kwargs)

        with patch.object(fixtures.FakeWorksheet, "append_rows", new=partial):
            at = self.open_app()
            self.assertTrue(any("歷史補算暫未完成" in w.value for w in at.warning))
            self.assertEqual(len(self.rows()), 2)
            self.button(at, "補算缺漏日期").click().run()
        self.assertFalse(at.exception)
        self.assertEqual(len(self.rows()), 4)


if __name__ == "__main__":
    unittest.main()
