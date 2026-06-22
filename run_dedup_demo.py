"""
Duplicate / moved-provider detection demo (pipeline requirement #5).

Run:  python run_dedup_demo.py

Scans a sample directory slice and reports duplicate candidates, plus the
cost win from blocking (pairs avoided vs a naive all-pairs scan).
"""
import json
import os

from hlpipe import ProviderRecord
from hlpipe import dedup

HERE = os.path.dirname(os.path.abspath(__file__))


def load_sample() -> list[ProviderRecord]:
    with open(os.path.join(HERE, "data", "dedup_sample.json")) as f:
        return [ProviderRecord(**r) for r in json.load(f).values()]


def main() -> None:
    records = load_sample()

    stats = dedup.comparison_stats(records)
    print("=" * 70)
    print("BLOCKING COST CONTROL")
    print("=" * 70)
    print(f"  records           : {stats['records']}")
    print(f"  naive O(n^2) pairs : {stats['naive_pairs']}")
    print(f"  pairs compared     : {stats['blocked_pairs']}")
    print(f"  pairs avoided      : {stats['pairs_avoided']}")
    print("  (at directory scale this is the difference between billions of")
    print("   comparisons and a few million — the reason dedup is affordable)")

    print("\n" + "=" * 70)
    print("DUPLICATE CANDIDATES (all routed to human review — merges are")
    print("never auto-applied because merging is destructive)")
    print("=" * 70)

    matches = dedup.find_duplicates(records)
    if not matches:
        print("  none found")
    for m in matches:
        print(f"\n  {m.record_a}  <->  {m.record_b}   [{m.classification}]")
        print(f"    composite score : {m.score:.3f}   (same NPI: {m.same_npi})")
        print(f"    surfaced via    : block '{m.block_key}'")
        print(f"    field scores    : "
              + ", ".join(f"{k}={v:.2f}" for k, v in m.field_scores.items()))
        print(f"    reason          : {m.reason}")


if __name__ == "__main__":
    main()
