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

    args = parser.parse_args(argv)

    if args.command == "list":
        return _cmd_list(args)
    if args.command == "show":
        return _cmd_show(args)
    if args.command == "approve":
        return _resolve(args, review_queue.approve)
    if args.command == "reject":
        return _resolve(args, review_queue.reject)
    return 1


if __name__ == "__main__":
    sys.exit(main())
