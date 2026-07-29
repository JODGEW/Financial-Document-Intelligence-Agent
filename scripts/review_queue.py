"""Human review queue CLI (Governance_layer.md §7.8, Phase 5).

Inspect and resolve answers the agent held for human review. The agent enqueues a
held item when an answer trips humanReviewRequired; this tool lists pending items,
shows the full draft and its evidence, and approves or rejects them.

Approve/reject moves the item between queue files. It does not mutate the audit
log and does not re-deliver the held answer to the user; both are out of scope for
this phase.

Examples:
    # Pending items waiting on a reviewer.
    python scripts/review_queue.py list

    # Full draft answer + retrieved sources for one item.
    python scripts/review_queue.py show --review-id review_<auditId>

    # Approve or reject, with an optional note.
    python scripts/review_queue.py approve --review-id review_<auditId> --note "Checked sources."
    python scripts/review_queue.py reject  --review-id review_<auditId>

    # JSON for piping.
    python scripts/review_queue.py list --output json

    # Seed local demo data: one pending, one approved, one rejected item,
    # generated through the real validation -> hold -> resolve pipeline under
    # the shipped policy (no thresholds touched, no model call). Refuses to
    # touch a non-empty queue unless --reset is given; --reset deletes only
    # the three queue JSONL files under the queue directory.
    python scripts/review_queue.py seed
    python scripts/review_queue.py seed --reset
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
from governance import review_queue


def _summary(item: dict) -> dict:
    """The list-view fields for one pending item."""
    return {
        "reviewId": item.get("reviewId"),
        "question": item.get("question"),
        "riskScore": item.get("riskScore"),
        "riskLevel": item.get("riskLevel"),
        "riskReasons": item.get("riskReasons", []),
    }


def _format_list_text(items: list[dict]) -> str:
    if not items:
        return "No pending review items."
    lines = [f"Pending review items: {len(items)}", ""]
    for item in items:
        reasons = ", ".join(item.get("riskReasons") or []) or "none"
        lines.append(f"[{item.get('reviewId')}]")
        lines.append(f"  Q: {item.get('question')}")
        lines.append(f"  Risk: {item.get('riskLevel')} ({item.get('riskScore')})")
        lines.append(f"  Reasons: {reasons}")
        lines.append("")
    return "\n".join(lines).rstrip()


def _format_show_text(item: dict) -> str:
    lines = [
        f"Review ID: {item.get('reviewId')}",
        f"Audit ID:  {item.get('auditId')}",
        f"Status:    {item.get('reviewStatus')}",
        f"Created:   {item.get('createdAt')}",
        f"Risk:      {item.get('riskLevel')} ({item.get('riskScore')})",
        f"Reasons:   {', '.join(item.get('riskReasons') or []) or 'none'}",
        "",
        f"Question:\n  {item.get('question')}",
        "",
        "Draft answer:",
        item.get("draftAnswer", ""),
        "",
        f"Retrieved sources: {len(item.get('retrievedSources') or [])}",
    ]
    for source in item.get("retrievedSources") or []:
        page = source.get("page")
        page_str = f" p.{page}" if page is not None else ""
        lines.append(f"  - {source.get('source_name')}{page_str}")
    return "\n".join(lines)


def _cmd_list(args) -> int:
    items = review_queue.list_pending(args.queue_dir)
    if args.output == "json":
        print(json.dumps([_summary(item) for item in items], indent=2))
    else:
        print(_format_list_text(items))
    return 0


def _cmd_show(args) -> int:
    item = review_queue.get(args.review_id, args.queue_dir)
    if item is None:
        if args.output == "json":
            print(json.dumps({"reviewId": args.review_id, "error": "not_found"}, indent=2))
        else:
            print(f"Review id {args.review_id!r} not found in the pending queue.")
        return 1
    if args.output == "json":
        print(json.dumps(item, indent=2))
    else:
        print(_format_show_text(item))
    return 0


def _resolve(args, action) -> int:
    """Shared body for approve/reject (``action`` is the review_queue function)."""
    item = action(args.review_id, args.queue_dir, note=args.note)
    if item is None:
        if args.output == "json":
            print(json.dumps({"reviewId": args.review_id, "error": "not_found"}, indent=2))
        else:
            print(f"Review id {args.review_id!r} not found in the pending queue.")
        return 1
    if args.output == "json":
        print(json.dumps(item, indent=2))
    else:
        print(f"{item['reviewStatus'].capitalize()} {item['reviewId']}.")
    return 0


# Demo seed cases. Each draft is deliberately ungrounded (fabricated numbers,
# citation to a file that was never retrieved) so the real grounding validator
# and risk scorer hold it via the mandatory grounding floor under the shipped
# policy. Evidence excerpts use corpus-relative paths only - seeded records
# must never carry a developer's absolute filesystem paths.
_SEED_TOOL_CONTENT = (
    "[Source 1: docs/acme-corp-10k-excerpt-2025.pdf, page 2]\n"
    "Total revenue was $284.7 million, an increase of 18 percent over fiscal 2024."
)

_SEED_CASES = (
    {
        "question": "What did Acme report for security remediation costs in fiscal 2025?",
        "draft": (
            "## Result Summary\n\n"
            "Internal Corpus Answer: Available. Acme Corporation reported "
            "$512 million in security remediation costs for fiscal 2025 "
            "[acme-shadow-report.pdf p.8].\n\n"
            "External Context: Unavailable."
        ),
        "resolution": None,
        "note": None,
    },
    {
        "question": "How fast did Acme's cybersecurity spending grow year over year?",
        "draft": (
            "## Result Summary\n\n"
            "Internal Corpus Answer: Available. Cybersecurity spending grew 63% "
            "year over year while peer incidents fell sharply "
            "[acme-shadow-report.pdf p.8].\n\n"
            "External Context: Unavailable."
        ),
        "resolution": "approve",
        "note": "Demo seed: reviewed for the walkthrough and approved.",
    },
    {
        "question": "What share of Acme revenue came from its largest customer?",
        "draft": (
            "## Result Summary\n\n"
            "Internal Corpus Answer: Available. The largest customer accounted "
            "for 47% of total revenue [acme-shadow-report.pdf p.8].\n\n"
            "External Context: Unavailable."
        ),
        "resolution": "reject",
        "note": "Demo seed: numbers are not present in the retrieved evidence.",
    },
)

_QUEUE_FILES = (
    review_queue.PENDING_FILE,
    review_queue.APPROVED_FILE,
    review_queue.REJECTED_FILE,
)


def _cmd_seed(args) -> int:
    """Seed one pending, one approved, and one rejected demo item.

    Every item runs through the real pipeline: grounding validation, risk
    scoring, decision routing, queue enqueue, and (for the terminal two) the
    real approve/reject functions - so risk labels, reason codes, and audit
    records come from the currently loaded policy, never from a snapshot.
    Refuses to touch a non-empty queue without --reset; --reset removes only
    the three queue JSONL files under the queue directory.
    """
    queue_dir = Path(args.queue_dir)
    existing = len(review_queue.list_items(queue_dir, "all"))
    if existing and not args.reset:
        print(
            f"Queue at {queue_dir} already holds {existing} item(s). "
            "Rerun with --reset to replace them."
        )
        return 1
    if args.reset:
        for name in _QUEUE_FILES:
            try:
                (queue_dir / name).unlink()
            except FileNotFoundError:
                pass

    # Heavy import kept out of list/show; no model call happens - finalize is
    # validation + scoring + routing + persistence only.
    from agent import _finalize_query_result

    messages = [
        {"type": "tool", "name": "local_search", "content": _SEED_TOOL_CONTENT}
    ]
    previous_queue_dir = config.REVIEW_QUEUE_DIR
    config.REVIEW_QUEUE_DIR = str(queue_dir)
    created: list[tuple[str, str]] = []
    try:
        for case in _SEED_CASES:
            result = _finalize_query_result(
                question=case["question"],
                output=case["draft"],
                result_messages=messages,
                trace_messages=messages,
                guardrail_outcome=None,
            )
            report = result.get("governance_report") or {}
            if report.get("decision") != "held_for_review":
                print(
                    "Seed draft was not held for review under the current "
                    f"policy (decision: {report.get('decision')!r}); aborting."
                )
                return 1
            review_id = f"review_{report.get('auditId')}"
            status = "pending"
            if case["resolution"] == "approve":
                review_queue.approve(review_id, queue_dir, note=case["note"])
                status = "approved"
            elif case["resolution"] == "reject":
                review_queue.reject(review_id, queue_dir, note=case["note"])
                status = "rejected"
            created.append((review_id, status))
    finally:
        config.REVIEW_QUEUE_DIR = previous_queue_dir

    print(f"Seeded {len(created)} demo review item(s) in {queue_dir}:")
    for review_id, status in created:
        print(f"  {status:8s} {review_id}")
    print(
        "Matching audit records were written to the local audit log, so the "
        "reviewer UI's governance-report join works for these items."
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Human review queue for held answers")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--queue-dir",
        default=config.REVIEW_QUEUE_DIR,
        help="Queue directory (default: REVIEW_QUEUE_DIR / review_queue/)",
    )
    common.add_argument("--output", choices=["text", "json"], default="text")

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", parents=[common], help="List pending review items")

    show_p = sub.add_parser("show", parents=[common], help="Show one item in full")
    show_p.add_argument("--review-id", required=True)

    approve_p = sub.add_parser("approve", parents=[common], help="Approve a held item")
    approve_p.add_argument("--review-id", required=True)
    approve_p.add_argument("--note", default=None, help="Optional reviewer note")

    reject_p = sub.add_parser("reject", parents=[common], help="Reject a held item")
    reject_p.add_argument("--review-id", required=True)
    reject_p.add_argument("--note", default=None, help="Optional reviewer note")

    seed_p = sub.add_parser(
        "seed", parents=[common], help="Seed local demo review items"
    )
    seed_p.add_argument(
        "--reset",
        action="store_true",
        help="Replace existing queue files (deletes only the three queue "
        "JSONL files under the queue directory).",
    )

    args = parser.parse_args(argv)

    if args.command == "list":
        return _cmd_list(args)
    if args.command == "show":
        return _cmd_show(args)
    if args.command == "approve":
        return _resolve(args, review_queue.approve)
    if args.command == "reject":
        return _resolve(args, review_queue.reject)
    if args.command == "seed":
        return _cmd_seed(args)
    return 1


if __name__ == "__main__":
    sys.exit(main())
