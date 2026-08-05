"""Merge-blocking tests for the v3 holdout human-review preparation and
admission contract.

Every test runs over SYNTHETIC fixtures in temporary directories. The frozen v3
holdout identities are reused (they are public metadata), but every body is a
hand-written fictional HTML document from ``tests/helpers/v3_blind_fixtures``.
No real filing excerpt, no real packet body, no real machine proposal, and no
file from the local review workspace is ever read or copied into a test, and no
human annotation of any kind exists in the repository.

The synthetic corpus reproduces the committed shape exactly — eight review-ready
pairs, two blocked pairs, zero human-verified labels — and includes a pair whose
current side repeats a normalized heading, so the canonical
``side:sequence:unit_key`` identity is exercised rather than assumed.

What this suite pins:

- provenance and immutability: committed inventory bindings, packet and machine
  proposal hashes (evaluated content AND raw bytes), comparison result hashes,
  source and section digests, and the rule that a human review file is a
  separate artifact which can never overwrite the machine proposal;
- the deterministic review queue: committed-inventory ordering, exactly-once
  membership, no quality-based sorting, bounded prose-free metadata, and no
  human decision before the reviewer supplies one;
- the human-only guarantees: no tool writes a label, an annotator id, a
  verification timestamp, a completion marker without an explicit confirmation,
  or a sign-off; nothing calls a model, a heuristic, the gold evaluator, or an
  agreement metric; file presence and unchanged machine values are never
  approval;
- annotation validation: canonical subject shape, closure over the build's unit
  inventory exactly once, evidence-reference consistency, undetermined reasons,
  direction shape, duplicate and unknown identities, and order independence;
- admission metadata: explicit ``human_verified`` status, non-placeholder
  annotator, explicit-UTC timestamp postdating packet generation, and the
  refusal of partial reviewer metadata;
- safety and frozen compatibility: no network, no credentials, no parser or
  detector execution, no packet regeneration, no metric output, no absolute
  paths in tool output, and byte-identical committed corpus artifacts.
"""

from __future__ import annotations

import ast
import copy
import json
import shutil
from datetime import timedelta
from pathlib import Path

import pytest

import real_filing_benchmark as rfb
import real_filing_v3_holdout as rfv3
import real_filing_v3_human_review as hr
from scripts import prepare_v3_holdout_human_review as prep
from scripts import run_real_filing_v3_holdout_blind_extraction as blind_cli
from scripts import validate_v3_holdout_human_annotations as vha
from tests.helpers import v3_blind_fixtures as fx

REPO_ROOT = Path(__file__).resolve().parent.parent
COMMITTED_MANIFEST = rfv3.load_v3_holdout_manifest()
COMMITTED_REPORT_DIR = REPO_ROOT / "benchmarks" / rfv3.V3_HOLDOUT_BENCHMARK_ID

#: The committed corpus blocks the pair at index 2 (missing on both sides) and
#: the pair at index 9 (ambiguous on both sides). One synthetic failing side is
#: enough to reproduce the blocked shape, and the repeated-heading fixture goes
#: on a third pair so a repeated normalized heading is always in the workspace.
MISSING_INDEX = 2
AMBIGUOUS_INDEX = 9
REPEATED_HEADING_INDEX = 1

#: A deterministic stand-in for the metadata-only selection-freeze hash: the
#: synthetic chain starts here, one step before the source-verified hash.
_SELECTION_FREEZE_HASH = "f" * 64

REVIEWER = "synthetic-reviewer-01"


# --- Synthetic workspace ------------------------------------------------------


def _synthetic():
    return fx.synthetic_manifest(
        COMMITTED_MANIFEST,
        missing_current_index=MISSING_INDEX,
        ambiguous_previous_index=AMBIGUOUS_INDEX,
        repeated_heading_index=REPEATED_HEADING_INDEX,
    )


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    """One blind pipeline run over the synthetic corpus plus the synthesized
    chain reports: the exact state the review preparation must accept."""
    base = tmp_path_factory.mktemp("v3_human_review")
    root = fx.untracked_root(base, "v3_review")
    document, contents = _synthetic()
    manifest_path = root / "manifest.json"
    fx.write_manifest(manifest_path, document)
    fx.seed_corpus(root / "corpus", document, contents)
    report_dir = root / "reports"

    assert (
        blind_cli.main(
            [
                "--manifest", str(manifest_path),
                "--corpus-dir", str(root / "corpus"),
                "--report-dir", str(report_dir),
            ]
        )
        == 0
    )

    blind = json.loads(
        (report_dir / "blind_extraction_report.json").read_text(encoding="utf-8")
    )
    frozen_parser = document["frozen_parser_source_sha256"]
    rfb.write_json_atomic(
        report_dir / "source_verification_report.json",
        {
            "prior_manifest_sha256": _SELECTION_FREEZE_HASH,
            "new_manifest_sha256": blind["prior_manifest_sha256"],
            "frozen_parser_source_sha256": frozen_parser,
        },
    )
    rfb.write_json_atomic(
        report_dir / "selection_report.json",
        {
            "holdout_manifest_sha256": _SELECTION_FREEZE_HASH,
            "frozen_parser_source_sha256": frozen_parser,
        },
    )
    shutil.copyfile(
        COMMITTED_REPORT_DIR / "evaluation_config.json",
        report_dir / "evaluation_config.json",
    )
    inventory = json.loads(
        (report_dir / "annotation_packet_inventory.json").read_text(encoding="utf-8")
    )
    # The synthetic corpus reproduces the committed 8-written / 2-blocked shape.
    assert inventory["packets_written"] == 8
    assert inventory["packets_blocked"] == 2
    assert inventory["human_verified_label_count"] == 0
    return {"root": root, "inventory": inventory}


@pytest.fixture
def ws(built, tmp_path):
    """A disposable prepared workspace: one copy of the built corpus with the
    review queue, review records, and empty templates created."""
    root = tmp_path / "ws"
    shutil.copytree(built["root"], root)
    assert _prep(root, "prepare") == 0
    return root


@pytest.fixture
def raw(built, tmp_path):
    """A disposable built-but-unprepared workspace."""
    root = tmp_path / "raw"
    shutil.copytree(built["root"], root)
    return root


# --- Helpers ------------------------------------------------------------------


def _layout(root: Path) -> rfb.CorpusLayout:
    return rfb.CorpusLayout(root / "corpus")


def _args(root: Path) -> list[str]:
    return [
        "--manifest", str(root / "manifest.json"),
        "--corpus-dir", str(root / "corpus"),
        "--report-dir", str(root / "reports"),
    ]


def _prep(root: Path, *command: str) -> int:
    return prep.main([*_args(root), *command])


def _validate(root: Path, *extra: str) -> int:
    return vha.main([*_args(root), *extra])


def _findings(root: Path, mode: str, pair_id: str | None = None) -> hr.Findings:
    return hr.run_validation(
        layout=_layout(root),
        manifest_path=root / "manifest.json",
        report_dir=root / "reports",
        mode=mode,
        pair_id=pair_id,
    )


def _codes(findings: hr.Findings) -> set[str]:
    return {row["code"].split(":")[0] for row in findings.failed if row["code"]}


def _inventory(root: Path) -> dict:
    return json.loads(
        (root / "reports" / "annotation_packet_inventory.json").read_text(
            encoding="utf-8"
        )
    )


def _queue(root: Path) -> dict:
    return json.loads(
        hr.review_queue_path(_layout(root)).read_text(encoding="utf-8")
    )


def _ready_ids(root: Path) -> list[str]:
    return [str(row["pair_id"]) for row in hr.review_ready_rows(_inventory(root))]


def _blocked_ids(root: Path) -> list[str]:
    return [str(row["pair_id"]) for row in hr.blocked_rows(_inventory(root))]


def _edit_json(path: Path, mutate) -> None:
    document = json.loads(path.read_text(encoding="utf-8"))
    mutate(document)
    path.write_text(json.dumps(document, indent=2, sort_keys=True), encoding="utf-8")


def _later(timestamp: str, hours: int = 1) -> str:
    return (hr.parse_aware(timestamp) + timedelta(hours=hours)).isoformat()


