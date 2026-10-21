"""Offline checks for mobile input hints and lossless stock-code entry.

Run: python -m unittest -v test_mobile_inputs
"""
import json
import unittest
from decimal import Decimal

from streamlit.testing.v1 import AppTest

import app
import test_existing_holdings as existing_tests


class StockCodeTests(unittest.TestCase):
    def test_leading_zeros_market_and_letter_suffix_are_preserved(self):
        for parts, expected in (
            (("0050", "TW", ""), "0050.TW"),
            (("006208", "TW", ""), "006208.TW"),
            (("6488", "TWO", ""), "6488.TWO"),
            (("00937", "TW", "B"), "00937B.TW"),
            (("00937B", "TW", "B"), "00937B.TW"),
            ((" 6488.two ", "TW", "B"), "6488.TWO"),
            (("006208.TW", "TWO", "B"), "006208.TW"),
        ):
            with self.subTest(parts=parts):
                self.assertEqual(app.ticker_text(app.assemble_ticker(*parts)), expected)

    def test_default_code_market_and_suffix_roundtrip(self):
        for symbol in ("0050.TW", "006208.TW", "6488.TWO", "00937B.TW"):
            code, market, suffix = app.ticker_input_parts(symbol)
            self.assertEqual(app.assemble_ticker(code, market, suffix), symbol)

    def test_invalid_code_never_stages_an_event(self):
        ledger = app.load_bundle("", [])
        for code in ("", ".TW", "2330.US", "23 30", "<script>"):
            with self.subTest(code=code), self.assertRaises(app.LedgerError):
                app.create_event(ledger, "set_existing_holding", ticker=app.assemble_ticker(code, "TW"), shares=1)
        self.assertEqual(ledger.events, [])


class MobileInputUITests(unittest.TestCase):
    PASSWORD = "mobile-input-test-password"
    setUp = existing_tests.ExistingHoldingsUITests.setUp
    fake_ticker = existing_tests.ExistingHoldingsUITests.fake_ticker
    open_app = existing_tests.ExistingHoldingsUITests.open_app
    button = staticmethod(existing_tests.ExistingHoldingsUITests.button)
    assert_metric = existing_tests.ExistingHoldingsUITests.assert_metric

    def test_numeric_code_and_market_preview_keep_zeroes_and_original_finances(self):
        at = self.open_app()
        at.text_input(key="existing_ticker").set_value("006208")
        at.selectbox(key="existing_ticker_market").select("TW")
        at.number_input(key="existing_shares").set_value(7)
        self.button(at, "預覽既有庫存").click().run()
        self.assertFalse(at.exception)
        event = at.session_state["pending_event"]
        self.assertEqual((event["ticker"], event["shares"]), ("006208.TW", 7))
        self.button(at, "確認寫入").click().run()
        self.assertFalse(at.exception)
        self.assertEqual(app.SheetStore(self.book, baseline_id=42).read().state["existing_holdings"], {"006208.TW": 7})
        self.assert_metric(at, "目前總資產", "NT$ 1,239.00")

    def test_tpex_market_is_attached_to_code(self):
        at = self.open_app()
        at.text_input(key="existing_ticker").set_value("6488")
        at.selectbox(key="existing_ticker_market").select("TWO")
        at.number_input(key="existing_shares").set_value(13)
        self.button(at, "預覽既有庫存").click().run()
        self.assertFalse(at.exception)
        self.assertEqual(at.session_state["pending_event"]["ticker"], "6488.TWO")

    def test_decimal_money_and_letter_code_are_saved_without_coercion(self):
        self.prices["00937B.TW"] = 25.0
        at = self.open_app()
        at.selectbox(key="entry_action").select("buy").run()
        at.text_input(key="entry_buy_ticker").set_value("00937")
        at.text_input(key="entry_buy_ticker_suffix").set_value("B")
        at.number_input(key="entry_buy_shares").set_value(3)
        at.number_input(key="entry_buy_price").set_value(10.25)
        at.number_input(key="entry_buy_fee").set_value(1.5)
        at.number_input(key="entry_buy_other").set_value(0.25)
        self.button(at, "預覽此筆操作").click().run()
        self.assertFalse(at.exception)
        event = at.session_state["pending_event"]
        self.assertEqual((event["ticker"], event["shares"], event["price"], event["fee"], event["other_fee"]),
                         ("00937B.TW", 3, "10.25", "1.50", "0.25"))
        self.button(at, "確認寫入").click().run()
        self.assertFalse(at.exception)
        self.assertEqual(app.SheetStore(self.book, baseline_id=42).read().state["balance"], Decimal("906.50"))

    def test_amendment_defaults_and_complete_paste_override_old_suffix(self):
        def script():
            import streamlit as st
            import app
            with st.form("test_amend"):
                fields = app.record_fields(st, "set_existing_holding", app.make_state(app.default_data()),
                    prefix="amend_test", initial={"ticker": "00937B.TWO", "shares": 20})
                if st.form_submit_button("Preview"):
                    st.json(fields)
        at = AppTest.from_function(script, default_timeout=15).run()
        self.assertFalse(at.exception)
        self.assertEqual(at.text_input(key="amend_test_ticker").value, "00937")
        self.assertEqual(at.text_input(key="amend_test_ticker_suffix").value, "B")
        self.assertEqual(at.selectbox(key="amend_test_ticker_market").value, "TWO")
        at.text_input(key="amend_test_ticker").set_value("006208.TW")
        self.button(at, "Preview").click().run()
        self.assertFalse(at.exception)
        self.assertEqual(json.loads(at.json[0].value)["ticker"], "006208.TW")

    def test_fee_has_decimal_hint_even_with_step_of_one(self):
        def script():
            import streamlit as st
            import app
            app.numeric_input(st, "Shares", min_value=1, value=1, step=1, key="test_shares")
            app.numeric_input(st, "Fee", min_value=0.0, value=0.0, step=1.0, format="%.2f", key="test_fee")
        at = AppTest.from_function(script, default_timeout=15).run()
        self.assertFalse(at.exception)
        self.assertIsNotNone(at.get_by_key("pc_integer_test_shares"))
        self.assertIsNotNone(at.get_by_key("pc_decimal_test_fee"))


if __name__ == "__main__":
    unittest.main()
