"""Offline integration tests for reconstruction, persistence and chart merging."""

import unittest
from copy import deepcopy
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal as D
from unittest.mock import Mock, patch

import app
import history_backfill as backfill
from historical_ledger import reconstruct_states


AT = "2026-10-21T18:17:00+08:00"


def advance(ledger, action, **fields):
    event, updated = app.create_event(ledger, action, **fields)
    return updated, event


def funded(day="2026-10-18"):
    return advance(app.load_bundle("", []), "deposit", amount="1000", date=day)[0]


def bought():
    return advance(funded(), "buy", ticker="2330.TW", shares=3,
                   price="100", fee="1", date="2026-10-19")[0]


def history(values=None, coverage=None):
    values = values or {}
    return {"quotes": {day: {"price": D(str(value)), "at": day, "source": "offline as-traded close"}
                       for day, value in values.items()},
            "coverage": coverage if coverage is not None else [(date(2026, 10, 1), date(2026, 10, 31))]}


class BatchStore:
    def __init__(self, ledger, rows=None, mode="normal"):
        self.ledger = ledger
        self.rows = deepcopy(rows or [])
        self.mode = mode
        self.batches = []
        self.snapshot_reads = 0

    def read(self):
        return self.ledger

    def read_snapshots(self):
        self.snapshot_reads += 1
        if self.mode == "confirmation_failure" and self.snapshot_reads >= 3:
            raise TimeoutError("offline read failure")
        return deepcopy(self.rows)

    def append_snapshots(self, snapshots):
        self.batches.append(deepcopy(snapshots))
        if self.mode in {"timeout_before", "missing_after"}:
            inserted = []
        elif self.mode == "timeout_partial":
            inserted = snapshots[:1]
        else:
            inserted = snapshots
        self.rows.extend([[app.canonical(snapshot)] for snapshot in inserted])
        if self.mode.startswith("timeout"):
            raise TimeoutError("offline write failure")


