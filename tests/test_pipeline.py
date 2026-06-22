import json
import os

import pytest

from hlpipe import Pipeline, ProviderRecord
from hlpipe.normalize import (
    normalize_address, normalize_name, normalize_phone, normalize_specialty,
)

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def _record(pid):
    with open(os.path.join(ROOT, "data", "hl_directory.json")) as f:
        return ProviderRecord(**json.load(f)[pid])


# --- normalization kills cosmetic diffs -------------------------------------
def test_phone_normalization_equal():
    assert normalize_phone("(239) 555-9000") == normalize_phone("239-555-9000")
    assert normalize_phone("239.555.2020") == normalize_phone("239-555-2020")


def test_name_normalization_equal():
    assert normalize_name("John Smith, MD") == normalize_name("Smith, John M.D.")
    assert normalize_name("Dr. John Smith") == normalize_name("John Smith, MD")


def test_specialty_crosswalk():
    assert normalize_specialty("Cardiology") == normalize_specialty("Cardiovascular Disease")


def test_address_suite_change_not_a_move():
    a = normalize_address("100 Main St Ste 200, Naples, FL 34102")["key"]
    b = normalize_address("100 Main Street, Naples, FL 34102")["key"]
    assert a == b


def test_address_real_move_detected():
    a = normalize_address("100 Main St, Naples, FL 34102")["key"]
    b = normalize_address("250 Health Park Dr, Fort Myers, FL 33908")["key"]
    assert a != b


# --- end-to-end decisions ---------------------------------------------------
@pytest.fixture
def pipe(tmp_path):
    return Pipeline(audit_path=str(tmp_path / "audit.jsonl"))


def test_example_record_auto_update(pipe):
    rec = pipe.evaluate(_record("HL_001")).recommendation
    assert rec.recommended_action == "auto_update"
    changed = {c.field for c in rec.changes}
    assert {"address", "phone"} <= changed
    assert rec.overall_confidence >= 0.85
    # address change must be multi-source
    addr = next(c for c in rec.changes if c.field == "address")
    assert len(addr.supporting_sources) >= 2


def test_conflict_routes_to_human_review(pipe):
    rec = pipe.evaluate(_record("HL_002")).recommendation
    assert rec.recommended_action == "human_review"
    assert "disagree" in rec.reason.lower() or "conflict" in rec.reason.lower()


def test_confirmed_record_no_change(pipe):
    result = pipe.evaluate(_record("HL_003"))
    assert result.recommendation.recommended_action == "no_change"
    # cost guard should have skipped the LLM-backed website source
    assert "PracticeWebsite" in result.sources_skipped


def test_audit_log_written(pipe, tmp_path):
    pipe.evaluate(_record("HL_001"))
    lines = open(pipe.audit.path).read().strip().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["provider_id"] == "HL_001"
    assert entry["evidence"]  # source observations captured


# --- duplicate / moved-provider detection (requirement #5) ------------------
def _dedup_records():
    with open(os.path.join(ROOT, "data", "dedup_sample.json")) as f:
        return [ProviderRecord(**r) for r in json.load(f).values()]


def test_same_npi_pair_flagged_exact_duplicate():
    from hlpipe import dedup
    matches = dedup.find_duplicates(_dedup_records())
    pair = next(m for m in matches if {m.record_a, m.record_b} == {"HL_001", "HL_005"})
    # caught purely on NPI even though the provider moved practices (low fuzzy score)
    assert pair.same_npi is True
    assert pair.classification == "exact_duplicate"
    assert pair.recommended_action == "human_review"  # merges are never auto-applied


def test_fuzzy_pair_flagged_without_npi_match():
    from hlpipe import dedup
    matches = dedup.find_duplicates(_dedup_records())
    pair = next(m for m in matches if {m.record_a, m.record_b} == {"HL_003", "HL_006"})
    assert pair.same_npi is False
    assert pair.score >= dedup.AUTO_DUPLICATE_THRESHOLD
    assert pair.classification == "likely_duplicate"


def test_distinct_records_not_flagged():
    from hlpipe import dedup
    matches = dedup.find_duplicates(_dedup_records())
    flagged = {frozenset((m.record_a, m.record_b)) for m in matches}
    # HL_007 (Patel/Pediatrics/Bonita Springs) shares no block with anyone
    assert not any("HL_007" in p for p in flagged)


def test_blocking_reduces_comparisons():
    from hlpipe import dedup
    stats = dedup.comparison_stats(_dedup_records())
    assert stats["blocked_pairs"] < stats["naive_pairs"]
    assert stats["pairs_avoided"] > 0
