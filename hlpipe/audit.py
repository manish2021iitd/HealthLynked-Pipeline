"""
Audit trail.

Append-only JSONL: one line per evaluated record, capturing what changed, why,
which sources supported it, the confidence, the action taken, and the exact
source observations used. This is the compliance backbone — every auto-update
is reproducible and every human decision has the evidence attached.

In production this writes to an append-only store (e.g. S3 + Glue, or a
write-once DB table) keyed by (provider_id, evaluated_at) and is never mutated.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict
from datetime import datetime
from typing import Optional

from .models import Recommendation, SourceRecord


class AuditLog:
    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)

    def write(
        self,
        rec: Recommendation,
        sources_used: list[SourceRecord],
        applied: bool,
        reviewer: Optional[str] = None,
    ) -> dict:
        entry = {
            "audit_id": f"{rec.provider_id}:{rec.evaluated_at}",
            "provider_id": rec.provider_id,
            "npi": rec.npi,
            "evaluated_at": rec.evaluated_at,
            "recommended_action": rec.recommended_action,
            "applied": applied,
            "reviewer": reviewer,
            "overall_confidence": round(rec.overall_confidence, 4),
            "reason": rec.reason,
            "changes": [asdict(c) for c in rec.changes],
            "evidence": [
                {
                    "source": s.source_name,
                    "observed_at": s.observed_at,
                    "fields": s.fields,
                }
                for s in sources_used
            ],
        }
        with open(self.path, "a") as f:
            f.write(json.dumps(entry) + "\n")
        return entry
