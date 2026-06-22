# HealthLynked — Provider/Practice Directory Refresh Pipeline

A repeatable, cost-efficient pipeline that continuously detects, verifies, and
updates provider and practice records. This repo is a **hybrid submission**: a
runnable prototype (`hlpipe/` + `run_demo.py`) plus this production architecture
write-up.

> TL;DR — Most records can be re-verified for **$0** by leaning on the free
> federal NPI registry first and doing all normalization, matching, and scoring
> in **deterministic code**. LLMs and paid APIs are gated behind that, invoked
> only for the genuinely hard cases. The pipeline auto-applies clean,
> multi-source-corroborated changes and sends only true conflicts to humans.

---

## 1. Run the prototype

```bash
pip install -r requirements.txt        # optional; prototype runs on stdlib alone
python run_demo.py                     # runs the reconciliation pipeline on sample data
python run_dedup_demo.py               # runs duplicate / moved-provider detection
python -m pytest -q                    # 13 tests: normalization, scoring, routing, dedup
```

`run_demo.py` processes four sample providers and demonstrates all paths:

| Record | Situation | Outcome |
|--------|-----------|---------|
| **HL_001** | Address + phone changed, corroborated by NPPES + State Board + practice site | **auto_update** (overall ≈ 0.87) |
| **HL_002** | NPPES + site say new address, State Board still shows old one | **human_review** (conflict, ≈ 0.60) |
| **HL_003** | Every field still matches trusted sources | **no_change** — cost guard *skips* the LLM source |
| **HL_004** | Verified last month | Never enters the run (staleness filter) |

Output matches the requested recommendation schema (with two additions:
`conflicting_sources` and `evaluated_at`). An append-only `audit_log.jsonl` is
written with the full evidence behind every decision.

---

## 2. Pipeline architecture


<img width="1024" height="1536" alt="ChatGPT Image Jun 22, 2026, 06_29_56 PM" src="https://github.com/user-attachments/assets/bd227f82-3c11-4529-8477-2c63ff1b0810" />


Each stage is one module in `hlpipe/`, depending only on the interfaces of its
neighbors, so any piece (a new source, a new scoring model) is swappable.

---

## 3. Data sources (legally accessible, cheap-first)

Authority is **field-specific**, encoded in the reliability matrix in
`config.py`. A source trusted for identity isn't automatically trusted for a
phone number.

| Source | Cost | Strong for | Notes |
|--------|------|-----------|-------|
| **NPPES / NPI Registry** | **Free** API, no key | Identity, NPI, taxonomy/specialty, status | Federal source of truth; the workhorse. Address/phone self-reported, can lag. |
| **CMS Care Compare / PECOS** | **Free** bulk files | Practice address, Medicare enrollment | Downloaded in bulk, joined offline — no per-record cost. |
| **State medical boards** | Free / scrape | License & credential status, name | Authoritative for credentials; format varies by state. |
| **Practice websites** | Free fetch | Freshest phone/address | Unstructured → the one place an LLM earns its cost (extraction). |
| **Google Places / paid dirs** | **Paid** | Phone/address tie-break | Last resort, only to break a conflict. Disabled by default. |
| Third-party aggregators | Cheap/free | Corroboration only | Low trust weight; never decisive alone. |

The live NPPES client is implemented in `sources/NPPESAdapter._fetch_live`
(real endpoint, real parsing). The sandbox can't reach it, so the demo reads
JSON fixtures; flip `use_live=True` in production.

---

## 4. Normalization — the cheapest cost control

Before anything is compared, every value is normalized so that cosmetic
differences are never mistaken for changes:

- **Phones** → E.164 (`phonenumbers`, regex fallback). `(239) 555-9000` == `239-555-9000`.
- **Names** → lowercase, honorific/credential-stripped, `Last, First` reordered, nicknames expanded, initials dropped. `Smith, John M.D.` == `Dr. John Smith` == `John Smith, MD`.
- **Addresses** → parsed to components (`usaddress`), compared on
  `(number, street, city, state, zip5)`. `100 Main St Ste 200` == `100 Main Street` (a suite-only edit is **not** a move); a city/zip change **is**.
- **Specialties** → mapped to **NUCC taxonomy codes**. `Cardiology` == `Cardiovascular Disease`.

Every "is this different?" decision compares normalized keys. This single layer
eliminates the bulk of false positives — and every false positive avoided is a
source call, possible LLM call, and possible human review avoided.

---

## 5. Confidence scoring (deterministic, explainable)

For each field we group all sources' normalized opinions and score the winning
value:

```
S_win    = Σ reliability(source, field)  over sources supporting the winner
S_other  = Σ reliability(source, field)  over sources supporting anything else

agreement  = S_win / (S_win + S_other)        # how clean the signal is
evidence   = 1 - exp(-K · S_win)              # how much trusted weight (K=1.25)
confidence = agreement · evidence
```

Properties that matter:

- **Independent corroboration compounds** — two trusted sources beat one.
- **A lone weak source can't reach high confidence** (saturation curve).
- **A real conflict tanks `agreement`**, pushing the record to review instead
  of an unsafe write. (HL_002: 0.60 → review.)
- **Fully traceable** — every number decomposes into named sources and weights,
  which is exactly what the audit log and a human reviewer need.

No LLM touches scoring; it's reproducible arithmetic. Constants live in
`config.py` and in production are fit on a labeled set of *(proposed change →
was it actually correct?)* and re-tuned periodically.

---

## 6. Decision policy

