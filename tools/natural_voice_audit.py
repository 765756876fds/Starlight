"""Replay frozen synthetic language only; never instantiate runtime or execute commands."""

import argparse
import hashlib
import json
from pathlib import Path
import statistics
import sys
import time


def audit(path, repeats=10):
    from app.voice.intent import IntentRouter
    from app.voice.natural_language import extract

    data = Path(path).read_bytes().replace(b"\r\n", b"\n")
    rows = json.loads(data)["cases"]
    predictions = []
    for row in rows:
        meaning = extract(row["text"])
        actual = (
            {"action": meaning.action, "target": meaning.target, "rating": meaning.rating}
            if meaning
            else {"action": "none", "target": "", "rating": ""}
        )
        keys = [k for k in actual if k in row]
        kind = IntentRouter({}).parse(row["text"]).intent_type
        predictions.append(
            dict(row, actual=actual, correct=all(row[k] == actual[k] for k in keys), router=kind)
        )
    timings = []
    for _ in range(repeats):
        for row in rows:
            start = time.perf_counter_ns()
            IntentRouter({}).parse(row["text"])
            timings.append((time.perf_counter_ns() - start) / 1e6)
    rejects = [r for r in predictions if r["action"] == "none"]
    return dict(
        sha256_lf=hashlib.sha256(data).hexdigest(),
        total=len(rows),
        strict_correct=sum(r["correct"] for r in predictions),
        reject_cases=len(rejects),
        unsafe_candidates=sum(
            r["router"] not in ("system.unknown", "system.clarify") for r in rejects
        ),
        p50_ms=statistics.median(timings),
        p95_ms=sorted(timings)[int(len(timings) * 0.95) - 1],
        samples=len(timings),
        predictions=predictions,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    result = audit(args.cases)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({k: v for k, v in result.items() if k != "predictions"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
