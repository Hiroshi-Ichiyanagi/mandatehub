"""Shared revenue/usage metrics derived from settled records (no wall clock; explicit `now`).

Used by operator.py's /metrics endpoint and by stats.py — one source of truth so the live
endpoint and the offline report can never disagree.
"""
from __future__ import annotations

from datetime import datetime

from mandatehub.core.ledger import Ledger
from mandatehub.intent.settlement import iter_settlement_records


def compute_metrics(ledger: Ledger, *, now: datetime, days: int = 7) -> dict:
    records = list(iter_settlement_records(ledger, as_of=now))
    revenue = sum(r.payee_receipt_cents for r in records)        # what merchants received
    booked = sum(r.authorized_outflow_cents for r in records)    # budget-plane outflow
    payees = {r.payee_account_id for r in records}
    intents = {r.intent_id for r in records}

    per_day: dict[str, dict] = {}
    for r in records:
        key = r.settled_at.date().isoformat()
        d = per_day.setdefault(key, {"count": 0, "revenue_cents": 0})
        d["count"] += 1
        d["revenue_cents"] += r.payee_receipt_cents

    ts = [r.settled_at for r in records]
    return {
        "settlements": len(records),
        "unique_intents": len(intents),
        "unique_payees": len(payees),
        "revenue_cents": revenue,           # payee_receipt plane (executed cost)
        "budget_booked_cents": booked,      # authorized_outflow plane (user limit)
        "first_settled_at": min(ts).isoformat() if ts else None,
        "last_settled_at": max(ts).isoformat() if ts else None,
        "per_day": dict(sorted(per_day.items())[-days:]),
    }
