"""Author and freeze new goal cases before the B2 decision implementation."""
import hashlib
import json
from pathlib import Path
import random


def make_cases(split, seed):
    rng = random.Random(seed)
    rows = []
    def row(label, *, music="stopped", plans=(), keep=False, change=False,
            generation=1, movie="stopped", inventory="ok", outcomes=(), expected="NEEDS_VERIFICATION",
            final="UNKNOWN", attempts=0, session=True):
        rows.append(dict(id=f"{split}-{len(rows)+1:03d}", label=label, world=dict(
            music=music, session=session, current="current", generation=generation,
            snapshot_generation=1, movie=movie, inventory=inventory, keep=keep, change=change,
            plans=list(plans)), outcomes=list(outcomes), expected=dict(
                action=expected, outcome=final, attempts=attempts)))
    def plan(ref, provider="collection", level="ATTEMPTABLE", **kwargs):
        return dict(ref=ref, provider=provider, level=level, owned=True, preference=0,
                    history=0, fresh=True, **kwargs)
    personal = plan("personal")
    chart = plan("chart", "chart")
    row("already playing", music="playing", expected="NO_OP", final="STARTED")
    row("valid paused session", music="paused", expected="RESUME_MUSIC", outcomes=["STARTED"], final="STARTED", attempts=1)
    row("known local attempt", plans=[personal], expected="TRY_COLLECTION", outcomes=["STARTED"], final="STARTED", attempts=1)
    row("membership only", plans=[plan("stored",level="UNVERIFIED")])
    row("chart capability", plans=[chart], expected="TRY_CHART", outcomes=["STARTED"], final="STARTED", attempts=1)
    row("catalog only", plans=[plan("catalog","chart",level="UNVERIFIED")])
    row("known empty", inventory="empty", expected="UNAVAILABLE")
    row("read error is not empty", inventory="error")
    row("keep content", plans=[personal], keep=True, expected="UNAVAILABLE")
    row("explicit new content", music="playing", plans=[personal], change=True, expected="TRY_COLLECTION", outcomes=["STARTED"], final="STARTED", attempts=1)
    row("stale generation", generation=2, plans=[personal])
    row("movie active", movie="playing", plans=[personal], expected="NEEDS_CLARIFICATION")
    row("failed personal fallback", plans=[personal,chart], expected="TRY_COLLECTION", outcomes=["FAILED_SAFE","STARTED"], final="STARTED", attempts=2)
    row("uncertain timeout stop", plans=[personal,chart], expected="TRY_COLLECTION", outcomes=["TIMEOUT_UNCERTAIN"], final="TIMEOUT", attempts=1)
    row("accepted is not started", plans=[personal], expected="TRY_COLLECTION", outcomes=["ACCEPTED_ONLY"], attempts=1)
    row("unknown player", music="unknown", plans=[personal], expected="NEEDS_CLARIFICATION")
    if split == "holdout":
        row("invalid paused session", music="paused", session=False)
        row("known absent resource", plans=[plan("gone",level="UNAVAILABLE")], expected="UNAVAILABLE")
        row("expiry", plans=[dict(personal,fresh=False)])
        row("safe timeout fallback", plans=[personal,chart], expected="TRY_COLLECTION", outcomes=["TIMEOUT_SAFE","STARTED"], final="STARTED", attempts=2)
        row("unsafe failure stop", plans=[personal,chart], expected="TRY_COLLECTION", outcomes=["FAILED_UNCERTAIN"], final="UNKNOWN", attempts=1)
        row("late request", plans=[personal,chart], expected="TRY_COLLECTION", outcomes=["STALE"], final="STALE", attempts=1)
        row("late capability", plans=[personal], expected="TRY_COLLECTION", outcomes=["RESOURCE_STALE"], final="STALE", attempts=1)
        row("bounded failures", plans=[plan(str(i)) for i in range(5)], expected="TRY_COLLECTION", outcomes=["FAILED_SAFE"]*5, final="FAILED", attempts=3)
        row("no movie action on resume", music="paused", movie="playing", expected="NEEDS_CLARIFICATION")
        row("unknown movie", movie="unknown", plans=[personal], expected="NEEDS_CLARIFICATION")
        row("no op may coexist", music="playing", movie="playing", expected="NO_OP", final="STARTED")
        row("no resume for explicit replacement", music="paused", change=True, plans=[chart], expected="TRY_CHART", outcomes=["STARTED"], final="STARTED", attempts=1)
        row("unverified collection fallback to chart", plans=[plan("maybe",level="UNVERIFIED"),chart], expected="TRY_CHART", outcomes=["STARTED"], final="STARTED", attempts=1)
        row("keep versus change conflict", keep=True, change=True, expected="NEEDS_CLARIFICATION")
    for case in rows:
        rng.shuffle(case["world"]["plans"])
        # Stable resource identity independent of the case label, name and row.
        for p in case["world"]["plans"]:
            p["ref"] = hashlib.sha256((split+":"+case["id"]+":"+p["ref"]).encode()).hexdigest()[:16]
    return rows


def main():
    root = Path(__file__).parent/"data"
    root.mkdir(exist_ok=True)
    if (root/"manifest.json").exists():
        raise SystemExit("refusing_to_overwrite_frozen_answers")
    manifest = {"scope":"new B2 synthetic goal cases, no utterance recognition", "files":{}}
    for split, seed in (("dev",202611171), ("holdout",202611172)):
        rows = make_cases(split,seed)
        data = b"".join((json.dumps(r,sort_keys=True)+"\n").encode() for r in rows)
        name=split+".jsonl"
        (root/name).write_bytes(data)
        manifest["files"][name] = {"count":len(rows),"sha256":hashlib.sha256(data).hexdigest()}
    (root/"manifest.json").write_bytes((json.dumps(manifest,indent=2)+"\n").encode())
    print(json.dumps(manifest,indent=2))


if __name__ == "__main__":
    main()
