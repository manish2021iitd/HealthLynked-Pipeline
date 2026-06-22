"""
All tunable policy lives here so it can be version-controlled, A/B tested, and
audited. In production these constants are fit on a labeled validation set
(proposed change -> was it actually correct?) and re-tuned periodically.

PER-FIELD reliability matrix
----------------------------
A flat "this source is trustworthy" number is wrong, because authority is
field-specific. The federal NPI registry (NPPES) is authoritative for identity
and taxonomy, but its *practice address/phone* often lag because providers
self-report and don't update promptly. A practice's own website is messy and
unstructured, but its contact info is usually the freshest. A state medical
board is the ground truth for license/credential status. We encode exactly
that nuance below.
"""

# reliability[source][field] in [0, 1]; falls back to "default" per source.
RELIABILITY: dict[str, dict[str, float]] = {
    "NPPES": {  # CMS National Plan & Provider Enumeration System (free API)
        "default": 0.92,
        "npi": 1.00,
        "provider_name": 0.95,
        "specialty": 0.95,
        "credential_status": 0.85,
        "address": 0.85,   # self-reported, can lag
        "phone": 0.80,
    },
    "StateBoard": {  # state medical licensing board
        "default": 0.85,
        "credential_status": 0.97,
        "provider_name": 0.92,
        "specialty": 0.85,
        "address": 0.75,
    },
    "CMS_CareCompare": {  # Medicare enrollment / Care Compare bulk files (free)
        "default": 0.85,
        "address": 0.85,
        "specialty": 0.88,
        "practice_name": 0.85,
    },
    "PracticeWebsite": {  # scraped + extracted; current but noisy
        "default": 0.70,
        "phone": 0.85,     # freshest contact info
        "address": 0.80,
        "practice_name": 0.80,
        "provider_name": 0.65,
    },
    "GooglePlaces": {  # paid; used only as a tie-breaker (see cost controls)
        "default": 0.60,
        "phone": 0.75,
        "address": 0.70,
        "practice_name": 0.70,
    },
    "Aggregator": {  # third-party directories; low trust, corroboration only
        "default": 0.40,
    },
}

DEFAULT_RELIABILITY = 0.50


def reliability(source: str, fname: str) -> float:
    table = RELIABILITY.get(source, {})
    return table.get(fname, table.get("default", DEFAULT_RELIABILITY))


# --- Confidence scoring constants -------------------------------------------
# Evidence saturation rate. Higher K => fewer corroborating sources needed to
# reach high confidence. Tuned so a single top-authority source lands ~0.6 and
# 2-3 agreeing sources land ~0.85-0.95.
EVIDENCE_K = 1.25

# --- Decision thresholds -----------------------------------------------------
AUTO_UPDATE_THRESHOLD = 0.85   # >= this AND no conflict => auto-approve
REVIEW_FLOOR = 0.55            # below this with no corroboration => low-confidence review
MIN_SOURCES_FOR_AUTO = 2       # never auto-update on a single source

# --- Staleness / cost controls ----------------------------------------------
STALE_AFTER_DAYS = 180         # records older than this become eligible to re-check
DAILY_RECHECK_BUDGET = 5000    # cap how many records we re-verify per run
# Fields whose change is "high impact" and should bias a record toward review
HIGH_IMPACT_FIELDS = ("provider_name", "npi", "credential_status")
