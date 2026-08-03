"""Committed source-verified holdout artifacts: what they may and may not claim.

After the thirteenth Stage 3.5 commit, ``benchmarks/real_filing_holdout_v1/``
carries three committed artifacts: the manifest (now ``source_verified``), the
untouched selection report, and the source-verification report. These tests
pin the whole chain — the same twenty identities the freeze selected, real
digests populated only by verification, one forward status step, the parser
and protocol hashes unchanged, and prose that never claims what has not
happened: the parser has NOT run over these filings, nothing is extracted or
annotated, and no generalization result exists.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

import real_filing_acquisition as rfa
import real_filing_benchmark as rfb
import real_filing_holdout as rfh
import real_filing_holdout_acquisition as rfha

REPO_ROOT = Path(__file__).resolve().parent.parent
HOLDOUT_DIR = REPO_ROOT / "benchmarks" / "real_filing_holdout_v1"
MANIFEST_PATH = HOLDOUT_DIR / "manifest.json"
SELECTION_REPORT_PATH = HOLDOUT_DIR / "selection_report.json"
SOURCE_REPORT_PATH = HOLDOUT_DIR / "source_verification_report.json"
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "comparison-regression.yml"


@pytest.fixture(scope="module")
def manifest():
    return rfh.load_holdout_manifest(MANIFEST_PATH)


@pytest.fixture(scope="module")
def selection_report():
    return json.loads(SELECTION_REPORT_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def source_report():
    return json.loads(SOURCE_REPORT_PATH.read_text(encoding="utf-8"))


# --- The transition happened, and only the transition --------------------------


def test_manifest_advanced_exactly_one_step(manifest, source_report):
    # The source-verification report still records ITS one step; the manifest
    # has since taken exactly one further documented step (the blind
    # extraction run, recorded by blind_extraction_report.json).
    assert source_report["prior_manifest_status"] == (
        rfh.STATUS_HOLDOUT_FROZEN_METADATA_ONLY
    )
    assert source_report["new_manifest_status"] == rfb.STATUS_SOURCE_VERIFIED
    rfh.validate_holdout_status_transition(
        source_report["prior_manifest_status"],
        source_report["new_manifest_status"],
    )
    rfh.validate_holdout_status_transition(
        source_report["new_manifest_status"], manifest["status"]
    )
    assert manifest["status"] == rfb.STATUS_CORPUS_BUILT
    # And nothing beyond it is claimed.
    assert manifest["status"] != rfb.STATUS_HUMAN_ANNOTATION_COMPLETE


def test_manifest_hash_chain_links_freeze_to_verification(
    selection_report, source_report
):
    """selection freeze -> source verification -> blind run -> committed
    bytes: every link recorded by the report that performed the step."""
    blind_report = json.loads(
        (HOLDOUT_DIR / "blind_extraction_report.json").read_text(
            encoding="utf-8"
        )
    )
    assert source_report["prior_manifest_sha256"] == (
        selection_report["holdout_manifest_sha256"]
    )
    assert blind_report["prior_manifest_sha256"] == (
        source_report["new_manifest_sha256"]
    )
    assert blind_report["new_manifest_sha256"] == rfb.sha256_file(MANIFEST_PATH)
    assert source_report["prior_manifest_sha256"] != (
        source_report["new_manifest_sha256"]
    )


def test_every_side_carries_a_real_verified_digest(manifest):
    digests = set()
    for pair in manifest["pairs"]:
        for side in ("previous", "current"):
            payload = pair[side]
            assert isinstance(payload["expected_sha256"], str)
            assert rfb._SHA256_RE.match(payload["expected_sha256"])
            assert payload["expected_sha256"] != rfb.PLACEHOLDER_SHA256
            assert payload["source_verified"] is True
            digests.add(payload["expected_sha256"])
    assert len(digests) == 20  # twenty distinct documents, twenty digests


def test_committed_manifest_still_passes_the_identity_gate(manifest):
    """The frozen identities still hold at corpus_built; the acquisition
    gate itself now correctly refuses because acquisition has nothing left
    to do beyond source verification."""
    import real_filing_holdout_extraction as rfhe

    rfhe.verify_blind_run_preconditions(
        manifest, rfh.default_holdout_manifest_path()
    )
    with pytest.raises(rfha.HoldoutAcquisitionError) as excinfo:
        rfha.verify_frozen_identity(manifest)
    assert excinfo.value.code == rfha.FAILURE_STATUS_NOT_ACQUIRABLE


# --- No pair was replaced -------------------------------------------------------


def test_verified_pairs_are_exactly_the_frozen_selection(
    manifest, selection_report, source_report
):
    frozen = {
        (pair["cik"], pair["previous_accession"], pair["current_accession"])
        for pair in selection_report["selected_pairs"]
    }
    manifest_now = {
        (
            pair["cik"],
            pair["previous"]["accession_number"],
            pair["current"]["accession_number"],
        )
        for pair in manifest["pairs"]
    }
    verified = {
        (item["cik"], item["accession_number"])
        for item in source_report["filings"]
    }
    assert manifest_now == frozen
    assert verified == {
        (cik, accession)
        for cik, previous, current in frozen
        for accession in (previous, current)
    }
    assert source_report["pair_ids"] == [
        pair["pair_id"] for pair in manifest["pairs"]
    ]


def test_parser_and_protocol_are_unchanged_since_the_freeze(
    manifest, selection_report, source_report
):
    for document in (manifest, source_report):
        assert document["frozen_extraction_parser_version"] == (
            rfh.FROZEN_EXTRACTION_PARSER_VERSION
        )
        assert document["frozen_parser_source_sha256"] == (
            selection_report["frozen_parser_source_sha256"]
        )
        assert document["selection_protocol_hash"] == (
            selection_report["selection_protocol_hash"]
        )
    # The parser bytes on disk still hash to the frozen digest.
    assert rfh.frozen_parser_source_sha256() == (
        manifest["frozen_parser_source_sha256"]
    )


def test_selection_report_was_not_rewritten(selection_report):
    """The freeze record stays the freeze record: its counters still describe
    the metadata-only selection, not this acquisition."""
    assert selection_report["filing_body_requests"] == 0
    assert selection_report["source_documents_downloaded"] == 0
    assert selection_report["source_checksums_verified"] == 0
    assert selection_report["selection_succeeded"] is True


# --- The source-verification report ---------------------------------------------


def test_report_identity_and_outcome(source_report):
    assert source_report["report_version"] == (
        rfha.HOLDOUT_SOURCE_VERIFICATION_REPORT_VERSION
    )
    assert source_report["benchmark_id"] == rfh.HOLDOUT_BENCHMARK_ID
    assert source_report["verification_outcome"] == "source_verified"
    assert source_report["failed_filings"] == 0


def test_report_counts_twenty_verified_sources_and_nothing_else(source_report):
    assert source_report["source_checksums_verified"] == 20
    assert source_report["source_documents_downloaded"] == 20
    assert source_report["filing_body_requests"] == 20
    assert source_report["total_verified_bytes"] > 0
    assert source_report["official_hosts_contacted"] == ["www.sec.gov"]
    assert len(source_report["filings"]) == 20


def test_report_counters_prove_zero_downstream_activity(source_report):
    assert source_report["extraction_runs"] == 0
    assert source_report["comparison_runs"] == 0
    assert source_report["annotation_packets"] == 0
    assert source_report["human_verified_labels"] == 0
    assert source_report["corpus_role"] == rfb.CORPUS_ROLE_EXTRACTION_HOLDOUT
    assert source_report["extraction_parser_developed_using_this_corpus"] is False
    assert source_report["extraction_holdout_evaluation"] is False
    assert source_report["generalization_claim_supported"] is False


def test_report_digests_match_the_manifest_exactly(manifest, source_report):
    from_manifest = {
        (pair["pair_id"], side): pair[side]["expected_sha256"]
        for pair in manifest["pairs"]
        for side in ("previous", "current")
    }
    from_report = {
        (item["pair_id"], item["side"]): item["sha256"]
        for item in source_report["filings"]
    }
    assert from_report == from_manifest


def test_report_records_only_canonical_official_urls(manifest, source_report):
    by_key = {
        (pair["pair_id"], side): rfb.canonical_source_url(
            pair["cik"],
            pair[side]["accession_number"],
            pair[side]["primary_document"],
        )
        for pair in manifest["pairs"]
        for side in ("previous", "current")
    }
    for item in source_report["filings"]:
        url = item["official_source_url"]
        assert url == by_key[(item["pair_id"], item["side"])]
        assert rfa.require_official_url(url) == url
        assert item["verified"] is True
        assert item["form"] == "10-K"
        assert item["acquired_at"]
        assert item["byte_count"] > 0


def test_local_source_bytes_match_the_frozen_digests_if_present(manifest):
    """When the gitignored corpus exists locally, its bytes must still hash to
    the committed digests. Skipped where the corpus was never acquired (CI)."""
    import config

    layout = rfb.CorpusLayout(config.REAL_FILING_HOLDOUT_DIR)
    if not layout.root.exists():
        pytest.skip("local holdout corpus not acquired in this environment")
    for pair in manifest["pairs"]:
        for side in ("previous", "current"):
            target = layout.source_file(
                pair["pair_id"], side, pair[side]["primary_document"]
            )
            assert target.is_file(), f"{pair['pair_id']}/{side}"
            assert rfb.sha256_file(target) == pair[side]["expected_sha256"]


# --- Honest prose ---------------------------------------------------------------

NEGATION_MARKERS = (
    "not", "never", "no ", "cannot", "false", "requires", "is required",
    "before any", "until", "would be", "unseen",
)


@pytest.mark.parametrize(
    "name",
    ("manifest.json", "selection_report.json", "source_verification_report.json"),
)
def test_committed_artifacts_state_their_denials_structurally(name):
    raw = (HOLDOUT_DIR / name).read_text(encoding="utf-8")
    assert '"extraction_holdout_evaluation": false' in raw
    assert '"generalization_claim_supported": false' in raw
    assert '"extraction_holdout_evaluation": true' not in raw
    assert '"generalization_claim_supported": true' not in raw


@pytest.mark.parametrize(
    "name", ("manifest.json", "source_verification_report.json")
)
def test_generalization_language_appears_only_in_denials(name):
    """Any sentence invoking a generalization concept must be negated or
    forward-looking — the same standard the development reports meet."""
    raw = (HOLDOUT_DIR / name).read_text(encoding="utf-8").lower()
    for sentence in re.split(r"(?<=[.;])\s+|\n", raw):
        if not any(root in sentence for root in ("generaliz", "out-of-sample")):
            continue
        assert any(marker in sentence for marker in NEGATION_MARKERS), (
            f"{name} asserts a generalization concept without denying it: "
            f"{sentence.strip()[:160]!r}"
        )


def test_prose_admits_the_blind_run_but_never_claims_verification(
    manifest, source_report
):
    detail = manifest["corpus_role_detail"]
    assert "acquired" in detail  # the stale "never acquired" claim is gone
    assert "checksum-verified" in detail
    # The stale "has NOT run" claim is gone too: the parser has now run,
    # exactly once and unchanged, and the prose says so — while still denying
    # everything that has not happened.
    assert "exactly once" in detail
    assert "blind" in detail
    assert "No label has been human-" in detail
    assert "no generalization claim is supported" in detail
    # The source-verification report is a preserved historical record of ITS
    # step, when the parser genuinely had not run.
    notes = " ".join(source_report["notes"])
    assert "NOT run" in notes


def test_committed_artifacts_leak_no_credentials_paths_or_content():
    for path in (MANIFEST_PATH, SELECTION_REPORT_PATH, SOURCE_REPORT_PATH):
        raw = path.read_text(encoding="utf-8")
        lowered = raw.lower()
        assert "@" not in raw, path.name  # no contact address, no user agent
        assert "sec_user_agent" not in lowered.replace(
            "requires sec_user_agent", ""
        ), path.name  # env var NAME allowed in regenerated_by only
        assert "/users/" not in lowered, path.name
        assert "/home/" not in lowered, path.name
        assert "c:\\" not in lowered, path.name
        assert "<html" not in lowered, path.name
        assert "risk factors" not in lowered, path.name  # no filing prose
        assert "benchmark_data/real_filing_holdout_v1/sources" not in lowered, (
            path.name
        )  # no local layout paths


def test_body_urls_appear_only_in_the_source_verification_report():
    """The metadata-only artifacts still reference no filing body; only the
    report of the acquisition that actually happened records body URLs."""
    for path in (MANIFEST_PATH, SELECTION_REPORT_PATH):
        assert "/archives/" not in path.read_text(encoding="utf-8").lower(), (
            path.name
        )
    report_raw = SOURCE_REPORT_PATH.read_text(encoding="utf-8")
    assert "https://www.sec.gov/Archives/edgar/data/" in report_raw


# --- The corpus itself stays out of the repository ------------------------------


def test_filing_bodies_are_never_committed():
    committed = {path.name for path in HOLDOUT_DIR.iterdir()}
    assert committed == {
        "manifest.json",
        "selection_report.json",
        "source_verification_report.json",
        "blind_extraction_report.json",
        "execution_report.json",
        "annotation_packet_inventory.json",
        # Declares how this corpus is scored. Bounded config, no filing content.
        "evaluation_config.json",
        # The gold evaluation's artifact of record. Metrics, counts, hashes.
        "gold_evaluation_report.json",
    }
    gitignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "benchmark_data/" in gitignore.splitlines()


# --- CI: offline suites only ----------------------------------------------------


def test_required_check_runs_the_source_verification_suites():
    workflow = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    job = workflow["jobs"]["comparison-regression"]
    runs = "\n".join(
        step["run"]
        for step in job["steps"]
        if isinstance(step.get("run"), str) and "pytest" in step["run"]
    )
    for suite in (
        "tests/test_real_filing_holdout_acquisition.py",
        "tests/test_real_filing_holdout_source_verification.py",
    ):
        assert suite in runs, suite
        assert (REPO_ROOT / suite).is_file(), suite


def test_required_check_never_acquires_the_holdout():
    raw = WORKFLOW_PATH.read_text(encoding="utf-8")
    for forbidden in (
        "acquire_real_filing_holdout",
        "fetch_real_filing_benchmark",
        "--allow-network",
        "SEC_USER_AGENT",
        "secrets.",
    ):
        assert forbidden not in raw, forbidden


# --- Documentation --------------------------------------------------------------


def test_documentation_states_the_source_verified_boundaries():
    for doc in ("BENCHMARK.md", "README.MD"):
        lowered = (REPO_ROOT / doc).read_text(encoding="utf-8").lower()
        assert "source_verified" in lowered, doc
        assert "checksum" in lowered, doc
        # The docs must still say the parser has not run over the holdout.
        assert "has not run" in lowered or "not yet been run" in lowered or (
            "not yet run" in lowered
        ), doc