def _decide_all(root: Path, pair_id: str, decision: str = hr.DECISION_RETAINED) -> None:
    """The reviewer's explicit per-subject decisions."""

    def mutate(document):
        for subject in document["subjects"]:
            subject["reviewer_decision"] = decision

    _edit_json(hr.review_record_path(_layout(root), pair_id), mutate)


def _human_labels(proposal: dict, pair_id: str) -> list[dict]:
    """Human labels derived from the SHAPE of each canonical subject — never
    copied from the machine proposal's decisions."""
    labels = []
    for label in proposal["labels"]:
        previous, current = label["previous_unit_id"], label["current_unit_id"]
        if previous and current:
            change, side = "unchanged", "both"
        elif previous:
            change, side = "removed", "previous"
        else:
            change, side = "added", "current"
        labels.append(
            {
                "label_id": rfb.label_id_for(pair_id, previous, current),
                "expected_change_type": change,
                "previous_unit_id": previous,
                "current_unit_id": current,
                "expected_reason_code": None,
                "expected_evidence_side": side,
                "expected_direction": None,
                "reviewer_note": None,
                "confidence": "high",
            }
        )
    return labels


def _admit(root: Path, pair_id: str, **overrides) -> dict:
    """A full, valid human admission for one pair: explicit decisions, explicit
    completion marker, explicit human_verified annotation."""
    layout = _layout(root)
    inventory = _inventory(root)
    proposal = json.loads(
        layout.machine_proposed_path(pair_id).read_text(encoding="utf-8")
    )
    document = {
        "schema_version": rfb.ANNOTATION_SCHEMA_VERSION,
        "annotation_protocol_version": rfb.ANNOTATION_PROTOCOL_VERSION,
        "benchmark_id": hr.BENCHMARK_ID,
        "pair_id": pair_id,
        "annotation_status": rfb.ANNOTATION_HUMAN_VERIFIED,
        "annotator_id": REVIEWER,
        "verification_timestamp": _later(inventory["generated_at"]),
        "source_manifest_hash": proposal["source_manifest_hash"],
        "previous_section_hash": proposal["previous_section_hash"],
        "current_section_hash": proposal["current_section_hash"],
        "labels": _human_labels(proposal, pair_id),
    }
    document.update(overrides)
    rfb.write_json_atomic(hr.human_review_path(layout, pair_id), document)
    _decide_all(root, pair_id)
    _edit_json(
        hr.review_record_path(layout, pair_id),
        lambda record: record.update({"reviewer_completed": True}),
    )
    return document


def _admit_all(root: Path) -> None:
    for pair_id in _ready_ids(root):
        _admit(root, pair_id)


def _tree_digest(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): rfb.sha256_file(path)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


# --- Provenance and immutability (1-12) ---------------------------------------


def test_prepared_workspace_passes_workspace_mode(ws):
    assert _findings(ws, hr.MODE_WORKSPACE).failed == []


def test_committed_inventory_is_the_authoritative_population(ws):
    inventory = _inventory(ws)
    queue = _queue(ws)
    assert len(queue["entries"]) == len(inventory["pairs"])
    assert inventory["packets_written"] == len(_ready_ids(ws))
    assert inventory["packets_blocked"] == len(_blocked_ids(ws))
    assert inventory["machine_proposed_label_count"] == sum(
        entry["proposed_label_count"] for entry in queue["entries"]
    )


def test_machine_annotation_count_derives_from_the_inventory(ws):
    layout = _layout(ws)
    for row in hr.review_ready_rows(_inventory(ws)):
        proposal = json.loads(
            layout.machine_proposed_path(row["pair_id"]).read_text(encoding="utf-8")
        )
        assert len(proposal["labels"]) == row["label_count"]
        assert rfb.annotation_hash(proposal) == row["annotation_sha256"]


def test_missing_packet_is_rejected(ws):
    _layout(ws).packet_json_path(_ready_ids(ws)[0]).unlink()
    assert "v3_review_packet_missing" in _codes(_findings(ws, hr.MODE_WORKSPACE))


def test_packet_hash_drift_is_rejected(ws):
    _edit_json(
        _layout(ws).packet_json_path(_ready_ids(ws)[0]),
        lambda packet: packet.update({"detector_change_count": 999}),
    )
    assert "v3_review_packet_hash_drift" in _codes(_findings(ws, hr.MODE_WORKSPACE))


def test_machine_proposal_content_hash_drift_is_rejected(ws):
    def flip(document):
        document["labels"][0]["expected_change_type"] = (
            "added"
            if document["labels"][0]["expected_change_type"] != "added"
            else "removed"
        )
        document["labels"][0]["previous_unit_id"] = None

    _edit_json(_layout(ws).machine_proposed_path(_ready_ids(ws)[0]), flip)
    codes = _codes(_findings(ws, hr.MODE_WORKSPACE))
    assert "v3_review_machine_proposal_drift" in codes


def test_machine_proposal_byte_drift_is_rejected(ws):
    """A reformat that leaves the evaluated content identical still breaks the
    byte pin the review record took at preparation time."""
    path = _layout(ws).machine_proposed_path(_ready_ids(ws)[0])
    document = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(json.dumps(document), encoding="utf-8")  # reflowed, same content
    codes = _codes(_findings(ws, hr.MODE_WORKSPACE))
    assert "v3_review_machine_proposal_bytes_drift" in codes


def test_comparison_result_hash_drift_is_rejected(ws):
    pair_id = _ready_ids(ws)[0]
    result_path = _layout(ws).build_dir(pair_id) / "detection_result.json"
    _edit_json(result_path, lambda result: result.update({"comparison_id": "tampered"}))
    assert "v3_review_result_hash_drift" in _codes(_findings(ws, hr.MODE_WORKSPACE))


def test_source_binding_drift_is_rejected(ws):
    pair_id = _ready_ids(ws)[0]
    manifest_pair = next(
        pair
        for pair in json.loads((ws / "manifest.json").read_text(encoding="utf-8"))["pairs"]
        if pair["pair_id"] == pair_id
    )
    source = _layout(ws).source_file(
        pair_id, "previous", manifest_pair["previous"]["primary_document"]
    )
    source.write_text(
        source.read_text(encoding="utf-8") + "<!-- tampered -->", encoding="utf-8"
    )
    assert "v3_review_source_checksum_drift" in _codes(
        _findings(ws, hr.MODE_WORKSPACE)
    )


def test_section_text_drift_is_rejected(ws):
    pair_id = _ready_ids(ws)[0]
    path = _layout(ws).section_text_path(pair_id, "current")
    path.write_text(path.read_text(encoding="utf-8") + " tampered", encoding="utf-8")
    assert "v3_review_section_hash_drift" in _codes(_findings(ws, hr.MODE_WORKSPACE))


def test_preparation_leaves_every_frozen_artifact_byte_identical(raw):
    layout = rfb.CorpusLayout(raw / "corpus")
    frozen = {
        str(path.relative_to(raw)): rfb.sha256_file(path)
        for path in sorted((raw / "corpus").rglob("*"))
        if path.is_file()
    }
    committed_before = _tree_digest(raw / "reports")
    assert _prep(raw, "prepare") == 0
    after = {
        str(path.relative_to(raw)): rfb.sha256_file(path)
        for path in sorted((raw / "corpus").rglob("*"))
        if path.is_file()
    }
    # Preparation only ADDS review records and templates.
    assert all(after[name] == digest for name, digest in frozen.items())
    assert _tree_digest(raw / "reports") == committed_before


def test_human_review_file_is_a_separate_artifact(ws):
    layout = _layout(ws)
    for pair_id in _ready_ids(ws):
        human = hr.human_review_path(layout, pair_id)
        proposal = layout.machine_proposed_path(pair_id)
        assert human != proposal
        assert human.exists() and proposal.exists()
        assert human.name == f"{pair_id}.human_review.json"
        assert proposal.name == f"{pair_id}.machine_proposed.json"
        assert human.parent != proposal.parent


