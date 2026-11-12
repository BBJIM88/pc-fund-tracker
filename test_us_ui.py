"""Exercise U.S. stock forms and mixed holdings with fake books and quotes."""
from datetime import timedelta
from decimal import Decimal
import unittest
from unittest.mock import patch

import app
import test_existing_holdings as fixtures


class USStockUITests(unittest.TestCase):
    PASSWORD = "offline-us-ui-test-password"
    fake_ticker = fixtures.ExistingHoldingsUITests.fake_ticker
    open_app = fixtures.ExistingHoldingsUITests.open_app
    button = staticmethod(fixtures.ExistingHoldingsUITests.button)
    assert_metric = fixtures.ExistingHoldingsUITests.assert_metric

    def setUp(self):
        fixtures.ExistingHoldingsUITests.setUp(self)
        yesterday = app.datetime.now(app.TZ).date() - timedelta(days=1)
        quote = {"price": Decimal("3000"), "usd_price": Decimal("100"),
                 "fx_rate": Decimal("30"), "at": f"{yesterday.isoformat()}T04:20:00+08:00",
                 "market_date": (yesterday - timedelta(days=1)).isoformat(),
                 "fx_at": f"{yesterday.isoformat()}T17:00:00+08:00", "source": "mock U.S. market"}
        latest = patch("us_market.latest_quote", return_value=quote)
        latest.start()
        self.addCleanup(latest.stop)

    def submit_trade(self, at, action, ticker, shares, amount, usd=None):
        at.selectbox(key="entry_action").select(action).run()
        prefix = f"entry_{action}"
        at.radio(key=prefix + "_market_group").set_value("US").run()
        at.text_input(key=prefix + "_ticker_us").set_value(ticker)
        at.text_input(key=prefix + "_shares_us").set_value(shares)
        at.text_input(key=prefix + "_settled_twd").set_value(amount)
        if usd is not None:
            at.text_input(key=prefix + "_trade_price_usd").set_value(usd)
        self.button(at, "預覽此筆操作").click().run()
        self.assertFalse(at.exception)
        return at.session_state["pending_event"]

    def confirm(self, at):
        self.button(at, "確認寫入").click().run()
        self.assertFalse(at.exception)
        return app.SheetStore(self.book, baseline_id=42).read()

    def test_buy_and_sell_fractional_shares_use_actual_twd_cash_and_fifo(self):
        at = self.open_app()
        buy = self.submit_trade(at, "buy", "aapl", "0.250000000001", "700.50", "99.10")
        self.assertEqual(buy["ticker"], "AAPL")
        self.assertEqual(buy["shares"], "0.250000000001")
        self.assertEqual(Decimal(buy["settled_twd"]), Decimal("700.50"))
        self.assertEqual(Decimal(buy["trade_price_usd"]), Decimal("99.10"))
        ledger = self.confirm(at)
        self.assertEqual(ledger.state["balance"], Decimal("238.50"))
        self.assert_metric(at, "目前總資產", "NT$ 1,288.50")
        holdings = next(d.value for d in at.dataframe if "美元兌台幣匯率" in d.value.columns)
        self.assertEqual(holdings.loc[holdings["代號"] == "AAPL", "股數"].item(), "0.250000000001")
        self.assertEqual(holdings.loc[holdings["代號"] == "AAPL", "美元兌台幣匯率"].item(), 30)

        sell = self.submit_trade(at, "sell", "AAPL", "0.1", "350")
        self.assertEqual(Decimal(sell["settled_twd"]), Decimal("350"))
        ledger = self.confirm(at)
        self.assertEqual(app.holdings(ledger.state)["AAPL"], Decimal("0.150000000001"))
        self.assertEqual(ledger.state["sells"][-1]["basis"], Decimal("280.20"))
        self.assertEqual(ledger.state["balance"], Decimal("588.50"))

    def test_existing_fractional_us_holdings_keep_own_twd_valuation(self):
        at = self.open_app()
        at.radio(key="existing_market_group").set_value("US").run()
        at.text_input(key="existing_ticker_us").set_value("voo")
        at.text_input(key="existing_shares_us").set_value("0.125")
        self.button(at, "預覽既有庫存").click().run()
        self.assertFalse(at.exception)
        self.assertEqual(at.session_state["pending_event"]["shares"], "0.125")
        ledger = self.confirm(at)
        self.assertEqual(ledger.state["existing_holdings"], {"VOO": Decimal("0.125")})
        self.assert_metric(at, "既有庫存總市值", "NT$ 375.00")
        self.assert_metric(at, "目前總資產", "NT$ 1,239.00")

    def test_correction_preserves_fractional_default_and_requires_matching_market(self):
        at = self.open_app()
        buy = self.submit_trade(at, "buy", "BRK.B", "0.125", "500.25")
        self.assertEqual(buy["ticker"], "BRK-B")
        ledger = self.confirm(at)
        records = at.selectbox(key="amend_selection")
        event = next(record for record in ledger.records if record["id"] == buy["id"])
        records.select(event).run()
        self.assertFalse(at.exception)
        prefix = "amend_" + buy["id"]
        self.assertEqual(at.radio(key=prefix + "_market_group").value, "US")
        self.assertEqual(at.text_input(key=prefix + "_shares_us").value, "0.125")
        at.text_input(key=prefix + "_ticker_us").set_value("006208.TW")
        at.text_area(key="amend_reason_" + buy["id"]).set_value("測試錯選市場")
        self.button(at, "重算並預覽影響").click().run()
        self.assertFalse(at.exception)
        self.assertTrue(any("市場不符" in error.value for error in at.error))


if __name__ == "__main__":
    unittest.main()
