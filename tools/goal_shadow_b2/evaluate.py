"""Single-pass frozen goal comparison; labels/answers never enter choose()."""
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import statistics
import time

from tools.goal_shadow.contracts import Availability as A, GoalConstraints
from .capabilities import Capability, Level
from .decision import World, choose, Receipt, Outcome, SyntheticExecutor, simulate

ROOT = Path(__file__).parent


def receipts(tokens):
    values = {
        "STARTED":Receipt(Outcome.STARTED,accepted=True,progressed=True),
        "FAILED_SAFE":Receipt(Outcome.FAILED,no_effect_confirmed=True),
        "FAILED_UNCERTAIN":Receipt(Outcome.FAILED,accepted=True),
        "TIMEOUT_SAFE":Receipt(Outcome.TIMEOUT,no_effect_confirmed=True),
        "TIMEOUT_UNCERTAIN":Receipt(Outcome.TIMEOUT,accepted=True),
        "ACCEPTED_ONLY":Receipt(Outcome.UNKNOWN,accepted=True),
        "STALE":Receipt(Outcome.STARTED,accepted=True,progressed=True,generation_current=False),
        "RESOURCE_STALE":Receipt(Outcome.STARTED,accepted=True,progressed=True,resource_current=False),
    }
    return [values[t] for t in tokens]


def from_fields(data, now):
    capabilities = tuple(Capability(p["ref"],p["provider"],Level(p["level"]),now if p["fresh"] else now-10,
        ("synthetic",1),A.YES,A.YES if p["owned"] else A.UNKNOWN,p["ref"]+"-content",("synthetic_evidence",),
        explicit_preference=p["preference"],history=p["history"]) for p in data["plans"])
    return World(data["music"],data["generation"],data["snapshot_generation"],now,
                 data["current"],data["session"],data["movie"],data["inventory"],capabilities,
                 GoalConstraints(keep_content=data["keep"],change_content=data["change"]))


def load_cases(split):
    manifest=json.loads((ROOT/"data/manifest.json").read_text())
    path=ROOT/"data"/(split+".jsonl")
    assert hashlib.sha256(path.read_bytes()).hexdigest()==manifest["files"][path.name]["sha256"]
    return [json.loads(line) for line in path.read_text().splitlines()]


def run(split, output):
    output=Path(output)
    if output.exists():
        raise SystemExit("refusing_to_overwrite_first_results")
    output.mkdir(parents=True)
    results=[]
    timings=[]
    for case in load_cases(split):
        world=from_fields(case["world"],time.monotonic())
        start=time.perf_counter_ns()
        decision=choose(world,time.monotonic())
        timings.append((time.perf_counter_ns()-start)/1e6)
        simulated=simulate(decision,SyntheticExecutor(receipts(case["outcomes"])))
        actual=dict(action=decision.action.value,outcome=simulated.status.value,attempts=simulated.attempts)
        results.append(dict(id=case["id"],actual=actual,expected=case["expected"],
                            correct=actual==case["expected"],decision=asdict(decision),simulation=asdict(simulated)))
    summary=dict(count=len(results),exact=sum(r["correct"] for r in results),
                 initial_goal_satisfied=sum(r["decision"]["goal_satisfied"] for r in results),
                 attempted_cases=sum(r["simulation"]["attempts"]>0 for r in results),
                 simulated_goal_satisfied=sum(r["simulation"]["goal_satisfied"] for r in results),
                 real_business_executions=0, decision_p50_ms=statistics.median(timings),
                 decision_p95_ms=sorted(timings)[min(len(timings)-1,int(len(timings)*.95))],
                 max_attempts=max(r["simulation"]["attempts"] for r in results))
    (output/"predictions.jsonl").write_text("".join(json.dumps(r)+"\n" for r in results),encoding="utf8")
    (output/"summary.json").write_text(json.dumps(summary,indent=2),encoding="utf8")
    print(json.dumps(summary,indent=2))


if __name__=="__main__":
    import argparse
    parser=argparse.ArgumentParser()
    parser.add_argument("split",choices=["dev","holdout"])
    parser.add_argument("--output",required=True)
    arguments=parser.parse_args()
    run(arguments.split,arguments.output)