def test_human_review_cannot_overwrite_the_machine_proposal(ws):
    layout = _layout(ws)
    pair_id = _ready_ids(ws)[0]
    before = hr.file_sha256(layout.machine_proposed_path(pair_id))
    _admit(ws, pair_id)
    assert hr.file_sha256(layout.machine_proposed_path(pair_id)) == before


def test_repeated_preparation_preserves_reviewer_work(ws):
    pair_id = _ready_ids(ws)[0]
    _decide_all(ws, pair_id, hr.DECISION_CHANGED)
    record_before = hr.review_record_path(_layout(ws), pair_id).read_text(
        encoding="utf-8"
    )
    assert _prep(ws, "prepare") == 0
    assert (
        hr.review_record_path(_layout(ws), pair_id).read_text(encoding="utf-8")
        == record_before
    )


@pytest.mark.parametrize("index", [0, 1])
def test_blocked_pair_receives_no_fabricated_annotation(ws, index):
    pair_id = _blocked_ids(ws)[index]
    layout = _layout(ws)
    for path in (
        layout.packet_json_path(pair_id),
        layout.machine_proposed_path(pair_id),
        hr.human_review_path(layout, pair_id),
        hr.review_record_path(layout, pair_id),
    ):
        assert not path.exists()
    entry = next(
        item for item in _queue(ws)["entries"] if item["pair_id"] == pair_id
    )
    assert entry["review_status"] == hr.REVIEW_STATUS_BLOCKED
    assert entry["proposed_label_count"] == 0
    assert entry["blocking_reason"] in hr.KNOWN_BLOCKING_REASONS


def test_a_fabricated_blocked_pair_annotation_is_rejected(ws):
    pair_id = _blocked_ids(ws)[0]
    rfb.write_json_atomic(
        hr.human_review_path(_layout(ws), pair_id),
        {"annotation_status": rfb.ANNOTATION_HUMAN_VERIFIED},
    )
    codes = _codes(_findings(ws, hr.MODE_WORKSPACE))
    assert "v3_review_blocked_pair_surface_forbidden" in codes
    assert "v3_review_unexpected_review_file" in codes


def test_a_human_decision_smuggled_into_annotations_is_rejected(ws):
    """``annotations/`` holds machine proposals only. The blind runner's frozen
    precondition refuses any other status there, so this validator refuses the
    file outright rather than letting it accumulate."""
    rfb.write_json_atomic(
        _layout(ws).annotations_dir() / f"{_ready_ids(ws)[0]}.json",
        {"annotation_status": rfb.ANNOTATION_HUMAN_VERIFIED},
    )
    assert "v3_review_unexpected_annotation_file" in _codes(
        _findings(ws, hr.MODE_WORKSPACE)
    )


def test_annotations_directory_stays_readable_by_the_blind_run_precondition(ws):
    """A prepared workspace must not break the frozen blind-run gate.

    ``verify_no_human_annotation`` refuses any ``annotations/*.json`` whose
    status is not ``machine_proposed``. Preparation therefore keeps the human
    template out of that directory entirely.
    """
    import real_filing_v3_holdout_extraction as rfx

    assert rfx.verify_no_human_annotation(_layout(ws)) == len(_ready_ids(ws))


def test_blocked_pair_cannot_be_validated_as_a_pair(ws):
    with pytest.raises(hr.HumanReviewError) as excinfo:
        _findings(ws, hr.MODE_PAIR, _blocked_ids(ws)[0])
    assert excinfo.value.code == "v3_review_pair_blocked"


def test_corpus_admission_ignores_blocked_pairs(ws):
    _admit_all(ws)
    findings = _findings(ws, hr.MODE_CORPUS)
    assert findings.failed == []
    blocked = set(_blocked_ids(ws))
    admitted = {
        row["pair_id"]
        for row in findings.rows
        if row["check"] == "human_annotation_admitted"
    }
    assert not (admitted & blocked)


# --- Review queue (13-27) ------------------------------------------------------


def test_queue_order_follows_the_committed_inventory(ws):
    inventory = _inventory(ws)
    assert [entry["pair_id"] for entry in _queue(ws)["entries"]] == [
        row["pair_id"] for row in inventory["pairs"]
    ]


def test_queue_is_deterministic_across_reruns(ws):
    first = hr.review_queue_path(_layout(ws)).read_bytes()
    assert _prep(ws, "prepare") == 0
    assert hr.review_queue_path(_layout(ws)).read_bytes() == first


def test_queue_is_not_sorted_by_any_quality_signal(ws):
    entries = _queue(ws)["entries"]
    labels = [entry["proposed_label_count"] for entry in entries]
    units = [entry["canonical_unit_id_count"] for entry in entries]
    assert labels != sorted(labels) and labels != sorted(labels, reverse=True)
    assert units != sorted(units) and units != sorted(units, reverse=True)
    assert _queue(ws)["ordering"] == "committed_annotation_packet_inventory_order"


def test_every_packet_appears_in_the_queue_exactly_once(ws):
    ids = [entry["pair_id"] for entry in _queue(ws)["entries"]]
    assert len(ids) == len(set(ids))
    assert set(ids) == {row["pair_id"] for row in _inventory(ws)["pairs"]}


def test_unknown_queue_pair_is_rejected(ws):
    def inject(queue):
        rogue = copy.deepcopy(queue["entries"][0])
        rogue["pair_id"] = "sic-9000s-99"
        queue["entries"].append(rogue)

    _edit_json(hr.review_queue_path(_layout(ws)), inject)
    codes = _codes(_findings(ws, hr.MODE_WORKSPACE))
    assert "v3_review_queue_unknown_pair" in codes


def test_duplicate_queue_pair_is_rejected(ws):
    _edit_json(
        hr.review_queue_path(_layout(ws)),
        lambda queue: queue["entries"].append(copy.deepcopy(queue["entries"][0])),
    )
    assert "v3_review_queue_duplicate_pair" in _codes(
        _findings(ws, hr.MODE_WORKSPACE)
    )


def test_reordered_queue_is_rejected(ws):
    _edit_json(
        hr.review_queue_path(_layout(ws)),
        lambda queue: queue["entries"].reverse(),
    )
    assert "v3_review_queue_order_drift" in _codes(_findings(ws, hr.MODE_WORKSPACE))


def test_pending_is_the_initial_state_and_is_not_approval(ws):
    for entry in _queue(ws)["entries"]:
        expected = (
            hr.REVIEW_STATUS_BLOCKED
            if entry["blocking_reason"]
            else hr.REVIEW_STATUS_PENDING
        )
        assert entry["review_status"] == expected
        assert entry["subjects_decided"] == 0
    assert _findings(ws, hr.MODE_CORPUS).failed  # pending never admits


def test_unchanged_copied_machine_values_are_not_approval(ws):
    """A human file byte-equal in decisions to the machine proposal still needs
    an explicit reviewer decision and an explicit completion marker."""
    pair_id = _ready_ids(ws)[0]
    layout = _layout(ws)
    proposal = json.loads(
        layout.machine_proposed_path(pair_id).read_text(encoding="utf-8")
    )
    copied = copy.deepcopy(proposal)
    copied["annotation_status"] = rfb.ANNOTATION_HUMAN_VERIFIED
    copied["annotator_id"] = REVIEWER
    copied["verification_timestamp"] = _later(_inventory(ws)["generated_at"])
    copied.pop("generated_by", None)
    rfb.write_json_atomic(hr.human_review_path(layout, pair_id), copied)
    codes = _codes(_findings(ws, hr.MODE_PAIR, pair_id))
    assert "v3_review_subject_undecided" in codes
    assert "v3_review_completion_marker_missing" in codes


def test_explicit_per_label_review_markers_are_required(ws):
    pair_id = _ready_ids(ws)[0]
    _admit(ws, pair_id)
    _edit_json(
        hr.review_record_path(_layout(ws), pair_id),
        lambda record: record["subjects"][0].update({"reviewer_decision": None}),
    )
    assert "v3_review_subject_undecided" in _codes(
        _findings(ws, hr.MODE_PAIR, pair_id)
    )


