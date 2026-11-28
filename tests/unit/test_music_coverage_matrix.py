"""Check independent catalog counting, evidence scope, and unknown outcomes."""
import json

import pytest

from tools.music_coverage_matrix import aggregate, main, read_rows


def trial(**updates):
    return {"evidence": "controlled", "catalog": "shared-directory", "platform": "fixture",
            "query": "synthetic-song", "trial": "first", **updates}


def test_two_resolvers_share_one_search_and_two_distinct_pages():
    result = aggregate([
        trial(resolver="resolver-a", backend="shared-service", resource="r1", catalog_hit=True,
              first_result_ms=125, exact_match=True, page=1, page_ok=True, resolved=True),
        trial(resolver="resolver-b", backend="shared-service", resource="r2", catalog_hit=True,
              first_result_ms=125, page=1, page_ok=True, resolved=False),
        trial(page=2, page_ok=True),
    ])["evidence"]["controlled"]
    assert result["catalog_count"] == result["search_trial_count"] == 1
    catalog, = result["catalogs"]
    assert catalog["first_result_ms"] == {"observed": 1, "p50": 125, "p95": 125, "max": 125}
    assert catalog["pages_observed"] == [1, 2]
    assert catalog["page_outcomes"]["page_ok"] == {
        "yes": 2, "no": 0, "unknown": 0, "observed": 2, "rate": 1,
    }
    first, second = result["resolvers"]
    assert first["resource_outcomes"]["resolved"]["yes"] == 1
    assert second["resource_outcomes"]["resolved"]["no"] == 1
    assert first["backends_observed"] == second["backends_observed"] == ["shared-service"]


def test_trial_platform_catalog_and_evidence_identities_are_distinct():
    evidence = aggregate([
        trial(catalog_hit=True),
        trial(trial="second", catalog_hit=False),
        trial(platform="second-platform"),
        trial(catalog="other-directory", exact_match=None),
        trial(evidence="authorized_public", evidence_ref="public-proof-01", catalog_hit=False),
    ])["evidence"]
    controlled = evidence["controlled"]
    assert (controlled["catalog_count"], controlled["search_trial_count"]) == (2, 4)
    first_platform = next(row for row in controlled["catalogs"]
                          if row["catalog"] == "shared-directory" and row["platform"] == "fixture")
    assert first_platform["catalog_outcomes"]["catalog_hit"]["rate"] == .5
    assert first_platform["catalog_outcomes"]["exact_match"] == {
        "yes": 0, "no": 0, "unknown": 2, "observed": 0, "rate": None,
    }
    assert evidence["authorized_public"]["catalogs"][0]["catalog_outcomes"]["catalog_hit"]["rate"] == 0
    assert evidence["authorized_commercial"]["catalog_count"] == 0


def test_resource_only_public_observation_does_not_count_as_a_search():
    evidence = aggregate([
        trial(evidence="authorized_public", evidence_ref="known-file-proof", resolver="approved",
              resource="known-file", readable=True, decoded=True),
        trial(trial="unknown-search", resolver="approved", catalog_hit=None),
        trial(trial="page-probe", resolver="approved", page=2, page_ok=True),
    ])["evidence"]
    public = evidence["authorized_public"]
    assert public["catalog_count"] == public["search_trial_count"] == 0
    assert public["catalogs"] == []
    assert public["resolvers"][0]["resource_outcomes"]["decoded"]["yes"] == 1
    controlled = evidence["controlled"]
    assert controlled["search_trial_count"] == 2
    assert controlled["catalogs"][0]["catalog_outcomes"]["catalog_hit"]["unknown"] == 2


def test_resource_evidence_merges_fact_rows_without_treating_probe_bytes_as_full_size():
    resource = {"resolver": "approved-resolver", "resource": "r1"}
    result = aggregate([
        trial(**resource, declared_quality="lossless", predownload_bytes=64_000,
              size_evidence="content_range", probe_prefix_bytes=4096, resolved=True),
        trial(**resource, verified_quality="lossy", quality_agrees=False, readable=True,
              decoded=True, continuous_playback=True, played_seconds=15, best_download=True,
              exact_download=False, lyrics=True, cover=None, recovered=True),
        trial(resolver="approved-resolver", resource="r2",
              declared_quality="hires", probe_prefix_bytes=1024),
    ])["evidence"]["controlled"]["resolvers"][0]
    assert result["resource_attempts"] == 2
    assert result["probe_prefix_bytes"]["observed"] == 2
    assert result["predownload_bytes_by_evidence"]["content_range"]["p50"] == 64_000
    assert result["predownload_bytes_by_evidence"]["head"]["observed"] == 0
    assert result["quality_pairs"] == [
        {"declared": "hires", "verified": None, "observed": 1},
        {"declared": "lossless", "verified": "lossy", "observed": 1},
    ]
    assert result["resource_outcomes"]["quality_agrees"]["no"] == 1
    assert result["resource_outcomes"]["decoded"]["yes"] == 1
    assert result["resource_outcomes"]["cover"]["unknown"] == 2


@pytest.mark.parametrize(("row", "reason"), [
    (trial(evidence="authorized_commercial"), "evidence_ref"),
    (trial(catalog="https://localhost/private"), "catalog identifier"),
    (trial(first_result_ms=True), "first_result_ms"),
    (trial(first_result_ms=float("inf")), "first_result_ms"),
    (trial(page_ok=True), "requires page"),
    (trial(readable=True), "require resolver"),
    (trial(resolver="r", size_evidence=[]), "size_evidence"),
    (trial(resolver="r", predownload_bytes=1024), "supplied together"),
    (trial(resolver="r", verified_quality="lossless", quality_agrees=True), "requires declared"),
])
def test_rejects_unscoped_or_malformed_evidence(row, reason):
    with pytest.raises(ValueError, match=reason):
        aggregate([row])


def test_contradictory_catalog_results_cannot_be_resolved_by_record_order():
    with pytest.raises(ValueError, match="row 2: contradictory catalog_hit"):
        aggregate([trial(resolver="a", catalog_hit=True), trial(resolver="b", catalog_hit=False)])


def test_cli_combines_json_and_jsonl_and_preserves_existing_report(tmp_path, capsys):
    one = tmp_path / "first.json"
    two = tmp_path / "second.jsonl"
    output = tmp_path / "report.json"
    one.write_text(json.dumps([trial(page=1, page_ok=True)]), encoding="utf-8")
    two.write_text(json.dumps(trial(page=2, page_ok=True)) + "\n", encoding="utf-8")
    args = [str(one), str(two), "-o", str(output)]
    assert main(args) == 0
    before = output.read_bytes()
    summary = json.loads(before)["evidence"]["controlled"]
    assert summary["search_trial_count"] == 1
    assert summary["catalogs"][0]["pages_observed"] == [1, 2]
    assert main(args) == 2
    assert output.read_bytes() == before
    assert "coverage matrix:" in capsys.readouterr().err


def test_duplicate_jsonl_fields_report_the_input_line(tmp_path):
    input_file = tmp_path / "in.jsonl"
    input_file.write_text('\n{"catalog":"a","catalog":"b"}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="JSONL line 2: duplicate JSON key"):
        list(read_rows(input_file))
