"""Analyze immutable predictions; never tune prompts, prototypes or held-out labels."""
import argparse
import copy
import csv
import hashlib
import json
import os
from pathlib import Path
import time

from .run import MODELS, percentile, summarize
from .semantics import gate, prediction, rule_baseline, score, valid


def active_candidate(value):
    """A negated action name is a topic, not an executable proposal."""
    return (valid(value) and value["intent"] not in ("none", "clarify")
        and value["polarity"] == "affirmative" and (value["speech_act"] == "command"
        or value["speech_act"] == "evaluation" and value["implicit_action"]))


def corrected(records, rows):
    lookup = {r["id"]:r for r in rows}
    result=[]
    assert len({r["id"] for r in records}) == len(records), "duplicate predictions"
    assert set(lookup) == {r["id"] for r in records}, "missing benchmark samples"
    for original in records:
        item=copy.deepcopy(original)
        row=lookup[item["id"]]
        item["format_valid"]=valid(item["raw"])
        raw=item["raw"] if item["format_valid"] else prediction("none")
        item["raw_score"]=score(raw,row)
        item["final_score"]=score(item["final"],row)
        result.append(item)
    return result


def additional_metrics(records, rows):
    lookup={r["id"]:r for r in rows}
    dev_texts={r["text"] for r in rows if r["split"]=="dev"}
    result={}
    for name,condition in {
        "novel_wording":lambda r:r["split"]=="test" and r["text"] not in dev_texts,
        "same_words_different_state":lambda r:r["split"]=="test" and r["text"] in dev_texts,
        "noncommands_only":lambda r:r["split"]=="test" and r["gold"]["intent"]=="none",
        "denial":lambda r:r["split"]=="test" and r["category"]=="denial",
        "questions_reported":lambda r:r["split"]=="test" and r["category"]=="reported_question" and r["gold"]["intent"]=="none",
        "development":lambda r:r["split"]=="dev",
    }.items():
        subset=[r for r in records if condition(lookup[r["id"]])]
        result[name]={"n":len(subset)}
        for variant in ("raw","final"):
            result[name][variant+"_action_correct"]=sum(r["format_valid"] and "error" not in r
                and r[variant+"_score"]["action_correct"] for r in subset)
            result[name][variant+"_full_correct"]=sum(r["format_valid"] and "error" not in r
                and r[variant+"_score"]["full_correct"] for r in subset)
            result[name][variant+"_false_execution_proposal"]=sum(r[variant+"_score"]["false_execution_proposal"] for r in subset)
    held=[r for r in records if lookup[r["id"]]["split"]=="test"]
    for variant in ("raw","final"):
        result[variant+"_safe_equivalent_actions"] = sum(r["format_valid"] and "error" not in r
            and (r[variant+"_score"]["action_correct"] or
                 lookup[r["id"]]["gold"]["intent"] in ("none","clarify") and
                 (r[variant] or {}).get("intent") in ("none","clarify")) for r in held)
        proposed=[r for r in held if active_candidate(r[variant])]
        result[variant+"_active_wrong_action"] = sum(not r[variant+"_score"]["action_correct"] for r in proposed)
        result[variant+"_active_false_execution"] = sum(lookup[r["id"]]["gold"]["intent"] in ("none","clarify") for r in proposed)
        result[variant+"_effective_action_correct"] = sum(
            r["format_valid"] and "error" not in r and
            ((r[variant+"_score"]["action_correct"] and active_candidate(r[variant]))
             or lookup[r["id"]]["gold"]["intent"] in ("none","clarify") and not active_candidate(r[variant]))
            for r in held)
    return result


