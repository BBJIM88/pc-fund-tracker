"""Historical-price adapter tests; all public requests are mocked."""
import json
import unittest
from datetime import date
from decimal import Decimal as D
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import historical_prices as hp
import requests


def twse(month="20250601", code="0050", rows=None):
    rows = rows if rows is not None else [["114/06/10", "188.65"], ["114/06/18", "47.57"]]
    return {"stat": "OK", "date": month, "title": f"114年06月 {code} 測試 各日成交資訊",
            "fields": ["日期", "收盤價"], "data": rows, "total": len(rows)}


def tpex(month="20250601", code="6488", rows=None):
    rows = rows if rows is not None else [["114/06/02", "293.00"], ["114/06/03", "287.00"]]
    return {"stat": "ok", "date": month, "code": code, "tables": [
        {"date": month, "fields": ["日 期", "收盤"], "data": rows, "totalCount": len(rows)}]}


class FakeResponse:
    def __init__(self, content):
        self.content = content

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def raise_for_status(self):
        return None

    def iter_content(self, chunk_size):
        for offset in range(0, len(self.content), chunk_size):
            yield self.content[offset:offset + chunk_size]


def response(payload):
    content = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return FakeResponse(content)


class HistoricalPriceTests(unittest.TestCase):
    def setUp(self):
        hp._cached_month_report.cache_clear()
        self.addCleanup(hp._cached_month_report.cache_clear)

    def test_twse_month_preserves_unadjusted_prices_before_and_after_split(self):
        with patch("requests.get", return_value=response(twse())) as request:
            result = hp.load_history("0050.TW", date(2025, 6, 1), date(2025, 6, 30))
        self.assertEqual(result["quotes"]["2025-06-10"]["price"], D("188.65"))
        self.assertEqual(result["quotes"]["2025-06-18"]["price"], D("47.57"))
        self.assertEqual(result["quotes"]["2025-06-10"]["at"], "2025-06-10")
        self.assertIn("原始", result["quotes"]["2025-06-10"]["source"])
        self.assertEqual(result["coverage"], [(date(2025, 6, 1), date(2025, 6, 30))])
        query = parse_qs(urlparse(request.call_args.args[0]).query)
        self.assertEqual(query["date"], ["20250601"])
        self.assertEqual(query["stockNo"], ["0050"])
        self.assertEqual(request.call_args.kwargs["timeout"], (3, 5))
        self.assertTrue(request.call_args.kwargs["stream"])
        self.assertNotIn("verify", request.call_args.kwargs)

    def test_tpex_uses_verified_monthly_endpoint_and_whitespace_fields(self):
        with patch("requests.get", return_value=response(tpex())) as request:
            result = hp.load_history("6488.TWO", date(2025, 6, 1), date(2025, 6, 30))
        self.assertEqual(result["quotes"]["2025-06-02"]["price"], D("293.00"))
        self.assertIn("櫃檯買賣", result["quotes"]["2025-06-02"]["source"])
        self.assertEqual(len(result["coverage"]), 1)
        url = request.call_args.args[0]
        self.assertEqual(urlparse(url).path, "/www/zh-tw/afterTrading/tradingStock")
        self.assertEqual(parse_qs(urlparse(url).query)["date"], ["2025/06/01"])

    def test_only_requested_inclusive_dates_are_returned(self):
        payload = twse(rows=[["114/06/09", "183.70"], ["114/06/10", "188.65"], ["114/06/18", "47.57"]])
        with patch("requests.get", return_value=response(payload)):
            result = hp.load_history("0050.TW", date(2025, 6, 10), date(2025, 6, 17))
        self.assertEqual(set(result["quotes"]), {"2025-06-10"})
        self.assertEqual(result["coverage"], [(date(2025, 6, 10), date(2025, 6, 17))])

    def test_weekend_has_confirmed_coverage_but_no_manufactured_quote(self):
        payload = twse(rows=[["114/06/06", "181.95"], ["114/06/09", "183.70"]])
        with patch("requests.get", return_value=response(payload)):
            result = hp.load_history("0050.TW", date(2025, 6, 7), date(2025, 6, 8))
        self.assertEqual(result["quotes"], {})
        self.assertEqual(result["coverage"], [(date(2025, 6, 7), date(2025, 6, 8))])

    def test_one_failed_month_does_not_claim_holiday_coverage_or_drop_other_month(self):
        june = twse()
        with patch("requests.get", side_effect=[TimeoutError("sensitive server text"), response(june)]):
            result = hp.load_history("0050.TW", date(2025, 5, 31), date(2025, 6, 30))
        self.assertEqual(result["coverage"], [(date(2025, 6, 1), date(2025, 6, 30))])
        self.assertEqual(len(result["quotes"]), 2)

    def test_month_boundary_and_year_boundary_issue_correct_requests(self):
        dec = twse("20241201", rows=[["113/12/31", "190"]])
        jan = twse("20250101", rows=[["114/01/02", "191"]])
        with patch("requests.get", side_effect=[response(dec), response(jan)]) as request:
            result = hp.load_history("0050.TW", date(2024, 12, 31), date(2025, 1, 2))
        self.assertEqual(len(request.call_args_list), 2)
        self.assertEqual(set(result["quotes"]), {"2024-12-31", "2025-01-02"})
        self.assertEqual(result["coverage"], [(date(2024, 12, 31), date(2024, 12, 31)),
                                              (date(2025, 1, 1), date(2025, 1, 2))])

    def test_invalid_input_never_issues_a_request(self):
        cases = [("0050", date(2025, 6, 1), date(2025, 6, 30)),
                 ("0050.US", date(2025, 6, 1), date(2025, 6, 30)),
                 ("0050.TW", "2025-06-01", date(2025, 6, 30)),
                 ("0050.TW", date(2025, 6, 30), date(2025, 6, 1)),
                 ("0050.TW", date(2024, 1, 1), date(2025, 1, 1)),
                 ("0050.TW", date(2025, 1, 1), date(9999, 1, 1))]
        with patch("requests.get") as request:
            for args in cases:
                with self.subTest(args=args), self.assertRaises(ValueError):
                    hp.load_history(*args)
        request.assert_not_called()

    def test_invalid_stat_symbol_month_fields_or_partial_rows_have_no_coverage(self):
        for changes in ({"stat": "查無資料"}, {"title": "2330 台積電"}, {"date": "20250501"},
                        {"fields": ["日期", "開盤價"]}, {"total": 99}, {"data": []}):
            hp._cached_month_report.cache_clear()
            payload = {**twse(), **changes}
            with self.subTest(changes=changes), patch("requests.get", return_value=response(payload)):
                result = hp.load_history("0050.TW", date(2025, 6, 1), date(2025, 6, 30))
            self.assertEqual(result, {"quotes": {}, "coverage": []})

    def test_invalid_tpex_table_or_symbol_has_no_coverage(self):
        payloads = []
        for changes in ({"code": "2330"}, {"date": "20250501"}, {"stat": "error"}, {"tables": []}):
            payloads.append({**tpex(), **changes})
        broken_table = tpex()
        broken_table["tables"][0]["totalCount"] = 99
        payloads.append(broken_table)
        for payload in payloads:
            hp._cached_month_report.cache_clear()
            with self.subTest(payload=payload), patch("requests.get", return_value=response(payload)):
                result = hp.load_history("6488.TWO", date(2025, 6, 1), date(2025, 6, 30))
            self.assertEqual(result, {"quotes": {}, "coverage": []})

    def test_malformed_daily_row_rejects_entire_month_instead_of_calling_it_holiday(self):
        invalid_rows = [["bad", "100"], ["114/05/30", "100"], ["114/06/10", "--"],
                        ["114/06/10", "0"], ["114/06/10", "-1"], ["114/06/10", "NaN"],
                        ["114/06/10", True], ["114/06/10", "0.001"], ["114/06/10"]]
        for row in invalid_rows:
            hp._cached_month_report.cache_clear()
            payload = twse(rows=[["114/06/18", "47.57"], row])
            with self.subTest(row=row), patch("requests.get", return_value=response(payload)):
                result = hp.load_history("0050.TW", date(2025, 6, 1), date(2025, 6, 30))
            self.assertEqual(result, {"quotes": {}, "coverage": []})

    def test_duplicate_dates_are_not_silently_overwritten(self):
        payload = twse(rows=[["114/06/10", "188.65"], ["114/06/10", "47.57"]])
        with patch("requests.get", return_value=response(payload)):
            result = hp.load_history("0050.TW", date(2025, 6, 1), date(2025, 6, 30))
        self.assertEqual(result, {"quotes": {}, "coverage": []})

    def test_gregorian_dates_comma_prices_and_decimal_rounding_are_supported(self):
        payload = twse(rows=[["2025/06/10", "1,234.565"]])
        with patch("requests.get", return_value=response(payload)):
            result = hp.load_history("0050.TW", date(2025, 6, 1), date(2025, 6, 30))
        self.assertEqual(result["quotes"]["2025-06-10"]["price"], D("1234.57"))

    def test_malformed_json_or_nonobject_payload_is_unavailable(self):
        for payload in (b"not json", b"[]"):
            hp._cached_month_report.cache_clear()
            with self.subTest(payload=payload), patch("requests.get", return_value=response(payload)):
                result = hp.load_history("0050.TW", date(2025, 6, 1), date(2025, 6, 30))
            self.assertEqual(result, {"quotes": {}, "coverage": []})

    def test_oversized_payload_is_rejected(self):
        with patch.object(hp, "MAX_RESPONSE_BYTES", 10), patch("requests.get", return_value=response(b"x" * 11)):
            result = hp.load_history("0050.TW", date(2025, 6, 1), date(2025, 6, 30))
        self.assertEqual(result, {"quotes": {}, "coverage": []})

    def test_error_logs_only_type_and_do_not_retry(self):
        with patch("requests.get", side_effect=TimeoutError("secret raw response")) as request:
            with self.assertLogs("pc_fund_tracker", level="WARNING") as output:
                result = hp.load_history("0050.TW", date(2025, 6, 1), date(2025, 6, 30))
        request.assert_called_once()
        self.assertEqual(result["coverage"], [])
        self.assertIn("TimeoutError", output.output[0])
        self.assertNotIn("secret", output.output[0])
        self.assertNotIn("0050", output.output[0])

    def test_certificate_failure_is_not_retried_without_verification(self):
        with patch("requests.get", side_effect=requests.exceptions.SSLError("invalid certificate")) as request:
            result = hp.load_history("6488.TWO", date(2025, 6, 1), date(2025, 6, 30))
        request.assert_called_once()
        self.assertNotIn("verify", request.call_args.kwargs)
        self.assertEqual(result, {"quotes": {}, "coverage": []})

    def test_http_error_is_not_parsed_as_success(self):
        fake = response(twse())
        with patch.object(fake, "raise_for_status", side_effect=requests.exceptions.HTTPError("403")):
            with patch("requests.get", return_value=fake):
                result = hp.load_history("0050.TW", date(2025, 6, 1), date(2025, 6, 30))
        self.assertEqual(result, {"quotes": {}, "coverage": []})

    def test_successful_public_month_is_reused_across_date_ranges(self):
        with patch("requests.get", return_value=response(twse())) as request:
            first = hp.load_history("0050.TW", date(2025, 6, 1), date(2025, 6, 30))
            second = hp.load_history("0050.TW", date(2025, 6, 10), date(2025, 6, 18))
        request.assert_called_once()
        self.assertEqual(len(first["quotes"]), 2)
        self.assertEqual(first["quotes"], second["quotes"])
        self.assertEqual(second["coverage"], [(date(2025, 6, 10), date(2025, 6, 18))])
        self.assertEqual(hp._cached_month_report.cache_info().maxsize, 256)

    def test_failed_month_is_not_cached_and_next_attempt_can_succeed(self):
        with patch("requests.get", side_effect=[TimeoutError("offline"), response(twse())]) as request:
            first = hp.load_history("0050.TW", date(2025, 6, 1), date(2025, 6, 30))
            second = hp.load_history("0050.TW", date(2025, 6, 1), date(2025, 6, 30))
        self.assertEqual(request.call_count, 2)
        self.assertEqual(first["coverage"], [])
        self.assertEqual(len(second["quotes"]), 2)

    def test_cached_months_symbols_and_markets_are_isolated(self):
        payloads = [twse(), twse(code="2330"), twse("20250701", rows=[["114/07/01", "50"]]), tpex()]
        with patch("requests.get", side_effect=[response(p) for p in payloads]) as request:
            hp.load_history("0050.TW", date(2025, 6, 1), date(2025, 6, 30))
            hp.load_history("2330.TW", date(2025, 6, 1), date(2025, 6, 30))
            hp.load_history("0050.TW", date(2025, 7, 1), date(2025, 7, 31))
            hp.load_history("6488.TWO", date(2025, 6, 1), date(2025, 6, 30))
        self.assertEqual(request.call_count, 4)

    def test_cache_expires_at_hour_bucket_change(self):
        with patch("requests.get", side_effect=[response(twse()), response(twse())]) as request:
            with patch.object(hp.time, "monotonic", return_value=3601):
                hp.load_history("0050.TW", date(2025, 6, 1), date(2025, 6, 30))
            with patch.object(hp.time, "monotonic", return_value=7201):
                hp.load_history("0050.TW", date(2025, 6, 1), date(2025, 6, 30))
        self.assertEqual(request.call_count, 2)

    def test_calendar_day_change_also_invalidates_same_hour_bucket(self):
        with patch("requests.get", side_effect=[response(twse()), response(twse())]) as request:
            with patch.object(hp.time, "monotonic", return_value=3601), patch.object(hp, "datetime") as clock:
                clock.now.return_value.date.side_effect = [date(2025, 7, 1), date(2025, 7, 2)]
                hp.load_history("0050.TW", date(2025, 6, 1), date(2025, 6, 30))
                hp.load_history("0050.TW", date(2025, 6, 1), date(2025, 6, 30))
        self.assertEqual(request.call_count, 2)

    def test_returned_quote_mutation_does_not_change_public_cache(self):
        with patch("requests.get", return_value=response(twse())) as request:
            first = hp.load_history("0050.TW", date(2025, 6, 1), date(2025, 6, 30))
            first["quotes"]["2025-06-10"]["price"] = D("1")
            second = hp.load_history("0050.TW", date(2025, 6, 1), date(2025, 6, 30))
        request.assert_called_once()
        self.assertEqual(second["quotes"]["2025-06-10"]["price"], D("188.65"))


if __name__ == "__main__":
    unittest.main()
