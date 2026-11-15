"""One model at a time. Real inference, frozen splits, bounded requests, no media execution."""
import argparse
import hashlib
import json
import math
from pathlib import Path
import statistics
import time

from .embedding import EmbeddingAdapter, embed
from .engine import Server
from .semantics import SCHEMA, gate, messages, prediction, rule_baseline, score, valid


MODELS = [
    ("embedding", "Qwen3-Embedding-0.6B-Q8_0.gguf"),
    ("qwen35-08", "Qwen3.5-0.8B-Q8_0.gguf"),
    ("qwen3-17", "Qwen3-1.7B-Q8_0.gguf"),
    ("qwen35-2-aggressive", "Qwen3.5-2B-Uncensored-HauhauCS-Aggressive-Q4_K_M.gguf"),
]


def percentile(values, percent):
    if not values:
        return None
    values = sorted(values)
    position = (len(values)-1)*percent
    lo = math.floor(position)
    return values[lo] + (values[math.ceil(position)]-values[lo])*(position-lo)


def generate(server, row, *, timeout=45):
    response = server.request("/v1/chat/completions", {
        "messages": messages(row), "temperature": 0, "seed": 42, "max_tokens": 256,
        "chat_template_kwargs": {"enable_thinking": False}, "cache_prompt": False,
        "response_format": {"type": "json_schema", "json_schema": {
            "name": "semantic", "strict": True, "schema": SCHEMA}}}, timeout=timeout)
    content = response["choices"][0]["message"]["content"]
    try:
        parsed = json.loads(content)
    except (ValueError, TypeError):
        parsed = None
    return parsed, {"content": content, "usage": response.get("usage"),
                    "finish_reason": response["choices"][0].get("finish_reason"),
                    "timings": response.get("timings")}


def summarize(records, rows):
    lookup = {r["id"]: r for r in rows}
    selected = [r for r in records if lookup[r["id"]]["split"] == "test"]
    result = {"n": len(selected), "format_valid": sum(r["format_valid"] for r in selected),
              "failures": sum("error" in r or not r["format_valid"] for r in selected)}
    for variant in ("raw", "final"):
        for metric in ("action_correct", "full_correct", "wrong_action", "false_execution_proposal",
                       "speech_act_correct", "polarity_correct"):
            result[variant + "_" + metric] = sum(r[variant + "_score"][metric]
                and (not metric.endswith("correct") or r["format_valid"] and "error" not in r) for r in selected)
        implicit = [r for r in selected if lookup[r["id"]]["gold"]["implicit_action"]]
        reject = [r for r in selected if lookup[r["id"]]["gold"]["intent"] in ("none", "clarify")]
        result[variant+"_implicit"] = {"n": len(implicit), "action_correct": sum(r[variant+"_score"]["action_correct"]
            and r["format_valid"] and "error" not in r for r in implicit),
            "full_correct": sum(r[variant+"_score"]["full_correct"] and r["format_valid"] and "error" not in r for r in implicit)}
        result[variant+"_reject"] = {"n": len(reject), "false_execution_proposal": sum(r[variant+"_score"]["false_execution_proposal"] for r in reject)}
    result["latency_ms"] = {"p50": percentile([r["latency_ms"] for r in selected], .5),
                            "p95": percentile([r["latency_ms"] for r in selected], .95)}
    result["categories"] = {}
    for category in sorted({r["category"] for r in rows}):
        part = [r for r in selected if lookup[r["id"]]["category"] == category]
        result["categories"][category] = {"n": len(part), "action_correct": sum(r["final_score"]["action_correct"]
            and r["format_valid"] and "error" not in r for r in part),
            "full_correct": sum(r["final_score"]["full_correct"] and r["format_valid"] and "error" not in r for r in part)}
    return result


def resource_summary(samples, idle):
    summary = {"sample_interval_seconds": .25, "idle_samples": len(idle), "total_samples": len(samples)}
    for key in ("working_set", "private_bytes", "Dedicated Usage", "Shared Usage"):
        seen = [r[key] for r in samples if key in r]
        baseline = [r[key] for r in idle if key in r]
        summary[key] = {"idle_median_bytes": statistics.median(baseline) if baseline else None,
                        "sampled_peak_bytes": max(seen) if seen else None}
    summary["os_peak_working_set_bytes"] = max((r["peak_working_set"] for r in samples), default=0)
    cpu = [(b["cpu_seconds"]-a["cpu_seconds"])/(b["at"]-a["at"])*100
           for a, b in zip(samples, samples[1:]) if "cpu_seconds" in a and "cpu_seconds" in b and b["at"] > a["at"]]
    summary["cpu_core_percent_p95"] = percentile(cpu, .95)
    summary["cpu_core_percent_mean"] = statistics.mean(cpu) if cpu else None
    return summary


