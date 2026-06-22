"""
HealthLynked directory-maintenance pipeline — end-to-end demo.

Run:  python run_demo.py
Outputs the structured recommendation for each provider and a run summary,
and writes an append-only audit log to ./audit_log.jsonl.
"""
import json
import os

from hlpipe import Pipeline, ProviderRecord, build_queue, priority_score

HERE = os.path.dirname(os.path.abspath(__file__))


def load_directory() -> list[ProviderRecord]:
    with open(os.path.join(HERE, "data", "hl_directory.json")) as f:
        raw = json.load(f)
    return [ProviderRecord(**r) for r in raw.values()]


def main():
    records = load_directory()

    # ---- Stage 1: cost-control front door -------------------------------
    print("=" * 70)
    print("STAGE 1  Staleness / risk prioritization (which records to check)")
    print("=" * 70)
    for r in records:
        print(f"  {r.provider_id}  priority={priority_score(r):.2f}  "
              f"last_verified={r.last_verified_date}  signals={r.hard_signals}")
    queue = build_queue(records)
    print(f"\n  -> {len(queue)} of {len(records)} records enter this run "
          f"(recently-verified records are skipped to save cost):")
    print("     " + ", ".join(r.provider_id for r in queue))

    # ---- Stages 2-9: verify, score, decide, audit -----------------------
    pipe = Pipeline(audit_path=os.path.join(HERE, "audit_log.jsonl"))

    summary = {"no_change": 0, "auto_update": 0, "human_review": 0}
    print("\n" + "=" * 70)
    print("STAGES 2-9  Verify -> normalize -> score -> decide -> audit")
    print("=" * 70)
    for r in queue:
        result = pipe.evaluate(r)
        rec = result.recommendation
        summary[rec.recommended_action] += 1
        print(f"\n----- {r.provider_id} ({r.provider_name}) -----")
        if result.sources_skipped:
            print(f"  cost guard: skipped {result.sources_skipped} "
                  f"(free sources already settled the record)")
        print(f"  sources used: {[s.source_name for s in result.sources_used]}")
        print(json.dumps(rec.to_dict(), indent=2))

    print("\n" + "=" * 70)
    print("RUN SUMMARY")
    print("=" * 70)
    print(f"  auto_update : {summary['auto_update']}")
    print(f"  human_review: {summary['human_review']}")
    print(f"  no_change   : {summary['no_change']}")
    print(f"  audit log   : {os.path.join(HERE, 'audit_log.jsonl')}")


if __name__ == "__main__":
    main()
