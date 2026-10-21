"""Pure, offline historical-ledger tests; no credentials, UI or market APIs."""

import unittest
from copy import deepcopy
from datetime import date
from decimal import Decimal as D

import app
from historical_ledger import history_start, reconstruct_states


def advance(ledger, action, **fields):
    event, updated = app.create_event(ledger, action, **fields)
    return updated, event


def funded():
    ledger, _ = advance(app.load_bundle("", []), "deposit", amount="1000", date="2026-01-01")
    return ledger


def states(ledger, start="2026-01-01", end="2026-01-10", **kwargs):
    return reconstruct_states(ledger, start, end, **kwargs)


class HistoricalLedgerTests(unittest.TestCase):
    def test_empty_ledger_has_no_fake_origin_or_states(self):
        ledger = app.load_bundle("", [])
        self.assertIsNone(history_start(ledger))
        self.assertEqual(states(ledger), {})

    def test_position_and_target_only_do_not_start_a_financial_history(self):
        ledger, _ = advance(app.load_bundle("", []), "set_existing_holding",
                            ticker="0050.TW", shares=20, date="2026-01-01")
        ledger, _ = advance(ledger, "target", amount="5000", date="2026-01-02")
        self.assertIsNone(history_start(ledger))
        self.assertEqual(states(ledger), {})

    def test_undated_legacy_cash_cannot_establish_a_historical_start(self):
        baseline = app.default_data()
        baseline["cash_account"] = {"balance": 1000, "history": [{"amount": 1000}]}
        ledger = app.load_bundle(app.canonical(baseline), [])
        self.assertIsNone(history_start(ledger))
        self.assertEqual(states(ledger), {})
        ledger, _ = advance(ledger, "deposit", amount="100", date="2026-01-04")
        self.assertEqual(history_start(ledger), date(2026, 1, 4))
        self.assertEqual(states(ledger)[date(2026, 1, 4)]["balance"], D("1100.00"))

    def test_first_financial_date_is_start_and_daily_cash_has_no_double_deposit(self):
        ledger = funded()
        self.assertEqual(history_start(ledger), date(2026, 1, 1))
        result = states(ledger, "2025-12-20", "2026-01-03")
        self.assertEqual(set(result), {date(2026, 1, 1), date(2026, 1, 2), date(2026, 1, 3)})
        self.assertTrue(all(state["balance"] == D("1000.00") for state in result.values()))

    def test_buy_sell_dividend_and_withdrawal_follow_actual_transaction_days(self):
        ledger = funded()
        ledger, _ = advance(ledger, "buy", ticker="2330.TW", shares=3,
                            price="100", fee="1", date="2026-01-02")
        ledger, _ = advance(ledger, "sell", ticker="2330.TW", shares=1,
                            price="120", fee="1", tax="2", date="2026-01-04")
        ledger, _ = advance(ledger, "dividend", ticker="2330.TW", amount="20", date="2026-01-05")
        ledger, _ = advance(ledger, "withdrawal", amount="100", date="2026-01-06")
        result = states(ledger)
        self.assertEqual(app.holdings(result[date(2026, 1, 1)]), {})
        self.assertEqual(app.holdings(result[date(2026, 1, 2)]), {"2330.TW": 3})
        self.assertEqual(result[date(2026, 1, 3)]["balance"], D("699.00"))
        self.assertEqual(app.holdings(result[date(2026, 1, 4)]), {"2330.TW": 2})
        self.assertEqual(result[date(2026, 1, 4)]["balance"], D("816.00"))
        self.assertEqual(result[date(2026, 1, 5)]["balance"], D("836.00"))
        self.assertEqual(result[date(2026, 1, 6)]["balance"], D("736.00"))
        self.assertEqual(result[date(2026, 1, 10)]["sells"], ledger.state["sells"])

    def test_backdated_deposit_uses_effective_day_without_breaking_revision_chain(self):
        ledger = funded()
        ledger, _ = advance(ledger, "deposit", amount="100", date="2026-01-04")
        ledger, _ = advance(ledger, "deposit", amount="200", date="2026-01-02")
        result = states(ledger)
        self.assertEqual(result[date(2026, 1, 1)]["balance"], D("1000.00"))
        self.assertEqual(result[date(2026, 1, 2)]["balance"], D("1200.00"))
        self.assertEqual(result[date(2026, 1, 4)]["balance"], D("1300.00"))
        self.assertEqual(len(ledger.accepted), 3)

    def test_current_amendment_changes_the_original_day_and_later_days(self):
        ledger = funded()
        ledger, buy = advance(ledger, "buy", ticker="2330.TW", shares=3,
                              price="100", fee="1", date="2026-01-02")
        ledger, _ = advance(ledger, "amend", target_id=buy["id"],
                            replacement={**buy, "price": "110", "date": "2026-01-03"}, reason="Correction")
        result = states(ledger)
        self.assertEqual(app.holdings(result[date(2026, 1, 2)]), {})
        self.assertEqual(result[date(2026, 1, 3)]["balance"], D("669.00"))
        self.assertEqual(app.holdings(result[date(2026, 1, 3)]), {"2330.TW": 3})

    def test_cancelled_transaction_is_absent_in_historical_reconstruction(self):
        ledger = funded()
        ledger, buy = advance(ledger, "buy", ticker="2330.TW", shares=3,
                              price="100", fee="1", date="2026-01-02")
        ledger, _ = advance(ledger, "amend", target_id=buy["id"], replacement=None, reason="Cancel")
        result = states(ledger)
        self.assertEqual(result[date(2026, 1, 2)]["balance"], D("1000.00"))
        self.assertEqual(app.holdings(result[date(2026, 1, 2)]), {})

    def test_split_preserves_cash_and_changes_shares_on_effective_day(self):
        ledger = funded()
        ledger, _ = advance(ledger, "buy", ticker="2330.TW", shares=3,
                            price="100", fee="1", date="2026-01-02")
        ledger, _ = advance(ledger, "split", ticker="2330.TW", numerator=4,
                            denominator=1, date="2026-01-04")
        result = states(ledger)
        self.assertEqual(app.holdings(result[date(2026, 1, 3)]), {"2330.TW": 3})
        self.assertEqual(app.holdings(result[date(2026, 1, 4)]), {"2330.TW": 12})
        self.assertEqual(result[date(2026, 1, 4)]["balance"], D("699.00"))
        self.assertEqual(result[date(2026, 1, 4)]["buys"][0]["remaining_cost"], D("301.00"))

    def test_same_day_end_state_replays_all_events_in_accepted_order(self):
        ledger = funded()
        ledger, _ = advance(ledger, "buy", ticker="2330.TW", shares=3,
                            price="100", fee="1", date="2026-01-02")
        ledger, _ = advance(ledger, "sell", ticker="2330.TW", shares=1,
                            price="120", fee="1", date="2026-01-02")
        result = states(ledger)
        self.assertEqual(app.holdings(result[date(2026, 1, 2)]), {"2330.TW": 2})
        self.assertEqual(result[date(2026, 1, 2)]["balance"], D("818.00"))

    def test_legacy_cash_is_closing_balance_and_dates_before_cutoff_are_omitted(self):
        baseline = app.default_data()
        baseline["cash_account"] = {"balance": 790,
                                     "history": [{"date": "2025-12-01", "amount": 1000}]}
        baseline["transactions"] = [{"type": "buy", "date": "2025-12-02", "ticker": "2330.TW",
                                      "shares": 2, "price": 100, "fee": 10}]
        ledger = app.load_bundle(app.canonical(baseline), [])
        ledger, _ = advance(ledger, "deposit", amount="100", date="2026-01-01")
        self.assertEqual(history_start(ledger), date(2025, 12, 2))
        result = states(ledger, "2025-12-01", "2026-01-02")
        self.assertNotIn(date(2025, 12, 1), result)
        self.assertEqual(result[date(2025, 12, 2)]["balance"], D("790.00"))
        self.assertEqual(app.holdings(result[date(2025, 12, 2)]), {"2330.TW": 2})
        self.assertEqual(result[date(2026, 1, 1)]["balance"], D("890.00"))

    def test_legacy_delete_remains_authoritative(self):
        baseline = app.default_data()
        baseline["cash_account"] = {"balance": 790,
                                     "history": [{"date": "2025-12-01", "amount": 1000}]}
        baseline["transactions"] = [{"type": "buy", "date": "2025-12-02", "ticker": "2330.TW",
                                      "shares": 2, "price": 100, "fee": 10}]
        delete = {"id": "old-delete", "action": "delete", "date": "2025-12-03",
                  "category": "buys", "target_id": "legacy-buy-0"}
        ledger = app.load_bundle(app.canonical(baseline), [delete])
        result = states(ledger, "2025-12-02", "2025-12-04")
        self.assertNotIn(date(2025, 12, 2), result)
        self.assertEqual(result[date(2025, 12, 3)]["balance"], D("1000.00"))
        self.assertEqual(app.holdings(result[date(2025, 12, 3)]), {})

    def test_legacy_buy_correction_uses_delta_and_updates_later_sales(self):
        baseline = app.default_data()
        baseline["cash_account"] = {"balance": 790,
                                     "history": [{"date": "2025-12-01", "amount": 1000}]}
        baseline["transactions"] = [{"type": "buy", "date": "2025-12-02", "ticker": "2330.TW",
                                      "shares": 2, "price": 100, "fee": 10}]
        ledger = app.load_bundle(app.canonical(baseline), [])
        ledger, _ = advance(ledger, "sell", ticker="2330.TW", shares=1,
                            price="200", fee="0", date="2026-01-03")
        original = ledger.records[0]["original"]
        ledger, _ = advance(ledger, "amend", target_id="legacy-buy-0",
                            replacement={**original, "price": "110"}, reason="Correction")
        result = states(ledger, "2025-12-02", "2026-01-04")
        self.assertEqual(result[date(2025, 12, 2)]["balance"], D("770.00"))
        self.assertEqual(result[date(2026, 1, 3)]["balance"], D("970.00"))
        self.assertEqual(result[date(2026, 1, 3)]["sells"][0]["basis"], D("115.00"))

    def test_existing_holdings_are_always_excluded(self):
        ledger, _ = advance(funded(), "set_existing_holding", ticker="0050.TW", shares=200, date="2026-01-02")
        result = states(ledger)
        self.assertTrue(all(state["existing_holdings"] == {} for state in result.values()))
        self.assertEqual(ledger.state["existing_holdings"], {"0050.TW": 200})

    def test_default_goal_then_dated_goals_and_same_day_last_goal(self):
        ledger, _ = advance(funded(), "target", amount="5000", date="2026-01-03")
        ledger, _ = advance(ledger, "target", amount="6000", date="2026-01-03")
        ledger, _ = advance(ledger, "target", amount="8000", date="2026-01-05")
        result = states(ledger)
        self.assertEqual(result[date(2026, 1, 2)]["target"], D("0.00"))
        self.assertEqual(result[date(2026, 1, 3)]["target"], D("6000.00"))
        self.assertEqual(result[date(2026, 1, 5)]["target"], D("8000.00"))

    def test_target_correction_and_cancellation_use_current_effective_history(self):
        ledger, goal = advance(funded(), "target", amount="5000", date="2026-01-03")
        ledger, _ = advance(ledger, "amend", target_id=goal["id"],
                            replacement={**goal, "amount": "6000", "date": "2026-01-04"}, reason="Correction")
        result = states(ledger)
        self.assertEqual(result[date(2026, 1, 3)]["target"], D("0.00"))
        self.assertEqual(result[date(2026, 1, 4)]["target"], D("6000.00"))
        ledger, _ = advance(ledger, "amend", target_id=goal["id"], replacement=None, reason="Cancel")
        self.assertEqual(states(ledger)[date(2026, 1, 4)]["target"], D("0.00"))

    def test_unknown_baseline_goal_is_not_filled_with_todays_goal(self):
        baseline = app.default_data()
        baseline["cash_account"] = {"balance": 1000,
                                     "history": [{"date": "2026-01-01", "amount": 1000}]}
        baseline["target_hardware"] = {"current_target": 8000, "history": []}
        ledger = app.load_bundle(app.canonical(baseline), [])
        ledger, _ = advance(ledger, "target", amount="9000", date="2026-01-05")
        result = states(ledger)
        self.assertIsNone(result[date(2026, 1, 1)]["target"])
        self.assertFalse(result[date(2026, 1, 1)]["history_target_known"])
        self.assertEqual(result[date(2026, 1, 5)]["target"], D("9000.00"))

    def test_baseline_dated_goal_is_used_only_from_its_known_day(self):
        baseline = app.default_data()
        baseline["cash_account"] = {"balance": 1000,
                                     "history": [{"date": "2026-01-01", "amount": 1000}]}
        baseline["target_hardware"] = {"current_target": 8000,
                                       "history": [{"date": "2026-01-03", "price": "8000"}]}
        ledger = app.load_bundle(app.canonical(baseline), [])
        result = states(ledger)
        self.assertIsNone(result[date(2026, 1, 2)]["target"])
        self.assertEqual(result[date(2026, 1, 3)]["target"], D("8000.00"))

    def test_incomplete_baseline_goal_history_remains_unknown(self):
        baseline = app.default_data()
        baseline["cash_account"] = {"balance": 1000,
                                     "history": [{"date": "2026-01-01", "amount": 1000}]}
        baseline["target_hardware"] = {"current_target": 8000,
                                       "history": [{"date": "2026-01-02", "price": "5000"}]}
        ledger = app.load_bundle(app.canonical(baseline), [])
        self.assertIsNone(states(ledger)[date(2026, 1, 3)]["target"])

    def test_sparse_days_replays_intervening_events_and_copies_only_selected_dates(self):
        ledger = funded()
        ledger, _ = advance(ledger, "deposit", amount="100", date="2026-01-04")
        result = states(ledger, days={date(2025, 12, 31), date(2026, 1, 2), date(2026, 1, 8), date(2026, 1, 11)})
        self.assertEqual(set(result), {date(2026, 1, 2), date(2026, 1, 8)})
        self.assertEqual(result[date(2026, 1, 8)]["balance"], D("1100.00"))
        self.assertEqual(states(ledger, days=set()), {})

    def test_result_and_ledger_are_independent(self):
        ledger = funded()
        original = deepcopy(ledger.state)
        result = states(ledger)
        result[date(2026, 1, 1)]["deposits"].clear()
        self.assertEqual(len(result[date(2026, 1, 2)]["deposits"]), 1)
        self.assertEqual(ledger.state, original)

    def test_rejected_conflicting_event_never_enters_history(self):
        ledger = funded()
        stale, _ = app.create_event(ledger, "deposit", amount="500", date="2026-01-03")
        ledger, _ = advance(ledger, "deposit", amount="100", date="2026-01-02")
        ledger = app.load_bundle(ledger.raw_baseline, ledger.events + [stale])
        self.assertEqual(ledger.statuses[stale["id"]]["status"], "衝突未入帳")
        self.assertEqual(states(ledger)[date(2026, 1, 3)]["balance"], D("1100.00"))

    def test_requested_range_outside_history_has_no_states(self):
        ledger = funded()
        self.assertEqual(states(ledger, "2025-12-01", "2025-12-30"), {})
        self.assertEqual(states(ledger, "2026-01-05", "2026-01-01"), {})
        self.assertEqual(states(ledger, "2099-01-01", "2099-01-02"), {})


if __name__ == "__main__":
    unittest.main()
