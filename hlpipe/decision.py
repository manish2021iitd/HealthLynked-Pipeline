"""
Decision routing.

Turns a set of scored FieldChanges into one of three actions. The rules are
deliberately conservative — when in doubt, route to a human. The cost lever is
that *only genuine conflicts and high-impact / low-confidence changes* reach the
review queue; clean high-confidence changes auto-apply, and confirmations are
suppressed entirely.
"""
from __future__ import annotations

from .config import (
    AUTO_UPDATE_THRESHOLD,
    HIGH_IMPACT_FIELDS,
    MIN_SOURCES_FOR_AUTO,
    REVIEW_FLOOR,
)
from .models import Action, FieldChange, Recommendation


def _overall(changes: list[FieldChange]) -> float:
    if not changes:
        return 1.0
    # conservative aggregate: the update is only as safe as its weakest change
    return min(c.confidence_score for c in changes)


def decide(provider_id: str, npi: str, changes: list[FieldChange]) -> Recommendation:
    if not changes:
        return Recommendation(
            provider_id, npi, change_detected=False, changes=[],
            overall_confidence=1.0,
            recommended_action=Action.NO_CHANGE.value,
            reason="All evaluated fields match trusted sources; record re-verified.",
        )

    overall = _overall(changes)
    has_conflict = any(c.conflicting_sources for c in changes)
    touches_high_impact = any(c.field in HIGH_IMPACT_FIELDS for c in changes)
    enough_sources = all(
        len(c.supporting_sources) >= MIN_SOURCES_FOR_AUTO for c in changes
    )

    # --- conflict between sources => never auto-update -----------------------
    if has_conflict:
        conflicted = next(c for c in changes if c.conflicting_sources)
        return Recommendation(
            provider_id, npi, True, changes, overall,
            Action.HUMAN_REVIEW.value,
            reason=(
                f"Sources disagree on '{conflicted.field}' "
                f"({', '.join(conflicted.supporting_sources)} vs "
                f"{', '.join(conflicted.conflicting_sources)}). "
                "Manual verification recommended."
            ),
        )

    # --- high-impact identity/credential changes always get eyes ------------
    if touches_high_impact:
        return Recommendation(
            provider_id, npi, True, changes, overall,
            Action.HUMAN_REVIEW.value,
            reason="Change affects identity or credential status; routed to review by policy.",
        )

    # --- clean, well-supported, high-confidence => auto ---------------------
    if overall >= AUTO_UPDATE_THRESHOLD and enough_sources:
        fields = ", ".join(c.field for c in changes)
        srcs = sorted({s for c in changes for s in c.supporting_sources})
        return Recommendation(
            provider_id, npi, True, changes, overall,
            Action.AUTO_UPDATE.value,
            reason=(
                f"Updated {fields} confirmed by multiple reliable sources "
                f"({', '.join(srcs)})."
            ),
        )

    # --- everything else => review with the reason why ----------------------
    if overall < REVIEW_FLOOR:
        reason = "Low confidence / insufficient corroboration."
    else:
        reason = "Confidence below auto-update threshold or single-source change."
    return Recommendation(
        provider_id, npi, True, changes, overall,
        Action.HUMAN_REVIEW.value, reason=reason,
    )