| Condition | Action |
|-----------|--------|
| No field differs from trusted sources | `no_change` (record re-verified) |
| `overall ≥ 0.85`, ≥ 2 supporting sources, no conflict, no identity/credential change | `auto_update` |
| Any source conflict | `human_review` |
| Change to name / NPI / credential status | `human_review` (policy, regardless of score) |
| Otherwise (low confidence / single source) | `human_review` |

`overall_confidence` is the **min** of field confidences — an update is only as
safe as its weakest change. Thresholds are configurable per deployment risk
appetite.

---

## 6.5 Duplicate / moved-provider detection (`hlpipe/dedup.py`)

The reconciliation pipeline above fixes a *known* record against external
sources keyed by NPI. `dedup.py` solves the complementary problem in
requirement #5: finding records that are secretly the **same** provider/practice
under different `provider_id`s — the classic "entered twice after a move" or
"practice rebranded" rot.

- **Blocking is the cost control.** All-pairs comparison is O(n²) (250k records ≈
  31B pairs — unrunnable). Records are bucketed into cheap blocks — `npi`,
  `zip5 + last-name`, `specialty + city` — and only pairs sharing a block are
  scored. Multiple block families keep recall high when any single field is
  wrong or missing. `comparison_stats()` reports the pairs avoided.
- **Deterministic scoring**, no LLM: a weighted blend of normalized
  name / practice / address-key / phone similarity (`rapidfuzz` when present,
  `difflib` fallback).
- **Conservative routing.** A shared NPI is an `exact_duplicate`; a high fuzzy
  score is a `likely_duplicate`; the middle band is `possible_duplicate`. **All**
  go to human review — merges are destructive and are never auto-applied. This
  mirrors the main pipeline's "uncertain → human" stance.

`run_dedup_demo.py` catches a same-NPI pair that moved practices (low fuzzy
score, surfaced purely by the NPI block) and a rebranded "Last, First" duplicate
scoring 0.99 with no NPI match.

---

## 7. Cost controls (the core ask)

1. **Staleness front door** (`staleness.py`). The whole directory is never
   re-checked. Records are scored by `recency_pressure + traffic_value +
   hard_signal_boost`, ranked, and capped at a daily budget. External calls,
   LLM usage, and review effort all scale with *how many records we touch* —
   this is the biggest lever.
2. **Free + authoritative first, with early stop** (`orchestrator.py`). Sources
   are ordered cheapest/most-trusted first. Once the free sources settle every
   field, the costlier/LLM-backed sources are **skipped** (demo: HL_003 skips
   the practice-site LLM extraction).
3. **Deterministic-by-default, LLM-last.** Normalization, matching, scoring, and
   routing are all plain code — fast, free, reproducible. The LLM is used for
   exactly one thing: extracting structured fields from messy practice-site
   HTML, and only when cheaper signals are insufficient.
4. **Normalization kills false positives** before they cost anything (§4).
5. **Caching / change-detection.** Source fetches and LLM extractions are cached
   by content hash + ETag; an unchanged page or unchanged NPPES record costs
   nothing on the next run.
6. **Review-queue minimization.** Confirmations are suppressed entirely; only
   genuine conflicts and high-impact/low-confidence changes reach a human.

Expected effect: the large majority of records resolve through free sources and
deterministic logic at ~$0; LLM and paid-API spend concentrate on the small
fraction that are both changed *and* ambiguous.

---

## 8. Scaling to production

- **Orchestration.** Run as a scheduled batch (Airflow / Step Functions / cron).
  The `Pipeline` is stateless per record and trivially parallelizable; shard the
  prioritized queue across workers.
- **Entity resolution at scale.** With NPI present, matching is a deterministic
  key. For NPI-less or duplicate detection, **block** on `(zip5, specialty,
  name-soundex)` then fuzzy-match within blocks (`rapidfuzz`) to stay sub-O(n²).
- **Storage.** Directory in Postgres; audit log append-only to S3/Parquet
  (immutable, queryable for compliance); CMS bulk files staged in object storage
  and joined offline.
- **Human-in-the-loop.** `human_review` items flow to a queue (e.g. a small web
  app or a Label-Studio-style tool) pre-loaded with the side-by-side evidence
  from the audit entry, so a reviewer decides in seconds. Decisions feed back as
  labels to re-tune reliability weights and thresholds.
- **Feedback loop.** Track auto-update reversal rate; if a source's auto-updates
  get reversed often, its reliability weight is lowered automatically.
- **Compliance & safety.** Only public/licensed sources; every write is
  reproducible from the audit log; identity and credential changes always get
  human eyes; provenance (which source, observed when) is retained per field.
- **Observability.** Per-run metrics: records processed, auto-update rate,
  review rate, reversal rate, $ source cost, LLM tokens, cache-hit rate.

---

## 9. Layout

```
hlpipe/
  models.py        data contracts (ProviderRecord, SourceRecord, Recommendation)
  config.py        reliability matrix, thresholds, scoring constants (all tunable)
  staleness.py     prioritization / daily budget  (cost front door)
  sources.py       adapters: live NPPES client + mock-backed sources
  normalize.py     names · addresses · phones · specialties
  confidence.py    per-field evidence scoring
  decision.py      no_change / auto_update / human_review routing
  dedup.py         duplicate / moved-provider detection (blocking + fuzzy match)
  audit.py         append-only audit log
  orchestrator.py  ties stages together with the early-stop cost guard
data/              sample directory + mock source responses + dedup sample
run_demo.py        end-to-end reconciliation demo
run_dedup_demo.py  duplicate-detection demo
tests/             pytest suite (13 tests)
```
