"""Offline paired comparison, field errors and representative synthetic cases."""

import argparse
import csv
import json
from pathlib import Path

from .core import decide
from .run import canonical, summarize


def analyze(folder, data):
    rows = json.loads((data / "cases.json").read_text("utf8"))
    lookup = {r["id"]: r for r in rows}
    results = {}
    records = {}
    for key in ("a", "b_direct", "b_hybrid", "c"):
        values = json.loads((folder / (key + "-predictions.json")).read_text("utf8"))
        records[key] = {r["id"]: r for r in values}
        test = [r for r in values if lookup[r["id"]]["split"] == "test"]
        stats = summarize(values, rows)
        stats["fields"] = {
            f: sum(
                r["format_valid"]
                and (
                    sorted(set(r["semantic"][f])) == sorted(set(lookup[r["id"]]["semantic"][f]))
                    if f == "signals"
                    else r["semantic"][f] == lookup[r["id"]]["semantic"][f]
                )
                for r in test
            )
            for f in ("speech_act", "signals", "target_hint", "polarity")
        }
        stats["paired_vs_a"] = {
            "wins": sum(
                r["strict_correct"] and not records["a"][r["id"]]["strict_correct"] for r in test
            ),
            "losses": sum(
                not r["strict_correct"] and records["a"][r["id"]]["strict_correct"] for r in test
            ),
        }
        examples = {}
        for kind, predicate in {
            "correct": lambda r: r["strict_correct"],
            "errors": lambda r: not r["strict_correct"],
            "unsafe": lambda r: r["false_execution"],
            "rescued_by_context": lambda r: r["strict_correct"] and not r["semantic_correct"],
        }.items():
            examples[kind] = [
                {
                    "id": r["id"],
                    "text": lookup[r["id"]]["text"],
                    "state": lookup[r["id"]]["state"],
                    "gold_semantic": lookup[r["id"]]["semantic"],
                    "semantic": r["semantic"],
                    "gold": lookup[r["id"]]["gold"],
                    "plan": r["plan"],
                }
                for r in test
                if predicate(r)
            ][:12]
        results[key] = {"statistics": stats, "examples": examples}
    # Oracle is a simulator consistency check, not a learned backend accuracy.
    oracle = [
        r["id"]
        for r in rows
        if canonical(decide(r["semantic"], r["state"], r["text"])) != canonical(r["gold"])
    ]
    return {
        "pipelines": results,
        "oracle_decision_mismatches": oracle,
        "warning": "All playback and preference effects are proposed plans on synthetic snapshots, never executed.",
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("folder", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--data", type=Path, default=Path(__file__).parent / "data")
    args = parser.parse_args()
    result = analyze(args.folder, args.data)
    args.output.mkdir(exist_ok=True, parents=True)
    target = args.output / "comparison.json"
    if target.exists():
        raise FileExistsError("preserve the previous analysis")
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf8")
    fields = [
        "pipeline",
        "n",
        "semantic_correct",
        "action_correct",
        "strict_correct",
        "false_execution",
        "wrong_rejection",
        "p50_ms",
        "p95_ms",
    ]
    with (args.output / "comparison.csv").open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for key, data in result["pipelines"].items():
            s = data["statistics"]
            writer.writerow(
                {
                    "pipeline": key,
                    **{k: s[k] for k in fields[1:7]},
                    "p50_ms": s["latency_ms"]["p50"],
                    "p95_ms": s["latency_ms"]["p95"],
                }
            )
    print("Offline comparison written; no model requests.")


if __name__ == "__main__":
    main()