def test_explicit_packet_completion_marker_is_required(ws):
    pair_id = _ready_ids(ws)[0]
    _admit(ws, pair_id)
    _edit_json(
        hr.review_record_path(_layout(ws), pair_id),
        lambda record: record.update({"reviewer_completed": False}),
    )
    assert "v3_review_completion_marker_missing" in _codes(
        _findings(ws, hr.MODE_PAIR, pair_id)
    )


def test_incomplete_packet_is_rejected_from_admission(ws):
    pair_id = _ready_ids(ws)[0]
    assert _validate(ws, "--pair", pair_id) == 1


def test_queue_contains_no_filing_text_or_packet_prose(ws):
    layout = _layout(ws)
    queue_text = hr.review_queue_path(layout).read_text(encoding="utf-8")
    for pair_id in _ready_ids(ws):
        packet = json.loads(
            layout.packet_json_path(pair_id).read_text(encoding="utf-8")
        )
        for entry in packet.get("alignments", []):
            for side in ("previous", "current"):
                excerpt = entry.get(f"{side}_excerpt")
                if excerpt and len(excerpt) > 40:
                    assert excerpt[:40] not in queue_text
        assert packet["issuer_name"] not in queue_text
        assert packet["banner"] not in queue_text


def test_queue_entries_carry_only_bounded_metadata(ws):
    for entry in _queue(ws)["entries"]:
        assert set(entry) == set(hr._QUEUE_ENTRY_KEYS)
        for field in hr.LABEL_DECISION_FIELDS + hr.DOCUMENT_DECISION_FIELDS:
            assert field not in entry


def test_queue_contains_no_human_decision_before_review(ws):
    queue = _queue(ws)
    assert queue["human_verified_count"] == 0
    assert all(entry["subjects_decided"] == 0 for entry in queue["entries"])


def test_queue_carries_no_absolute_path(ws):
    text = hr.review_queue_path(_layout(ws)).read_text(encoding="utf-8")
    assert str(ws) not in text
    assert hr.text_sensitive_reason(text) is None


# --- Human-only guarantees (28-38) --------------------------------------------

_TOOL_SOURCES = (
    "real_filing_v3_human_review.py",
    "scripts/prepare_v3_holdout_human_review.py",
    "scripts/validate_v3_holdout_human_annotations.py",
)


def _source(name: str) -> str:
    return (REPO_ROOT / name).read_text(encoding="utf-8")


def _code_only(name: str) -> str:
    """The module's executable source with comments and docstrings removed.

    The tool docstrings deliberately SAY "never opens Chroma", "never computes
    a metric", "no generalization sign-off". Scanning raw text for those words
    would fail on the very sentences that state the guarantee, so the behavior
    tests read code, not prose.
    """
    tree = ast.parse(_source(name))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                node.body = body[1:] or [ast.Pass()]
    return ast.unparse(ast.fix_missing_locations(tree))


class _StripStrings(ast.NodeTransformer):
    def visit_Constant(self, node: ast.Constant):  # noqa: N802
        if isinstance(node.value, str):
            return ast.Constant(value="")
        return node


def _code_without_strings(name: str) -> str:
    """Executable source with every string literal blanked.

    A user-facing disclaimer that says "no accuracy claim exists" is not a
    computation. Only identifiers, attributes, and calls are searched for
    metric machinery.
    """
    tree = _StripStrings().visit(ast.parse(_code_only(name)))
    return ast.unparse(ast.fix_missing_locations(tree))


def _imports_of(name: str) -> set[str]:
    tree = ast.parse(_source(name))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
            modules.update(f"{node.module}.{alias.name}" for alias in node.names)
    return modules


def _assigned_string_fields(name: str) -> set[str]:
    """Every dict key a tool source assigns a non-null literal to."""
    tree = ast.parse(_source(name))
    fields: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if (
                    isinstance(key, ast.Constant)
                    and isinstance(key.value, str)
                    and not (
                        isinstance(value, ast.Constant) and value.value is None
                    )
                ):
                    fields.add(key.value)
        if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant):
            parent_assign = getattr(node, "_is_store", False)
            if parent_assign and isinstance(node.slice.value, str):
                fields.add(node.slice.value)
    return fields


def test_preparation_never_assigns_a_decision_field():
    """No tool source ever writes a label decision or reviewer identity."""
    for name in _TOOL_SOURCES:
        assigned = _assigned_string_fields(name)
        for field in hr.LABEL_DECISION_FIELDS:
            assert field not in assigned, f"{name} assigns {field}"
        for field in hr.DOCUMENT_DECISION_FIELDS:
            # Templates set these to null only, which the extractor excludes.
            assert field not in assigned, f"{name} assigns {field}"


def test_template_writes_only_nulls_into_decision_fields(ws):
    layout = _layout(ws)
    for pair_id in _ready_ids(ws):
        template = json.loads(
            hr.human_review_path(layout, pair_id).read_text(encoding="utf-8")
        )
        assert hr.is_untouched_template(template)
        for field in hr.DOCUMENT_DECISION_FIELDS:
            assert template[field] is None
        for label in template["labels"]:
            for field in hr.LABEL_DECISION_FIELDS:
                assert label[field] is None


def test_preparation_never_accepts_all_machine_proposals(ws):
    for pair_id in _ready_ids(ws):
        record = json.loads(
            hr.review_record_path(_layout(ws), pair_id).read_text(encoding="utf-8")
        )
        assert record["reviewer_completed"] is False
        assert all(
            subject["reviewer_decision"] is None for subject in record["subjects"]
        )


def test_completion_requires_an_explicit_confirmation_flag(ws):
    pair_id = _ready_ids(ws)[0]
    _admit(ws, pair_id)
    _edit_json(
        hr.review_record_path(_layout(ws), pair_id),
        lambda record: record.update({"reviewer_completed": False}),
    )
    assert _prep(ws, "complete", pair_id) == 2
    record = json.loads(
        hr.review_record_path(_layout(ws), pair_id).read_text(encoding="utf-8")
    )
    assert record["reviewer_completed"] is False


def test_completion_refuses_an_undecided_packet(ws):
    pair_id = _ready_ids(ws)[0]
    assert _prep(ws, "complete", pair_id, "--confirm-reviewed") == 1
    record = json.loads(
        hr.review_record_path(_layout(ws), pair_id).read_text(encoding="utf-8")
    )
    assert record["reviewer_completed"] is False


def test_completion_writes_only_the_marker(ws):
    pair_id = _ready_ids(ws)[0]
    layout = _layout(ws)
    _admit(ws, pair_id)
    _edit_json(
        hr.review_record_path(layout, pair_id),
        lambda record: record.update({"reviewer_completed": False}),
    )
    before = json.loads(
        hr.review_record_path(layout, pair_id).read_text(encoding="utf-8")
    )
    human_before = hr.human_review_path(layout, pair_id).read_bytes()
    assert _prep(ws, "complete", pair_id, "--confirm-reviewed") == 0
    after = json.loads(
        hr.review_record_path(layout, pair_id).read_text(encoding="utf-8")
    )
    assert after.pop("reviewer_completed") is True
    assert before.pop("reviewer_completed") is False
    assert after == before
    assert hr.human_review_path(layout, pair_id).read_bytes() == human_before


def test_no_tool_generates_reviewer_identity_or_a_timestamp():
    for name in _TOOL_SOURCES:
        code = _code_only(name)
        for token in (
            "datetime.now",
            "utcnow",
            "utc_now_iso",
            "time.time",
            "getpass",
            "os.environ",
            "getenv",
            "subprocess",
        ):
            assert token not in code, f"{name} uses {token}"


def test_no_tool_generates_a_generalization_signoff():
    """The only sign-off reference in code is a check that none exists."""
    for name in _TOOL_SOURCES:
        code = _code_only(name)
        assert "signer_id" not in code
        assert "signed_at_utc" not in code
        assert "acknowledged_pairs_scored" not in code
        assert 'generalization_claim_supported"' not in code
        for line in code.splitlines():
            if "signoff" in line:
                assert "is None" in line, line


