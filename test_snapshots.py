"""Daily snapshot regression tests; all Sheets, quotes and credentials are fakes."""
import unittest
from copy import deepcopy
from decimal import Decimal as D
from unittest.mock import patch

import app
import test_existing_holdings as fixtures


class SnapshotStore:
    def __init__(self, ledger=None, rows=None, mode="normal"):
        self.ledger = ledger or fixtures.funded()
        self.rows = deepcopy(rows or [])
        self.mode = mode
        self.appends = 0
        self.reads = 0

    def read(self):
        self.reads += 1
        return self.ledger

    def read_snapshots(self):
        return deepcopy(self.rows)

    def append_snapshot(self, snapshot):
        self.appends += 1
        if self.mode not in {"timeout_before", "missing_after"}:
            self.rows.append([app.canonical(snapshot)])
        if self.mode.startswith("timeout"):
            raise TimeoutError("offline fake")


def quotes(price="100", at="2026-10-21"):
    return {"2330.TW": {"price": D(price), "at": at, "source": "offline close"}}


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.ledger = fixtures.funded()
        self.store = SnapshotStore(self.ledger)
        self.at = "2026-10-21T18:17:00+08:00"

    def save(self, **kwargs):
        return app.save_daily_snapshot(self.store, self.ledger, quotes(), recorded_at=self.at, **kwargs)

    def test_snapshot_records_cash_positions_close_date_and_reconciled_assets(self):
        before = deepcopy(self.ledger.state)
        result = self.save()
        snapshot = result["snapshot"]
        self.assertTrue(result["written"])
        self.assertEqual(snapshot["cash_balance"], "939.00")
        self.assertEqual(snapshot["market_value"], "300.00")
        self.assertEqual(snapshot["total_assets"], "1239.00")
        self.assertEqual(snapshot["net_principal"], "1000.00")
        self.assertEqual(snapshot["profit"], "239.00")
        self.assertEqual(snapshot["holdings"], {"2330.TW": 3})
        self.assertEqual(snapshot["quotes"]["2330.TW"]["at"], "2026-10-21")
        self.assertEqual(snapshot["ledger_revision"], self.ledger.revision)
        self.assertEqual(self.ledger.state, before)
        self.assertEqual(app.parse_snapshots(self.store.rows)[1], [])

    def test_existing_holdings_never_enter_snapshot_or_require_a_quote(self):
        ledger, _ = fixtures.advance(self.ledger, "set_existing_holding", ticker="0050.TW", shares=100)
        snapshot = app.build_daily_snapshot(ledger, quotes(), recorded_at=self.at)
        self.assertEqual(snapshot["total_assets"], "1239.00")
        self.assertEqual(snapshot["holdings"], {"2330.TW": 3})
        self.assertNotIn("0050.TW", snapshot["quotes"])

    def test_duplicate_same_day_skips_write_and_cached_duplicate_skips_remote_reads(self):
        first = self.save()
        second = self.save(snapshot_rows=first["rows"])
        self.assertFalse(second["written"])
        self.assertEqual(self.store.appends, 1)
        self.assertEqual(self.store.reads, 1)
        self.assertEqual(second["snapshot"]["id"], first["snapshot"]["id"])

    def test_fresh_rows_are_checked_even_if_display_cache_is_empty(self):
        self.save()
        result = self.save(snapshot_rows=[])
        self.assertFalse(result["written"])
        self.assertEqual(self.store.appends, 1)

    def test_return_to_an_older_value_still_saves_latest_state(self):
        self.save()
        app.save_daily_snapshot(self.store, self.ledger, quotes("110"),
                                recorded_at="2026-10-21T18:18:00+08:00")
        result = app.save_daily_snapshot(self.store, self.ledger, quotes(),
                                         recorded_at="2026-10-21T18:19:00+08:00")
        self.assertTrue(result["written"])
        self.assertEqual(self.store.appends, 3)
        merged, _ = app.merged_snapshots(self.ledger, self.store.rows)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged["2026-10-21"]["total_assets"], "1239.00")

    def test_new_day_uses_taipei_date_and_does_not_backfill_missing_days(self):
        self.save()
        result = app.save_daily_snapshot(self.store, self.ledger, quotes(),
                                         recorded_at="2026-10-24T16:01:00Z")
        self.assertEqual(result["snapshot"]["day"], "2026-10-25")
        self.assertEqual(result["snapshot"]["quotes"]["2330.TW"]["at"], "2026-10-21")
        merged, _ = app.merged_snapshots(self.ledger, self.store.rows)
        self.assertEqual(set(merged), {"2026-10-21", "2026-10-25"})

    def test_same_day_transaction_changes_snapshot_but_chart_still_has_one_point(self):
        self.save()
        ledger, _ = fixtures.advance(self.ledger, "deposit", amount="100", date="2026-10-21")
        self.store.ledger = ledger
        result = app.save_daily_snapshot(self.store, ledger, quotes(),
                                         recorded_at="2026-10-21T18:18:00+08:00")
        self.assertTrue(result["written"])
        merged, _ = app.merged_snapshots(ledger, self.store.rows)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged["2026-10-21"]["total_assets"], "1339.00")

    def test_background_success_is_recorded_even_if_app_saved_same_values(self):
        self.save()
        result = self.save(source="github_actions")
        self.assertTrue(result["written"])
        self.assertEqual(result["snapshot"]["source"], "github_actions")
        self.assertFalse(self.save(source="github_actions")["written"])
        self.assertFalse(self.save(source="app")["written"])
        self.assertEqual(self.store.appends, 2)

    def test_manual_save_can_force_a_new_record(self):
        self.save()
        self.assertTrue(self.save(source="manual", force=True)["written"])
        self.assertEqual(self.store.appends, 2)

    def test_cash_only_ledger_does_not_need_market_quotes(self):
        ledger, _ = fixtures.advance(app.load_bundle("", []), "deposit", amount="500", date="2026-10-21")
        snapshot = app.build_daily_snapshot(ledger, {}, recorded_at=self.at)
        self.assertEqual(snapshot["total_assets"], "500.00")
        self.assertEqual(snapshot["market_value"], "0.00")
        self.assertEqual(snapshot["quotes"], {})

    def test_bad_or_missing_quote_preserves_all_existing_rows(self):
        self.save()
        before = deepcopy(self.store.rows)
        for value in (None, {}, {"price": D("0"), "at": "2026-10-21"},
                      {"price": "NaN", "at": "2026-10-21"},
                      {"price": D("100"), "at": "2026-10-22"},
                      {"price": D("100"), "at": "bad date"}):
            with self.subTest(value=value), self.assertRaises(app.LedgerError):
                app.save_daily_snapshot(self.store, self.ledger, {"2330.TW": value}, recorded_at=self.at)
            self.assertEqual(self.store.rows, before)
        self.assertEqual(self.store.appends, 1)

    def test_numeric_string_quote_is_validated_before_valuation(self):
        snapshot = app.build_daily_snapshot(self.ledger, {"2330.TW": {"price": "100", "at": "2026-10-21"}},
                                            recorded_at=self.at)
        self.assertEqual(snapshot["total_assets"], "1239.00")

    def test_concurrent_financial_write_prevents_stale_snapshot_append(self):
        self.store.ledger, _ = fixtures.advance(self.ledger, "deposit", amount="100", date="2026-10-21")
        with self.assertRaises(app.Conflict):
            self.save()
        self.assertEqual(self.store.appends, 0)

    def test_timeout_after_store_is_confirmed_without_repeating_append(self):
        self.store.mode = "timeout_after"
        result = self.save()
        self.assertTrue(result["written"])
        self.assertEqual(result["reason"], "confirmed")
        self.assertEqual(self.store.appends, 1)

    def test_timeout_before_store_or_missing_confirmation_is_not_success(self):
        for mode in ("timeout_before", "missing_after"):
            with self.subTest(mode=mode):
                self.store.mode = mode
                with self.assertRaises(app.WriteUncertain):
                    self.save()
                self.assertEqual(self.store.rows, [])

    def test_chart_chooses_capture_time_when_appends_arrive_out_of_order(self):
        latest = app.build_daily_snapshot(self.ledger, quotes("110"), recorded_at=self.at)
        earlier = app.build_daily_snapshot(self.ledger, quotes(), recorded_at="2026-10-21T17:17:00+08:00")
        rows = [[app.canonical(s)] for s in (latest, earlier)]
        merged, _ = app.merged_snapshots(self.ledger, rows)
        self.assertEqual(merged["2026-10-21"]["id"], latest["id"])

    def test_checksum_failures_and_malformed_rows_are_ignored(self):
        snapshot = app.build_daily_snapshot(self.ledger, quotes(), recorded_at=self.at)
        invalid = {**snapshot, "total_assets": "999999"}
        rows = [[app.canonical(invalid)], ["[]"], ["not json"], [app.canonical(snapshot)]]
        merged, errors = app.merged_snapshots(self.ledger, rows)
        self.assertEqual(errors, [1, 2, 3])
        self.assertEqual(merged["2026-10-21"]["total_assets"], "1239.00")

    def test_legacy_snapshot_survives_and_modern_row_supersedes_same_day(self):
        ledger = deepcopy(self.ledger)
        ledger.state["snapshots"] = {"2026-10-05": {"total_assets": "3500", "target_price": "8000"},
                                      "2026-10-21": {"total_assets": "1", "target_price": "8000"}}
        rows = self.save()["rows"]
        merged, errors = app.merged_snapshots(ledger, rows)
        self.assertFalse(errors)
        self.assertTrue(merged["2026-10-05"]["legacy"])
        self.assertEqual(merged["2026-10-21"]["total_assets"], "1239.00")

    def test_today_app_save_does_not_hide_stale_background_job(self):
        old = app.build_daily_snapshot(self.ledger, quotes(at="2026-10-18"), source="github_actions",
                                        recorded_at="2026-10-18T18:17:00+08:00")
        self.store.rows = [[app.canonical(old)]]
        self.save()
        health = app.snapshot_health(self.ledger, self.store.rows, at=self.at)
        self.assertEqual(health["age_days"], 0)
        self.assertEqual(health["background_age_days"], 3)