def evaluate(directory, data, inventory):
    rows=json.loads((data/"cases.json").read_text(encoding="utf-8"))
    manifest=json.loads((data/"frozen.sha256.json").read_text(encoding="utf-8"))
    protocol=json.loads((directory/"protocol-frozen.json").read_text(encoding="utf-8"))
    assert protocol["data_sha256"] == manifest
    for name,digest in manifest.items():
        assert hashlib.sha256((data/name).read_bytes()).hexdigest()==digest
    models={row["file"]:row for row in inventory}
    result,examples={},{}
    baseline=json.loads((directory/"rules-predictions.json").read_text(encoding="utf-8"))
    for key,filename in [("rules",None),*MODELS]:
        if key=="rules":
            records=baseline
            summary={}
        else:
            summary=json.loads((directory/key/"summary.json").read_text(encoding="utf-8"))
            records=[json.loads(line) for line in (directory/key/"predictions.jsonl").read_text(encoding="utf-8").splitlines()]
            cleanup=json.loads((directory/key/"cleanup.json").read_text(encoding="utf-8"))
            assert cleanup["port_released"] and cleanup["sampler_stopped"]
            summary["cleanup"]=cleanup
            summary["model_inventory"]={k:models[filename][k] for k in
                ("file","bytes","parameter_count","sha256","known_tensor_bounds_valid")}
            samples=json.loads((directory/key/"resources.json").read_text(encoding="utf-8"))
            boundary=samples[0]["at"]+summary["load_seconds"]+2
            inference=[s for s in samples if s["at"]>=boundary]
            summary["sampled_inference_peak"]={name:max((s[name] for s in inference if name in s),default=None)
                for name in ("working_set","private_bytes","Dedicated Usage","Shared Usage")}
            summary["resources"]["cpu_machine_mean_percent"] = summary["resources"]["cpu_core_percent_mean"]/os.cpu_count()
            summary["resources"]["cpu_machine_p95_percent"] = summary["resources"]["cpu_core_percent_p95"]/os.cpu_count()
        records=corrected(records,rows)
        summary.update(summarize(records,rows))
        summary["additional"]=additional_metrics(records,rows)
        if key=="embedding":
            calibration=json.loads((directory/key/"embedding-calibration-frozen.json").read_text(encoding="utf-8"))
            summary["retrieval_threshold"]=calibration["threshold"]
            summary["retrieval_margin"]=calibration["margin"]
            summary["prototype_count"]=calibration["prototype_count"]
            summary["index_seconds"]=calibration["index_seconds"]
            held=[r for r in records if r["split"]=="test"]
            lookup={r["id"]:r for r in rows}
            summary["unthresholded_top1_action_correct"]=sum(score(r["retrieval_raw"],lookup[r["id"]])["action_correct"] for r in held)
        result[key]=summary
        lookup={r["id"]:r for r in rows}
        examples[key]={"correct":[],"errors":[],"unsafe":[]}
        for r in records:
            row=lookup[r["id"]]
            if row["split"]!="test":
                continue
            sample={"id":r["id"],"text":row["text"],"state":row["state"],"gold":row["gold"],
                    "raw":r["raw"],"final":r["final"],"format_valid":r["format_valid"]}
            group="correct" if r["final_score"]["full_correct"] and r["format_valid"] else "errors"
            if len(examples[key][group])<8:
                examples[key][group].append(sample)
            if r["final_score"]["false_execution_proposal"]:
                examples[key]["unsafe"].append(sample)
    return result,examples


def rule_latency(rows):
    # Windows Python3.11 monotonic() can round sub-ms work to zero. Supplement
    # timing with QPC; no labels, prompts or semantic decisions are changed.
    for row in rows[:40]:
        gate(rule_baseline(row),row)
    samples=[]
    for _ in range(10):
        for row in rows:
            begin=time.perf_counter_ns()
            gate(rule_baseline(row),row)
            samples.append((time.perf_counter_ns()-begin)/1e6)
    return {"n":len(samples),"p50_ms":percentile(samples,.5),"p95_ms":percentile(samples,.95),
            "clock":str(time.get_clock_info("perf_counter")),
            "original_monotonic_clock":str(time.get_clock_info("monotonic"))}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("experiment",type=Path)
    parser.add_argument("--inventory",type=Path,required=True)
    parser.add_argument("--output",type=Path,required=True)
    parser.add_argument("--data",type=Path,default=Path(__file__).parent/"data")
    args=parser.parse_args()
    args.output.mkdir(parents=True,exist_ok=True)
    inventory=json.loads(args.inventory.read_text(encoding="utf-8"))
    comparison,examples=evaluate(args.experiment,args.data,inventory)
    latency=rule_latency(json.loads((args.data/"cases.json").read_text(encoding="utf-8")))
    comparison["rules"]["qpc_warm_latency"]=latency
    for name,value in (("comparison.json",comparison),("examples.json",examples)):
        (args.output/name).write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding="utf-8")
    with (args.output/"comparison.csv").open("w",encoding="utf-8-sig",newline="") as stream:
        writer=csv.writer(stream)
        writer.writerow(["pipeline","n","raw_action_correct","raw_full_correct","final_action_correct",
            "final_full_correct","raw_wrong_action","final_wrong_action","format_valid","p50_ms","p95_ms"])
        for name,s in comparison.items():
            writer.writerow([name]+[s[k] for k in ("n","raw_action_correct","raw_full_correct","final_action_correct",
                "final_full_correct","raw_wrong_action","final_wrong_action","format_valid")]+[s["latency_ms"][k] for k in ("p50","p95")])
    print(json.dumps({name:{k:s[k] for k in ("n","final_action_correct","final_full_correct","format_valid")}
                      for name,s in comparison.items()},ensure_ascii=False))


if __name__=="__main__":
    main()
