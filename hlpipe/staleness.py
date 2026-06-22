"""
Staleness & risk prioritization — the pipeline's cost front door.

We do NOT re-verify the whole directory every run. We score each record for how
likely it is to be wrong AND how much it matters, sort descending, and only
process the top of the queue up to a daily budget. This is the single biggest
cost lever: external calls, LLM extraction, and review effort all scale with how
many records we choose to touch.

priority = recency_pressure + traffic_value + hard_signal_boost
"""
from __future__ import annotations

from datetime import date, datetime

from .config import DAILY_RECHECK_BUDGET, STALE_AFTER_DAYS
from .models import ProviderRecord


def _days_since(iso: str) -> int:
    try:
        d = datetime.fromisoformat(iso).date()
    except ValueError:
        d = datetime.strptime(iso, "%Y-%m-%d").date()
    return (date.today() - d).days


def priority_score(rec: ProviderRecord) -> float:
    age = _days_since(rec.last_verified_date)
    # recency pressure: 0 until stale, then ramps toward 1 over a year past due
    recency = max(0.0, (age - STALE_AFTER_DAYS)) / 365.0
    recency = min(recency, 1.0)
    # traffic value: popular providers are worth keeping correct (log-damped)
    import math
    traffic = math.log1p(rec.monthly_search_volume) / 12.0
    traffic = min(traffic, 1.0)
    # hard signals (returned mail, failed call, NPI deactivation) jump the queue
    signal_boost = 0.5 * len(rec.hard_signals)
    return round(recency + traffic + signal_boost, 4)


def build_queue(
    records: list[ProviderRecord],
    budget: int = DAILY_RECHECK_BUDGET,
) -> list[ProviderRecord]:
    """Return records worth checking this run, highest priority first, capped."""
    scored = [
        (priority_score(r), r)
        for r in records
        if _days_since(r.last_verified_date) >= STALE_AFTER_DAYS or r.hard_signals
    ]
    scored.sort(key=lambda x: x[0], reverse=True)
    return [r for _, r in scored[:budget]]
