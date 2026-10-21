"""Save today's asset snapshot without starting or importing Streamlit."""

from __future__ import annotations

import io
import json
import logging
import os
import re
import sys
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from datetime import date

import app


class ConfigurationError(ValueError):
    """Required GitHub Actions environment configuration is invalid."""


def store_from_environment(environ=None):
    """Connect by spreadsheet ID; never inspect local credential files."""
    values = os.environ if environ is None else environ
    key_json = values.get("GCP_KEY_JSON", "").strip()
    spreadsheet_id = values.get("SPREADSHEET_ID", "").strip()
    if not key_json or not re.fullmatch(r"[A-Za-z0-9_-]+", spreadsheet_id):
        raise ConfigurationError("Required snapshot configuration is missing or invalid.")
    try:
        credentials = json.loads(key_json)
    except (TypeError, ValueError) as exc:
        raise ConfigurationError("GCP_KEY_JSON must contain a JSON object.") from exc
    if not isinstance(credentials, dict) or not credentials:
        raise ConfigurationError("GCP_KEY_JSON must contain a JSON object.")

    worksheet_id_text = values.get("BASELINE_WORKSHEET_ID", "").strip()
    if worksheet_id_text and not re.fullmatch(r"[0-9]+", worksheet_id_text):
        raise ConfigurationError("BASELINE_WORKSHEET_ID must be a nonnegative integer.")
    baseline_id = int(worksheet_id_text) if worksheet_id_text else None
    baseline_title = values.get("BASELINE_WORKSHEET_NAME", "").strip() or None

    import gspread
    client = gspread.service_account_from_dict(
        credentials, scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    spreadsheet = client.open_by_key(spreadsheet_id)
    return app.SheetStore(
        spreadsheet, baseline_id=baseline_id, baseline_title=baseline_title, client=client
    )


def run_snapshot_job(store, *, quote_loader=None):
    """Reprice once if a concurrent ledger write invalidates the first read."""
    quote_loader = app.quote_uncached if quote_loader is None else quote_loader
    for attempt in range(2):
        ledger = store.read()
        prices = {}
        for ticker in sorted(app.holdings(ledger.state)):
            quote = quote_loader(ticker)
            if quote is None:
                raise app.LedgerError("A required quote is unavailable; snapshot was not saved.")
            prices[ticker] = quote
        try:
            return app.save_daily_snapshot(store, ledger, prices, source="github_actions")
        except app.Conflict:
            if attempt:
                raise
    raise AssertionError("Snapshot retry limit was not enforced.")


@contextmanager
def quiet_service_output():
    """Third-party diagnostics can contain ticker/account details or credentials."""
    previous_disable = logging.root.manager.disable
    output = io.StringIO()
    try:
        logging.disable(logging.CRITICAL)
        with redirect_stdout(output), redirect_stderr(output):
            yield
    finally:
        logging.disable(previous_disable)
        output.close()


def main():
    try:
        with quiet_service_output():
            result = run_snapshot_job(store_from_environment())
            day = date.fromisoformat(result["snapshot"]["day"]).isoformat()
            status = "written" if result["written"] else "unchanged"
    except Exception as exc:
        # Deliberately omit exception messages, reprs, and tracebacks from public job logs.
        print(f"Snapshot job failed ({type(exc).__name__}).", file=sys.stderr)
        return 1
    print(f"{day} {status}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
