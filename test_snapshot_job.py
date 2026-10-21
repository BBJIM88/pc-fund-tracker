"""Headless snapshot runner tests; no credentials, network, or Streamlit required."""

import contextlib
import io
import json
import logging
import sys
import types
import unittest
from copy import deepcopy
from unittest.mock import Mock, patch

import app
import snapshot_job


class ConfigurationTests(unittest.TestCase):
    def environment(self, **extra):
        return {"GCP_KEY_JSON": json.dumps({"client_email": "fake@example.invalid"}),
                "SPREADSHEET_ID": "fake-sheet-id", **extra}

    def test_opens_exact_spreadsheet_id_and_passes_baseline_settings(self):
        client = Mock()
        gspread = types.SimpleNamespace(service_account_from_dict=Mock(return_value=client))
        with patch.dict(sys.modules, {"gspread": gspread}), patch.object(app, "SheetStore") as store:
            result = snapshot_job.store_from_environment(self.environment(
                BASELINE_WORKSHEET_ID="0", BASELINE_WORKSHEET_NAME="原始帳本"))
        self.assertIs(result, store.return_value)
        client.open_by_key.assert_called_once_with("fake-sheet-id")
        client.open.assert_not_called()
        store.assert_called_once_with(client.open_by_key.return_value,
                                      baseline_id=0, baseline_title="原始帳本", client=client)
        self.assertEqual(gspread.service_account_from_dict.call_args.kwargs["scopes"],
                         ["https://www.googleapis.com/auth/spreadsheets"])

    def test_optional_baseline_is_none(self):
        client = Mock()
        gspread = types.SimpleNamespace(service_account_from_dict=Mock(return_value=client))
        with patch.dict(sys.modules, {"gspread": gspread}), patch.object(app, "SheetStore") as store:
            snapshot_job.store_from_environment(self.environment())
        self.assertIsNone(store.call_args.kwargs["baseline_id"])
        self.assertIsNone(store.call_args.kwargs["baseline_title"])

    def test_requires_both_secrets_and_rejects_title_or_url(self):
        for values in ({}, self.environment(GCP_KEY_JSON=""), self.environment(SPREADSHEET_ID=""),
                       self.environment(SPREADSHEET_ID="My Spreadsheet"),
                       self.environment(SPREADSHEET_ID="https://docs.google.com/spreadsheets/d/id/edit")):
            with self.subTest(values=values), self.assertRaises(snapshot_job.ConfigurationError):
                snapshot_job.store_from_environment(values)

    def test_rejects_malformed_json_and_file_paths(self):
        for key_json in ("gcp_key.json", "not json", "[]", "{}", "null", '"key"'):
            with self.subTest(key_json=key_json), self.assertRaises(snapshot_job.ConfigurationError):
                snapshot_job.store_from_environment(self.environment(GCP_KEY_JSON=key_json))

    def test_rejects_invalid_worksheet_id(self):
        for worksheet_id in ("-1", "1.2", "true", "baseline"):
            with self.subTest(worksheet_id=worksheet_id), self.assertRaises(snapshot_job.ConfigurationError):
                snapshot_job.store_from_environment(self.environment(BASELINE_WORKSHEET_ID=worksheet_id))


