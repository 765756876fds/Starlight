"""Frozen, sequential, loopback-only experiment. No real media commands."""

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from tools.semantic_lab.engine import Resources, Server
from tools.semantic_lab.run import check_free_memory, percentile, resource_summary

from .backends import (
    VectorIndex,
    development_examples,
    feature_assist,
    generate,
    prompt,
    prototypes,
    threshold,
)
from .core import ACTIONS, SCHEMA, decide, lexical, valid


def canonical(plan):
    return {k: plan[k] for k in ("disposition", "actions", "params", "weak_feedback")}


def sem_equal(a, b):
    return (
        valid(a)
        and valid(b)
        and {**a, "signals": sorted(set(a["signals"]))}
        == {**b, "signals": sorted(set(b["signals"]))}
    )


def record(row, value, elapsed, detail=None, error=None):
    okay = valid(value) and error is None
    proposed = decide(value, row["state"], row["text"])
    oracle = decide(row["semantic"], row["state"], row["text"])
    gold = row["gold"]
    action_correct = (
        proposed["disposition"] == gold["disposition"] and proposed["actions"] == gold["actions"]
    )
    return {
        "id": row["id"],
        "semantic": value,
        "plan": proposed,
        "format_valid": okay,
        "latency_ms": elapsed,
        "detail": detail,
        "error": error,
        "semantic_correct": sem_equal(value, row["semantic"]) and okay,
        "action_correct": action_correct and okay,
        "strict_correct": canonical(proposed) == canonical(gold) and okay,
        "wrong_rejection": gold["disposition"] == "execute"
        and proposed["disposition"] != "execute",
        "false_execution": gold["disposition"] != "execute"
        and proposed["disposition"] == "execute",
        "semantic_error": not sem_equal(value, row["semantic"]),
        "decision_error": canonical(oracle) != canonical(gold),
        "parameter_error": action_correct and canonical(proposed) != canonical(gold),
    }