class BackfillTests(unittest.TestCase):
    def setUp(self):
        self.ledger = bought()
        self.store = BatchStore(self.ledger)
        self.loader = Mock(return_value=history({"2026-10-19": 110, "2026-10-20": 120}))

    def run_backfill(self, **kwargs):
        return backfill.backfill_missing_days(self.store, self.ledger,
                                              history_loader=self.loader, at=AT, **kwargs)

    def merged(self):
        return app.merged_snapshots(self.ledger, self.store.rows)[0]

    def observed(self, day, price="111"):
        state = reconstruct_states(self.ledger, day, day)[date.fromisoformat(day)]
        prices = {ticker: {"price": D(price), "at": day, "source": "offline observed"}
                  for ticker in app.holdings(state)}
        return app.build_daily_snapshot(replace(self.ledger, state=state), prices,
                                         recorded_at=day + "T18:00:00+08:00")

    def test_fills_missing_closed_days_with_their_cash_and_positions(self):
        self.ledger, _ = advance(self.ledger, "sell", ticker="2330.TW", shares=1,
                                 price="120", fee="1", tax="2", date="2026-10-20")
        self.ledger, _ = advance(self.ledger, "dividend", ticker="2330.TW", amount="20", date="2026-10-20")
        self.store.ledger = self.ledger
        result = self.run_backfill()
        self.assertEqual(result["written"], 3)
        self.assertEqual(result["unavailable"], 0)
        merged = self.merged()
        self.assertEqual(set(merged), {"2026-10-18", "2026-10-19", "2026-10-20"})
        self.assertNotIn("2026-10-21", merged)
        self.assertEqual(merged["2026-10-18"]["total_assets"], "1000.00")
        self.assertEqual(merged["2026-10-19"]["total_assets"], "1029.00")
        self.assertEqual(merged["2026-10-20"]["holdings"], {"2330.TW": 2})
        self.assertEqual(merged["2026-10-20"]["total_assets"], "1076.00")
        self.assertTrue(all(snapshot["source"] == "historical_backfill" for snapshot in merged.values()))

    def test_first_financial_operation_today_has_nothing_to_backfill(self):
        self.ledger = funded("2026-10-21")
        self.store.ledger = self.ledger
        result = self.run_backfill()
        self.assertEqual(result["written"], 0)
        self.assertEqual(self.store.rows, [])
        self.loader.assert_not_called()

    def test_completed_calendar_coverage_can_carry_last_close_over_weekend(self):
        self.ledger, _ = advance(funded("2026-10-15"), "buy", ticker="2330.TW", shares=3,
                                 price="100", fee="1", date="2026-10-16")
        self.store.ledger = self.ledger
        self.loader.return_value = history({"2026-10-16": 110, "2026-10-19": 115, "2026-10-20": 120})
        self.run_backfill()
        merged = self.merged()
        for day in ("2026-10-17", "2026-10-18"):
            self.assertEqual(merged[day]["total_assets"], "1029.00")
            self.assertEqual(merged[day]["quotes"]["2330.TW"]["at"], "2026-10-16")

    def test_failed_intervening_coverage_is_not_treated_as_a_holiday(self):
        self.ledger, _ = advance(funded("2026-09-29"), "buy", ticker="2330.TW", shares=3,
                                 price="100", fee="1", date="2026-09-30")
        self.store.ledger = self.ledger
        self.loader.return_value = history({"2026-09-30": 100, "2026-10-05": 110},
                                           [(date(2026, 9, 1), date(2026, 9, 30)),
                                            (date(2026, 10, 2), date(2026, 10, 20))])
        result = self.run_backfill()
        self.assertEqual(result["unavailable"], 4)
        merged = self.merged()
        self.assertIn("2026-09-30", merged)
        for day in ("2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04"):
            self.assertNotIn(day, merged)
        self.assertIn("2026-10-05", merged)

    def test_no_carry_forward_across_split_without_a_post_split_quote(self):
        self.ledger, _ = advance(self.ledger, "split", ticker="2330.TW", numerator=4,
                                 denominator=1, date="2026-10-20")
        self.store.ledger = self.ledger
        self.loader.return_value = history({"2026-10-19": 100})
        result = self.run_backfill()
        self.assertEqual(result["written"], 2)
        self.assertEqual(result["unavailable"], 1)
        self.assertNotIn("2026-10-20", self.merged())

    def test_original_close_and_historical_shares_reconcile_across_split(self):
        self.ledger, _ = advance(self.ledger, "split", ticker="2330.TW", numerator=4,
                                 denominator=1, date="2026-10-20")
        self.store.ledger = self.ledger
        self.loader.return_value = history({"2026-10-19": 100, "2026-10-20": 25})
        self.run_backfill()
        merged = self.merged()
        self.assertEqual(merged["2026-10-19"]["holdings"], {"2330.TW": 3})
        self.assertEqual(merged["2026-10-20"]["holdings"], {"2330.TW": 12})
        self.assertEqual(merged["2026-10-19"]["total_assets"], merged["2026-10-20"]["total_assets"])

    def test_actual_snapshot_and_legacy_snapshot_have_priority_over_reconstruction(self):
        actual = self.observed("2026-10-20")
        self.store.rows = [[app.canonical(actual)]]
        self.ledger.state["snapshots"]["2026-10-19"] = {"total_assets": "9999", "target_price": "5000"}
        result = self.run_backfill()
        self.assertEqual(result["written"], 1)
        self.loader.assert_not_called()
        merged = self.merged()
        self.assertTrue(merged["2026-10-19"]["legacy"])
        self.assertEqual(merged["2026-10-19"]["total_assets"], "9999")
        self.assertEqual(merged["2026-10-20"]["id"], actual["id"])
        state = reconstruct_states(self.ledger, "2026-10-19", "2026-10-19")[date(2026, 10, 19)]
        reconstructed = backfill.build_historical_snapshot(self.ledger, state, date(2026, 10, 19),
            {"2330.TW": {"price": D("110"), "at": "2026-10-19"}}, recorded_at=AT)
        self.store.rows.append([app.canonical(reconstructed)])
        self.assertTrue(self.merged()["2026-10-19"]["legacy"])

    def test_same_day_repeat_does_not_fetch_history_or_append_again(self):
        self.run_backfill()
        saved = deepcopy(self.store.rows)
        self.loader.reset_mock()
        result = self.run_backfill()
        self.assertEqual(result["written"], 0)
        self.assertEqual(self.store.rows, saved)
        self.assertEqual(len(self.store.batches), 1)
        self.loader.assert_not_called()

    def test_transaction_change_recalculates_only_affected_reconstructed_days(self):
        self.run_backfill()
        original = self.merged()
        self.ledger, _ = advance(self.ledger, "deposit", amount="50", date="2026-10-20")
        self.store.ledger = self.ledger
        self.loader.reset_mock()
        self.assertNotIn("2026-10-20", self.merged())
        result = self.run_backfill()
        merged = self.merged()
        self.assertEqual(result["written"], 1)
        for day in ("2026-10-18", "2026-10-19"):
            self.assertEqual(merged[day]["id"], original[day]["id"])
        self.assertNotEqual(merged["2026-10-20"]["id"], original["2026-10-20"]["id"])
        self.assertEqual(merged["2026-10-20"]["total_assets"], "1109.00")

    def test_existing_holdings_change_does_not_invalidate_historical_finances(self):
        self.run_backfill()
        self.ledger, _ = advance(self.ledger, "set_existing_holding", ticker="0050.TW",
                                 shares=100, date="2026-10-21")
        self.store.ledger = self.ledger
        self.loader.reset_mock()
        result = self.run_backfill()
        self.assertEqual(result["written"], 0)
        self.assertEqual(len(self.merged()), 3)
        self.loader.assert_not_called()

    def test_actual_snapshots_stay_unchanged_after_a_financial_correction(self):
        actual = self.observed("2026-10-19")
        self.store.rows.append([app.canonical(actual)])
        self.run_backfill()
        self.ledger, _ = advance(self.ledger, "deposit", amount="50", date="2026-10-19")
        self.store.ledger = self.ledger
        self.run_backfill()
        self.assertEqual(self.merged()["2026-10-19"]["id"], actual["id"])
        self.assertEqual(self.store.rows[0], [app.canonical(actual)])

    def test_cancelled_only_transaction_removes_old_reconstructions_from_chart(self):
        self.ledger = funded()
        self.store = BatchStore(self.ledger)
        self.run_backfill()
        saved = deepcopy(self.store.rows)
        deposit = self.ledger.records[0]["original"]
        self.ledger, _ = advance(self.ledger, "amend", target_id=deposit["id"], replacement=None, reason="Cancel")
        self.store.ledger = self.ledger
        self.loader.reset_mock()
        self.assertEqual(self.merged(), {})
        result = self.run_backfill()
        self.assertEqual(result["written"], 0)
        self.assertEqual(self.store.rows, saved)
        self.loader.assert_not_called()

    def test_first_transaction_redating_removes_pre_start_reconstructions(self):
        self.ledger = funded()
        self.store = BatchStore(self.ledger)
        self.run_backfill()
        deposit = self.ledger.records[0]["original"]
        self.ledger, _ = advance(self.ledger, "amend", target_id=deposit["id"],
                                 replacement={**deposit, "date": "2026-10-20"}, reason="Date correction")
        self.store.ledger = self.ledger
        self.assertEqual(set(self.merged()), {"2026-10-20"})
        self.assertEqual(self.run_backfill()["written"], 0)
        self.assertEqual(len(self.store.rows), 3)

    def test_unknown_goal_is_omitted_and_does_not_make_snapshot_invalid(self):
        baseline = app.default_data()
        baseline["cash_account"] = {"balance": 1000,
                                     "history": [{"date": "2026-10-18", "amount": 1000}]}
        baseline["target_hardware"] = {"current_target": 8000, "history": []}
        self.ledger = app.load_bundle(app.canonical(baseline), [])
        self.store = BatchStore(self.ledger)
        result = self.run_backfill()
        self.assertEqual(result["written"], 3)
        parsed, errors = app.parse_snapshots(self.store.rows)
        self.assertFalse(errors)
        self.assertTrue(all("target_price" not in snapshot for snapshot in parsed))
        self.assertEqual(len(self.merged()), 3)

    def test_timeout_after_full_batch_is_confirmed_without_retrying_append(self):
        self.store.mode = "timeout_after"
        self.assertEqual(self.run_backfill()["written"], 3)
        self.assertEqual(len(self.store.batches), 1)
        self.assertEqual(len(self.merged()), 3)

    def test_timeout_before_batch_or_missing_confirmation_is_not_reported_success(self):
        for mode in ("timeout_before", "missing_after"):
            with self.subTest(mode=mode):
                self.store = BatchStore(self.ledger, mode=mode)
                with self.assertRaises(app.WriteUncertain):
                    self.run_backfill()
                self.assertEqual(self.store.rows, [])
                self.assertEqual(len(self.store.batches), 1)

    def test_partial_batch_timeout_is_uncertain_then_retry_fills_only_missing_rows(self):
        self.store.mode = "timeout_partial"
        with self.assertRaises(app.WriteUncertain):
            self.run_backfill()
        self.assertEqual(len(self.store.rows), 1)
        self.store.mode = "normal"
        result = self.run_backfill()
        self.assertEqual(result["written"], 2)
        self.assertEqual(len(self.store.rows), 3)
        self.assertEqual(len(self.merged()), 3)

    def test_confirmation_read_failure_remains_uncertain(self):
        self.store.mode = "confirmation_failure"
        with self.assertRaises(app.WriteUncertain):
            self.run_backfill()
        self.assertEqual(len(self.store.rows), 3)
        self.assertEqual(len(self.store.batches), 1)

    def test_concurrent_ledger_revision_change_blocks_the_entire_batch(self):
        self.store.ledger, _ = advance(self.ledger, "deposit", amount="50", date="2026-10-20")
        with self.assertRaises(app.Conflict):
            self.run_backfill()
        self.assertEqual(self.store.rows, [])
        self.assertEqual(self.store.batches, [])

    def test_fresh_snapshot_read_preserves_actual_record_arriving_during_fetch(self):
        actual = self.observed("2026-10-19")

        def load(*args):
            self.store.rows.append([app.canonical(actual)])
            return history({"2026-10-19": 110, "2026-10-20": 120})

        self.loader.side_effect = load
        result = self.run_backfill()
        self.assertEqual(result["written"], 2)
        self.assertEqual(self.merged()["2026-10-19"]["id"], actual["id"])

    def test_each_batch_spans_at_most_90_calendar_days_and_reports_older_remaining(self):
        self.ledger = funded("2026-01-01")
        self.store = BatchStore(self.ledger)
        result = self.run_backfill()
        expected_days = (date(2026, 10, 20) - date(2026, 1, 1)).days + 1
        self.assertEqual(result["written"], 90)
        self.assertEqual(result["remaining"], expected_days - 90)
        batch_days = [date.fromisoformat(snapshot["day"]) for snapshot in self.store.batches[0]]
        self.assertEqual(max(batch_days) - min(batch_days), timedelta(days=89))
        second = self.run_backfill()
        self.assertEqual(second["written"], 90)
        self.assertEqual(second["remaining"], expected_days - 180)
        self.assertEqual(len(self.merged()), 180)
        self.loader.assert_not_called()

    def test_invalid_batch_window_is_rejected(self):
        for window in (0, 91):
            with self.subTest(window=window), self.assertRaises(ValueError):
                self.run_backfill(window_days=window)

    def test_no_financial_start_makes_zero_price_api_requests(self):
        self.ledger = app.load_bundle("", [])
        self.store = BatchStore(self.ledger)
        self.assertEqual(self.run_backfill()["written"], 0)
        self.loader.assert_not_called()
        self.assertEqual(self.store.batches, [])

    def test_one_missing_stock_never_saves_a_partial_valuation(self):
        self.ledger, _ = advance(self.ledger, "buy", ticker="0050.TW", shares=1,
                                 price="50", fee="0", date="2026-10-20")
        self.store.ledger = self.ledger
        self.loader.side_effect = lambda ticker, start, end: (
            history({"2026-10-19": 110, "2026-10-20": 120}) if ticker == "2330.TW" else history())
        result = self.run_backfill()
        self.assertEqual(result["written"], 2)
        self.assertEqual(result["unavailable"], 1)
        self.assertNotIn("2026-10-20", self.merged())

    def test_budget_offset_reaches_next_stock_after_an_earlier_stock_keeps_failing(self):
        self.ledger, _ = advance(funded("2026-10-17"), "buy", ticker="0050.TW", shares=1,
                                 price="50", fee="1", date="2026-10-18")
        self.ledger, _ = advance(self.ledger, "sell", ticker="0050.TW", shares=1,
                                 price="50", fee="0", date="2026-10-19")
        self.ledger, _ = advance(self.ledger, "buy", ticker="2330.TW", shares=1,
                                 price="100", fee="1", date="2026-10-20")
        self.store = BatchStore(self.ledger)

        def load(ticker, start, end):
            if ticker == "0050.TW":
                raise TimeoutError("offline persistent failure")
            return history({"2026-10-20": 110})

        self.loader.side_effect = load
        with patch("history_backfill.time.monotonic", side_effect=[0, 0, 16]):
            first = self.run_backfill(budget_seconds=15)
        self.assertEqual(first["next_offset"], 1)
        self.assertEqual(first["written"], 2)  # Cash-only days can already progress.
        self.assertEqual(first["unavailable"], 2)
        self.assertEqual([call.args[0] for call in self.loader.call_args_list], ["0050.TW"])

        with patch("history_backfill.time.monotonic", side_effect=[0, 0, 16]):
            second = self.run_backfill(budget_seconds=15, ticker_offset=first["next_offset"])
        self.assertEqual(second["written"], 1)
        self.assertEqual(second["unavailable"], 1)
        self.assertEqual(second["next_offset"], 0)
        self.assertEqual([call.args[0] for call in self.loader.call_args_list], ["0050.TW", "2330.TW"])
        self.assertNotIn("2026-10-18", self.merged())
        self.assertIn("2026-10-20", self.merged())


if __name__ == "__main__":
    unittest.main()