@pytest.mark.parametrize("name", _TOOL_SOURCES)
def test_tools_import_nothing_that_can_reach_a_model_or_a_network(name):
    forbidden = (
        "boto3",
        "botocore",
        "requests",
        "urllib",
        "urllib.request",
        "http.client",
        "socket",
        "chromadb",
        "langchain",
        "langchain_aws",
        "openai",
        "anthropic",
        "loaders",
        "ingest",
        "comparison_detector",
        "tools",
        "agent",
    )
    modules = _imports_of(name)
    assert not modules & set(forbidden), sorted(modules & set(forbidden))


@pytest.mark.parametrize("name", _TOOL_SOURCES)
def test_tools_never_import_or_invoke_the_gold_evaluator(name):
    modules = _imports_of(name)
    assert not any("eval_real_filing_benchmark" in module for module in modules)
    code = _code_only(name)
    assert "eval_real_filing_benchmark" not in code
    assert "gold_evaluation_report" not in code


@pytest.mark.parametrize("name", _TOOL_SOURCES)
def test_tools_compute_no_metric_and_no_agreement(name):
    code = _code_without_strings(name)
    for token in (
        "precision",
        "recall",
        "f1",
        "exact_match",
        "accuracy",
        "agreement_rate",
        "machine_agreement",
        "false_positive_rate",
    ):
        assert token not in code, f"{name} computes {token}"


def test_validator_json_declares_that_no_metric_was_computed(ws, capsys):
    _validate(ws, "--workspace", "--json")
    payload = json.loads(capsys.readouterr().out)
    assert payload["gold_metrics_computed"] is False
    assert "gold_metrics" not in payload


def test_validation_is_read_only(ws):
    before = _tree_digest(ws)
    _validate(ws, "--workspace")
    _validate(ws)
    assert _tree_digest(ws) == before


def test_file_presence_is_never_approval(ws):
    """An empty template is a file; it admits nothing."""
    pair_id = _ready_ids(ws)[0]
    assert hr.human_review_path(_layout(ws), pair_id).exists()
    codes = _codes(_findings(ws, hr.MODE_PAIR, pair_id))
    assert "v3_review_annotation_not_completed" in codes


# --- Annotation validation (39-57) --------------------------------------------


def test_valid_retained_decision_is_accepted_with_explicit_action(ws):
    pair_id = _ready_ids(ws)[0]
    _admit(ws, pair_id)
    assert _findings(ws, hr.MODE_PAIR, pair_id).failed == []


def test_valid_changed_decision_is_accepted(ws):
    pair_id = _ready_ids(ws)[0]
    _admit(ws, pair_id)
    _decide_all(ws, pair_id, hr.DECISION_CHANGED)
    assert _findings(ws, hr.MODE_PAIR, pair_id).failed == []


def test_valid_undetermined_decision_with_a_reason_is_accepted(ws):
    pair_id = _ready_ids(ws)[0]
    document = _admit(ws, pair_id)
    for label in document["labels"]:
        if label["previous_unit_id"] and label["current_unit_id"]:
            label["expected_change_type"] = "undetermined"
            label["expected_reason_code"] = "evidence_insufficient"
            label["expected_evidence_side"] = "none"
            break
    rfb.write_json_atomic(hr.human_review_path(_layout(ws), pair_id), document)
    _decide_all(ws, pair_id, hr.DECISION_UNDETERMINED)
    assert _findings(ws, hr.MODE_PAIR, pair_id).failed == []


def test_undetermined_without_a_reason_is_rejected(ws):
    pair_id = _ready_ids(ws)[0]
    document = _admit(ws, pair_id)
    for label in document["labels"]:
        if label["previous_unit_id"] and label["current_unit_id"]:
            label["expected_change_type"] = "undetermined"
            label["expected_evidence_side"] = "none"
            break
    rfb.write_json_atomic(hr.human_review_path(_layout(ws), pair_id), document)
    assert "v3_review_undetermined_reason_missing" in _codes(
        _findings(ws, hr.MODE_PAIR, pair_id)
    )


def test_a_rejected_malformed_subject_stops_admission(ws):
    pair_id = _ready_ids(ws)[0]
    _admit(ws, pair_id)
    _edit_json(
        hr.review_record_path(_layout(ws), pair_id),
        lambda record: record["subjects"][0].update(
            {"reviewer_decision": hr.DECISION_REJECTED_MALFORMED}
        ),
    )
    assert "v3_review_subject_rejected_malformed" in _codes(
        _findings(ws, hr.MODE_PAIR, pair_id)
    )


def test_invalid_change_type_is_rejected(ws):
    pair_id = _ready_ids(ws)[0]
    document = _admit(ws, pair_id)
    document["labels"][0]["expected_change_type"] = "slightly_different"
    rfb.write_json_atomic(hr.human_review_path(_layout(ws), pair_id), document)
    assert "v3_review_annotation_schema" in _codes(
        _findings(ws, hr.MODE_PAIR, pair_id)
    )


def test_invalid_subject_shape_is_rejected(ws):
    pair_id = _ready_ids(ws)[0]
    document = _admit(ws, pair_id)
    for label in document["labels"]:
        if label["previous_unit_id"] and label["current_unit_id"]:
            label["expected_change_type"] = "added"  # 'added' binds current only
            break
    rfb.write_json_atomic(hr.human_review_path(_layout(ws), pair_id), document)
    assert "v3_review_annotation_schema" in _codes(
        _findings(ws, hr.MODE_PAIR, pair_id)
    )


def test_missing_canonical_unit_identity_is_rejected(ws):
    pair_id = _ready_ids(ws)[0]
    document = _admit(ws, pair_id)
    document["labels"] = document["labels"][:-1]
    rfb.write_json_atomic(hr.human_review_path(_layout(ws), pair_id), document)
    assert "v3_review_unit_uncovered" in _codes(_findings(ws, hr.MODE_PAIR, pair_id))


def test_unit_key_only_subject_is_rejected(ws):
    pair_id = _ready_ids(ws)[0]
    document = _admit(ws, pair_id)
    target = next(
        label for label in document["labels"] if label["current_unit_id"]
    )
    target["current_unit_id"] = target["current_unit_id"].split(":", 2)[2]
    target["label_id"] = rfb.label_id_for(
        pair_id, target["previous_unit_id"], target["current_unit_id"]
    )
    rfb.write_json_atomic(hr.human_review_path(_layout(ws), pair_id), document)
    codes = _codes(_findings(ws, hr.MODE_PAIR, pair_id))
    assert "v3_review_unit_key_only_subject" in codes


def test_side_mismatch_is_rejected(ws):
    pair_id = _ready_ids(ws)[0]
    document = _admit(ws, pair_id)
    target = next(label for label in document["labels"] if label["current_unit_id"])
    target["current_unit_id"] = target["current_unit_id"].replace(
        "current:", "previous:", 1
    )
    target["label_id"] = rfb.label_id_for(
        pair_id, target["previous_unit_id"], target["current_unit_id"]
    )
    rfb.write_json_atomic(hr.human_review_path(_layout(ws), pair_id), document)
    assert "v3_review_unit_side_mismatch" in _codes(
        _findings(ws, hr.MODE_PAIR, pair_id)
    )


def test_position_metadata_mismatch_is_rejected(ws):
    """A canonical position that exists but under a different unit_key."""
    pair_id = _ready_ids(ws)[0]
    document = _admit(ws, pair_id)
    target = next(label for label in document["labels"] if label["current_unit_id"])
    side, sequence, _key = target["current_unit_id"].split(":", 2)
    target["current_unit_id"] = f"{side}:{sequence}:not-the-recorded-key"
    target["label_id"] = rfb.label_id_for(
        pair_id, target["previous_unit_id"], target["current_unit_id"]
    )
    rfb.write_json_atomic(hr.human_review_path(_layout(ws), pair_id), document)
    codes = _codes(_findings(ws, hr.MODE_PAIR, pair_id))
    assert "v3_review_unknown_unit_identity" in codes


