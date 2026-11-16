"""Explicit, aggregate-only inspection of existing user data; never create DBs."""
import argparse
import json
from pathlib import Path
import time

from .readonly import collections, preference_summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--music-db", required=True)
    parser.add_argument("--preferences", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    measured = time.perf_counter_ns()
    started = time.monotonic()
    rows, inventory = collections(args.music_db, now=started, deadline=started+.5)
    notes, explicit, history = preference_summary(args.preferences, now=started,
                                                 deadline=started+.5)
    report = dict(scope="explicit_database_read_only_no_live_controller", collection_items_observed=len(rows),
                  inventory_state=inventory.state.value, inventory_reason=inventory.reason,
                  playable_items_confirmed=0, explicit_artist_evidence_count=len(explicit),
                  historical_artist_evidence_count=len(history), preference_notes=notes,
                  elapsed_ms=(time.perf_counter_ns()-measured)/1e6,
                  execution_performed=False, production_hook_installed=False)
    destination = Path(args.output)
    if destination.exists():
        raise SystemExit("refusing_to_overwrite_evidence")
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf8")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
