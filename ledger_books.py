"""Independent named ledgers in one workbook, with an append-only catalogue."""
from __future__ import annotations

import json
import re
import uuid

CATALOGUE = "LedgerBooks_v1"
PREFIX = "PCFT_BOOK_"
PRIMARY = {"id": "main", "name": "原本帳本"}


def _core(core):
    if core is None:
        import app
        return app
    return core


def _worksheet(spreadsheet, name, *, create=False):
    import gspread
    try:
        return spreadsheet.worksheet(name)
    except gspread.WorksheetNotFound:
        if not create:
            return None
        try:
            return spreadsheet.add_worksheet(title=name, rows=100, cols=1)
        except gspread.exceptions.APIError:
            return spreadsheet.worksheet(name)


def book_name(value, *, core=None):
    core = _core(core)
    if (not isinstance(value, str) or not 1 <= len(value.strip()) <= 40 or
            any(ord(ch) < 32 or ord(ch) == 127 for ch in value)):
        raise core.LedgerError("帳本名稱請填 1～40 個字，不能包含控制字元。")
    return value.strip()


def list_books(spreadsheet, *, core=None):
    core = _core(core)
    ws = _worksheet(spreadsheet, CATALOGUE)
    books, seen, sheet_ids = [dict(PRIMARY)], {}, set()
    for line, row in enumerate(ws.get_all_values() if ws else [], 1):
        if not row or not row[0]:
            continue
        try:
            book = json.loads(row[0])
            if (not isinstance(book, dict) or book.get("version") != 1 or
                    book.get("checksum") != core.digest({k: v for k, v in book.items() if k != "checksum"}) or
                    not re.fullmatch(r"[0-9a-f]{32}", book.get("id", "")) or
                    book.get("spreadsheet_id") != spreadsheet.id):
                raise ValueError()
            book_name(book["name"], core=core)
            ids = [book[key] for key in ("baseline_id", "events_id", "snapshots_id")]
            if any(type(value) is not int or value < 0 for value in ids) or len(set(ids)) != 3:
                raise ValueError()
            if book["id"] in seen:
                if seen[book["id"]] != book:
                    raise ValueError()
                continue
            if sheet_ids.intersection(ids):
                raise ValueError()
        except (ValueError, KeyError, TypeError) as exc:
            raise core.LedgerError(f"帳本目錄第 {line} 列無法驗證，請保留原資料並檢查。") from exc
        seen[book["id"]] = book
        sheet_ids.update(ids)
        books.append(book)
    return books


def prepare_book(spreadsheet, name, book_id, *, raw_baseline=None, core=None):
    """Prepare only this operation's sheets; never overwrite an existing ledger."""
    core = _core(core)
    name = book_name(name, core=core)
    if not isinstance(book_id, str) or not re.fullmatch(r"[0-9a-f]{32}", book_id):
        raise core.LedgerError("建立帳本的識別碼無效。")
    raw = raw_baseline if raw_baseline is not None else core.canonical({**core.default_data(), "ledger_id": book_id})
    core.parse_baseline(raw)
    baseline = _worksheet(spreadsheet, PREFIX + book_id + "_A1", create=True)
    current = baseline.acell("A1").value or ""
    if current and current != raw:
        raise core.LedgerError("建立中的基準分頁已有其他資料，已停止建立。")
    if not current:
        try:
            baseline.update([[raw]], range_name="A1", value_input_option="RAW")
        except Exception:
            if (baseline.acell("A1").value or "") != raw:
                raise core.WriteUncertain("新帳本的建立結果尚未確認；請使用同一個建立操作重試。")
    if (baseline.acell("A1").value or "") != raw:
        raise core.WriteUncertain("尚未確認新帳本的基準資料，請稍後重試。")
    events = _worksheet(spreadsheet, PREFIX + book_id + "_Events", create=True)
    snapshots = _worksheet(spreadsheet, PREFIX + book_id + "_Snapshots", create=True)
    if any(any(row) for ws in (events, snapshots) for row in ws.get_all_values()):
        raise core.LedgerError("建立中的帳本分頁已有紀錄，請先查詢帳本目錄。")
    book = {"version": 1, "id": book_id, "name": name, "spreadsheet_id": spreadsheet.id,
            "baseline_id": baseline.id, "events_id": events.id, "snapshots_id": snapshots.id}
    book["checksum"] = core.digest(book)
    return book


def publish_book(spreadsheet, book, *, core=None):
    core = _core(core)
    catalogue = _worksheet(spreadsheet, CATALOGUE, create=True)
    try:
        catalogue.append_row([core.canonical(book)], value_input_option="RAW", insert_data_option="INSERT_ROWS")
    except Exception:
        pass
    try:
        existing = next((b for b in list_books(spreadsheet, core=core) if b["id"] == book["id"]), None)
        if existing == book:
            return book
    except Exception as exc:
        raise core.WriteUncertain("帳本目錄的保存結果尚未確認，請使用同一個建立操作重試。") from exc
    raise core.WriteUncertain("帳本目錄尚未確認保存，請稍後重試；請勿刪除建立中的分頁。")


def create_book(spreadsheet, name, *, operation_id=None, core=None):
    core = _core(core)
    name = book_name(name, core=core)
    book_id = operation_id or uuid.uuid4().hex
    books = list_books(spreadsheet, core=core)
    same = next((book for book in books if book["id"] == book_id), None)
    if same:
        if same["name"] != name:
            raise core.LedgerError("同一個建立操作不能更換帳本名稱。")
        return same
    if any(book["name"].casefold() == name.casefold() for book in books):
        raise core.LedgerError("已有同名帳本，請換一個名稱。")
    if len(books) >= 20:
        raise core.LedgerError("目前最多可建立 20 個帳本。")
    book = prepare_book(spreadsheet, name, book_id, core=core)
    # Recheck before publication, including an operation whose first request
    # timed out after the catalogue append actually succeeded.
    same = next((b for b in list_books(spreadsheet, core=core) if b["id"] == book_id), None)
    if same:
        if same != book:
            raise core.LedgerError("新帳本的目錄資料不一致，已停止操作。")
        return same
    return publish_book(spreadsheet, book, core=core)