def test_duplicate_canonical_subject_is_rejected(ws):
    pair_id = _ready_ids(ws)[0]
    document = _admit(ws, pair_id)
    duplicate = copy.deepcopy(document["labels"][0])
    duplicate["label_id"] = duplicate["label_id"] + "x"
    document["labels"].append(duplicate)
    rfb.write_json_atomic(hr.human_review_path(_layout(ws), pair_id), document)
    codes = _codes(_findings(ws, hr.MODE_PAIR, pair_id))
    assert "v3_review_annotation_schema" in codes or {
        "v3_review_duplicate_canonical_subject",
        "v3_review_unit_multiply_covered",
    } & codes


def test_unknown_unit_identity_is_rejected(ws):
    pair_id = _ready_ids(ws)[0]
    document = _admit(ws, pair_id)
    target = next(label for label in document["labels"] if label["current_unit_id"])
    target["current_unit_id"] = "current:900:invented-unit"
    target["label_id"] = rfb.label_id_for(
        pair_id, target["previous_unit_id"], target["current_unit_id"]
    )
    rfb.write_json_atomic(hr.human_review_path(_layout(ws), pair_id), document)
    assert "v3_review_unknown_unit_identity" in _codes(
        _findings(ws, hr.MODE_PAIR, pair_id)
    )


def test_missing_evidence_reference_is_rejected(ws):
    pair_id = _ready_ids(ws)[0]
    document = _admit(ws, pair_id)
    document["labels"][0]["expected_evidence_side"] = None
    rfb.write_json_atomic(hr.human_review_path(_layout(ws), pair_id), document)
    assert "v3_review_annotation_schema" in _codes(
        _findings(ws, hr.MODE_PAIR, pair_id)
    )


def test_evidence_reference_naming_an_unbound_side_is_rejected(ws):
    pair_id = _ready_ids(ws)[0]
    document = _admit(ws, pair_id)
    for label in document["labels"]:
        if label["previous_unit_id"] and label["current_unit_id"]:
            label["expected_change_type"] = "removed"
            label["current_unit_id"] = None
            label["expected_evidence_side"] = "current"
            label["label_id"] = rfb.label_id_for(
                pair_id, label["previous_unit_id"], None
            )
            break
    rfb.write_json_atomic(hr.human_review_path(_layout(ws), pair_id), document)
    codes = _codes(_findings(ws, hr.MODE_PAIR, pair_id))
    assert "v3_review_evidence_reference_invalid" in codes


def test_direction_without_both_sides_is_rejected(ws):
    pair_id = _ready_ids(ws)[0]
    document = _admit(ws, pair_id)
    for label in document["labels"]:
        if label["previous_unit_id"] and label["current_unit_id"]:
            label["expected_change_type"] = "removed"
            label["current_unit_id"] = None
            label["expected_evidence_side"] = "previous"
            label["expected_direction"] = "increased"
            label["label_id"] = rfb.label_id_for(
                pair_id, label["previous_unit_id"], None
            )
            break
    rfb.write_json_atomic(hr.human_review_path(_layout(ws), pair_id), document)
    codes = _codes(_findings(ws, hr.MODE_PAIR, pair_id))
    assert "v3_review_direction_without_both_sides" in codes


def test_extra_annotation_subject_is_rejected(ws):
    pair_id = _ready_ids(ws)[0]
    document = _admit(ws, pair_id)
    document["labels"].append(
        {
            "label_id": rfb.label_id_for(pair_id, None, "current:900:invented"),
            "expected_change_type": "added",
            "previous_unit_id": None,
            "current_unit_id": "current:900:invented",
            "expected_reason_code": None,
            "expected_evidence_side": "current",
            "expected_direction": None,
            "reviewer_note": None,
            "confidence": "high",
        }
    )
    rfb.write_json_atomic(hr.human_review_path(_layout(ws), pair_id), document)
    assert "v3_review_unknown_unit_identity" in _codes(
        _findings(ws, hr.MODE_PAIR, pair_id)
    )


def test_label_order_does_not_affect_validation(ws):
    pair_id = _ready_ids(ws)[0]
    document = _admit(ws, pair_id)
    assert _findings(ws, hr.MODE_PAIR, pair_id).failed == []
    document["labels"].reverse()
    rfb.write_json_atomic(hr.human_review_path(_layout(ws), pair_id), document)
    assert _findings(ws, hr.MODE_PAIR, pair_id).failed == []


def test_missing_confidence_is_rejected(ws):
    pair_id = _ready_ids(ws)[0]
    document = _admit(ws, pair_id)
    document["labels"][0]["confidence"] = None
    rfb.write_json_atomic(hr.human_review_path(_layout(ws), pair_id), document)
    assert "v3_review_annotation_schema" in _codes(
        _findings(ws, hr.MODE_PAIR, pair_id)
    )


def test_non_canonical_label_id_is_rejected(ws):
    pair_id = _ready_ids(ws)[0]
    document = _admit(ws, pair_id)
    document["labels"][0]["label_id"] = "lbl-handwritten"
    rfb.write_json_atomic(hr.human_review_path(_layout(ws), pair_id), document)
    assert "v3_review_label_id_not_canonical" in _codes(
        _findings(ws, hr.MODE_PAIR, pair_id)
    )


def test_section_hash_drift_invalidates_an_admitted_annotation(ws):
    pair_id = _ready_ids(ws)[0]
    document = _admit(ws, pair_id)
    document["current_section_hash"] = "0" * 64
    rfb.write_json_atomic(hr.human_review_path(_layout(ws), pair_id), document)
    codes = _codes(_findings(ws, hr.MODE_PAIR, pair_id))
    assert "v3_review_annotation_binding_drift" in codes
    assert "v3_review_annotation_build_binding" in codes


# --- Admission metadata (58-70) -----------------------------------------------


def test_machine_proposed_status_cannot_be_admitted(ws):
    pair_id = _ready_ids(ws)[0]
    _admit(ws, pair_id)
    _edit_json(
        hr.human_review_path(_layout(ws), pair_id),
        lambda document: document.update(
            {
                "annotation_status": rfb.ANNOTATION_MACHINE_PROPOSED,
                "annotator_id": None,
                "verification_timestamp": None,
            }
        ),
    )
    assert "v3_review_machine_status_in_human_file" in _codes(
        _findings(ws, hr.MODE_PAIR, pair_id)
    )


def test_human_verified_requires_an_annotator_id(ws):
    pair_id = _ready_ids(ws)[0]
    _admit(ws, pair_id)
    _edit_json(
        hr.human_review_path(_layout(ws), pair_id),
        lambda document: document.update({"annotator_id": None}),
    )
    codes = _codes(_findings(ws, hr.MODE_PAIR, pair_id))
    assert "v3_review_annotation_schema" in codes or (
        "v3_review_annotator_placeholder" in codes
    )


@pytest.mark.parametrize(
    "placeholder", ["TODO", "  tbd ", "reviewer", "Your Name", "-", "", "n/a"]
)
def test_placeholder_annotator_id_is_rejected(ws, placeholder):
    pair_id = _ready_ids(ws)[0]
    _admit(ws, pair_id, annotator_id=placeholder)
    codes = _codes(_findings(ws, hr.MODE_PAIR, pair_id))
    assert "v3_review_annotator_placeholder" in codes or (
        "v3_review_annotation_schema" in codes
    )


def test_missing_verification_timestamp_is_rejected(ws):
    pair_id = _ready_ids(ws)[0]
    _admit(ws, pair_id)
    _edit_json(
        hr.human_review_path(_layout(ws), pair_id),
        lambda document: document.update({"verification_timestamp": None}),
    )
    assert "v3_review_annotation_schema" in _codes(
        _findings(ws, hr.MODE_PAIR, pair_id)
    )


def test_naive_timestamp_is_rejected(ws):
    pair_id = _ready_ids(ws)[0]
    stamp = _later(_inventory(ws)["generated_at"])
    _admit(ws, pair_id, verification_timestamp=stamp.replace("+00:00", ""))
    codes = _codes(_findings(ws, hr.MODE_PAIR, pair_id))
    assert "v3_review_timestamp_not_explicit_utc" in codes or (
        "v3_review_annotation_schema" in codes
    )