def check_free_memory():
    import ctypes
    class Status(ctypes.Structure):
        _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong)] + [(n, ctypes.c_ulonglong) for n in
            ("total_phys", "avail_phys", "total_page", "avail_page", "total_virtual", "avail_virtual", "extended")]
    state = Status()
    state.length = ctypes.sizeof(state)
    if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(state)) and state.avail_phys < 768*1024*1024:
        raise MemoryError("experiment stops below 768 MiB available RAM")


def run_model(key, filename, args, rows):
    output = args.output/key
    if output.exists():
        raise FileExistsError("preserve existing model run; use a new output root")
    records, summary = [], {}
    started = time.monotonic()
    with Server(args.engine, args.models/filename, output, embedding=key=="embedding", gpu=not args.cpu) as server:
        summary["load_seconds"] = server.load_seconds
        idle_start = time.monotonic()
        time.sleep(2)
        idle = [r for r in server.resources.samples if r["at"] >= idle_start]
        adapter = None
        if key == "embedding":
            before = time.monotonic()
            embed(server, [rows[0]["text"]])
            summary["cold_first_request_ms"] = (time.monotonic()-before)*1000
            adapter = EmbeddingAdapter(server, [r for r in rows if r["split"] == "dev"])
        dev = [r for r in rows if r["split"] == "dev"]
        held = [r for r in rows if r["split"] == "test"]
        for split, cases in (("dev", dev), ("test", held)):
            if split == "test" and adapter:
                # Only development labels select the retrieval cutoff. Held-out
                # vectors/predictions have not been requested at this point.
                best = None
                for threshold in (.35,.4,.45,.5,.55,.6,.65,.7,.75,.8):
                    for margin in (0,.025,.05):
                        adapter.threshold, adapter.margin = threshold, margin
                        scored = [score(gate(adapter.filter(r["retrieval_raw"], r["detail"]), case), case)
                                  for r, case in zip(records, dev)]
                        objective = sum(x["full_correct"]-2*x["false_execution_proposal"] for x in scored)
                        if best is None or objective > best[0]:
                            best = objective, threshold, margin
                _, adapter.threshold, adapter.margin = best
                adapter.freeze(output/"embedding-calibration-frozen.json")
            for index, row in enumerate(cases):
                check_free_memory()
                if time.monotonic()-started > 1800:
                    raise TimeoutError("30 minute per-model experiment budget")
                begin = time.monotonic()
                detail, raw, error = {}, None, None
                try:
                    if adapter:
                        raw, detail = adapter.raw(row)
                        retrieval_raw = raw
                        raw = adapter.filter(raw, detail) if split == "test" else raw
                    else:
                        raw, detail = generate(server, row)
                except Exception as exc:
                    error = type(exc).__name__
                elapsed = (time.monotonic()-begin)*1000
                formatted = valid(raw)
                safe_raw = raw if formatted else prediction("none", evidence="真实请求失败或格式无效")
                final = gate(safe_raw, row)
                record = {"id": row["id"], "split": split, "raw": raw, "final": final,
                    "detail": detail, "format_valid": formatted, "latency_ms": elapsed,
                    "raw_score": score(safe_raw,row), "final_score": score(final,row)}
                if adapter:
                    record["retrieval_raw"] = retrieval_raw if error is None else prediction("none")
                if error:
                    record["error"] = error
                    record["raw_score"]["action_correct"] = record["raw_score"]["full_correct"] = False
                    record["final_score"]["action_correct"] = record["final_score"]["full_correct"] = False
                if not records and not adapter:
                    summary["cold_first_request_ms"] = elapsed
                records.append(record)
                with (output/"predictions.jsonl").open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(record, ensure_ascii=False)+"\n")
                if index % 10 == 0 or index+1 == len(cases):
                    print(key, split, index+1, "/",len(cases), "ms",round(elapsed), "error",error,flush=True)
        # Exactly one deliberately expired HTTP request, then one recovery.
        probe_start = time.monotonic()
        try:
            server.request("/v1/embeddings" if adapter else "/completion",
                {"input": ["隔离超时验证"*100]} if adapter else
                {"prompt": "Generate a long list of integers from one to a thousand.", "n_predict":1024}, timeout=.005)
            summary["timeout_probe"] = "completed_before_deadline"
        except Exception as exc:
            summary["timeout_probe"] = type(exc).__name__
        summary["timeout_probe_ms"] = (time.monotonic()-probe_start)*1000
        recovery = time.monotonic()
        try:
            if adapter:
                embed(server,["音乐暂停"])
            else:
                recovered, _ = generate(server, dev[0], timeout=30)
                if not valid(recovered):
                    raise ValueError("recovery format invalid")
            summary["timeout_recovered"] = True
        except Exception as exc:
            summary["timeout_recovered"] = False
            summary["recovery_error"] = type(exc).__name__
        summary["recovery_ms"] = (time.monotonic()-recovery)*1000
        summary["resources"] = resource_summary(server.resources.samples, idle)
    summary.update(summarize(records, rows))
    summary["model"] = filename
    summary["total_seconds"] = time.monotonic()-started
    (output/"summary.json").write_text(json.dumps(summary, ensure_ascii=False,indent=2),encoding="utf-8")
    print(key,"COMPLETE",json.dumps({k:summary[k] for k in ('n','final_action_correct','final_full_correct','latency_ms')},ensure_ascii=False),flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--models",type=Path,required=True)
    parser.add_argument("--engine",type=Path,required=True)
    parser.add_argument("--output",type=Path,required=True)
    parser.add_argument("--data",type=Path,default=Path(__file__).parent/"data")
    parser.add_argument("--model",choices=[x[0] for x in MODELS]+["all"],default="all")
    parser.add_argument("--cpu",action="store_true")
    args=parser.parse_args()
    args.output.mkdir(parents=True,exist_ok=True)
    manifest=json.loads((args.data/"frozen.sha256.json").read_text(encoding="utf-8"))
    for name,digest in manifest.items():
        assert hashlib.sha256((args.data/name).read_bytes()).hexdigest()==digest,"frozen corpus changed"
    rows=json.loads((args.data/"cases.json").read_text(encoding="utf-8"))
    protocol={"schema":SCHEMA,"messages":messages(rows[0]),"data_sha256":manifest,
              "engine_sha256":hashlib.sha256(args.engine.read_bytes()).hexdigest(),
              "source_sha256":{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in Path(__file__).parent.glob("*.py")},
              "seed":42,"temperature":0,"max_output_tokens":256,"context":4096,
              "cache_prompt":False,"gpu":not args.cpu,"sampling_ms":250,"models":MODELS}
    protocol_path=args.output/"protocol-frozen.json"
    if protocol_path.exists():
        raise FileExistsError("preserve the frozen experiment protocol")
    protocol_path.write_text(json.dumps(protocol,ensure_ascii=False,indent=2),encoding="utf-8")
    baseline=[]
    for row in rows:
        start=time.monotonic()
        raw=rule_baseline(row)
        final=gate(raw,row)
        baseline.append({"id":row["id"],"raw":raw,"final":final,"format_valid":valid(raw),
            "latency_ms":(time.monotonic()-start)*1000,"raw_score":score(raw,row),"final_score":score(final,row)})
    (args.output/"rules-predictions.json").write_text(json.dumps(baseline,ensure_ascii=False,indent=2),encoding="utf-8")
    (args.output/"rules-summary.json").write_text(json.dumps(summarize(baseline,rows),ensure_ascii=False,indent=2),encoding="utf-8")
    failures={}
    for key,filename in MODELS:
        if args.model not in ("all",key):
            continue
        try:
            run_model(key,filename,args,rows)
        except Exception as exc:
            failures[key]={"error":type(exc).__name__,"message":str(exc)}
            print(key,"FAILED",type(exc).__name__,str(exc),flush=True)
    (args.output/"failures.json").write_text(json.dumps(failures,ensure_ascii=False,indent=2),encoding="utf-8")
    return bool(failures)


if __name__ == "__main__":
    raise SystemExit(main())