def summarize(records, rows):
    lookup = {r["id"]: r for r in rows}
    chosen = [r for r in records if lookup[r["id"]]["split"] == "test"]
    result = {"n": len(chosen)}
    for field in (
        "format_valid",
        "semantic_correct",
        "action_correct",
        "strict_correct",
        "wrong_rejection",
        "false_execution",
        "semantic_error",
        "decision_error",
        "parameter_error",
    ):
        result[field] = sum(bool(r[field]) for r in chosen)
    result["failures"] = sum(r["error"] is not None for r in chosen)
    for name, predicate in {
        "implicit": lambda r: r["implicit"],
        "executable": lambda r: r["gold"]["disposition"] == "execute",
        "reject": lambda r: r["gold"]["disposition"] != "execute",
        "negative_question_report": lambda r: (
            r["semantic"]["polarity"] == "negative"
            or r["semantic"]["speech_act"] in ("question", "reported")
        ),
    }.items():
        part = [r for r in chosen if predicate(lookup[r["id"]])]
        result[name] = {
            "n": len(part),
            **{
                key: sum(bool(r[key]) for r in part)
                for key in (
                    "semantic_correct",
                    "strict_correct",
                    "false_execution",
                    "wrong_rejection",
                )
            },
        }
    times = [r["latency_ms"] for r in chosen]
    result["latency_ms"] = {"p50": percentile(times, 0.5), "p95": percentile(times, 0.95)}
    result["categories"] = {
        cat: {
            "n": sum(lookup[r["id"]]["category"] == cat for r in chosen),
            "semantic_correct": sum(
                r["semantic_correct"] for r in chosen if lookup[r["id"]]["category"] == cat
            ),
            "strict_correct": sum(
                r["strict_correct"] for r in chosen if lookup[r["id"]]["category"] == cat
            ),
        }
        for cat in sorted({r["category"] for r in rows})
    }
    return result


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def measure_a(rows, output):
    sampler = Resources(os.getpid())
    try:
        time.sleep(2)
        idle = copy.deepcopy(sampler.samples[-8:])
        records = []
        for row in rows:
            times = []
            for _ in range(20):
                start = time.perf_counter_ns()
                value = lexical(row["text"])
                decide(value, row["state"], row["text"])
                times.append((time.perf_counter_ns() - start) / 1e6)
            records.append(record(row, value, percentile(times, 0.5)))
        time.sleep(0.3)
    finally:
        sampler.close()
    startup = []
    for _ in range(3):
        start = time.perf_counter_ns()
        result = subprocess.run(
            [
                sys.executable,
                "-B",
                "-c",
                "import time;t=time.perf_counter_ns();import tools.semantic_light.core;print((time.perf_counter_ns()-t)/1e6)",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        startup.append(
            {
                "import_ms": float(result.stdout.strip()),
                "process_launch_import_exit_ms": (time.perf_counter_ns() - start) / 1e6,
            }
        )
    save(output / "a-resources.json", sampler.samples)
    save(output / "a-startup.json", startup)
    save(output / "a-predictions.json", records)
    return {
        **summarize(records, rows),
        "resources": resource_summary(sampler.samples, idle),
        "startup": startup,
        "resource_scope": "Python experiment harness before model loading; not an extra runtime service",
    }


def calibrate(dev, raw, variant):
    choices = []
    for minimum in (0, 0.25, 0.35, 0.45, 0.55):
        for margin in (0, 0.015, 0.03):
            records = []
            for row, candidate in zip(dev, raw):
                value = threshold(candidate["semantic"], candidate["detail"], minimum, margin)
                if variant == "hybrid":
                    value = feature_assist(value, row["text"])
                records.append(record(row, value, 0))
            strict = sum(r["strict_correct"] for r in records)
            unsafe = sum(r["false_execution"] for r in records)
            exact = sum(r["semantic_correct"] for r in records)
            choices.append(
                {
                    "minimum": minimum,
                    "margin": margin,
                    "strict": strict,
                    "unsafe": unsafe,
                    "semantic": exact,
                    "objective": strict - 2 * unsafe,
                }
            )
    best = max(choices, key=lambda c: (c["objective"], c["semantic"], -c["minimum"], -c["margin"]))
    return best, choices


def probe_recovery(server, model):
    outcome = {}
    try:
        if model == "embedding":
            server.request("/v1/embeddings", {"input": ["用于有界超时恢复探针"]}, timeout=0.005)
        else:
            generate(server, "暂停音乐", timeout=0.005)
        outcome["probe"] = "completed_before_deadline"
    except (OSError, TimeoutError) as exc:
        outcome["probe"] = type(exc).__name__
    start = time.perf_counter()
    if model == "embedding":
        result = server.request("/v1/embeddings", {"input": ["恢复探针"]}, timeout=30)
        outcome["recovered"] = len(result["data"]) == 1
    else:
        value, _ = generate(server, "暂停音乐", timeout=30)
        outcome["recovered"] = valid(value)
    outcome["recovery_ms"] = (time.perf_counter() - start) * 1000
    return outcome


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--data", type=Path, default=Path(__file__).parent / "data")
    parser.add_argument("--models", type=Path, default=Path(r"C:\StarlightModels\Semantic"))
    parser.add_argument(
        "--engine", type=Path, default=Path("third_party/llama/vulkan/llama-server.exe")
    )
    args = parser.parse_args()
    output = args.output
    output.mkdir(parents=True, exist_ok=True)
    if (output / "protocol.json").exists():
        raise FileExistsError("do not overwrite an experiment")
    data = args.data
    manifest = json.loads((data / "frozen.sha256.json").read_text("utf8"))
    for name, digest in manifest.items():
        assert hashlib.sha256((data / name).read_bytes()).hexdigest() == digest
    rows = json.loads((data / "cases.json").read_text("utf8"))
    dev = [r for r in rows if r["split"] == "dev"]
    protocol = {
        "data_sha256": manifest,
        "source_sha256": {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in Path(__file__).parent.glob("*.py")
        },
        "schema": SCHEMA,
        "prompt": prompt(),
        "development_examples": development_examples(),
        "prototypes": prototypes(dev),
        "decision_whitelist": sorted(ACTIONS),
        "temperature": 0,
        "seed": 42,
        "context": 4096,
        "max_output_tokens": 96,
        "no_prompt_cache": True,
        "models": {
            "embedding": "Qwen3-Embedding-0.6B-Q8_0.gguf",
            "generation": "Qwen3.5-0.8B-Q8_0.gguf",
        },
        "engine_sha256": hashlib.sha256(args.engine.read_bytes()).hexdigest(),
        "clock": str(time.get_clock_info("perf_counter")),
    }
    save(output / "protocol.json", protocol)
    results = {"a": measure_a(rows, output)}
    save(output / "summary.json", results)
    print("A complete", results["a"]["strict_correct"], "/", results["a"]["n"], flush=True)
    for model, filename in protocol["models"].items():
        folder = output / model
        records = {}
        summary = {}
        try:
            check_free_memory()
            with Server(
                args.engine, args.models / filename, folder, embedding=model == "embedding"
            ) as server:
                time.sleep(2)
                idle = copy.deepcopy(server.resources.samples[-8:])
                deadline = time.perf_counter() + 1200
                summary["load_seconds"] = server.load_seconds
                if model == "embedding":
                    index = VectorIndex(server, dev)
                    raw_dev = []
                    for row in dev:
                        value, detail = index.retrieve(row["text"])
                        raw_dev.append({"id": row["id"], "semantic": value, "detail": detail})
                    save(folder / "dev-retrieval.json", raw_dev)
                    calibrated = {v: calibrate(dev, raw_dev, v) for v in ("direct", "hybrid")}
                    save(folder / "calibration-before-test.json", calibrated)
                    records = {"b_direct": [], "b_hybrid": []}
                    summary["index_seconds"] = index.index_seconds
                    for i, row in enumerate(rows):
                        if time.perf_counter() > deadline:
                            raise TimeoutError("model phase budget")
                        check_free_memory()
                        start = time.perf_counter_ns()
                        value, detail = index.retrieve(row["text"])
                        request_ms = (time.perf_counter_ns() - start) / 1e6
                        for variant, key in (("direct", "b_direct"), ("hybrid", "b_hybrid")):
                            begin = time.perf_counter_ns()
                            cal = calibrated[variant][0]
                            selected = threshold(value, detail, cal["minimum"], cal["margin"])
                            if variant == "hybrid":
                                selected = feature_assist(selected, row["text"])
                            decide(selected, row["state"], row["text"])
                            elapsed = request_ms + (time.perf_counter_ns() - begin) / 1e6
                            records[key].append(
                                record(row, selected, elapsed, {**detail, "unthresholded": value})
                            )
                        if i % 40 == 39:
                            print(model, i + 1, "/", len(rows), flush=True)
                    summary["calibration"] = {v: calibrated[v][0] for v in calibrated}
                else:
                    records = {"c": []}
                    for i, row in enumerate(rows):
                        if time.perf_counter() > deadline:
                            raise TimeoutError("model phase budget")
                        check_free_memory()
                        start = time.perf_counter_ns()
                        try:
                            value, detail = generate(server, row["text"])
                            decide(value, row["state"], row["text"])
                            elapsed = (time.perf_counter_ns() - start) / 1e6
                            records["c"].append(record(row, value, elapsed, detail))
                        except (OSError, ValueError, TimeoutError) as exc:
                            records["c"].append(
                                record(
                                    row,
                                    None,
                                    (time.perf_counter_ns() - start) / 1e6,
                                    error=type(exc).__name__,
                                )
                            )
                        if i % 20 == 19:
                            print(model, i + 1, "/", len(rows), flush=True)
                summary["timeout_recovery"] = probe_recovery(server, model)
                summary["resources"] = resource_summary(server.resources.samples, idle)
                summary["model_bytes"] = (args.models / filename).stat().st_size
            for key, value in records.items():
                save(output / (key + "-predictions.json"), value)
                results[key] = {**summarize(value, rows), **summary}
            save(folder / "phase-summary.json", summary)
        except Exception as exc:
            save(
                output / (model + "-first-failure.json"),
                {"type": type(exc).__name__, "message": str(exc)},
            )
            for key, value in records.items():
                save(output / (key + "-partial.json"), value)
            raise
        finally:
            save(output / "summary.json", results)
    print("All real inference complete", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
