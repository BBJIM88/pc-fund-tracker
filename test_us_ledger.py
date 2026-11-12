"""Offline coverage for US share quantities and TWD cash settlement."""

import csv
import io
import json
import unittest
from decimal import Decimal as D

import app


def funded():
    ledger = app.load_bundle("", [])
    return app.create_event(ledger, "deposit", amount="10000", date="2026-01-01")[1]


def fractional_trade(ledger, action="buy", **fields):
    values = {"ticker": "AAPL", "shares": "0.2", "settled_twd": "600", "date": "2026-01-02", **fields}
    return app.create_event(ledger, action, **values)


class UsLedgerTests(unittest.TestCase):
    def test_ticker_formats_and_quantity_precision_stay_market_specific(self):
        self.assertEqual(app.ticker_text(" brk.b "), "BRK-B")
        self.assertEqual(app.ticker_text("voo"), "VOO")
        self.assertEqual(app.ticker_text("6488.two"), "6488.TWO")
        for invalid in (None, "2330", "AAPL/US", "AA.TWO", "AAPL=F", "US:AAPL", "AAPL---B"):
            with self.subTest(invalid=invalid), self.assertRaises(app.LedgerError):
                app.ticker_text(invalid)
        self.assertEqual(app.share_count("0.123456789012", "AAPL"), D("0.123456789012"))
        self.assertEqual(app.share_count("1.0", "2330.TW"), 1)
        for invalid in (True, None, "Infinity", "0.0000000000001", "0", "-0.2", "1000000000000.01"):
            with self.subTest(invalid=invalid), self.assertRaises(app.LedgerError):
                app.share_count(invalid, "AAPL")
        with self.assertRaises(app.LedgerError):
            app.share_count("0.2", "2330.TW")
        self.assertEqual(app.serializable_holdings({"AAPL": D("0.1200"), "2330.TW": 100}),
                         {"AAPL": "0.12", "2330.TW": 100})

    def test_actual_cash_settlement_is_not_recomputed_from_usd_or_fees(self):
        ledger = funded()
        event, ledger = fractional_trade(ledger, shares=D("0.25"), settled_twd=D("805.01"),
                                        trade_price_usd=D("100.1234"), fee="5.01")
        self.assertEqual(event["schema_version"], 3)
        self.assertEqual(event["shares"], "0.25")
        self.assertEqual(event["trade_price_usd"], "100.1234")
        self.assertEqual(app.trade_cash_amount(event), D("805.01"))
        self.assertEqual(ledger.state["balance"], D("9194.99"))
        self.assertEqual(ledger.state["buys"][0]["price"], D("3220.04"))
        self.assertEqual(ledger.state["buys"][0]["trade_price_usd"], D("100.1234"))
        sale, ledger = fractional_trade(ledger, "sell", shares="0.25", settled_twd="850.02", fee="5")
        self.assertEqual(app.trade_cash_amount(sale), D("850.02"))
        self.assertEqual(ledger.state["balance"], D("10045.01"))
        self.assertEqual(ledger.state["sells"][0]["realized"], D("45.01"))

    def test_backdated_fifo_uses_fractional_quantities_and_conserves_cost(self):
        _, ledger = fractional_trade(funded(), shares="0.3", settled_twd="1200", date="2026-01-03")
        _, ledger = fractional_trade(ledger)  # Earlier lot should be sold first.
        _, ledger = fractional_trade(ledger, "sell", shares="0.35", settled_twd="2000", date="2026-01-04")
        _, ledger = app.create_event(ledger, "dividend", ticker="AAPL", amount="12.34", date="2026-01-05")
        self.assertEqual(ledger.state["balance"], D("10212.34"))
        self.assertEqual(app.holdings(ledger.state), {"AAPL": D("0.15")})
        sale = ledger.state["sells"][0]
        self.assertEqual([a["shares"] for a in sale["allocations"]], [D("0.2"), D("0.15")])
        self.assertEqual(sale["basis"], D("1200.00"))
        self.assertEqual(sale["realized"], D("800.00"))
        prices = {"AAPL": {"price": D("5000.00")}}
        self.assertEqual(app.valuation(ledger.state, prices), (D("750.00"), D("10962.34")))
        self.assertEqual(app.profit_parts(ledger.state, prices)["total"], D("962.34"))
        copy = app.load_bundle("", json.loads(app.canonical(ledger.events)))
        self.assertEqual(copy.state, ledger.state)
        self.assertEqual(copy.revision, ledger.revision)

    def test_amendments_normalize_decimals_and_replay_broker_cash(self):
        buy, ledger = fractional_trade(funded(), ticker="BRK.B", shares="0.2", settled_twd="1000")
        _, ledger = fractional_trade(ledger, "sell", ticker="brk-b", shares="0.05", settled_twd="300")
        replacement = {**buy, "shares": D("0.25"), "settled_twd": D("950.10"), "trade_price_usd": D("415.125")}
        amend, ledger = app.create_event(ledger, "amend", target_id=buy["id"], replacement=replacement,
                                         reason="對帳後更正")
        self.assertEqual(amend["schema_version"], 3)
        self.assertEqual(amend["replacement"]["shares"], "0.25")
        self.assertEqual(app.holdings(ledger.state), {"BRK-B": D("0.20")})
        self.assertEqual(ledger.state["balance"], D("9349.90"))
        self.assertEqual(ledger.state["sells"][0]["basis"], D("190.02"))

    def test_fifo_rounds_from_exact_share_units_when_just_below_a_half_cent(self):
        total_units = 10**24
        cost_cents = 10**17 - 1
        # The exact fractional cent is 0.5 - 1/total_units, not 0.5.
        sold_units = (total_units // 2 - 1) * pow(cost_cents, -1, total_units) % total_units
        ledger = app.create_event(app.load_bundle("", []), "deposit", amount="1000000000000000",
                                  date="2026-01-01")[1]
        _, ledger = fractional_trade(ledger, shares="1000000000000", settled_twd="999999999999999.99")
        _, ledger = fractional_trade(ledger, "sell", shares=D(sold_units).scaleb(-12), settled_twd="1")
        expected_cents = cost_cents * sold_units // total_units
        self.assertEqual(ledger.state["sells"][0]["basis"], D(expected_cents).scaleb(-2))

    def test_taiwan_event_schema_stays_v2_and_cross_market_amendments_use_v3(self):
        buy, ledger = app.create_event(funded(), "buy", ticker="0050.TW", shares=2, price="50", fee="1",
                                       date="2026-01-02")
        self.assertEqual(buy["schema_version"], 2)
        self.assertEqual(app.trade_cash_amount(buy), D("101.00"))
        amend, ledger = app.create_event(ledger, "amend", target_id=buy["id"],
            replacement={**buy, "ticker": "VOO", "shares": "0.125", "settled_twd": "600"}, reason="更正市場")
        self.assertEqual(amend["schema_version"], 3)
        self.assertEqual(app.holdings(ledger.state), {"VOO": D("0.125")})
        restore, ledger = app.create_event(ledger, "amend", target_id=buy["id"], replacement=buy, reason="還原")
        self.assertEqual(restore["schema_version"], 3)
        self.assertEqual(app.holdings(ledger.state), {"0050.TW": 2})
        self.assertEqual(ledger.state["balance"], D("9899.00"))

    def test_splits_preserve_basis_and_reject_unrepresentable_shares(self):
        _, ledger = fractional_trade(funded(), shares="0.125", settled_twd="600")
        split, ledger = app.create_event(ledger, "split", ticker="AAPL", numerator=3, denominator=2, date="2026-01-03")
        self.assertEqual(split["schema_version"], 3)
        self.assertEqual(app.holdings(ledger.state), {"AAPL": D("0.1875")})
        self.assertEqual(ledger.state["buys"][0]["remaining_cost"], D("600.00"))
        with self.assertRaisesRegex(app.LedgerError, "無法精確"):
            app.create_event(ledger, "split", ticker="AAPL", numerator=1, denominator=7, date="2026-01-04")

    def test_invalid_us_debit_and_overselling_leave_ledger_unchanged(self):
        ledger = funded()
        for settled_twd in (None, "0", "-5", "10001", True, "NaN"):
            with self.subTest(settled_twd=settled_twd), self.assertRaises(app.LedgerError):
                fractional_trade(ledger, settled_twd=settled_twd)
        _, ledger = fractional_trade(ledger)
        with self.assertRaisesRegex(app.LedgerError, "超過"):
            fractional_trade(ledger, "sell", shares="0.200000000001")
        self.assertEqual(app.holdings(ledger.state), {"AAPL": D("0.2")})
        self.assertEqual(ledger.state["balance"], D("9400.00"))

    def test_existing_fractional_shares_are_excluded_from_financial_ledger(self):
        _, ledger = fractional_trade(funded())
        event, ledger = app.create_event(ledger, "set_existing_holding", ticker="AAPL", shares="0.375")
        self.assertEqual(event["schema_version"], 3)
        prices = {"AAPL": {"price": D("3200.00")}}
        self.assertEqual(app.existing_valuation(ledger.state, prices), D("1200.00"))
        self.assertEqual(app.valuation(ledger.state, prices), (D("640.00"), D("10040.00")))
        with self.assertRaises(app.LedgerError):
            fractional_trade(ledger, "sell", shares="0.375")
        _, ledger = app.create_event(ledger, "remove_existing_holding", ticker="AAPL")
        self.assertEqual(ledger.state["existing_holdings"], {})

    def test_csv_and_complete_backup_preserve_fractional_and_settlement_values(self):
        _, ledger = fractional_trade(funded(), shares="0.123456789012", settled_twd="123.40", trade_price_usd="92.625")
        rows = list(csv.DictReader(io.StringIO(app.records_csv(ledger).decode("utf-8-sig"))))
        buy = next(row for row in rows if row["代號"] == "AAPL")
        self.assertEqual(buy["股數"], "0.123456789012")
        self.assertEqual(buy["美元成交單價"], "92.625")
        self.assertEqual(buy["券商實際台幣扣款／入帳"], "123.40")
        payload = app.backup_document(ledger, spreadsheet_id="mock-book", baseline_worksheet_id=42)
        self.assertEqual(app.validate_backup(json.loads(app.canonical(payload))).state, ledger.state)


if __name__ == "__main__":
    unittest.main()
