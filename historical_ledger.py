"""Reconstruct end-of-day financial states from an already validated v2 ledger.

The original A1 cash value is a legacy closing balance.  This module never
pretends it is an opening cash balance and never filters the signed event chain.
Unknown historical targets are represented by ``None`` rather than today's goal.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import date, timedelta

import app


FINANCIAL_ACTIONS = {"deposit", "withdrawal", "buy", "sell", "dividend", "split"}


def _effective_by_id(ledger):
    return {record["id"]: record["effective"] for record in ledger.records}


def history_start(ledger) -> date | None:
    """First defensible reconstruction day; no transactions means no fake origin."""
    if ledger.cutoff != date.min:
        return ledger.cutoff
    days = []
    for record in ledger.records:
        entry = record.get("effective")
        if (not entry or entry["action"] not in FINANCIAL_ACTIONS
                or record.get("status") == "舊版已刪除"):
            continue
        try:
            days.append(app.trade_day(entry.get("date", "")))
        except app.LedgerError:
            # Old deposits may be undated.  Their stored balance remains useful,
            # but they cannot establish the first historical calendar day.
            continue
    return min(days, default=None)


def _legacy_state(ledger, effective):
    # make_state applies correction deltas to the authoritative A1 cash.  Deleted
    # legacy records must still exist here so their accepted delete can undo them.
    state = app.make_state(ledger.baseline, effective)
    for event in ledger.accepted:
        if (event.get("schema_version") == 2
                or event["action"] in app.POSITION_ACTIONS | {"baseline", "amend", "snapshot"}):
            continue
        entry = effective.get(event["id"], event)
        if entry is not None:
            app.apply_record(state, entry)
    state["existing_holdings"] = {}
    return state


def _target_timeline(ledger, effective):
    """Only dated goals are historical evidence; an empty zero goal is known."""
    baseline_goal = app.money(ledger.baseline["target_hardware"]["current_target"])
    raw_history = ledger.baseline["target_hardware"].get("history", [])
    fallback = app.money(0) if baseline_goal == 0 and not raw_history else None
    baseline_entries = []
    try:
        for sequence, entry in enumerate(raw_history):
            day = app.trade_day(entry["date"])
            value = app.money(entry["price"])
            if value < 0:
                raise ValueError()
            baseline_entries.append((day, 0, sequence, value))
        # If the history cannot explain the stored goal, the date of the missing
        # change is unknown.  Do not infer any old goal from that incomplete list.
        if baseline_entries and max(baseline_entries, key=lambda row: row[:3])[3] == baseline_goal:
            timeline = baseline_entries
        else:
            timeline = []
    except (KeyError, TypeError, ValueError):
        timeline = []

    for sequence, event in enumerate(ledger.accepted):
        if event["action"] != "target":
            continue
        entry = effective.get(event["id"], event)
        if entry is not None:
            timeline.append((app.trade_day(entry["date"]),
                             2 if event.get("schema_version") == 2 else 1,
                             sequence, app.money(entry["amount"])))
    return fallback, sorted(timeline, key=lambda row: row[:3])


def reconstruct_states(ledger, start, end, *, days=None) -> dict[date, dict]:
    """Return independent end-of-day states using currently effective records.

    ``days`` optionally selects output dates without copying every intervening
    state.  All necessary events are still replayed in chronological order.
    Dates before the reliable legacy cutoff, and future dates, are omitted.
    Existing holdings are excluded.  ``state['target']`` is None when unknown;
    callers must omit that chart value, rather than substitute today's target.
    """
    first = history_start(ledger)
    if first is None:
        return {}
    lower = max(app.trade_day(start), first)
    upper = min(app.trade_day(end), app.datetime.now(app.TZ).date())
    if lower > upper:
        return {}
    if days is None:
        requested = (lower + timedelta(days=offset) for offset in range((upper - lower).days + 1))
    else:
        requested = sorted({day for value in days if lower <= (day := app.trade_day(value)) <= upper})

    effective = _effective_by_id(ledger)
    state = _legacy_state(ledger, effective)
    timeline = []
    for sequence, event in enumerate(ledger.accepted):
        if event.get("schema_version") != 2 or event["action"] not in FINANCIAL_ACTIONS:
            continue
        entry = effective.get(event["id"])
        if entry is not None:
            timeline.append((app.trade_day(entry["date"]), sequence, entry))
    timeline.sort(key=lambda row: row[:2])
    target, targets = _target_timeline(ledger, effective)
    event_index = target_index = 0
    result = {}
    target_history = []
    for day in requested:
        while event_index < len(timeline) and timeline[event_index][0] <= day:
            app.apply_record(state, timeline[event_index][2], chronological_fifo=True)
            event_index += 1
        while target_index < len(targets) and targets[target_index][0] <= day:
            target_day, _, _, target = targets[target_index]
            target_history.append({"date": target_day.isoformat(), "price": str(target)})
            target_index += 1
        state["target"] = target
        state["history_target_known"] = target is not None
        state["target_history"] = target_history
        # Old snapshots are not evidence of a historical portfolio composition.
        # Keep only contemporaneous values in this independent output object.
        output = deepcopy(state)
        output["snapshots"] = {key: value for key, value in output["snapshots"].items()
                               if app.trade_day(key) <= day}
        result[day] = output
    return result