def test_non_utc_timestamp_is_rejected(ws):
    pair_id = _ready_ids(ws)[0]
    stamp = _later(_inventory(ws)["generated_at"])
    _admit(ws, pair_id, verification_timestamp=stamp.replace("+00:00", "+05:30"))
    assert "v3_review_timestamp_not_explicit_utc" in _codes(
        _findings(ws, hr.MODE_PAIR, pair_id)
    )


def test_invalid_timestamp_is_rejected(ws):
    pair_id = _ready_ids(ws)[0]
    _admit(ws, pair_id, verification_timestamp="not-a-timestamp")
    codes = _codes(_findings(ws, hr.MODE_PAIR, pair_id))
    assert "v3_review_timestamp_not_explicit_utc" in codes or (
        "v3_review_annotation_schema" in codes
    )


def test_timestamp_predating_packet_generation_is_rejected(ws):
    pair_id = _ready_ids(ws)[0]
    _admit(
        ws,
        pair_id,
        verification_timestamp=_later(_inventory(ws)["generated_at"], hours=-4),
    )
    assert "v3_review_timestamp_precedes_packets" in _codes(
        _findings(ws, hr.MODE_PAIR, pair_id)
    )


def test_partial_reviewer_metadata_is_rejected(ws):
    pair_id = _ready_ids(ws)[0]
    _admit(ws, pair_id)
    _edit_json(
        hr.human_review_path(_layout(ws), pair_id),
        lambda document: document.update(
            {"annotation_status": rfb.ANNOTATION_HUMAN_IN_PROGRESS}
        ),
    )
    assert "v3_review_annotation_not_human_verified" in _codes(
        _findings(ws, hr.MODE_PAIR, pair_id)
    )


def test_reviewer_metadata_is_never_carried_from_a_prior_corpus(ws):
    """Nothing in the workspace references the earlier holdout's annotations."""
    for name in _TOOL_SOURCES:
        code = _code_only(name)
        assert "real_filing_holdout_v1" not in code
        assert "real_filings_v1" not in code
    layout = _layout(ws)
    for pair_id in _ready_ids(ws):
        template = json.loads(
            hr.human_review_path(layout, pair_id).read_text(encoding="utf-8")
        )
        assert template["benchmark_id"] == hr.BENCHMARK_ID


def test_admission_does_not_advance_the_manifest_or_create_a_signoff(ws):
    manifest_before = (ws / "manifest.json").read_bytes()
    reports_before = _tree_digest(ws / "reports")
    _admit_all(ws)
    assert _validate(ws) == 0
    assert (ws / "manifest.json").read_bytes() == manifest_before
    assert _tree_digest(ws / "reports") == reports_before
    manifest = json.loads(manifest_before.decode("utf-8"))
    assert manifest["status"] == rfb.STATUS_CORPUS_BUILT
    assert manifest["generalization_claim_supported"] is False
    assert manifest["extraction_holdout_evaluation"] is False


def test_admission_produces_no_report_and_no_metric(ws):
    before = _tree_digest(ws)
    _admit_all(ws)
    assert _validate(ws) == 0
    after = _tree_digest(ws)
    new_files = set(after) - set(before)
    assert not any("gold" in name or "metric" in name for name in new_files)


# --- Repeated normalized headings (71-76) -------------------------------------


@pytest.fixture
def repeated_pair(ws):
    """The pair whose current side repeats a normalized heading."""
    inventory = _inventory(ws)
    row = inventory["pairs"][REPEATED_HEADING_INDEX]
    assert row["packet_status"] == "written"
    return str(row["pair_id"])


def _unit_keys(root: Path, pair_id: str, side: str) -> list[str]:
    record = json.loads(
        _layout(root).build_record_path(pair_id).read_text(encoding="utf-8")
    )
    return [unit["unit_key"] for unit in record[side]["units"]]


def test_repeated_normalized_headings_stay_separate_occurrences(ws, repeated_pair):
    keys = _unit_keys(ws, repeated_pair, "current")
    assert len(keys) > len(set(keys)), "fixture must repeat a normalized heading"
    record = json.loads(
        _layout(ws).build_record_path(repeated_pair).read_text(encoding="utf-8")
    )
    unit_ids = [unit["unit_id"] for unit in record["current"]["units"]]
    assert len(unit_ids) == len(set(unit_ids))


def test_every_repeated_occurrence_gets_its_own_review_row(ws, repeated_pair):
    record = json.loads(
        hr.review_record_path(_layout(ws), repeated_pair).read_text(encoding="utf-8")
    )
    subjects = [
        (subject["previous_unit_id"], subject["current_unit_id"])
        for subject in record["subjects"]
    ]
    assert len(subjects) == len(set(subjects))
    build = json.loads(
        _layout(ws).build_record_path(repeated_pair).read_text(encoding="utf-8")
    )
    inventory, _problems = hr.canonical_unit_inventory(build)
    referenced = {unit for subject in subjects for unit in subject if unit}
    assert referenced == set(inventory)


def test_occurrence_one_cannot_satisfy_occurrence_two(ws, repeated_pair):
    document = _admit(ws, repeated_pair)
    assert _findings(ws, hr.MODE_PAIR, repeated_pair).failed == []
    keys = _unit_keys(ws, repeated_pair, "current")
    repeated_key = next(key for key in keys if keys.count(key) > 1)
    victims = [
        label
        for label in document["labels"]
        if label["current_unit_id"]
        and label["current_unit_id"].endswith(f":{repeated_key}")
    ]
    assert len(victims) >= 2
    # Drop the second occurrence's label: the first must not cover it.
    document["labels"] = [label for label in document["labels"] if label is not victims[1]]
    document["labels"].append(
        {
            "label_id": rfb.label_id_for(
                repeated_pair, victims[1]["previous_unit_id"], None
            ),
            "expected_change_type": "removed",
            "previous_unit_id": victims[1]["previous_unit_id"],
            "current_unit_id": None,
            "expected_reason_code": None,
            "expected_evidence_side": "previous",
            "expected_direction": None,
            "reviewer_note": None,
            "confidence": "high",
        }
        if victims[1]["previous_unit_id"]
        else {
            "label_id": "placeholder",
            "expected_change_type": "unchanged",
            "previous_unit_id": None,
            "current_unit_id": None,
            "expected_reason_code": None,
            "expected_evidence_side": "none",
            "expected_direction": None,
            "reviewer_note": None,
            "confidence": "high",
        }
    )
    rfb.write_json_atomic(hr.human_review_path(_layout(ws), repeated_pair), document)
    codes = _codes(_findings(ws, hr.MODE_PAIR, repeated_pair))
    assert codes, "dropping one occurrence's label must not validate"


def test_one_decision_cannot_cover_two_subjects(ws, repeated_pair):
    document = _admit(ws, repeated_pair)
    first, second = document["labels"][0], document["labels"][1]
    document["labels"] = [first] + document["labels"][2:]
    codes_before = _codes(_findings(ws, hr.MODE_PAIR, repeated_pair))
    assert codes_before == set()
    rfb.write_json_atomic(hr.human_review_path(_layout(ws), repeated_pair), document)
    codes = _codes(_findings(ws, hr.MODE_PAIR, repeated_pair))
    assert "v3_review_unit_uncovered" in codes
    assert second["label_id"] not in codes


def test_no_unit_key_dict_collapse_anywhere_in_review(ws, repeated_pair):
    record = json.loads(
        hr.review_record_path(_layout(ws), repeated_pair).read_text(encoding="utf-8")
    )
    for side in ("previous", "current"):
        ids = record["canonical_unit_ids"][side]
        assert len(ids) == len(set(ids))
        assert all(len(unit.split(":", 2)) == 3 for unit in ids)
    build = json.loads(
        _layout(ws).build_record_path(repeated_pair).read_text(encoding="utf-8")
    )
    inventory, _problems = hr.canonical_unit_inventory(build)
    assert len(inventory) == sum(
        len(build[side]["units"]) for side in ("previous", "current")
    )


def test_sequence_aware_identities_survive_a_review_file_round_trip(ws, repeated_pair):
    layout = _layout(ws)
    before = json.loads(
        layout.machine_proposed_path(repeated_pair).read_text(encoding="utf-8")
    )
    template = json.loads(
        hr.human_review_path(layout, repeated_pair).read_text(encoding="utf-8")
    )
    assert [
        (label["previous_unit_id"], label["current_unit_id"])
        for label in template["labels"]
    ] == [
        (label["previous_unit_id"], label["current_unit_id"])
        for label in before["labels"]
    ]


