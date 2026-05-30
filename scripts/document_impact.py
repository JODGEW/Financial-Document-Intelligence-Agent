"""Document change impact analysis CLI (Governance_layer.md §8, Phase 4).

When a document changes, find which past answers used the chunks that are now
gone or different. Thin wrapper over governance/impact.py.

Examples:
    # Change impact: compare a new version against the current index.
    python scripts/document_impact.py --document-id compliance-policy-personal-trading \\
        --new-source /tmp/modified-policy.md

    # Dependency scan: which past queries used this document's current chunks.
    python scripts/document_impact.py --document-id compliance-policy-personal-trading

    # JSON for piping.
    python scripts/document_impact.py --document-id acme-corp-10k-excerpt \\
        --new-source /tmp/new-10k.pdf --output json
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
from governance.impact import (
    build_impact_report,
    compute_new_chunks,
    diff_chunks,
    document_version,
    load_current_chunks,
    open_chroma,
    scan_audit_log,
)


def _group_queries(queries: list[dict]) -> dict[str, list[dict]]:
    """Group affected-query matches by auditId for readable text output."""
    grouped: dict[str, list[dict]] = {}
    for entry in queries:
        grouped.setdefault(str(entry.get("auditId")), []).append(entry)
    return grouped


def _format_change_text(report: dict, diff: dict) -> str:
    """Human-readable change-impact summary."""
    lines: list[str] = []
    lines.append(f"Document: {report['documentId']}")
    lines.append(
        f"Version: {report.get('oldVersionHash')} -> {report.get('newVersionHash')}"
    )
    lines.append("")

    added = diff.get("added", [])
    removed = diff.get("removed", [])
    modified = diff.get("modified", [])
    lines.append(
        f"Changed chunks: {len(modified)} modified, {len(removed)} removed, {len(added)} added"
    )
    for pair in modified:
        lines.append(f"  ~ modified {pair['old']['chunk_id']} -> {pair['new']['chunk_id']}")
    for chunk in removed:
        lines.append(f"  - removed  {chunk['chunk_id']}")
    for chunk in added:
        lines.append(f"  + added    {chunk['chunk_id']}")
    lines.append("")

    queries = report.get("affectedPastQueries", [])
    if not queries:
        lines.append("Affected past queries: none in the audit log.")
    else:
        grouped = _group_queries(queries)
        lines.append(f"Affected past queries: {len(grouped)} (across {len(queries)} chunk uses)")
        for audit_id, entries in grouped.items():
            first = entries[0]
            lines.append(f"  [{audit_id}] {first.get('timestamp')}")
            lines.append(f"      Q: {first.get('question')}")
            for entry in entries:
                lines.append(f"      used old chunk: {entry['usedOldChunk']}")
    lines.append("")

    if report["requiresReevaluation"]:
        lines.append(
            "Recommendation: re-evaluate the answers above. They cited chunks that "
            "changed or were removed in the new version."
        )
    else:
        lines.append(
            "Recommendation: no past answer in the audit log used the changed chunks. "
            "No re-evaluation needed."
        )
    return "\n".join(lines)


def _format_dependency_text(document_id: str, old_count: int, queries: list[dict]) -> str:
    """Human-readable dependency scan (no new version supplied)."""
    lines: list[str] = []
    lines.append(f"Document: {document_id}")
    lines.append(f"Current chunks in index: {old_count}")
    lines.append("")
    lines.append(
        "No --new-source supplied: this is a dependency scan, not a change impact. "
        "It lists past queries that used this document's current chunks; if the "
        "document changes, these are the answers at risk."
    )
    lines.append("")
    if not queries:
        lines.append("Past queries using this document: none in the audit log.")
    else:
        grouped = _group_queries(queries)
        lines.append(f"Past queries using this document: {len(grouped)}")
        for audit_id, entries in grouped.items():
            first = entries[0]
            lines.append(f"  [{audit_id}] {first.get('timestamp')}")
            lines.append(f"      Q: {first.get('question')}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Document change impact analysis")
    parser.add_argument("--document-id", required=True, help="Document family id to check")
    parser.add_argument(
        "--new-source",
        help="Path to the new version. Omit for a dependency scan against audit history only.",
    )
    parser.add_argument(
        "--audit-log",
        default=config.AUDIT_LOG_PATH,
        help="Audit JSONL path (default: AUDIT_LOG_PATH / audit_logs/query_audit.jsonl)",
    )
    parser.add_argument("--output", choices=["text", "json"], default="text")
    args = parser.parse_args(argv)

    client = open_chroma()
    old_chunks = load_current_chunks(args.document_id, client)

    if not old_chunks:
        message = f"Document id {args.document_id!r} not found in the index."
        if args.output == "json":
            print(json.dumps({"documentId": args.document_id, "error": "not_found"}, indent=2))
        else:
            print(message)
        return 1

    if args.new_source:
        # ingest's load/split print progress to stdout; keep stdout clean (JSON
        # must be pipeable) by sending that noise to stderr.
        with contextlib.redirect_stdout(sys.stderr):
            new_chunks = compute_new_chunks(args.new_source)
        diff = diff_chunks(old_chunks, new_chunks)
        affected_ids = {c["chunk_id"] for c in (diff["removed"] + [m["old"] for m in diff["modified"]])}
        affected_queries = scan_audit_log(args.audit_log, affected_ids)
        report = build_impact_report(
            args.document_id,
            diff,
            affected_queries,
            old_version_hash=document_version(old_chunks),
            new_version_hash=document_version(new_chunks),
        )
        if args.output == "json":
            print(json.dumps(report, indent=2))
        else:
            print(_format_change_text(report, diff))
        return 0

    # Dependency scan: no new version, so nothing changed. Report which past
    # queries used the document's current chunks.
    current_ids = {c["chunk_id"] for c in old_chunks}
    dependent_queries = scan_audit_log(args.audit_log, current_ids)
    if args.output == "json":
        print(
            json.dumps(
                {
                    "documentId": args.document_id,
                    "mode": "dependency_scan",
                    "currentChunkCount": len(old_chunks),
                    "dependentPastQueries": dependent_queries,
                    "requiresReevaluation": False,
                },
                indent=2,
            )
        )
    else:
        print(_format_dependency_text(args.document_id, len(old_chunks), dependent_queries))
    return 0


if __name__ == "__main__":
    sys.exit(main())
