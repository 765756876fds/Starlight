"""Publish bounded UI test results without exporting runtime/private diagnostics."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import xml.etree.ElementTree as ET

GROUPS = ("harness", "theme", "notifications", "movie-cards", "queue")


def junit_report(path: Path, group: str) -> tuple[ET.Element, dict]:
    root = ET.parse(path).getroot()
    if root.tag not in {"testsuite", "testsuites"}:
        raise ValueError("Unsupported JUnit root")
    suites = [s for s in root.iter("testsuite") if not s.findall(".//testsuite")]
    cases = list(root.iter("testcase"))
    if not suites or sum(int(s.attrib["tests"]) for s in suites) != len(cases):
        raise ValueError("Incomplete JUnit counts")
    counts = dict(tests=len(cases), passed=0, failed=0, errors=0, skipped=0)
    public = ET.Element("testsuite", name=group)
    for case in cases:
        # Do not copy hostname, properties, file paths, output, or failure payloads.
        safe = ET.SubElement(public, "testcase", {
            key: case.get(key, "") for key in ("classname", "name")
        })
        states = [tag for tag in ("failure", "error", "skipped") if case.find(tag) is not None]
        if len(states) > 1:
            raise ValueError("Conflicting JUnit outcomes")
        tag = states[0] if states else None
        key = {"failure": "failed", "error": "errors", "skipped": "skipped"}.get(tag, "passed")
        counts[key] += 1
        if tag:
            ET.SubElement(safe, tag, message="See runner-local test diagnostics")
    public.attrib.update({"tests": str(counts["tests"]), "failures": str(counts["failed"]),
                          "errors": str(counts["errors"]), "skipped": str(counts["skipped"])})
    return public, counts


def warning_count(log: Path) -> int | None:
    count = None
    if log.is_file():
        with log.open(encoding="utf-8", errors="replace") as stream:
            for line in stream:
                summary = line.strip().strip("= ")
                if re.fullmatch(r"(?:\d+ [\w]+(?:, )?)+ in \d+(?:\.\d+)?s(?: \([^\n]+\))?", summary):
                    match = re.search(r"(\d+) warnings?", summary)
                    count = int(match[1]) if match else 0
    return count


def publish_group(source: Path, target: Path, group: str) -> dict:
    target.mkdir(parents=True, exist_ok=False)
    result = dict(group=group, status="not_run", returncode=None, process_returncode=None,
                  timed_out=False, counts=None, warnings=None)
    metadata = source / "result.json"
    junit = source / "junit.xml"
    try:
        if metadata.is_file():
            raw = json.loads(metadata.read_text("utf-8"))
            for key in ("returncode", "process_returncode"):
                if type(raw.get(key)) is not int:
                    raise ValueError("Invalid exit status")
                result[key] = raw[key]
            if type(raw.get("timed_out")) is not bool:
                raise ValueError("Invalid timeout status")
            result["timed_out"] = raw["timed_out"]
        if junit.is_file():
            public, result["counts"] = junit_report(junit, group)
            ET.ElementTree(public).write(target / "junit.xml", encoding="utf-8", xml_declaration=True)
            result["warnings"] = warning_count(source / "pytest.log")
            failed = result["counts"]["failed"] or result["counts"]["errors"]
            if metadata.is_file():
                result["status"] = ("failed" if failed or result["returncode"] != 0
                                    or result["timed_out"] else "passed")
            else:
                # The harness step owns its exit status; JUnit alone cannot prove process success.
                result["status"] = "failed" if failed else "reported"
        elif metadata.is_file():
            raise ValueError("Missing JUnit")
    except (OSError, ValueError, KeyError, TypeError, ET.ParseError):
        result["status"] = "report_error"
        result["report_error"] = "Missing or invalid test report; inspect runner-local diagnostics"
        public = ET.Element("testsuite", name=group, tests="1", errors="1")
        case = ET.SubElement(public, "testcase", name="report-integrity")
        ET.SubElement(case, "error", message=result["report_error"])
        ET.ElementTree(public).write(target / "junit.xml", encoding="utf-8", xml_declaration=True)
    text = json.dumps(result, indent=2)
    (target / "result.json").write_text(text + "\n", encoding="utf-8")
    (target / "summary.log").write_text(json.dumps(result) + "\n", encoding="utf-8")
    return result


def publish_reports(source: Path, target: Path) -> tuple[str, int]:
    target.mkdir(parents=True, exist_ok=False)
    rows = [publish_group(source / group, target / group, group) for group in GROUPS]
    lines = ["## Windows UI smoke", "",
             "Each Qt group runs in a fresh process. Counts come from its own JUnit report.", "",
             "| Group | Status | Passed | Failed | Errors | Skipped | Warnings | Exit | Timeout |",
             "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |"]
    for row in rows:
        counts = row["counts"] or {}
        values = [row["group"], row["status"], *(counts.get(k, "—") for k in
                  ("passed", "failed", "errors", "skipped")),
                  row["warnings"] if row["warnings"] is not None else "—",
                  row["returncode"] if row["returncode"] is not None else "—", row["timed_out"]]
        lines.append("| " + " | ".join(map(str, values)) + " |")
    lines += ["", "`reported`: counts available, process exit not recorded. "
              "`not_run`: no report; never counted as passed.", ""]
    summary = "\n".join(lines)
    (target / "summary.md").write_text(summary, encoding="utf-8")
    return summary, int(any(row["status"] == "report_error" for row in rows))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    source, target = args.input_dir.resolve(), args.output_dir.resolve()
    if source == target or source in target.parents or target in source.parents:
        parser.error("input and output must be separate directories")
    summary, code = publish_reports(source, target)
    print(summary)
    if destination := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(destination, "a", encoding="utf-8") as stream:
            stream.write(summary)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
