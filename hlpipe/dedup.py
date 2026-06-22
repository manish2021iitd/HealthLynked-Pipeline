"""
Duplicate / moved-provider detection (pipeline requirement #5).

The rest of the pipeline reconciles a *known* record against external sources
keyed by NPI. This module solves the complementary problem: finding records
that are secretly the *same provider or practice* but live under different
provider_ids — the classic directory-rot pattern where a provider gets entered
twice (old practice + new practice), or a practice rebrands and is duplicated.

COST PHILOSOPHY (consistent with the rest of hlpipe):
  1. BLOCKING is the cost control. A naive all-pairs comparison is O(n^2):
     250k providers => ~31 billion pairs, unrunnable. Instead we bucket records
     into cheap "blocks" (same NPI, OR same zip5 + last-name initial, OR same
     specialty + city) and only compare within a block. Real duplicates almost
     always share at least one block key, so recall stays high while the number
     of comparisons collapses by orders of magnitude.
  2. DETERMINISTIC scoring only — string similarity on already-normalized
     values. No LLM, no paid identity-resolution API. Same evidence-style math
     the confidence module uses.
  3. Tiered routing mirrors the main pipeline: an exact NPI collision is a
     near-certain duplicate; a high fuzzy score is a likely duplicate; a middle
     score goes to HUMAN_REVIEW rather than auto-merging (merging is
     destructive, so we are deliberately conservative).

Uses rapidfuzz when present; falls back to difflib so the prototype always runs.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from itertools import combinations
from typing import Any, Iterable

from .models import ProviderRecord, Action
from .normalize import (
    normalize_name,
    normalize_address,
    normalize_phone,
    normalize_specialty,
)

# --- optional dep, graceful fallback ----------------------------------------
try:
    from rapidfuzz import fuzz as _rf_fuzz  # type: ignore

    def _ratio(a: str, b: str) -> float:
        return _rf_fuzz.token_sort_ratio(a, b) / 100.0
except Exception:  # pragma: no cover - exercised only when dep missing
    from difflib import SequenceMatcher

    def _ratio(a: str, b: str) -> float:
        # token_sort-ish: sort whitespace tokens so word order doesn't matter
        a2 = " ".join(sorted(a.split()))
        b2 = " ".join(sorted(b.split()))
        return SequenceMatcher(None, a2, b2).ratio()


# ---------------------------------------------------------------------------
# Tunables (kept here, not in config.py, so the dedup pass is self-contained)
# ---------------------------------------------------------------------------
# Field weights for the composite similarity score. Name + practice identity
# carry the most signal; phone is a strong but lower-coverage tiebreaker.
WEIGHTS = {
    "name": 0.40,
    "practice": 0.25,
    "address": 0.20,
    "phone": 0.15,
}
AUTO_DUPLICATE_THRESHOLD = 0.90   # >= => likely_duplicate (still queued, not silently merged)
REVIEW_FLOOR = 0.70               # [floor, auto) => human_review
# below REVIEW_FLOOR => treated as distinct


# ---------------------------------------------------------------------------
# Output contract
# ---------------------------------------------------------------------------
@dataclass
class DuplicateMatch:
    record_a: str                       # provider_id
    record_b: str                       # provider_id
    score: float                        # 0..1 composite similarity
    field_scores: dict[str, float]
    same_npi: bool
    block_key: str                      # which block surfaced this pair
    classification: str                 # exact_duplicate | likely_duplicate | possible_duplicate
    recommended_action: str             # Action value
    reason: str

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["score"] = round(self.score, 3)
        d["field_scores"] = {k: round(v, 3) for k, v in self.field_scores.items()}
        return d


# ---------------------------------------------------------------------------
# Blocking
# ---------------------------------------------------------------------------
def _block_keys(r: ProviderRecord) -> set[str]:
    """
    Cheap candidate-grouping keys. A pair is only compared if it shares at
    least one key. Multiple key families => robust to any single field being
    wrong or missing (e.g. a duplicate with a typo'd name still collides on
    zip5, and a moved provider with a new address still collides on NPI).
    """
    keys: set[str] = set()

    if r.npi:
        keys.add(f"npi:{r.npi}")

    nm = normalize_name(r.provider_name) or ""
    addr = normalize_address(r.address) or {}
    zip5 = addr.get("zip5", "")
    last = nm.split()[-1] if nm else ""

    if zip5 and last:
        keys.add(f"zip-last:{zip5}:{last[:4]}")

    spec = normalize_specialty(r.specialty) or ""
    city = addr.get("city", "")
    if spec and city:
        keys.add(f"spec-city:{spec}:{city}")

    return keys


def _index_by_block(records: Iterable[ProviderRecord]) -> dict[str, list[ProviderRecord]]:
    index: dict[str, list[ProviderRecord]] = {}
    for r in records:
        for k in _block_keys(r):
            index.setdefault(k, []).append(r)
    return index


# ---------------------------------------------------------------------------
# Pairwise scoring
# ---------------------------------------------------------------------------
def _field_similarity(a: ProviderRecord, b: ProviderRecord) -> dict[str, float]:
    na, nb = normalize_name(a.provider_name) or "", normalize_name(b.provider_name) or ""
    pa, pb = (a.practice_name or "").lower(), (b.practice_name or "").lower()

    aa, ab = normalize_address(a.address) or {}, normalize_address(b.address) or {}
    addr_key_a, addr_key_b = aa.get("key", ""), ab.get("key", "")

    pha, phb = normalize_phone(a.phone) or "", normalize_phone(b.phone) or ""

    return {
        "name": _ratio(na, nb) if na and nb else 0.0,
        "practice": _ratio(pa, pb) if pa and pb else 0.0,
        # address: exact key match is the meaningful signal; partial gets fuzzy
        "address": 1.0 if (addr_key_a and addr_key_a == addr_key_b)
        else (_ratio(addr_key_a, addr_key_b) if addr_key_a and addr_key_b else 0.0),
        "phone": 1.0 if (pha and pha == phb) else 0.0,
    }


def _composite(field_scores: dict[str, float]) -> float:
    return sum(WEIGHTS[f] * s for f, s in field_scores.items())


def _classify(score: float, same_npi: bool) -> tuple[str, str, str]:
    """Return (classification, action, reason)."""
    if same_npi:
        return (
            "exact_duplicate",
            Action.HUMAN_REVIEW.value,
            "Two records share the same NPI — almost certainly the same provider. "
            "Queued for a merge decision (merges are never auto-applied).",
        )
    if score >= AUTO_DUPLICATE_THRESHOLD:
        return (
            "likely_duplicate",
            Action.HUMAN_REVIEW.value,
            f"High identity similarity ({score:.2f}) across name/practice/address. "
            "Strong duplicate candidate; queued for merge confirmation.",
        )
    return (
        "possible_duplicate",
        Action.HUMAN_REVIEW.value,
        f"Moderate similarity ({score:.2f}). Could be a duplicate, a moved "
        "provider, or two clinicians sharing a practice — needs a human.",
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def find_duplicates(
    records: list[ProviderRecord],
    review_floor: float = REVIEW_FLOOR,
) -> list[DuplicateMatch]:
    """
    Scan a directory slice for duplicate / moved-provider candidates.

    Returns matches at or above `review_floor`, plus every same-NPI collision
    regardless of fuzzy score (an NPI collision is decisive on its own). Each
    candidate pair is scored once even if multiple blocks surface it.
    """
    index = _index_by_block(records)
    seen: set[tuple[str, str]] = set()
    matches: list[DuplicateMatch] = []

    for block_key, bucket in index.items():
        if len(bucket) < 2:
            continue
        for a, b in combinations(bucket, 2):
            pair = tuple(sorted((a.provider_id, b.provider_id)))
            if pair in seen:
                continue
            seen.add(pair)

            same_npi = bool(a.npi and a.npi == b.npi)
            fs = _field_similarity(a, b)
            score = _composite(fs)

            if not same_npi and score < review_floor:
                continue

            classification, action, reason = _classify(score, same_npi)
            matches.append(
                DuplicateMatch(
                    record_a=pair[0],
                    record_b=pair[1],
                    score=score,
                    field_scores=fs,
                    same_npi=same_npi,
                    block_key=block_key,
                    classification=classification,
                    recommended_action=action,
                    reason=reason,
                )
            )

    # strongest candidates first
    matches.sort(key=lambda m: (m.same_npi, m.score), reverse=True)
    return matches


def comparison_stats(records: list[ProviderRecord]) -> dict[str, int]:
    """
    Demonstrates the cost win from blocking: naive O(n^2) pair count vs the
    number of pairs we actually compare after blocking.
    """
    n = len(records)
    naive = n * (n - 1) // 2
    index = _index_by_block(records)
    blocked_pairs = set()
    for bucket in index.values():
        for a, b in combinations(bucket, 2):
            blocked_pairs.add(tuple(sorted((a.provider_id, b.provider_id))))
    return {
        "records": n,
        "naive_pairs": naive,
        "blocked_pairs": len(blocked_pairs),
        "pairs_avoided": naive - len(blocked_pairs),
    }
