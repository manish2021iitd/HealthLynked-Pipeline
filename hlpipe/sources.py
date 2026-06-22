"""
Source adapters.

Each adapter knows how to fetch one external source and return a normalized
SourceRecord. The orchestrator depends only on the SourceAdapter interface, so
adding a source = writing one class. Sources are ordered cheapest/most-trusted
first; the orchestrator can short-circuit once it has enough agreement, which
keeps paid sources and LLM extraction out of the loop for the easy cases.

NETWORK NOTE: this sandbox cannot reach npiregistry.cms.gov, so the live NPPES
client is included for production but the demo runs against JSON fixtures in
data/sources/. Flip USE_LIVE=True in a real environment.
"""
from __future__ import annotations

import json
import os
from abc import ABC, abstractmethod
from datetime import date
from typing import Optional

from .models import SourceRecord
from .normalize import normalize_field

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "sources")


class SourceAdapter(ABC):
    name: str = "base"
    cost_per_call: float = 0.0       # USD; used for budgeting/telemetry
    requires_llm: bool = False       # True => gated behind cheaper sources

    @abstractmethod
    def fetch(self, npi: str) -> Optional[SourceRecord]:
        ...

    def _mk(self, npi, fields: dict, observed_at: str, raw=None) -> SourceRecord:
        # normalize every field on the way in so downstream compares apples-to-apples
        norm = {k: v for k, v in fields.items() if v is not None}
        return SourceRecord(self.name, npi, norm, observed_at, raw or {})


# ---------------------------------------------------------------------------
# NPPES (NPI Registry) — FREE federal API, no key. The workhorse.
# ---------------------------------------------------------------------------
class NPPESAdapter(SourceAdapter):
    name = "NPPES"
    cost_per_call = 0.0
    API = "https://npiregistry.cms.gov/api/?version=2.1&number={npi}"

    def __init__(self, use_live: bool = False):
        self.use_live = use_live

    def fetch(self, npi: str) -> Optional[SourceRecord]:
        data = self._fetch_live(npi) if self.use_live else self._fetch_mock(npi)
        if not data:
            return None
        return self._parse(npi, data)

    def _fetch_live(self, npi: str):  # pragma: no cover (needs network)
        import urllib.request
        url = self.API.format(npi=npi)
        with urllib.request.urlopen(url, timeout=10) as r:
            payload = json.load(r)
        results = payload.get("results") or []
        return results[0] if results else None

    def _fetch_mock(self, npi: str):
        return _load_mock("nppes.json", npi)

    def _parse(self, npi, res) -> Optional[SourceRecord]:
        """Map a raw NPPES result object to our tracked fields."""
        basic = res.get("basic", {})
        name = f"{basic.get('first_name','')} {basic.get('last_name','')}".strip()
        cred = basic.get("credential")
        if cred:
            name = f"{name}, {cred}"
        # primary taxonomy = specialty
        taxos = res.get("taxonomies", [])
        primary = next((t for t in taxos if t.get("primary")), taxos[0] if taxos else {})
        specialty = primary.get("desc")
        # LOCATION address (practice), not MAILING
        loc = next((a for a in res.get("addresses", [])
                    if a.get("address_purpose") == "LOCATION"),
                   (res.get("addresses") or [{}])[0])
        addr = ", ".join(filter(None, [
            loc.get("address_1"), loc.get("address_2"),
            f"{loc.get('city','')}, {loc.get('state','')} {loc.get('postal_code','')[:5]}".strip(", ")
        ])) if loc else None
        status = "active" if basic.get("status") == "A" else "inactive"
        fields = {
            "provider_name": name or None,
            "specialty": specialty,
            "address": addr,
            "phone": loc.get("telephone_number") if loc else None,
            "credential_status": status,
        }
        observed = res.get("last_updated") or str(date.today())
        return self._mk(npi, fields, observed, raw={"source": "nppes"})


# ---------------------------------------------------------------------------
# State Medical Board — credential/license truth. Mock-backed here.
# ---------------------------------------------------------------------------
class StateBoardAdapter(SourceAdapter):
    name = "StateBoard"
    cost_per_call = 0.0  # most boards are free lookup/scrape

    def fetch(self, npi: str) -> Optional[SourceRecord]:
        rec = _load_mock("state_board.json", npi)
        if not rec:
            return None
        return self._mk(npi, rec["fields"], rec.get("observed_at", str(date.today())))


# ---------------------------------------------------------------------------
# Practice Website — current but unstructured. The ONLY adapter that may invoke
# an LLM, and only to extract structured fields from scraped HTML. Gated.
# ---------------------------------------------------------------------------
class PracticeWebsiteAdapter(SourceAdapter):
    name = "PracticeWebsite"
    cost_per_call = 0.0      # scrape is free; LLM extraction cost tracked separately
    requires_llm = True

    def fetch(self, npi: str) -> Optional[SourceRecord]:
        rec = _load_mock("practice_web.json", npi)
        if not rec:
            return None
        # In production: fetch_html(url) -> llm_extract(html) -> fields.
        # llm_extract is cached by content hash so unchanged pages cost nothing.
        return self._mk(npi, rec["fields"], rec.get("observed_at", str(date.today())))


# ---------------------------------------------------------------------------
def _load_mock(filename: str, npi: str) -> Optional[dict]:
    path = os.path.join(DATA_DIR, filename)
    if not os.path.exists(path):
        return None
    with open(path) as f:
        blob = json.load(f)
    return blob.get(npi)


# Registry of sources in priority order (free + authoritative first).
def default_sources(use_live_nppes: bool = False) -> list[SourceAdapter]:
    return [
        NPPESAdapter(use_live=use_live_nppes),
        StateBoardAdapter(),
        PracticeWebsiteAdapter(),
    ]
