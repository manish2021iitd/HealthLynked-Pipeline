"""
Core data contracts that flow through the pipeline.

Everything is a plain dataclass so it serializes cleanly to JSON for the
audit log and the review queue, and so the pipeline stays framework-agnostic.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timezone
from enum import Enum
from typing import Any, Optional


# Fields we actively track and reconcile. Keeping this explicit means a new
# source can only ever write into a known slot — no silent schema drift.
TRACKED_FIELDS = (
    "provider_name",
    "specialty",
    "practice_name",
    "address",
    "phone",
    "credential_status",
)


class Action(str, Enum):
    NO_CHANGE = "no_change"
    AUTO_UPDATE = "auto_update"
    HUMAN_REVIEW = "human_review"


@dataclass
class ProviderRecord:
    """A record as it currently exists in the HealthLynked directory."""
    provider_id: str
    npi: str
    provider_name: str
    specialty: str
    practice_name: str
    address: str
    phone: str
    last_verified_date: str
    credential_status: str = "active"
    # operational signals used for staleness prioritization (cost control)
    monthly_search_volume: int = 0
    hard_signals: list[str] = field(default_factory=list)  # e.g. ["returned_mail"]

    def get(self, fname: str) -> Any:
        return getattr(self, fname, None)


@dataclass
class SourceRecord:
    """
    A normalized observation about a provider, pulled from one external source.
    `fields` only contains the slots that source actually reports.
    """
    source_name: str
    npi: str
    fields: dict[str, Any]
    observed_at: str  # ISO date the source last refreshed this data
    raw: dict[str, Any] = field(default_factory=dict)  # provenance / debugging


@dataclass
class FieldChange:
    field: str
    old_value: Any
    new_value: Any
    confidence_score: float
    supporting_sources: list[str]
    conflicting_sources: list[str] = field(default_factory=list)


@dataclass
class Recommendation:
    provider_id: str
    npi: str
    change_detected: bool
    changes: list[FieldChange]
    overall_confidence: float
    recommended_action: str
    reason: str
    evaluated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        # round floats for clean JSON output
        d["overall_confidence"] = round(self.overall_confidence, 2)
        for c in d["changes"]:
            c["confidence_score"] = round(c["confidence_score"], 2)
        return d
