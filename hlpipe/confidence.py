"""
Confidence scoring.

For each tracked field we gather every source's NORMALIZED opinion, group the
opinions by value, and score the winning candidate value with an explainable
evidence model:

    For the candidate (winning) value:
        S_win   = sum of per-field reliability of sources that support it
    For all other reported values combined:
        S_other = sum of per-field reliability of sources that disagree

        agreement = S_win / (S_win + S_other)          # how clean the signal is
        evidence  = 1 - exp(-K * S_win)                 # how much trusted weight
        confidence = agreement * evidence

Why this shape:
  * Two independent trusted sources beat one (evidence rises with S_win).
  * A lone weak source can't reach high confidence (saturation curve).
  * A real conflict between authoritative sources tanks `agreement`, which
    pushes the record to human review instead of an unsafe auto-update.
  * It's monotonic and fully explainable — every number traces to named
    sources and their weights, which is exactly what the audit log needs.

This is deterministic arithmetic. No LLM is involved in scoring.
"""
from __future__ import annotations

import math
from collections import defaultdict
from typing import Any, Optional

from .config import EVIDENCE_K, reliability
from .models import FieldChange, ProviderRecord, SourceRecord
from .normalize import normalize_field


def _group_opinions(fname: str, sources: list[SourceRecord]):
    """value_key -> {'raw': original_value, 'sources': [(name, weight)]}.
    The stored 'raw' is taken from the highest-reliability source in the group,
    so the recommended new_value reflects the most authoritative formatting."""
    groups: dict[Any, dict] = defaultdict(
        lambda: {"raw": None, "_best_w": -1.0, "sources": []}
    )
    for s in sources:
        if fname not in s.fields:
            continue
        raw_val = s.fields[fname]
        key = normalize_field(fname, raw_val)
        if key is None:
            continue
        w = reliability(s.source_name, fname)
        g = groups[key]
        g["sources"].append((s.source_name, w))
        if w > g["_best_w"]:
            g["_best_w"] = w
            g["raw"] = raw_val
    return groups


def score_field(
    fname: str,
    current_value: Any,
    sources: list[SourceRecord],
) -> Optional[FieldChange]:
    """
    Returns a FieldChange iff the best-supported external value differs from the
    current directory value. Returns None when sources confirm the status quo or
    say nothing.
    """
    groups = _group_opinions(fname, sources)
    if not groups:
        return None

    cur_key = normalize_field(fname, current_value)

    # winning candidate = the value group with the most trusted evidence
    def weight(g):
        return sum(w for _, w in g["sources"])

    winner_key = max(groups, key=lambda k: weight(groups[k]))
    s_win = weight(groups[winner_key])
    s_other = sum(weight(g) for k, g in groups.items() if k != winner_key)

    agreement = s_win / (s_win + s_other) if (s_win + s_other) else 0.0
    evidence = 1 - math.exp(-EVIDENCE_K * s_win)
    confidence = agreement * evidence

    supporting = [name for name, _ in groups[winner_key]["sources"]]
    conflicting = sorted({
        name
        for k, g in groups.items() if k != winner_key
        for name, _ in g["sources"]
    })

    # No change: external winner matches what we already have -> confirmation.
    if winner_key == cur_key:
        return None

    return FieldChange(
        field=fname,
        old_value=current_value,
        new_value=groups[winner_key]["raw"],
        confidence_score=round(confidence, 4),
        supporting_sources=supporting,
        conflicting_sources=conflicting,
    )


def confirmed_fields(record: ProviderRecord, sources: list[SourceRecord]) -> list[str]:
    """Fields where the top external value matches the directory (re-verified)."""
    confirmed = []
    from .models import TRACKED_FIELDS
    for fname in TRACKED_FIELDS:
        groups = _group_opinions(fname, sources)
        if not groups:
            continue
        winner = max(groups, key=lambda k: sum(w for _, w in groups[k]["sources"]))
        if winner == normalize_field(fname, record.get(fname)):
            confirmed.append(fname)
    return confirmed
