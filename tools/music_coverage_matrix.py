"""Aggregate hand-labeled music acceptance evidence without sending requests.

The input is a JSON array of objects or one object per line in a JSONL file.
Use a stable catalog identifier for an actual independent directory, and
stable query/trial labels shared by observations from that search. A trial
is one run of a query against a catalog and platform. Make a new trial for
a repetition, or use the same labels to add stages to an existing trial.

Required row fields are evidence, catalog, platform, query, and trial.
Optional resolver, backend, and resource identify an attempted route and
representation. Set the same resolver and resource on subsequent observations
of that representation, including recovery. Labels are identifiers, not URLs
or credentials. Resolve attempts that return no resource omit resource.
Resolver rows with only resource observations do not count as search trials.

All observations are nullable. An omitted field and explicit null both mean
the stage was unobserved. Use catalog_hit, exact_match, version_match and
grouping_correct for outcomes of the whole search. page and page_ok record a
specific probe of page traversal. first_result_ms records receipt of an actual
first result. Duplicate catalog facts must agree across resolver rows.

Resolver outcomes include resolved, readable, decoded, continuous_playback,
quality_agrees, best_download, exact_download, lyrics, cover and recovered.
declared_quality and verified_quality preserve independent labels. An assessor
records quality_agrees after checking evidence. played_seconds records elapsed
verified playback. predownload_bytes records the estimated size of the whole
resource with size_evidence; probe_prefix_bytes records bytes inspected and is
never used as the estimated full size.

Rows explicitly declare controlled, authorized_public or authorized_commercial.
Public and commercial rows require an evidence_ref identifying an independently
retained proof with access permission. This tool does not verify that proof.
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
import math
from pathlib import Path
import re
import sys


EVIDENCE = ("controlled", "authorized_public", "authorized_commercial")
SIZE_EVIDENCE = frozenset(("catalog_declared", "head", "content_range"))
CATALOG_OUTCOMES = ("catalog_hit", "exact_match", "version_match", "grouping_correct")
RESOURCE_OUTCOMES = ("resolved", "readable", "decoded", "continuous_playback", "quality_agrees",
                     "best_download", "exact_download", "lyrics", "cover", "recovered")
RESOURCE_VALUES = ("declared_quality", "verified_quality", "played_seconds",
                   "predownload_bytes", "size_evidence", "probe_prefix_bytes")
REQUIRED = ("evidence", "catalog", "platform", "query", "trial")
LABELS = ("catalog", "platform", "query", "trial", "resolver", "backend",
          "resource", "evidence_ref", "declared_quality", "verified_quality")
FIELDS = {*REQUIRED, *LABELS, *CATALOG_OUTCOMES, *RESOURCE_OUTCOMES, *RESOURCE_VALUES,
          "page", "page_ok", "first_result_ms"}
LABEL = re.compile(r"[\w.-]{1,128}", re.UNICODE)


def _validate(row):
    if not isinstance(row, dict) or set(row) - FIELDS:
        raise ValueError("unexpected row fields or type")
    if any(row.get(field) is None for field in REQUIRED) or row["evidence"] not in EVIDENCE:
        raise ValueError("missing trial identity or unknown evidence stratum")
    for field in LABELS:
        label = row.get(field)
        if label is not None and (not isinstance(label, str) or not LABEL.fullmatch(label)):
            raise ValueError(f"invalid {field} identifier")
    if row["evidence"] != "controlled" and not row.get("evidence_ref"):
        raise ValueError("authorized row requires evidence_ref")
    for field in (*CATALOG_OUTCOMES, *RESOURCE_OUTCOMES, "page_ok"):
        value = row.get(field)
        if value is not None and type(value) is not bool:
            raise ValueError(f"{field} must be boolean or null")
    for field in ("first_result_ms", "played_seconds"):
        value = row.get(field)
        if value is not None and (type(value) not in (float, int) or not 0 <= value <= 10**9):
            raise ValueError(f"{field} must be finite and nonnegative")
    for field, minimum, maximum in (("page", 1, 2**31 - 1), ("predownload_bytes", 1, 2**40),
                                    ("probe_prefix_bytes", 0, 2**40)):
        value = row.get(field)
        if value is not None and (type(value) is not int or not minimum <= value <= maximum):
            raise ValueError(f"invalid {field}")
    if row.get("page_ok") is not None and row.get("page") is None:
        raise ValueError("page_ok requires page")
    if not row.get("resolver") and any(row.get(field) is not None for field in (
            "backend", "resource", *RESOURCE_OUTCOMES, *RESOURCE_VALUES)):
        raise ValueError("resource observations require resolver")
    evidence = row.get("size_evidence")
    if evidence is not None and (not isinstance(evidence, str) or evidence not in SIZE_EVIDENCE):
        raise ValueError("unknown size_evidence")


def _record(store, key, row, fields):
    facts = store.setdefault(key, {})
    for field in fields:
        value = row.get(field)
        if value is None:
            continue
        if field in facts and facts[field] != value:
            raise ValueError(f"contradictory {field} for the same observation")
        facts[field] = value


def _outcomes(rows, fields):
    metrics = {}
    for field in fields:
        passed = sum(item.get(field) is True for item in rows)
        failed = sum(item.get(field) is False for item in rows)
        observed = passed + failed
        metrics[field] = {"yes": passed, "no": failed, "unknown": len(rows) - observed,
                          "observed": observed, "rate": passed / observed if observed else None}
    return metrics


def _numbers(values):
    values = sorted(values)
    if not values:
        return {"observed": 0, "p50": None, "p95": None, "max": None}
    return {"observed": len(values), "p50": values[math.ceil(len(values) * .5) - 1],
            "p95": values[math.ceil(len(values) * .95) - 1], "max": values[-1]}


def _observed(rows, field):
    return (row[field] for row in rows if field in row)


def _catalogs(searches, pages):
    groups = {}
    for key, facts in searches.items():
        groups.setdefault(key[:2], []).append(facts)
    page_groups = {}
    for key, facts in pages.items():
        page_groups.setdefault(key[:2], []).append((key[-1], facts))
    result = []
    for (catalog, platform), rows in sorted(groups.items()):
        checks = page_groups.get((catalog, platform), [])
        result.append({
            "catalog": catalog, "platform": platform, "search_trials": len(rows),
            "catalog_outcomes": _outcomes(rows, CATALOG_OUTCOMES),
            "first_result_ms": _numbers(_observed(rows, "first_result_ms")),
            "page_outcomes": _outcomes([facts for _, facts in checks], ("page_ok",)),
            "pages_observed": sorted({page for page, _ in checks}),
        })
    return result


def _resources(resources):
    groups = {}
    for key, facts in resources.items():
        groups.setdefault((*key[:2], key[-2]), []).append(facts)
    result = []
    for (catalog, platform, resolver), rows in sorted(groups.items()):
        size = {evidence: _numbers(facts["predownload_bytes"] for facts in rows
                                   if facts.get("size_evidence") == evidence)
                for evidence in sorted(SIZE_EVIDENCE)}
        pairs = Counter((row.get("declared_quality"), row.get("verified_quality"))
                        for row in rows if row.get("declared_quality") or row.get("verified_quality"))
        result.append({
            "catalog": catalog, "platform": platform, "resolver": resolver,
            "backends_observed": sorted(set(_observed(rows, "backend"))),
            "resource_attempts": len(rows),
            "resource_outcomes": _outcomes(rows, RESOURCE_OUTCOMES),
            "quality_pairs": [{"declared": declared, "verified": verified, "observed": count}
                              for (declared, verified), count in sorted(
                                  pairs.items(), key=lambda item: tuple(value or "" for value in item[0]))],
            "played_seconds": _numbers(_observed(rows, "played_seconds")),
            "predownload_bytes_by_evidence": size,
            "probe_prefix_bytes": _numbers(_observed(rows, "probe_prefix_bytes")),
        })
    return result


def aggregate(rows):
    """Return independently scoped summaries from an iterable of evidence rows."""
    groups = {evidence: ({}, {}, {}) for evidence in EVIDENCE}
    for index, row in enumerate(rows, 1):
        try:
            _validate(row)
            key = tuple(row[field] for field in REQUIRED[1:])
            searches, pages, resources = groups[row["evidence"]]
            search_fields = (*CATALOG_OUTCOMES, "first_result_ms")
            if not row.get("resolver") or row.get("page") is not None or any(
                    field in row for field in search_fields):
                _record(searches, key, row, search_fields)
            if row.get("page") is not None:
                _record(pages, (*key, row["page"]), row, ("page_ok",))
            if row.get("resolver"):
                _record(resources, (*key, row["resolver"], row.get("resource")), row,
                        (*RESOURCE_OUTCOMES, *RESOURCE_VALUES, "backend"))
        except ValueError as error:
            raise ValueError(f"row {index}: {error}") from None

    summaries = {}
    for evidence, (searches, pages, resources) in groups.items():
        for facts in resources.values():
            if ("predownload_bytes" in facts) != ("size_evidence" in facts):
                raise ValueError("predownload_bytes and size_evidence must be supplied together")
            if facts.get("quality_agrees") is not None and not (
                    facts.get("declared_quality") and facts.get("verified_quality")):
                raise ValueError("quality_agrees requires declared and verified quality")
        summaries[evidence] = {
            "catalog_count": len({key[0] for key in searches}),
            "search_trial_count": len(searches),
            "catalogs": _catalogs(searches, pages),
            "resolvers": _resources(resources),
        }
    return {"schema_version": 1, "evidence": summaries}


def _unique_object(pairs):
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("duplicate JSON key")
    return result


def read_rows(path):
    with Path(path).open(encoding="utf-8-sig") as source:
        if Path(path).suffix.lower() == ".jsonl":
            for number, line in enumerate(source, 1):
                if line.strip():
                    try:
                        yield json.loads(line, object_pairs_hook=_unique_object)
                    except ValueError as error:
                        raise ValueError(f"JSONL line {number}: {error}") from None
        else:
            rows = json.load(source, object_pairs_hook=_unique_object)
            if not isinstance(rows, list):
                raise ValueError("JSON input must be an array")
            yield from rows


def main(argv=None):
    description, _, schema = __doc__.partition("\n\n")
    parser = argparse.ArgumentParser(description=description, epilog=schema,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("input", nargs="+", type=Path, help="JSON or JSONL evidence files")
    parser.add_argument("-o", "--output", type=Path, help="create a new report file")
    options = parser.parse_args(argv)
    try:
        report = aggregate(row for path in options.input for row in read_rows(path))
        data = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        if options.output is None:
            print(data, end="")
        else:
            with options.output.open("x", encoding="utf-8") as target:
                target.write(data)
    except (OSError, ValueError) as error:
        print(f"coverage matrix: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
