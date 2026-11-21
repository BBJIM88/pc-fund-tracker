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


def book_revision(book, *, core=None):
    core = _core(core)
    return core.digest({key: value for key, value in book.items() if key != "checksum"})


def _read_catalogue(spreadsheet, *, core=None):
    core = _core(core)
    ws = _worksheet(spreadsheet, CATALOGUE)
    books, seen, sheet_ids = [dict(PRIMARY)], {}, set()
    operations, statuses = {}, {}
    for line, row in enumerate(ws.get_all_values() if ws else [], 1):
        if not row or not row[0]:
            continue
        try:
            book = json.loads(row[0])
            if (not isinstance(book, dict) or book.get("version") != 1 or
                    book.get("checksum") != core.digest({k: v for k, v in book.items() if k != "checksum"}) or
                    book.get("spreadsheet_id") != spreadsheet.id):
                raise ValueError()
            book_name(book["name"], core=core)
            if book.get("action") == "rename":
                operation_id, target = book["operation_id"], book["book_id"]
                if (not re.fullmatch(r"[0-9a-f]{32}", operation_id) or
                        not re.fullmatch(r"[0-9a-f]{64}", book["expected_revision"])):
                    raise ValueError()
                index = next((i for i, item in enumerate(books) if item["id"] == target), None)
                if index is None:
                    raise ValueError()
                if operation_id in operations:
                    if operations[operation_id] != book:
                        raise ValueError()
                    continue
                operations[operation_id] = book
                current = books[index]
                if (book_revision(current, core=core) != book["expected_revision"] or
                        any(item["id"] != target and item["name"].casefold() == book["name"].casefold()
                            for item in books)):
                    # A concurrent rename wins in append order; preserve the
                    # rejected attempt for audit rather than overwrite it.
                    statuses[operation_id] = "conflict"
                else:
                    updated = {**current, "name": book["name"]}
                    if "checksum" in updated:
                        updated["checksum"] = book_revision(updated, core=core)
                    books[index] = updated
                    statuses[operation_id] = "applied"
                continue
            if book.get("action") is not None or not re.fullmatch(r"[0-9a-f]{32}", book.get("id", "")):
                raise ValueError()
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
    return {"books": books, "created": seen, "operations": operations, "statuses": statuses}


def list_books(spreadsheet, *, core=None):
    return _read_catalogue(spreadsheet, core=core)["books"]


def rename_book(spreadsheet, book_id, name, *, operation_id=None, expected_revision=None, core=None):
    """Append a confirmed, retryable name change; financial identity is stable."""
    core = _core(core)
    name = book_name(name, core=core)
    operation_id = operation_id or uuid.uuid4().hex
    if not isinstance(operation_id, str) or not re.fullmatch(r"[0-9a-f]{32}", operation_id):
        raise core.LedgerError("重新命名操作的識別碼無效。")
    state = _read_catalogue(spreadsheet, core=core)
    current = next((book for book in state["books"] if book["id"] == book_id), None)
    if current is None:
        raise core.LedgerError("找不到指定帳本。")
    prior = state["operations"].get(operation_id)
    if prior:
        if prior["book_id"] != book_id or prior["name"] != name:
            raise core.LedgerError("同一個重新命名操作不能更換帳本或名稱。")
        if state["statuses"][operation_id] != "applied":
            raise core.LedgerError("帳本名稱已由其他操作變更，請重新整理後再試。")
        return current
    if expected_revision is not None and expected_revision != book_revision(current, core=core):
        raise core.LedgerError("帳本名稱已更新，請重新整理後再試。")
    if any(book["id"] != book_id and book["name"].casefold() == name.casefold() for book in state["books"]):
        raise core.LedgerError("已有同名帳本，請換一個名稱。")
    if current["name"] == name:
        return current
    change = {"version": 1, "action": "rename", "operation_id": operation_id, "book_id": book_id,
              "name": name, "expected_revision": book_revision(current, core=core), "spreadsheet_id": spreadsheet.id}
    change["checksum"] = core.digest(change)
    catalogue = _worksheet(spreadsheet, CATALOGUE, create=True)
    try:
        catalogue.append_row([core.canonical(change)], value_input_option="RAW", insert_data_option="INSERT_ROWS")
    except Exception:
        pass
    try:
        confirmed = _read_catalogue(spreadsheet, core=core)
    except Exception as exc:
        raise core.WriteUncertain("重新命名的保存結果尚未確認，請使用同一個操作重試。") from exc
    if confirmed["operations"].get(operation_id) != change:
        raise core.WriteUncertain("重新命名尚未確認保存，請使用同一個操作重試。")
    if confirmed["statuses"][operation_id] != "applied":
        raise core.LedgerError("帳本名稱已被其他操作使用或更新，請重新整理後再試。")
    return next(book for book in confirmed["books"] if book["id"] == book_id)


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
        state = _read_catalogue(spreadsheet, core=core)
        if state["created"].get(book["id"]) == book:
            return next(b for b in state["books"] if b["id"] == book["id"])
    except Exception as exc:
        raise core.WriteUncertain("帳本目錄的保存結果尚未確認，請使用同一個建立操作重試。") from exc
    raise core.WriteUncertain("帳本目錄尚未確認保存，請稍後重試；請勿刪除建立中的分頁。")


def create_book(spreadsheet, name, *, operation_id=None, core=None):
    core = _core(core)
    name = book_name(name, core=core)
    book_id = operation_id or uuid.uuid4().hex
    if not isinstance(book_id, str) or not re.fullmatch(r"[0-9a-f]{32}", book_id):
        raise core.LedgerError("建立帳本的識別碼無效。")
    state = _read_catalogue(spreadsheet, core=core)
    books = state["books"]
    same = next((book for book in books if book["id"] == book_id), None)
    if same:
        if state["created"][book_id]["name"] != name:
            raise core.LedgerError("同一個建立操作不能更換帳本名稱。")
        return same
    if any(book["name"].casefold() == name.casefold() for book in books):
        raise core.LedgerError("已有同名帳本，請換一個名稱。")
    if len(books) >= 20:
        raise core.LedgerError("目前最多可建立 20 個帳本。")
    book = prepare_book(spreadsheet, name, book_id, core=core)
    # Recheck before publication, including an operation whose first request
    # timed out after the catalogue append actually succeeded.
    latest = _read_catalogue(spreadsheet, core=core)
    same = next((b for b in latest["books"] if b["id"] == book_id), None)
    if same:
        if latest["created"].get(book_id) != book:
            raise core.LedgerError("新帳本的目錄資料不一致，已停止操作。")
        return same
    return publish_book(spreadsheet, book, core=core)
