"""The committed v3 extraction holdout at ``source_verified``.

Where ``test_v3_holdout_source_acquisition.py`` covers the mechanism, this
suite covers the artifacts that mechanism produced: the advanced manifest, the
bounded source-verification report, their hash chain back to the metadata-only
freeze, and every denial that must still hold.

Entirely offline. It reads committed JSON and, when the gitignored local corpus
happens to be present, re-hashes local bytes. It never contacts SEC EDGAR,
never supplies a user agent, and never runs a parser over a filing.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

import real_filing_acquisition as rfa
import real_filing_benchmark as rfb
import real_filing_v3_holdout as rfv3
import real_filing_v3_holdout_acquisition as rfv3a

REPO_ROOT = Path(__file__).resolve().parent.parent
V3_DIR = REPO_ROOT / "benchmarks" / "real_filing_v3_holdout_v1"
MANIFEST_PATH = V3_DIR / "manifest.json"
SELECTION_REPORT_PATH = V3_DIR / "selection_report.json"
SOURCE_REPORT_PATH = V3_DIR / "source_verification_report.json"
CONFIG_PATH = V3_DIR / "evaluation_config.json"
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "comparison-regression.yml"

#: The metadata-only manifest hash the committed selection report froze. The
#: transition must chain from exactly this value.
FROZEN_METADATA_ONLY_MANIFEST_SHA256 = (
    "8c1f5fc52d7dbe4a09bcb07b0d30d408f8844331808ffe72e91a0b644aa7be46"
)
#: The selection report and evaluation config are historical artifacts of
#: earlier steps and must survive this one byte-identical.
FROZEN_SELECTION_REPORT_SHA256 = (
    "900f59642f3eef9855641b4074dad4bfcfc5572513855f60e72291bdd6fe1bc2"
)
FROZEN_EVALUATION_CONFIG_SHA256 = (
    "92f970646c03b7c87927c4055eff3435ecfc9c45809fd452a2a76bdd197c1991"
)

NEGATION_MARKERS = (
    "not", "never", "no ", "cannot", "false", "requires", "is required",
    "before any", "until", "would be", "unseen",
)


@pytest.fixture(scope="module")
def manifest():
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def source_report():
    return json.loads(SOURCE_REPORT_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def selection_report():
    return json.loads(SELECTION_REPORT_PATH.read_text(encoding="utf-8"))


# --- The transition -------------------------------------------------------------


def test_manifest_advanced_exactly_one_step(manifest, source_report):
    assert manifest["status"] == rfb.STATUS_SOURCE_VERIFIED
    assert source_report["prior_manifest_status"] == (
        rfv3.STATUS_HOLDOUT_FROZEN_METADATA_ONLY
    )
    assert source_report["new_manifest_status"] == rfb.STATUS_SOURCE_VERIFIED
    order = list(rfv3.V3_HOLDOUT_STATUS_ORDER)
    assert (
        order.index(source_report["new_manifest_status"])
        - order.index(source_report["prior_manifest_status"])
        == 1
    )
    # And it is a legal forward step by the shared rule, not just by index.
    rfv3.validate_v3_holdout_status_transition(
        source_report["prior_manifest_status"], manifest["status"]
    )


def test_committed_manifest_is_schema_valid(manifest):
    rfv3.validate_v3_holdout_manifest(manifest)


def test_manifest_hash_chain_links_the_freeze_to_the_verification(
    source_report, selection_report
):
    """freeze -> selection report -> source report -> advanced manifest."""
    assert selection_report["holdout_manifest_sha256"] == (
        FROZEN_METADATA_ONLY_MANIFEST_SHA256
    )
    assert source_report["prior_manifest_sha256"] == (
        FROZEN_METADATA_ONLY_MANIFEST_SHA256
    )
    assert source_report["new_manifest_sha256"] == rfb.sha256_file(MANIFEST_PATH)
    assert source_report["new_manifest_sha256"] != (
        source_report["prior_manifest_sha256"]
    )
    assert rfb._SHA256_RE.match(source_report["new_manifest_sha256"])


def test_reproducible_manifest_hash_is_recorded_and_correct(manifest, source_report):
    assert source_report["new_reproducible_manifest_hash"] == (
        rfv3.reproducible_manifest_hash(manifest)
    )


def test_committed_report_records_the_transition_not_a_later_rerun(source_report):
    """A rerun verifies; it does not re-transition.

    The committed report is the record of the acquisition that actually moved
    the manifest: twenty downloads and a prior hash that differs from the new
    one. A re-verification run reports zero downloads and identical prior/new
    hashes — writing that over this file would erase the chain back to the
    metadata-only freeze, so the CLI keeps re-verification out of
    ``benchmarks/``.
    """
    assert source_report["prior_manifest_sha256"] != (
        source_report["new_manifest_sha256"]
    )
    assert source_report["prior_manifest_status"] != (
        source_report["new_manifest_status"]
    )
    assert source_report["source_documents_downloaded"] == 20
    assert source_report["reused_verified_cache"] == 0
    assert source_report["successful_body_requests"] == 20


def test_cli_does_not_overwrite_the_committed_report_on_a_rerun():
    source = (
        REPO_ROOT / "scripts" / "acquire_real_filing_v3_holdout.py"
    ).read_text(encoding="utf-8")
    assert "REVERIFICATION_REPORT_NAME" in source
    assert "if transitioned or args.report_out:" in source


def test_every_side_carries_a_real_verified_digest(manifest):
    digests = []
    for pair in manifest["pairs"]:
        for side in ("previous", "current"):
            digest = pair[side]["expected_sha256"]
            assert isinstance(digest, str)
            assert rfb._SHA256_RE.match(digest), digest
            assert digest != rfb.PLACEHOLDER_SHA256
            assert digest == digest.lower()
            assert pair[side]["source_verified"] is True
            digests.append(digest)
    assert len(digests) == 20
    assert len(set(digests)) == 20  # twenty distinct filings, never merged


def test_committed_manifest_still_passes_the_identity_gate(manifest):
    rfv3a.verify_frozen_identity(manifest, repo_root=REPO_ROOT)
    rfv3a.verify_manifest_hash_chain(MANIFEST_PATH)


def test_frozen_code_identities_are_live_pinned(manifest):
    rfv3.verify_frozen_code_identities(manifest, REPO_ROOT)
    rfv3.verify_exclusion_provenance(manifest, REPO_ROOT)


# --- No filing was replaced -----------------------------------------------------


def test_verified_pairs_are_exactly_the_frozen_selection(manifest, selection_report):
    """The identities in the advanced manifest are the ones the freeze chose.

    Compared against the selection report — an artifact of the earlier step
    that this commit does not rewrite — so a swapped issuer or accession is
    caught by an independent record.
    """
    selected = {pair["cik"]: pair for pair in selection_report["selected_pairs"]}
    assert len(selected) == 10
    assert {pair["cik"] for pair in manifest["pairs"]} == set(selected)
    for pair in manifest["pairs"]:
        frozen = selected[pair["cik"]]
        assert pair["issuer_name"] == frozen["issuer_name"]
        assert pair["sic"] == frozen["sic"]
        assert pair["stratum_id"] == frozen["stratum_id"]
        assert pair["previous"]["accession_number"] == frozen["previous_accession"]
        assert pair["current"]["accession_number"] == frozen["current_accession"]


def test_pair_and_side_ordering_is_unchanged(manifest):
    assert [pair["pair_id"] for pair in manifest["pairs"]] == [
        "sic-2000s-01", "sic-2000s-02", "sic-3000s-01", "sic-3000s-02",
        "sic-4000s-01", "sic-4000s-02", "sic-5000s-01", "sic-5000s-02",
        "sic-6000s-01", "sic-6000s-02",
    ]


def test_selection_report_and_evaluation_config_were_not_rewritten():
    assert rfb.sha256_file(SELECTION_REPORT_PATH) == FROZEN_SELECTION_REPORT_SHA256
    assert rfb.sha256_file(CONFIG_PATH) == FROZEN_EVALUATION_CONFIG_SHA256


def test_selection_protocol_is_unchanged_since_the_freeze(manifest):
    assert manifest["selection_protocol_version"] == (
        "real-filing-v3-holdout-selection.v1"
    )
    assert manifest["selection_protocol_hash"] == rfv3.selection_protocol_hash()


# --- The report -----------------------------------------------------------------


def test_report_identity_and_outcome(source_report):
    assert source_report["report_version"] == (
        "real-filing-v3-holdout.source-verification.v1"
    )
    assert source_report["benchmark_id"] == "real_filing_v3_holdout_v1"
    assert source_report["verification_outcome"] == "source_verified"
    assert source_report["source_acquisition_protocol_version"] == (
        "real-filing-v3-source-acquisition.v1"
    )
    assert source_report["source_acquisition_protocol_hash"] == (
        rfv3a.source_acquisition_protocol_hash()
    )
    assert source_report["source_acquisition_protocol"] == (
        rfv3a.source_acquisition_protocol()
    )


def test_report_counts_twenty_verified_sources_and_nothing_else(source_report):
    assert source_report["pair_count"] == 10
    assert source_report["side_count"] == 20
    assert source_report["verified_source_count"] == 20
    assert source_report["source_checksums_verified"] == 20
    assert source_report["failed_source_count"] == 0
    assert source_report["failure_counts_by_reason"] == {}
    assert source_report["official_hosts_contacted"] == ["www.sec.gov"]
    assert source_report["hash_algorithm"] == "sha256"
    assert source_report["total_verified_bytes"] > 0
    assert len(source_report["filings"]) == 20


def test_report_counters_prove_zero_downstream_activity(source_report):
    for field in (
        "extraction_runs", "comparison_runs", "annotation_packets",
        "machine_proposed_labels", "human_verified_labels",
        "gold_evaluation_runs",
    ):
        assert source_report[field] == 0, field
    assert source_report["signoff_present"] is False
    assert source_report["extraction_holdout_evaluation"] is False
    assert source_report["generalization_claim_supported"] is False


def test_report_digests_match_the_manifest_exactly(manifest, source_report):
    rfv3a.verify_report_manifest_binding(source_report, manifest)
    reported = {
        (item["pair_id"], item["side"]): item for item in source_report["filings"]
    }
    total = 0
    for pair in manifest["pairs"]:
        for side in ("previous", "current"):
            item = reported[(pair["pair_id"], side)]
            assert item["sha256"] == pair[side]["expected_sha256"]
            assert item["source_verified"] is True
            assert item["cik"] == pair["cik"]
            assert item["accession_number"] == pair[side]["accession_number"]
            assert item["primary_document"] == pair[side]["primary_document"]
            assert item["byte_count"] > 0
            total += item["byte_count"]
    assert total == source_report["total_verified_bytes"]


def test_report_records_only_canonical_official_urls(manifest, source_report):
    reported = {
        (item["pair_id"], item["side"]): item for item in source_report["filings"]
    }
    for pair in manifest["pairs"]:
        for side in ("previous", "current"):
            item = reported[(pair["pair_id"], side)]
            expected = rfv3a.v3_body_url(pair, pair[side])
            assert item["official_source_url"] == expected
            assert item["final_source_url_equals_canonical"] is True
            rfv3a.require_v3_body_url(
                item["official_source_url"],
                cik=pair["cik"],
                accession=pair[side]["accession_number"],
                primary_document=pair[side]["primary_document"],
            )


def test_report_records_no_redirect_and_a_bounded_retry_policy(source_report):
    assert source_report["redirect_count"] == 0
    assert source_report["retry_policy"] == rfa.ACQUISITION_RETRY_POLICY
    assert source_report["request_attempts"] >= 20


def test_report_local_paths_are_relative_and_deterministic(manifest, source_report):
    reported = {
        (item["pair_id"], item["side"]): item for item in source_report["filings"]
    }
    for pair in manifest["pairs"]:
        for side in ("previous", "current"):
            item = reported[(pair["pair_id"], side)]
            assert item["local_source_path"] == (
                f"sources/{pair['pair_id']}/{side}/"
                f"{pair[side]['primary_document']}"
            )
            assert not Path(item["local_source_path"]).is_absolute()
    assert source_report["local_path_convention"] == rfv3a.LOCAL_PATH_CONVENTION


def test_report_states_the_entity_byte_semantics(source_report):
    semantics = source_report["source_byte_semantics"].lower()
    assert "content-encoding" in semantics
    assert "before any text decoding" in semantics
    assert "normalization" in semantics


def test_report_and_filing_ordering_is_deterministic(manifest, source_report):
    assert source_report["pair_ids"] == [
        pair["pair_id"] for pair in manifest["pairs"]
    ]
    assert [(item["pair_id"], item["side"]) for item in source_report["filings"]] == [
        (pair["pair_id"], side)
        for pair in manifest["pairs"]
        for side in ("previous", "current")
    ]


# --- Local bytes ----------------------------------------------------------------


def test_local_source_bytes_match_the_frozen_digests_if_present(manifest):
    """When the gitignored corpus exists locally, every byte still verifies.

    Skipped on a clean clone and in CI, where the corpus does not exist by
    design — the committed digests are the artifact, not the bodies.
    """
    import config

    layout = rfb.CorpusLayout(config.REAL_FILING_V3_HOLDOUT_DIR)
    if not layout.root.exists():
        pytest.skip("local v3 holdout corpus not present (expected in CI)")
    checked = 0
    for pair in manifest["pairs"]:
        for side in ("previous", "current"):
            target = layout.source_file(
                pair["pair_id"], side, pair[side]["primary_document"]
            )
            if not target.exists():
                continue
            assert rfb.sha256_file(target) == pair[side]["expected_sha256"], (
                f"{pair['pair_id']}/{side}"
            )
            checked += 1
    if checked:
        assert checked == 20, "a partial local corpus is not a verified corpus"


# --- Denials and prose ----------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ("manifest.json", "selection_report.json", "source_verification_report.json"),
)
def test_committed_artifacts_state_their_denials_structurally(name):
    raw = (V3_DIR / name).read_text(encoding="utf-8")
    assert '"extraction_holdout_evaluation": false' in raw
    assert '"generalization_claim_supported": false' in raw
    assert '"extraction_holdout_evaluation": true' not in raw
    assert '"generalization_claim_supported": true' not in raw


@pytest.mark.parametrize("name", ("manifest.json", "source_verification_report.json"))
def test_generalization_language_appears_only_in_denials(name):
    raw = (V3_DIR / name).read_text(encoding="utf-8").lower()
    for sentence in re.split(r"(?<=[.;])\s+|\n", raw):
        if not any(root in sentence for root in ("generaliz", "out-of-sample")):
            continue
        assert any(marker in sentence for marker in NEGATION_MARKERS), (
            f"{name} asserts a generalization concept without denying it: "
            f"{sentence.strip()[:160]!r}"
        )


def test_prose_admits_the_acquisition_but_claims_nothing_downstream(
    manifest, source_report
):
    detail = manifest["corpus_role_detail"]
    # The stale "No filing body has been acquired" claim is gone...
    assert "acquired" in detail
    assert "checksum-verified" in detail
    assert "No filing body has been acquired" not in detail
    # ...and everything that has NOT happened is still denied.
    assert "has NOT run" in detail
    assert "no generalization claim is supported" in detail
    assert "Source verification establishes" in detail

    notes = " ".join(source_report["notes"])
    assert "NOT run" in notes
    assert "not parser validation" in notes
    assert "never silently re-pins" in notes


def test_no_artifact_claims_extractable_item_1a_sections():
    """Source verification says nothing about Item 1A. Neither may the prose."""
    for path in (MANIFEST_PATH, SOURCE_REPORT_PATH):
        raw = path.read_text(encoding="utf-8").lower()
        for sentence in re.split(r"(?<=[.;])\s+|\n", raw):
            if "item 1a" not in sentence:
                continue
            assert any(marker in sentence for marker in NEGATION_MARKERS), (
                f"{path.name} asserts something about Item 1A without denying "
                f"it: {sentence.strip()[:160]!r}"
            )


def test_committed_artifacts_leak_no_credentials_paths_or_content():
    for path in (MANIFEST_PATH, SELECTION_REPORT_PATH, SOURCE_REPORT_PATH, CONFIG_PATH):
        raw = path.read_text(encoding="utf-8")
        lowered = raw.lower()
        assert "@" not in raw, path.name  # no contact address, no user agent
        assert "sec_user_agent" not in lowered.replace(
            "requires sec_user_agent", ""
        ), path.name  # env var NAME allowed in regenerated_by only
        assert "/users/" not in lowered, path.name
        assert "/home/" not in lowered, path.name
        assert "c:\\" not in lowered, path.name
        assert str(REPO_ROOT).lower() not in lowered, path.name
        assert "<html" not in lowered, path.name
        assert "risk factors" not in lowered, path.name  # no filing prose
        assert "cookie" not in lowered, path.name
        assert "set-cookie" not in lowered, path.name
        # Naming the gitignored tree in a note is fine; leaking the local
        # source layout or an absolute path is not.
        assert "benchmark_data/real_filing_v3_holdout_v1" not in lowered, path.name


def test_body_urls_appear_only_in_the_source_verification_report():
    for path in (MANIFEST_PATH, SELECTION_REPORT_PATH, CONFIG_PATH):
        assert "/archives/" not in path.read_text(encoding="utf-8").lower(), path.name
    report_raw = SOURCE_REPORT_PATH.read_text(encoding="utf-8")
    assert "https://www.sec.gov/Archives/edgar/data/" in report_raw


# --- The corpus itself stays out of the repository ------------------------------


def test_filing_bodies_are_never_committed():
    committed = {path.name for path in V3_DIR.iterdir()}
    assert committed == {
        "manifest.json",
        "selection_report.json",
        "source_verification_report.json",
        "evaluation_config.json",
    }
    gitignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "benchmark_data/" in gitignore.splitlines()
    assert not (REPO_ROOT / "benchmarks" / "real_filing_v3_holdout_v1" / "sources").exists()


def test_no_source_document_is_tracked_by_git():
    import subprocess

    tracked = subprocess.run(
        ["git", "ls-files"], cwd=REPO_ROOT, capture_output=True, text=True, check=True
    ).stdout.splitlines()
    for path in tracked:
        assert not path.startswith("benchmark_data/"), path
        assert not path.endswith((".htm", ".html")) or path.startswith("frontend/"), (
            path
        )


# --- CI: offline suites only ----------------------------------------------------


def test_required_check_runs_the_v3_source_verification_suites():
    workflow = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    job = workflow["jobs"]["comparison-regression"]
    runs = "\n".join(
        step["run"]
        for step in job["steps"]
        if isinstance(step.get("run"), str) and "pytest" in step["run"]
    )
    for suite in (
        "tests/test_v3_holdout_source_acquisition.py",
        "tests/test_v3_holdout_source_verification.py",
    ):
        assert suite in runs, suite
        assert (REPO_ROOT / suite).is_file(), suite


def test_required_check_identity_and_triggers_are_unchanged():
    workflow = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    assert workflow["name"] == "comparison-regression"
    assert set(workflow["jobs"]) == {"comparison-regression"}
    assert workflow["jobs"]["comparison-regression"]["name"] == (
        "comparison-regression"
    )
    triggers = workflow[True] if True in workflow else workflow["on"]
    assert "pull_request" in triggers
    assert triggers["push"]["branches"] == ["main"]
    # No path filters: the job always runs.
    assert triggers["pull_request"] in (None, {})


def test_required_check_never_acquires_the_v3_holdout():
    raw = WORKFLOW_PATH.read_text(encoding="utf-8")
    for forbidden in (
        "acquire_real_filing_v3_holdout",
        "acquire_real_filing_holdout",
        "select_real_filing_v3_holdout",
        "fetch_real_filing_benchmark",
        "--allow-network",
        "SEC_USER_AGENT",
        "secrets.",
        "aws-actions",
    ):
        assert forbidden not in raw, forbidden


def test_required_check_retains_the_regression_evaluator_and_artifact_upload():
    workflow = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["comparison-regression"]["steps"]
    runs = "\n".join(
        step["run"] for step in steps if isinstance(step.get("run"), str)
    )
    assert "scripts/eval_comparison_regression.py" in runs
    assert any(
        str(step.get("uses", "")).startswith("actions/upload-artifact")
        for step in steps
    )


# --- Documentation --------------------------------------------------------------


def test_documentation_states_the_v3_source_verified_boundaries():
    for doc in ("BENCHMARK.md", "README.MD"):
        raw = (REPO_ROOT / doc).read_text(encoding="utf-8")
        lowered = raw.lower()
        assert "real_filing_v3_holdout_v1" in lowered, doc
        assert "source_verified" in lowered, doc
        assert "checksum" in lowered, doc
        assert any(
            phrase in lowered
            for phrase in ("has not run", "not yet been run", "not yet run")
        ), doc


def test_documentation_makes_no_v3_accuracy_or_generalization_claim():
    for doc in ("BENCHMARK.md", "README.MD"):
        raw = (REPO_ROOT / doc).read_text(encoding="utf-8").lower()
        # Collapse markdown line wrapping first: a wrapped line is not a
        # sentence boundary, and splitting on it would strip the negation
        # off the front of a properly hedged sentence.
        lowered = re.sub(r"\s+", " ", raw)
        for sentence in re.split(r"(?<=[.;])\s+", lowered):
            # Scoped to sentences that actually invoke the v3 holdout. The
            # docs legitimately discuss the completed v2 holdout evaluation
            # and its real metrics; what this commit must not introduce is an
            # unqualified accuracy or generalization claim about v3.
            if not any(
                marker in sentence
                for marker in ("v3 holdout", "real_filing_v3_holdout_v1")
            ):
                continue
            if not any(
                root in sentence
                for root in ("generaliz", "out-of-sample", "accuracy", "represent")
            ):
                continue
            assert any(marker in sentence for marker in NEGATION_MARKERS), (
                f"{doc} makes an unqualified v3 claim: {sentence.strip()[:160]!r}"
            )


def test_holdout_evaluation_doc_is_untouched():
    """HOLDOUT_EVALUATION.md records the v1-holdout gold evaluation and is not
    this commit's business."""
    assert rfb.sha256_file(REPO_ROOT / "HOLDOUT_EVALUATION.md") == (
        "10a0b4ca10fb1a5a565355ae22b8539cde5c2f4c56b946c6ea9ba3c41a739c1c"
    )
