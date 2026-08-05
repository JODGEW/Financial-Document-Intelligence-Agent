"""Prepare the local human-review workspace for the v3 extraction holdout.

    python scripts/prepare_v3_holdout_human_review.py prepare
    python scripts/prepare_v3_holdout_human_review.py status
    python scripts/prepare_v3_holdout_human_review.py show <pair_id>
    python scripts/prepare_v3_holdout_human_review.py complete <pair_id> \
        --confirm-reviewed

Offline and credential-free: it reads the committed reports under
``benchmarks/real_filing_v3_holdout_v1/`` and the gitignored corpus under
``benchmark_data/real_filing_v3_holdout_v1/``. It never contacts the network,
never reads an environment variable for identity, never opens Chroma, never
runs the parser or the detector, never regenerates a packet, and never imports
or invokes the gold evaluator.

What it does
------------

``prepare`` refuses unless every frozen input still verifies, then creates two
things per review-ready pair and nothing else:

- ``review/<pair_id>.human_review.json`` — an EMPTY human annotation template. Unit
  bindings and frozen hashes are carried over from the machine proposal;
  every decision field is ``null``. The original
  ``annotations/<pair_id>.machine_proposed.json`` is never touched, and its
  bytes are pinned so a later edit is detectable.
- ``review/<pair_id>.review.json`` — the review record: provenance bindings
  plus one row per canonical ``side:sequence:unit_key`` subject, each with a
  ``reviewer_decision`` of ``null``.

It then writes ``review/queue.json`` in committed packet-inventory order.
Existing templates and review records are PRESERVED, never overwritten.

What it will not do
-------------------

It never chooses, suggests, ranks, or accepts a label. It never writes
``annotation_status``, ``annotator_id``, ``verification_timestamp``, or any
label decision field. It never sets a ``reviewer_decision``. ``complete`` only
records that a person says they finished a packet, and only after every
subject already carries an explicit decision the person wrote themselves —
file presence, an omitted field, a pressed Enter, and an unchanged machine
value are never approval.

Exit codes: 0 success, 1 the requested action could not complete (a packet is
not fully decided, an artifact disagrees), 2 invalid configuration or refused
preconditions (frozen-identity drift, unreadable reports, unknown pair).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import config  # noqa: E402
import real_filing_benchmark as rfb  # noqa: E402
import real_filing_v3_holdout as rfv3  # noqa: E402
import real_filing_v3_human_review as hr  # noqa: E402


def _paths(args: argparse.Namespace) -> tuple[Path, rfb.CorpusLayout, Path]:
    manifest_path = Path(args.manifest or rfv3.default_v3_holdout_manifest_path())
    layout = rfb.CorpusLayout(args.corpus_dir or config.REAL_FILING_V3_HOLDOUT_DIR)
    report_dir = Path(args.report_dir or manifest_path.parent)
    return manifest_path, layout, report_dir


def _print_findings(findings: hr.Findings, title: str) -> None:
    print(title)
    for row in findings.rows:
        marker = "ok  " if row["ok"] else "FAIL"
        scope = row["pair_id"] or "-"
        print(f"  [{marker}] {scope:<16} {row['check']}: {row['detail']}")
    failed = findings.failed
    print(
        f"\n  {len(findings.rows) - len(failed)}/{len(findings.rows)} checks "
        f"passed; {len(failed)} failed."
    )


def _frozen_digests(
    layout: rfb.CorpusLayout, inventory: dict
) -> dict[str, str]:
    """Byte digests of every artifact preparation must not modify."""
    digests: dict[str, str] = {}
    for row in hr.review_ready_rows(inventory):
        pair_id = str(row["pair_id"])
        for path in (
            layout.packet_json_path(pair_id),
            layout.packet_markdown_path(pair_id),
            layout.machine_proposed_path(pair_id),
            layout.build_record_path(pair_id),
        ):
            if path.exists():
                digests[layout.relative(path)] = hr.file_sha256(path)
    return digests


# --- prepare ------------------------------------------------------------------


def command_prepare(args: argparse.Namespace) -> int:
    manifest_path, layout, report_dir = _paths(args)
    artifacts = hr.load_committed_artifacts(manifest_path, report_dir)
    inventory = artifacts["inventory"]

    findings, loaded = hr.run_preflight(layout=layout, artifacts=artifacts)
    if findings.failed:
        if args.json:
            print(json.dumps({"prepared": False, "checks": findings.rows}, indent=2))
        else:
            _print_findings(findings, "v3 holdout review preparation — preflight")
            print(
                "\n  Nothing was written. Preparation refuses on any frozen-input "
                "drift: reviewing against changed inputs would produce labels "
                "about a different corpus."
            )
        return 2

    before = _frozen_digests(layout, inventory)
    actions: list[dict[str, str]] = []
    records: dict[str, dict | None] = {}
    humans: dict[str, dict | None] = {}

    for row in hr.review_ready_rows(inventory):
        pair_id = str(row["pair_id"])
        pieces = loaded[pair_id]
        proposal, record = pieces["proposal"], pieces["record"]

        record_path = hr.review_record_path(layout, pair_id)
        if record_path.exists():
            records[pair_id] = hr.load_json(record_path, f"{pair_id} review record")
            action = "preserved"
        else:
            document = hr.build_review_record(
                row=row,
                layout=layout,
                proposal=proposal,
                record=record,
                inventory=inventory,
            )
            rfb.write_json_atomic(record_path, document)
            records[pair_id] = document
            action = "created"

        template_path = hr.human_review_path(layout, pair_id)
        if template_path.exists():
            humans[pair_id] = hr.load_json(
                template_path, f"{pair_id} human annotation"
            )
            template_action = "preserved"
        else:
            template = hr.build_human_template(pair_id=pair_id, proposal=proposal)
            rfb.write_json_atomic(template_path, template)
            humans[pair_id] = template
            template_action = "created"

        actions.append(
            {
                "pair_id": pair_id,
                "review_record": action,
                "human_review_file": template_action,
            }
        )

    queue = hr.build_queue(
        layout=layout, inventory=inventory, records=records, humans=humans
    )
    rfb.write_json_atomic(hr.review_queue_path(layout), queue)

    after = _frozen_digests(layout, inventory)
    if before != after:
        changed = sorted(
            name for name in before if before[name] != after.get(name)
        )
        print(
            "Preparation modified a frozen artifact "
            f"[v3_review_frozen_artifact_modified]: {hr.bounded(changed)}",
            file=sys.stderr,
        )
        return 1

    if args.json:
        print(
            json.dumps(
                {
                    "prepared": True,
                    "benchmark_id": hr.BENCHMARK_ID,
                    "review_ready_pairs": len(actions),
                    "blocked_pairs": len(hr.blocked_rows(inventory)),
                    "queue_relative_path": layout.relative(
                        hr.review_queue_path(layout)
                    ),
                    "actions": actions,
                    "human_verified_admitted": 0,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    print("v3 holdout human-review workspace prepared (offline; nothing decided)")
    print(f"  queue     : {layout.relative(hr.review_queue_path(layout))}")
    for entry in queue["entries"]:
        action = next(
            (item for item in actions if item["pair_id"] == entry["pair_id"]), None
        )
        detail = (
            f"blocked ({entry['blocking_reason']})"
            if entry["review_status"] == hr.REVIEW_STATUS_BLOCKED
            else (
                f"labels={entry['proposed_label_count']:<3} "
                f"units={entry['canonical_unit_id_count']:<3} "
                f"record={action['review_record']:<9} "
                f"human={action['human_review_file']}"
            )
        )
        print(
            f"  {entry['review_position']:>2}. {entry['pair_id']:<16} "
            f"{entry['review_status']:<20} {detail}"
        )
    print(
        "\n  Machine proposals are unchanged and remain independently "
        "hash-verifiable. Every human annotation is an empty template: no "
        "label, no annotator id, no verification timestamp, and nothing "
        "admitted."
    )
    return 0


# --- status -------------------------------------------------------------------


def command_status(args: argparse.Namespace) -> int:
    manifest_path, layout, report_dir = _paths(args)
    artifacts = hr.load_committed_artifacts(manifest_path, report_dir)
    inventory = artifacts["inventory"]
    queue_path = hr.review_queue_path(layout)
    if not queue_path.exists():
        print(
            "No review queue [v3_review_queue_missing]: run the prepare "
            "command first.",
            file=sys.stderr,
        )
        return 2
    records = {
        str(row["pair_id"]): (
            hr.load_json(
                hr.review_record_path(layout, str(row["pair_id"])),
                f"{row['pair_id']} review record",
            )
            if hr.review_record_path(layout, str(row["pair_id"])).exists()
            else None
        )
        for row in hr.review_ready_rows(inventory)
    }
    humans = {
        str(row["pair_id"]): (
            hr.load_json(
                hr.human_review_path(layout, str(row["pair_id"])),
                f"{row['pair_id']} human annotation",
            )
            if hr.human_review_path(layout, str(row["pair_id"])).exists()
            else None
        )
        for row in hr.review_ready_rows(inventory)
    }
    live = hr.build_queue(
        layout=layout, inventory=inventory, records=records, humans=humans
    )
    if args.json:
        print(json.dumps(live, indent=2, sort_keys=True))
        return 0
    print("v3 holdout review queue (committed packet-inventory order)")
    for entry in live["entries"]:
        decided = (
            "-"
            if entry["review_status"] == hr.REVIEW_STATUS_BLOCKED
            else f"{entry['subjects_decided']}/{entry['proposed_label_count']}"
        )
        print(
            f"  {entry['review_position']:>2}. {entry['pair_id']:<16} "
            f"{entry['review_status']:<20} subjects_decided={decided:<8} "
            f"{entry['blocking_reason'] or ''}"
        )
    print(
        "\n  subjects_decided counts EXPLICIT reviewer decisions only. A null "
        "decision is not agreement, and no status here means a label is correct."
    )
    return 0


# --- show ---------------------------------------------------------------------


def command_show(args: argparse.Namespace) -> int:
    """Render ONE packet, only because a person asked for it by id.

    Packets carry real filing excerpts. Nothing here is ever written to a
    tracked file, and the machine values are labelled as proposals so they read
    as input to a decision rather than a recommendation.
    """
    manifest_path, layout, report_dir = _paths(args)
    artifacts = hr.load_committed_artifacts(manifest_path, report_dir)
    inventory = artifacts["inventory"]
    pair_id = args.pair_id
    row = next(
        (
            item
            for item in hr.inventory_rows(inventory)
            if str(item["pair_id"]) == pair_id
        ),
        None,
    )
    if row is None:
        print(
            f"Unknown pair [v3_review_unknown_pair]: {pair_id!r} is not in the "
            "committed packet inventory.",
            file=sys.stderr,
        )
        return 2
    if row.get("packet_status") != "written":
        print(
            f"Blocked pair [v3_review_pair_blocked]: {pair_id} has no packet "
            f"({row.get('blocking_reason')}). It stays in the corpus and "
            "receives no annotation.",
            file=sys.stderr,
        )
        return 2

    packet_path = layout.packet_json_path(pair_id)
    markdown_path = layout.packet_markdown_path(pair_id)
    if args.markdown and markdown_path.exists():
        print(markdown_path.read_text(encoding="utf-8"))
        return 0
    packet = hr.load_json(packet_path, f"{pair_id} packet")
    if args.json:
        print(json.dumps(packet, indent=2, sort_keys=True))
        return 0

    print(f"Packet {row['packet_id']} — {pair_id} ({layout.relative(packet_path)})")
    print(f"  {packet['banner']}")
    print(
        f"  canonical unit identities: previous="
        f"{row['previous_canonical_unit_id_count']} "
        f"current={row['current_canonical_unit_id_count']}"
    )
    for index, entry in enumerate(packet.get("alignments", [])):
        print(f"\n  --- subject {index} ---")
        print(f"  previous_unit_id : {entry.get('previous_unit_id')}")
        print(f"  current_unit_id  : {entry.get('current_unit_id')}")
        print(f"  unit_key         : {entry.get('unit_key')}")
        print(f"  previous_heading : {entry.get('previous_heading')}")
        print(f"  current_heading  : {entry.get('current_heading')}")
        print(
            "  MACHINE PROPOSAL (not ground truth): change_type="
            f"{entry.get('machine_proposed_change_type')!r} reason="
            f"{entry.get('machine_proposed_undetermined_reason')!r}"
        )
        for side in ("previous", "current"):
            excerpt = entry.get(f"{side}_excerpt")
            if excerpt:
                print(f"  {side}_excerpt: {excerpt}")
    print(
        "\n  You decide every field below for every subject, in the human "
        f"annotation file {layout.relative(hr.human_review_path(layout, pair_id))}:"
    )
    for field in hr.LABEL_DECISION_FIELDS:
        print(f"    - {field}")
    print(
        "  and record an explicit reviewer_decision per subject in "
        f"{layout.relative(hr.review_record_path(layout, pair_id))} "
        f"(one of {list(hr.REVIEWER_DECISIONS)})."
    )
    return 0


# --- complete -----------------------------------------------------------------


def command_complete(args: argparse.Namespace) -> int:
    """Record the reviewer's explicit completion marker for one packet.

    This writes exactly one boolean. It sets no label, no status, no annotator
    id, and no timestamp, and it refuses unless the person has already recorded
    an explicit decision for every canonical subject and filled every label's
    decision fields themselves.
    """
    manifest_path, layout, report_dir = _paths(args)
    artifacts = hr.load_committed_artifacts(manifest_path, report_dir)
    inventory = artifacts["inventory"]
    pair_id = args.pair_id

    row = next(
        (
            item
            for item in hr.inventory_rows(inventory)
            if str(item["pair_id"]) == pair_id
        ),
        None,
    )
    if row is None:
        print(
            f"Unknown pair [v3_review_unknown_pair]: {pair_id!r}", file=sys.stderr
        )
        return 2
    if row.get("packet_status") != "written":
        print(
            f"Blocked pair [v3_review_pair_blocked]: {pair_id} has no packet "
            "and is never marked reviewer_completed.",
            file=sys.stderr,
        )
        return 2
    if not args.confirm_reviewed:
        print(
            "Refused [v3_review_confirmation_required]: pass --confirm-reviewed "
            "to state that you personally reviewed every subject in this "
            "packet. Nothing else is treated as approval.",
            file=sys.stderr,
        )
        return 2

    record_path = hr.review_record_path(layout, pair_id)
    if not record_path.exists():
        print(
            f"No review record [v3_review_record_missing]: run prepare first.",
            file=sys.stderr,
        )
        return 2
    review_record = hr.load_json(record_path, f"{pair_id} review record")

    blockers: list[str] = []
    subjects = [
        subject
        for subject in review_record.get("subjects", [])
        if isinstance(subject, dict)
    ]
    undecided = [
        str(subject.get("label_id"))
        for subject in subjects
        if subject.get("reviewer_decision") is None
    ]
    if undecided:
        blockers.append(
            f"{len(undecided)} subjects carry no explicit reviewer_decision: "
            + hr.bounded(sorted(undecided))
        )
    unknown_decisions = [
        str(subject.get("label_id"))
        for subject in subjects
        if subject.get("reviewer_decision") is not None
        and subject.get("reviewer_decision") not in hr.REVIEWER_DECISIONS
    ]
    if unknown_decisions:
        blockers.append(
            "unknown reviewer_decision values on: "
            + hr.bounded(sorted(unknown_decisions))
        )

    human_path = hr.human_review_path(layout, pair_id)
    if not human_path.exists():
        blockers.append("the human annotation file does not exist")
    else:
        human = hr.load_json(human_path, f"{pair_id} human annotation")
        if hr.is_untouched_template(human):
            blockers.append(
                "the human annotation is still the empty template; an "
                "untouched file is never a reviewed one"
            )
        else:
            incomplete = sorted(
                str(label.get("label_id"))
                for label in human.get("labels", [])
                if isinstance(label, dict)
                and any(
                    label.get(field) is None
                    for field in (
                        "expected_change_type",
                        "expected_evidence_side",
                        "confidence",
                    )
                )
            )
            if incomplete:
                blockers.append(
                    f"{len(incomplete)} labels are not fully decided: "
                    + hr.bounded(incomplete)
                )

    if blockers:
        print(
            f"Completion refused [v3_review_packet_incomplete] for {pair_id}:",
            file=sys.stderr,
        )
        for blocker in blockers:
            print(f"  - {blocker}", file=sys.stderr)
        return 1

    if review_record.get("reviewer_completed") is True:
        print(f"{pair_id} was already marked reviewer_completed; nothing changed.")
        return 0

    review_record["reviewer_completed"] = True
    rfb.write_json_atomic(record_path, review_record)
    print(
        f"{pair_id} marked reviewer_completed ({len(subjects)} canonical "
        "subjects explicitly decided)."
    )
    print(
        "  No annotator id, verification timestamp, or annotation_status was "
        "written. Admission still requires you to edit "
        f"{layout.relative(human_path)} yourself."
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Prepare and drive the v3 holdout human-review workspace. Offline; "
            "decides no label, admits no annotation, computes no metric."
        )
    )
    parser.add_argument("--manifest", metavar="PATH", help="v3 holdout manifest path")
    parser.add_argument(
        "--corpus-dir", metavar="PATH", help="local v3 holdout corpus directory"
    )
    parser.add_argument(
        "--report-dir",
        metavar="PATH",
        help="directory of the committed v3 reports (default: beside the manifest)",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("prepare", help="create the review queue, records, and templates")
    sub.add_parser("status", help="print the live review queue")

    show = sub.add_parser("show", help="render ONE packet for review")
    show.add_argument("pair_id")
    show.add_argument(
        "--markdown", action="store_true", help="render the packet markdown"
    )

    complete = sub.add_parser(
        "complete", help="record your explicit completion marker for one packet"
    )
    complete.add_argument("pair_id")
    complete.add_argument(
        "--confirm-reviewed",
        action="store_true",
        help="state that you personally reviewed every subject in this packet",
    )

    args = parser.parse_args(argv)
    handlers = {
        "prepare": command_prepare,
        "status": command_status,
        "show": command_show,
        "complete": command_complete,
    }
    try:
        return handlers[args.command](args)
    except rfb.BenchmarkError as exc:
        print(f"Refused [{exc.code}]: {exc.message}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 — never leak raw exception text
        print(
            "Failed [v3_review_preparation_internal_error]: "
            f"{type(exc).__name__}",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    sys.exit(main())
