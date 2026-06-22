"""
Orchestrator — wires the stages together for one record and for a batch.

Per record:
  1. Pull from sources in priority order (free + authoritative first).
  2. EARLY STOP: if the cheap/free sources already agree on every tracked field
     with strong evidence, skip the remaining (costlier / LLM / paid) sources.
  3. Score each field, route to a decision, write the audit log.

The early-stop in step 2 is what keeps the practice-website LLM extraction and
any paid source out of the loop for the common, easy case.
"""
from __future__ import annotations

from dataclasses import dataclass

from .audit import AuditLog
from .confidence import score_field
from .config import AUTO_UPDATE_THRESHOLD, EVIDENCE_K, reliability
from .decision import decide
from .models import ProviderRecord, Recommendation, SourceRecord, TRACKED_FIELDS
from .normalize import normalize_field
from .sources import SourceAdapter, default_sources


@dataclass
class EvalResult:
    recommendation: Recommendation
    sources_used: list[SourceRecord]
    sources_skipped: list[str]


def _fields_settled(record: ProviderRecord, sources: list[SourceRecord]) -> bool:
    """True if the (free) sources fetched so far already pin every tracked field
    well enough that a costlier source couldn't change the decision. Cost guard.

    A field is settled when the sources agree on a single value AND either that
    value confirms the current record (no corroboration needed) or it carries
    enough trusted weight to auto-update on its own.
    """
    import math
    for fname in TRACKED_FIELDS:
        opinions = [(s.source_name, s.fields[fname]) for s in sources
                    if fname in s.fields]
        if not opinions:
            continue
        keys = {normalize_field(fname, v) for _, v in opinions}
        if len(keys) > 1:
            return False  # internal disagreement -> we must pull every source
        value_key = next(iter(keys))
        if value_key == normalize_field(fname, record.get(fname)):
            continue  # confirms current record; no extra source needed
        weight = sum(reliability(name, fname) for name, _ in opinions)
        if (1 - math.exp(-EVIDENCE_K * weight)) < AUTO_UPDATE_THRESHOLD:
            return False  # proposed change not yet strong enough; keep pulling
    return True


class Pipeline:
    def __init__(
        self,
        sources: list[SourceAdapter] | None = None,
        audit_path: str = "audit_log.jsonl",
        auto_apply: bool = True,
    ):
        self.sources = sources or default_sources()
        self.audit = AuditLog(audit_path)
        self.auto_apply = auto_apply

    def evaluate(self, record: ProviderRecord) -> EvalResult:
        used: list[SourceRecord] = []
        skipped: list[str] = []

        for adapter in self.sources:
            # cost guard: before calling a costly or LLM-backed source, check
            # whether the free sources we already have settle every field.
            is_costly = adapter.cost_per_call > 0 or adapter.requires_llm
            if used and is_costly and _fields_settled(record, used):
                skipped.append(adapter.name)
                continue
            sr = adapter.fetch(record.npi)
            if sr:
                used.append(sr)

        changes = []
        for fname in TRACKED_FIELDS:
            fc = score_field(fname, record.get(fname), used)
            if fc:
                changes.append(fc)

        rec = decide(record.provider_id, record.npi, changes)
        applied = self.auto_apply and rec.recommended_action == "auto_update"
        self.audit.write(rec, used, applied=applied)
        return EvalResult(rec, used, skipped)

    def run_batch(self, records: list[ProviderRecord]) -> list[EvalResult]:
        return [self.evaluate(r) for r in records]