def test_review_ordering_does_not_change_canonical_identities(ws, repeated_pair):
    record_path = hr.review_record_path(_layout(ws), repeated_pair)
    before = json.loads(record_path.read_text(encoding="utf-8"))
    _edit_json(record_path, lambda record: record["subjects"].reverse())
    _admit(ws, repeated_pair)
    # Reversing the review rows must be caught: canonical order is the machine
    # proposal's, and a renumbered record is a drifted record.
    assert "v3_review_record_subject_drift" in _codes(
        _findings(ws, hr.MODE_PAIR, repeated_pair)
    )
    ids_before = [
        (subject["previous_unit_id"], subject["current_unit_id"])
        for subject in before["subjects"]
    ]
    ids_after = [
        (subject["previous_unit_id"], subject["current_unit_id"])
        for subject in json.loads(record_path.read_text(encoding="utf-8"))["subjects"]
    ]
    assert sorted(map(str, ids_before)) == sorted(map(str, ids_after))


# --- Safety (77-94) -----------------------------------------------------------


def test_no_credential_or_user_agent_is_read():
    for name in _TOOL_SOURCES:
        code = _code_only(name)
        assert "SEC_USER_AGENT" not in code
        assert "AWS_" not in code
        assert "aws_access_key" not in code


def test_no_chroma_or_parser_or_detector_execution(ws):
    for name in _TOOL_SOURCES:
        code = _code_only(name)
        for token in (
            "Chroma",
            "get_vectorstore",
            "sec_headings",
            "extract_item_1a",
            "detect(",
            "execute_attempt",
            "build_packet",
            "create_real_filing_annotation_packets",
        ):
            assert token not in code, f"{name} references {token}"


def test_preparation_regenerates_no_packet(ws):
    layout = _layout(ws)
    before = {
        pair_id: hr.file_sha256(layout.packet_json_path(pair_id))
        for pair_id in _ready_ids(ws)
    }
    assert _prep(ws, "prepare") == 0
    assert {
        pair_id: hr.file_sha256(layout.packet_json_path(pair_id))
        for pair_id in _ready_ids(ws)
    } == before


def test_errors_are_stable_bounded_and_path_free(ws, capsys):
    (ws / "reports" / "annotation_packet_inventory.json").unlink()
    assert _validate(ws, "--workspace") == 2
    captured = capsys.readouterr()
    assert "v3_review_artifact_missing" in captured.err
    assert str(ws) not in captured.err
    assert hr.text_sensitive_reason(captured.err) is None


def test_error_ordering_is_deterministic(ws):
    _edit_json(
        _layout(ws).packet_json_path(_ready_ids(ws)[0]),
        lambda packet: packet.update({"detector_change_count": 999}),
    )
    first = [row["check"] for row in _findings(ws, hr.MODE_WORKSPACE).rows]
    second = [row["check"] for row in _findings(ws, hr.MODE_WORKSPACE).rows]
    assert first == second


def test_tool_output_contains_no_absolute_path(ws, capsys):
    _prep(ws, "prepare")
    _prep(ws, "status")
    _validate(ws, "--workspace")
    captured = capsys.readouterr()
    assert str(ws) not in captured.out
    assert hr.text_sensitive_reason(captured.out) is None


def test_show_requires_an_explicit_pair_and_refuses_a_blocked_one(ws, capsys):
    assert _prep(ws, "show", _blocked_ids(ws)[0]) == 2
    assert "v3_review_pair_blocked" in capsys.readouterr().err
    assert _prep(ws, "show", "sic-9000s-99") == 2
    assert "v3_review_unknown_pair" in capsys.readouterr().err
    assert _prep(ws, "show", _ready_ids(ws)[0]) == 0
    assert "MACHINE PROPOSAL (not ground truth)" in capsys.readouterr().out


def test_status_reports_explicit_decisions_only(ws, capsys):
    pair_id = _ready_ids(ws)[0]
    _prep(ws, "status")
    assert f"{pair_id:<16} {hr.REVIEW_STATUS_PENDING}" in capsys.readouterr().out
    _decide_all(ws, pair_id)
    _prep(ws, "status")
    assert hr.REVIEW_STATUS_IN_REVIEW in capsys.readouterr().out


# --- Frozen compatibility (95-107) --------------------------------------------

COMMITTED_ARTIFACTS = (
    "manifest.json",
    "selection_report.json",
    "source_verification_report.json",
    "blind_extraction_report.json",
    "execution_report.json",
    "annotation_packet_inventory.json",
    "evaluation_config.json",
)


@pytest.mark.parametrize("name", COMMITTED_ARTIFACTS)
def test_committed_v3_artifact_declares_no_human_label(name):
    document = json.loads(
        (COMMITTED_REPORT_DIR / name).read_text(encoding="utf-8")
    )
    assert document.get("extraction_holdout_evaluation") in (False, None)
    assert document.get("generalization_claim_supported") in (False, None)
    for field in ("human_verified_label_count", "human_verified_labels"):
        assert document.get(field) in (0, None)


def test_no_committed_v3_gold_report_exists():
    assert not (COMMITTED_REPORT_DIR / "gold_evaluation_report.json").exists()
    assert sorted(path.name for path in COMMITTED_REPORT_DIR.iterdir()) == sorted(
        COMMITTED_ARTIFACTS
    )


def test_committed_inventory_records_only_machine_proposals():
    inventory = json.loads(
        (COMMITTED_REPORT_DIR / "annotation_packet_inventory.json").read_text(
            encoding="utf-8"
        )
    )
    assert inventory["human_verified_label_count"] == 0
    assert inventory["gold_evaluation_runs"] == 0
    for row in inventory["pairs"]:
        assert row["annotation_status"] == rfb.ANNOTATION_MACHINE_PROPOSED
        assert row["human_verified"] is False
        assert row["annotator_id"] is None
        assert row["verification_timestamp"] is None


def test_committed_manifest_pins_the_frozen_semantic_code():
    manifest = rfv3.load_v3_holdout_manifest()
    rfv3.verify_frozen_code_identities(manifest)
    rfv3.verify_exclusion_provenance(manifest)
    assert manifest["status"] == rfb.STATUS_CORPUS_BUILT
    assert manifest["frozen_unit_identity_contract"] == "side:sequence:unit_key"
    assert manifest["frozen_evaluation_contract_version"] == (
        rfv3.FROZEN_EVALUATION_CONTRACT_VERSION
    )


def test_the_frozen_annotation_contract_is_unchanged():
    assert rfb.ANNOTATION_SCHEMA_VERSION == "real-filing-benchmark.annotation.v1"
    assert rfb.ANNOTATION_PROTOCOL_VERSION == "real-filing-annotation.v1"
    assert rfb.GOLD_STATUS == rfb.ANNOTATION_HUMAN_VERIFIED
    assert set(rfb.MACHINE_ONLY_STATUSES) == {
        rfb.ANNOTATION_UNREVIEWED,
        rfb.ANNOTATION_MACHINE_PROPOSED,
    }


def test_the_prior_holdout_validator_is_untouched_by_this_suite():
    """The v1 admission contract keeps its own frozen constants; this suite
    derives its population from the committed v3 inventory instead."""
    from scripts import validate_holdout_human_annotations as v1

    assert v1.HOLDOUT_BENCHMARK_ID == "real_filing_holdout_v1"
    assert len(v1.REVIEW_READY_PAIR_IDS) == 9
    assert v1.EXTRACTION_AMBIGUOUS_PAIR_ID == "sic-6000s-01"


def test_this_suite_is_pinned_to_the_required_ci_check():
    workflow = (
        REPO_ROOT / ".github" / "workflows" / "comparison-regression.yml"
    ).read_text(encoding="utf-8")
    assert "tests/test_v3_holdout_human_review.py" in workflow
    assert "benchmark_data" not in workflow
    assert "SEC_USER_AGENT" not in workflow