class SnapshotUITests(unittest.TestCase):
    PASSWORD = "snapshot-offline-test"
    setUp = fixtures.ExistingHoldingsUITests.setUp
    fake_ticker = fixtures.ExistingHoldingsUITests.fake_ticker
    open_app = fixtures.ExistingHoldingsUITests.open_app
    button = staticmethod(fixtures.ExistingHoldingsUITests.button)

    def rows(self):
        return app.SheetStore(self.book, baseline_id=42).read_snapshots()

    def test_login_saves_today_and_widget_rerun_does_not_add_duplicate(self):
        at = self.open_app()
        self.assertEqual(len(self.rows()), 1)
        self.assertTrue(any(m.label == "最新資產快照時間" for m in at.metric))
        at.selectbox(key="entry_action").select("withdrawal").run()
        self.assertFalse(at.exception)
        self.assertEqual(len(self.rows()), 1)

    def test_confirmed_operation_refreshes_today_and_preview_does_not_save(self):
        at = self.open_app()
        at.selectbox(key="entry_action").select("deposit").run()
        self.button(at, "預覽此筆操作").click().run()
        self.assertEqual(len(self.rows()), 1)
        self.button(at, "確認寫入").click().run()
        self.assertFalse(at.exception)
        ledger = app.SheetStore(self.book, baseline_id=42).read()
        merged, _ = app.merged_snapshots(ledger, self.rows())
        self.assertEqual(len(self.rows()), 2)
        self.assertEqual(len(merged), 1)
        latest = max(merged.values(), key=app.snapshot_order)
        self.assertEqual(latest["total_assets"], "2239.00")

    def test_missing_quote_preserves_previous_snapshot_and_disables_manual_save(self):
        old = app.build_daily_snapshot(fixtures.funded(), quotes(at="2026-10-18"),
                                        recorded_at="2026-10-18T18:17:00+08:00")
        self.book.add_worksheet(app.SNAPSHOTS_NAME).append_row([app.canonical(old)])
        self.prices["2330.TW"] = None
        at = self.open_app()
        self.assertEqual(len(self.rows()), 1)
        self.assertTrue(self.button(at, "保存今日快照").disabled)
        self.assertTrue(any("報價不完整，暫不保存" in caption.value for caption in at.caption))

    def test_stale_warning_remains_visible_after_app_saves_today(self):
        old = app.build_daily_snapshot(fixtures.funded(), quotes(at="2026-10-18"), source="github_actions",
                                        recorded_at="2026-10-18T18:17:00+08:00")
        self.book.add_worksheet(app.SNAPSHOTS_NAME).append_row([app.canonical(old)])
        at = self.open_app()
        self.assertEqual(len(self.rows()), 2)
        self.assertTrue(any("背景排程已" in w.value for w in at.warning))
        self.assertTrue(any("中間缺漏日期不會自動倒填" in w.value for w in at.warning))
        at.run()
        self.assertTrue(any("背景排程已" in w.value for w in at.warning))

    def test_manual_save_adds_a_confirmed_record(self):
        at = self.open_app()
        self.button(at, "保存今日快照").click().run()
        self.assertFalse(at.exception)
        self.assertEqual(len(self.rows()), 2)
        self.assertTrue(any("今日快照已保存" in message.value for message in at.success))

    def test_unknown_pending_operation_blocks_automatic_and_manual_snapshots(self):
        at = self.open_app()
        self.book.worksheet(app.SNAPSHOTS_NAME).rows.clear()
        at.session_state["_display_cache"].pop("snapshots", None)
        at.query_params["pending"] = "unknown-operation"
        at.run()
        self.assertFalse(at.exception)
        self.assertEqual(self.rows(), [])
        self.assertTrue(self.button(at, "保存今日快照").disabled)

    def test_failed_write_warns_without_crashing_or_repeating_on_widget_rerun(self):
        calls = []
        original = fixtures.FakeWorksheet.append_row

        def append(sheet, row, **kwargs):
            if sheet.title == app.SNAPSHOTS_NAME:
                calls.append(row)
                raise TimeoutError("offline")
            return original(sheet, row, **kwargs)

        with patch.object(fixtures.FakeWorksheet, "append_row", new=append):
            at = self.open_app()
            at.selectbox(key="entry_action").select("withdrawal").run()
            self.assertFalse(at.exception)
            self.assertEqual(len(calls), 1)
            self.assertTrue(any("今日快照尚未自動保存" in w.value for w in at.warning))
            self.assertEqual(self.rows(), [])


if __name__ == "__main__":
    unittest.main()
