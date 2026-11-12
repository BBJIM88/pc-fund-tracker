"""Reproduce an app rerun with a previous release still imported as app."""

import sys
import unittest
from datetime import timedelta
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

import app
import history_backfill
import test_existing_holdings as fixtures


class SnapshotReloadTests(unittest.TestCase):
    PASSWORD = "snapshot-reload-offline"
    setUp = fixtures.ExistingHoldingsUITests.setUp
    open_app = fixtures.ExistingHoldingsUITests.open_app
    button = staticmethod(fixtures.ExistingHoldingsUITests.button)
    fake_ticker = fixtures.ExistingHoldingsUITests.fake_ticker

    def test_streamlit_rerun_keeps_pre_us_snapshot_with_cached_old_app_module(self):
        today = app.datetime.now(app.TZ).date()
        before_us = today - timedelta(days=2)
        prior, _ = fixtures.advance(app.load_bundle("", []), "deposit", date=before_us.isoformat(), amount="1000")
        snapshot = history_backfill.build_historical_snapshot(
            prior, prior.state, before_us, {}, recorded_at=today.isoformat() + "T00:00:00+08:00")
        newer, _ = fixtures.advance(prior, "buy", date=(today - timedelta(days=1)).isoformat(),
                                    ticker="AAPL", shares="0.125", settled_twd="300")
        self.book.worksheet(app.EVENTS_NAME).rows = [[app.canonical(event)] for event in newer.events]
        snapshots = self.book.add_worksheet(app.SNAPSHOTS_NAME)
        snapshots.append_row([app.canonical(snapshot)])
        old_module = ModuleType("app")
        old_module.APP_VERSION = "2.3.0"
        # The entry script and helpers are refreshed, but the separately
        # imported app module is still in the server's module cache.
        with patch.dict(sys.modules, {"app": old_module}), \
             patch("us_market.latest_quote", return_value=None), \
             patch("us_market.load_history", return_value={"quotes": {}, "coverage": []}):
            sys.modules.pop("historical_ledger", None)
            sys.modules.pop("history_backfill", None)
            at = self.open_app()
        self.assertFalse(any("快照讀取失敗" in error.value for error in at.error))
        self.assertTrue(any(caption.value == "最新資料日期：" + before_us.isoformat() for caption in at.caption))
        self.assertTrue(any(element.label == "查看每日資料來源" for element in at.expander))
        self.assertEqual(snapshots.rows, [[app.canonical(snapshot)]])

    def test_failed_read_is_not_shown_as_missing_data_or_bad_permissions(self):
        ws = self.book.add_worksheet(app.SNAPSHOTS_NAME)
        with patch.object(ws, "get_all_values", side_effect=AttributeError("private test value", name="missing")):
            at = self.open_app()
        errors = [element.value for element in at.error]
        self.assertTrue(any("快照讀取失敗" in message for message in errors))
        self.assertFalse(any("Secrets" in message or "private test value" in message for message in errors))
        self.assertTrue(any(metric.label == "最新資產快照時間" and metric.value == "暫時無法確認"
                            for metric in at.metric))


class SessionReleaseTests(unittest.TestCase):
    def test_connection_cached_by_prior_release_is_rebuilt(self):
        settings = {"credentials": {}, "spreadsheet_id": "test-book", "baseline_id": 42, "baseline_title": None}
        old_store = object()
        new_store = object()
        old_key = app.digest({**settings, "app_version": "2.4.0"})
        ui = SimpleNamespace(secrets={"GCP_KEY_JSON": "{}", "SPREADSHEET_ID": "test-book", "BASELINE_WORKSHEET_ID": 42},
                             session_state={"_sheet_connection": (old_key, old_store), "_display_cache": {"snapshots": []}})
        with patch.object(app, "connect_store", return_value=new_store) as connect:
            self.assertIs(app.session_store(ui), new_store)
            self.assertIs(app.session_store(ui), new_store)
        connect.assert_called_once_with(ui)
        self.assertNotIn("_display_cache", ui.session_state)


if __name__ == "__main__":
    unittest.main()