class RunnerTests(unittest.TestCase):
    def ledger(self, ticker="0050.TW"):
        state = {"buys": [{"ticker": ticker, "remaining": 10}],
                 "existing_holdings": {"2330.TW": 100}}
        return types.SimpleNamespace(state=state)

    def result(self, written=True):
        return {"snapshot": {"day": "2026-10-21"}, "written": written,
                "rows": [], "reason": "test"}

    def test_prices_only_original_holdings_and_uses_shared_save(self):
        ledger = self.ledger()
        store = Mock()
        store.read.return_value = ledger
        quote = {"price": "123.45", "at": "2026-10-21"}
        quote_loader = Mock(return_value=quote)
        result = self.result()
        with patch.object(app, "save_daily_snapshot", return_value=result, create=True) as save:
            self.assertIs(snapshot_job.run_snapshot_job(store, quote_loader=quote_loader), result)
        quote_loader.assert_called_once_with("0050.TW")
        save.assert_called_once_with(store, ledger, {"0050.TW": quote}, source="github_actions")
        store.append.assert_not_called()
        store.ensure_marker.assert_not_called()

    def test_cash_only_account_does_not_fetch_quotes(self):
        store = Mock()
        ledger = types.SimpleNamespace(state={"buys": [], "existing_holdings": {"2330.TW": 100}})
        store.read.return_value = ledger
        quote_loader = Mock()
        with patch.object(app, "save_daily_snapshot", return_value=self.result(), create=True) as save:
            snapshot_job.run_snapshot_job(store, quote_loader=quote_loader)
        quote_loader.assert_not_called()
        save.assert_called_once_with(store, ledger, {}, source="github_actions")

    def test_missing_quote_does_not_call_save(self):
        store = Mock()
        store.read.return_value = self.ledger()
        with patch.object(app, "save_daily_snapshot", create=True) as save:
            with self.assertRaises(app.LedgerError):
                snapshot_job.run_snapshot_job(store, quote_loader=Mock(return_value=None))
        save.assert_not_called()
        self.assertEqual(store.read.call_count, 1)

    def test_conflict_rereads_and_reprices_latest_holdings_once(self):
        first, second = self.ledger(), self.ledger("006208.TW")
        store = Mock()
        store.read.side_effect = [first, second]
        first_quote, second_quote = {"price": 1}, {"price": 2}
        quote_loader = Mock(side_effect=[first_quote, second_quote])
        result = self.result()
        with patch.object(app, "save_daily_snapshot", side_effect=[app.Conflict("changed"), result],
                          create=True) as save:
            self.assertIs(snapshot_job.run_snapshot_job(store, quote_loader=quote_loader), result)
        self.assertEqual(store.read.call_count, 2)
        self.assertEqual([call.args[0] for call in quote_loader.call_args_list], ["0050.TW", "006208.TW"])
        self.assertEqual(save.call_args.args, (store, second, {"006208.TW": second_quote}))

    def test_second_conflict_stops(self):
        store = Mock()
        store.read.return_value = self.ledger()
        with patch.object(app, "save_daily_snapshot", side_effect=app.Conflict("changed"), create=True) as save:
            with self.assertRaises(app.Conflict):
                snapshot_job.run_snapshot_job(store, quote_loader=Mock(return_value={"price": 1}))
        self.assertEqual(store.read.call_count, 2)
        self.assertEqual(save.call_count, 2)

    def test_uncertain_write_is_not_blindly_retried(self):
        store = Mock()
        store.read.return_value = self.ledger()
        with patch.object(app, "save_daily_snapshot", side_effect=app.WriteUncertain("timeout"),
                          create=True) as save:
            with self.assertRaises(app.WriteUncertain):
                snapshot_job.run_snapshot_job(store, quote_loader=Mock(return_value={"price": 1}))
        self.assertEqual(store.read.call_count, 1)
        self.assertEqual(save.call_count, 1)

    def test_main_success_and_unchanged_output(self):
        for written, expected in ((True, "written"), (False, "unchanged")):
            stdout, stderr = io.StringIO(), io.StringIO()
            with patch.object(snapshot_job, "store_from_environment"), \
                    patch.object(snapshot_job, "run_snapshot_job", return_value=self.result(written)), \
                    contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                self.assertEqual(snapshot_job.main(), 0)
            self.assertEqual(stdout.getvalue(), f"2026-10-21 {expected}\n")
            self.assertEqual(stderr.getvalue(), "")

    def test_main_suppresses_service_messages_and_only_logs_error_class(self):
        secret = "DO-NOT-LOG-credential-account-balance"

        def noisy_service():
            print(secret)
            print(secret, file=sys.stderr)
            logging.error(secret)
            raise RuntimeError(secret)

        stdout, stderr = io.StringIO(), io.StringIO()
        previous_disable = logging.root.manager.disable
        with patch.object(snapshot_job, "store_from_environment", side_effect=noisy_service), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            self.assertEqual(snapshot_job.main(), 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "Snapshot job failed (RuntimeError).\n")
        self.assertEqual(logging.root.manager.disable, previous_disable)

    def test_invalid_configuration_returns_nonzero_without_raw_value(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.dict("os.environ", {"GCP_KEY_JSON": "sensitive-not-json", "SPREADSHEET_ID": "fake-id"},
                        clear=True), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            self.assertEqual(snapshot_job.main(), 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "Snapshot job failed (ConfigurationError).\n")


class LocalSnapshotStore:
    """Real ledger and serialized snapshot rows, with no external storage."""

    def __init__(self, ledger):
        self.ledger = ledger
        self.rows = []
        self.read_count = 0

    def read(self):
        self.read_count += 1
        return self.ledger

    def read_snapshots(self):
        return deepcopy(self.rows)

    def append_snapshot(self, snapshot):
        self.rows.append([app.canonical(snapshot)])


class RunnerIntegrationTests(unittest.TestCase):
    def setUp(self):
        ledger = app.load_bundle("", [])
        _, ledger = app.create_event(ledger, "deposit", amount="10000", date="2026-10-01")
        _, ledger = app.create_event(ledger, "buy", ticker="0050.TW", shares=10,
                                     price="100", fee="0", date="2026-10-02")
        _, ledger = app.create_event(ledger, "set_existing_holding", ticker="2330.TW", shares=100)
        self.store = LocalSnapshotStore(ledger)
        self.quote = {"price": "120", "at": "2026-10-21", "source": "test close"}
        real_snapshot_datetime = app.snapshot_datetime
        frozen_clock = patch.object(app, "snapshot_datetime", side_effect=lambda value=None:
                                    real_snapshot_datetime("2026-10-21T18:17:00+08:00"
                                                           if value is None else value))
        frozen_clock.start()
        self.addCleanup(frozen_clock.stop)

    def test_real_shared_save_records_background_source_and_only_original_assets(self):
        quote_loader = Mock(return_value=self.quote)
        result = snapshot_job.run_snapshot_job(self.store, quote_loader=quote_loader)
        self.assertTrue(result["written"])
        self.assertEqual(len(self.store.rows), 1)
        saved = json.loads(self.store.rows[0][0])
        self.assertEqual(saved["source"], "github_actions")
        self.assertEqual(saved["day"], "2026-10-21")
        self.assertEqual(saved["holdings"], {"0050.TW": 10})
        self.assertEqual(set(saved["quotes"]), {"0050.TW"})
        self.assertEqual(saved["total_assets"], "10200.00")
        self.assertEqual(saved["checksum"], app.digest({k: v for k, v in saved.items() if k != "checksum"}))
        quote_loader.assert_called_once_with("0050.TW")
        self.assertGreaterEqual(self.store.read_count, 2)

    def test_real_shared_save_deduplicates_same_day_rerun(self):
        first = snapshot_job.run_snapshot_job(self.store, quote_loader=Mock(return_value=self.quote))
        second = snapshot_job.run_snapshot_job(self.store, quote_loader=Mock(return_value=self.quote))
        self.assertTrue(first["written"])
        self.assertFalse(second["written"])
        self.assertEqual(first["snapshot"]["id"], second["snapshot"]["id"])
        self.assertEqual(len(self.store.rows), 1)

    def test_missing_or_incomplete_original_quote_never_replaces_saved_snapshot(self):
        snapshot_job.run_snapshot_job(self.store, quote_loader=Mock(return_value=self.quote))
        original_rows = deepcopy(self.store.rows)
        # None is rejected by the runner; incomplete quotes reach the real shared validator.
        for unavailable in (None, {"price": "120"}):
            with self.subTest(quote=unavailable), self.assertRaises(app.LedgerError):
                snapshot_job.run_snapshot_job(self.store, quote_loader=Mock(return_value=unavailable))
            self.assertEqual(self.store.rows, original_rows)


if __name__ == "__main__":
    unittest.main()
