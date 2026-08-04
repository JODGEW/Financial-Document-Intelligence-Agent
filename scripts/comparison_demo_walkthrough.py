#!/usr/bin/env python
"""Offline, isolated walkthrough of the structured filing-comparison workflow.

The demo uses the repository's controlled synthetic ``missing-section-pair``
fixture. It exercises the real protected FastAPI routes, durable SQLite job,
one-shot detection worker, governance hold, authenticated review decision, and
release-gated export. A deterministic local embedding stub is used only to
seed a temporary Chroma collection; comparison detection reads by metadata and
never embeds a query.

No AWS credentials, model calls, network access, or persistent repository state
are used. The fixture is fictional and the result is a workflow demonstration,
not evidence of accuracy on real SEC filings.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any, TextIO

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
_FIXTURE_PATH = (
    _REPO_ROOT
    / "tests"
    / "fixtures"
    / "comparison_regression"
    / "missing_section_pair.json"
)

_SUBJECT = "comparison-demo-admin"
_REVIEW_NOTE = (
    "Controlled demo: inspected the missing-section evidence and approved "
    "this workflow artifact."
)

ProgressCallback = Callable[[str, dict[str, Any]], None]


class _DemoEmbeddings:
    """Deterministic seed-only embeddings; detection must never query them."""

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [[float(len(text) % 7), 1.0] for text in texts]

    def embed_query(self, text: str) -> list[float]:
        raise AssertionError(
            "comparison detection must read sections by metadata, not embed a query"
        )


def _seed_fixture(workdir: Path):
    """Create a temporary registry and Chroma index from the tracked fixture."""
    from langchain_chroma import Chroma
    from langchain_core.documents import Document

    import chroma_batching
    import filing_registry

    fixture = json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))
    registry_path = workdir / "registry.jsonl"
    documents: list[Document] = []
    ids: list[str] = []
    form_type = filing_registry.normalize_form_type(fixture["form_type"])

    for filing in fixture["filings"]:
        source_name = filing["source_name"]
        for chunk in filing["chunks"]:
            metadata: dict[str, Any] = {
                "source": f"/controlled-demo/{source_name}",
                "source_name": source_name,
                "source_path": source_name,
                "filing_id": filing["filing_id"],
                "document_id": fixture["document_family_id"],
                "company": fixture["company_name"],
                "company_key": fixture["company_key"],
                "filing_type": form_type,
                "chunk_seq": chunk["chunk_seq"],
                "page": chunk["page"],
                "section_title": chunk["section_title"],
            }
            if chunk.get("section_key"):
                metadata["section_key"] = chunk["section_key"]
            documents.append(
                Document(page_content=chunk["text"], metadata=metadata)
            )
            ids.append(
                f"{source_name}:{chunk['page']}:"
                + hashlib.sha1(chunk["text"].encode("utf-8")).hexdigest()[:12]
            )

        filing_registry.record_outcome(
            registry_path,
            source_path=source_name,
            source_name=source_name,
            source_hash=filing["source_hash"],
            parse_status=filing_registry.PARSED,
            document_family_id=fixture["document_family_id"],
            filing_id=filing["filing_id"],
            company_key=fixture["company_key"],
            company_name=fixture["company_name"],
            form_type=form_type,
            period_end=filing["period_end"],
            identity_source="manifest",
            loader="controlled_demo_fixture",
            chunk_count=len(filing["chunks"]),
        )

    chroma = Chroma(
        collection_name="comparison_demo",
        persist_directory=str(workdir / "chroma"),
        embedding_function=_DemoEmbeddings(),
    )
    chroma_batching.add_documents_in_batches(
        chroma,
        documents,
        ids,
        operation="comparison_demo.seed_fixture",
    )
    return fixture, registry_path, chroma


def _response_json(response, expected_status: int, operation: str) -> dict:
    if response.status_code != expected_status:
        raise RuntimeError(
            f"{operation} returned HTTP {response.status_code}: "
            f"{response.text[:500]}"
        )
    return response.json()


def _run_in_directory(
    workdir: Path, *, on_progress: ProgressCallback | None = None
) -> dict[str, Any]:
    """Run the protected API + worker workflow and return a bounded summary."""
    fixture, registry_path, chroma = _seed_fixture(workdir)
    db_path = workdir / "comparisons.db"

    def report(stage: str, **details: Any) -> None:
        if on_progress is not None:
            on_progress(stage, details)

    report("inputs", fixture=fixture)

    # Set an ephemeral secret before importing api: its app-level authenticator
    # is constructed at import time. The value is never printed or persisted.
    previous_secret = os.environ.get("FDIA_AUTH_SECRET")
    os.environ["FDIA_AUTH_SECRET"] = secrets.token_urlsafe(48)

    import access_control
    import api
    import comparison_detection_worker
    import config
    from fastapi.testclient import TestClient

    previous_config = {
        "COMPARISON_DB_PATH": config.COMPARISON_DB_PATH,
        "FILING_REGISTRY_PATH": config.FILING_REGISTRY_PATH,
        "CHROMA_PERSIST_DIR": config.CHROMA_PERSIST_DIR,
    }
    previous_authenticator = api.app.state.authenticator

    try:
        config.COMPARISON_DB_PATH = str(db_path)
        config.FILING_REGISTRY_PATH = str(registry_path)
        config.CHROMA_PERSIST_DIR = str(workdir / "chroma")
        api.app.state.authenticator = access_control.Authenticator.from_environment()

        policy = access_control.load_access_control_policy()
        token = access_control.issue_access_token(
            policy=policy,
            secret=os.environ["FDIA_AUTH_SECRET"],
            subject=_SUBJECT,
            roles=("admin",),
            ttl_seconds=900,
        )
        headers = {"Authorization": f"Bearer {token}"}
        previous_id = fixture["filings"][0]["filing_id"]
        current_id = fixture["filings"][1]["filing_id"]

        with TestClient(api.app, headers=headers) as client:
            created = _response_json(
                client.post(
                    "/api/comparisons",
                    json={
                        "previousFilingId": previous_id,
                        "currentFilingId": current_id,
                    },
                ),
                201,
                "create comparison",
            )
            comparison_id = created["comparison"]["comparisonId"]

            queued = _response_json(
                client.post(f"/api/comparisons/{comparison_id}/detect"),
                202,
                "enqueue detection",
            )
            job_id = queued["jobId"]

            worker = comparison_detection_worker.run_one_job(
                worker_id="comparison-demo-worker",
                job_id=job_id,
                db_path=db_path,
                registry_path=registry_path,
                chroma_client=chroma,
            )
            if worker.get("job_status") != "succeeded":
                raise RuntimeError(
                    "comparison demo worker did not succeed: "
                    f"{worker.get('failure_code') or worker}"
                )

            result = _response_json(
                client.get(f"/api/comparisons/{comparison_id}/result"),
                200,
                "read detection result",
            )
            report(
                "detection",
                fixture=fixture,
                comparison=created["comparison"],
                job_id=job_id,
                worker=worker,
                result=result,
            )
            governed = _response_json(
                client.post(f"/api/comparisons/{comparison_id}/governance"),
                201,
                "evaluate governance",
            )["evaluation"]
            if governed["decision"] != "held_for_review":
                raise RuntimeError(
                    "controlled missing-section fixture was expected to be held, "
                    f"got {governed['decision']!r}"
                )
            report("governance", evaluation=governed)

            export_request = {"evaluationId": governed["evaluationId"]}
            before_review = client.post(
                f"/api/comparisons/{comparison_id}/exports",
                json=export_request,
            )
            if before_review.status_code != 409:
                raise RuntimeError(
                    "release gate should refuse a pending review, got HTTP "
                    f"{before_review.status_code}"
                )
            before_detail = before_review.json().get("detail") or {}
            report(
                "release_refused",
                status_code=before_review.status_code,
                detail=before_detail,
            )

            reviews = _response_json(
                client.get(
                    "/api/comparison-reviews",
                    params={"comparison_id": comparison_id},
                ),
                200,
                "list comparison reviews",
            )
            if len(reviews) != 1:
                raise RuntimeError(
                    f"expected one pending comparison review, got {len(reviews)}"
                )
            review_id = reviews[0]["reviewId"]

            decision = _response_json(
                client.post(
                    f"/api/comparison-reviews/{review_id}/decision",
                    json={
                        "action": "approved",
                        "reasonCode": "approved_after_evidence_review",
                        "reviewerNote": _REVIEW_NOTE,
                    },
                ),
                201,
                "approve comparison review",
            )["decision"]
            report("review", review_id=review_id, decision=decision)

            exported = _response_json(
                client.post(
                    f"/api/comparisons/{comparison_id}/exports",
                    json=export_request,
                ),
                201,
                "create released export",
            )["export"]
            fetched = _response_json(
                client.get(
                    f"/api/comparison-exports/{exported['export_id']}"
                ),
                200,
                "read released export",
            )
            if fetched != exported:
                raise RuntimeError("persisted export did not round-trip unchanged")
            report(
                "export",
                export=exported,
                round_trip_verified=True,
            )

        changes = result.get("changes") or []
        filing_summaries = []
        for filing in fixture["filings"]:
            filing_summaries.append(
                {
                    "side": filing["side"],
                    "filing_id": filing["filing_id"],
                    "period_end": filing["period_end"],
                    "source_name": filing["source_name"],
                    "chunk_count": len(filing["chunks"]),
                    "item_1a_chunk_count": sum(
                        chunk.get("section_key") == "item_1a_risk_factors"
                        for chunk in filing["chunks"]
                    ),
                }
            )
        return {
            "fixture_id": fixture["fixture_id"],
            "fictional_company": fixture["company_name"],
            "filings": filing_summaries,
            "comparison_id": comparison_id,
            "workflow_version": created["comparison"]["workflowVersion"],
            "job_id": job_id,
            "attempt_id": worker["attempt_id"],
            "job_status": worker["job_status"],
            "change_count": len(changes),
            "change_types": [change.get("change_type") for change in changes],
            "undetermined_reasons": [
                change.get("undetermined_reason")
                for change in changes
                if change.get("undetermined_reason")
            ],
            "result": result,
            "governance_evaluation_id": governed["evaluationId"],
            "governance_risk_score": governed["riskScore"],
            "governance_risk_level": governed["riskLevel"],
            "governance_decision": governed["decision"],
            "governance_reason_codes": governed["reasonCodes"],
            "release_refusal_http_status": before_review.status_code,
            "release_refusal_before_review": before_detail.get("code"),
            "review_id": review_id,
            "review_action": decision["action"],
            "reviewer_id": decision["reviewerId"],
            "reviewer_id_basis": decision["reviewerIdBasis"],
            "review_reason_code": decision["reasonCode"],
            "reviewer_note": decision["reviewerNote"],
            "export_id": exported["export_id"],
            "export_schema_version": exported["export_schema_version"],
            "release_basis": exported["release_basis"],
            "final_result_hash": exported["final_result_hash"],
            "export": exported,
        }
    finally:
        for name, value in previous_config.items():
            setattr(config, name, value)
        api.app.state.authenticator = previous_authenticator
        if previous_secret is None:
            os.environ.pop("FDIA_AUTH_SECRET", None)
        else:
            os.environ["FDIA_AUTH_SECRET"] = previous_secret


def _filing_by_side(fixture: dict[str, Any], side: str) -> dict[str, Any]:
    return next(filing for filing in fixture["filings"] if filing["side"] == side)


def _print_change(change: dict[str, Any], *, output: TextIO) -> None:
    print(
        f"  Change: {change.get('change_id')} "
        f"({change.get('change_type')})",
        file=output,
    )
    print(f"    Summary: {change.get('summary')}", file=output)
    if change.get("undetermined_reason"):
        print(
            f"    Why undetermined: {change['undetermined_reason']}",
            file=output,
        )
    print(
        "    Evidence references: "
        f"previous={len(change.get('previous_evidence') or [])}, "
        f"current={len(change.get('current_evidence') or [])}",
        file=output,
    )


def _progress_printer(
    *,
    output: TextIO,
    pause_between_steps: bool,
    show_json: bool,
    input_fn: Callable[[], str],
) -> ProgressCallback:
    """Build the human-facing six-step narrator around the real workflow."""

    def wait_for_next() -> None:
        if not pause_between_steps:
            return
        print("\n-- Press Enter to continue --", end="", file=output, flush=True)
        try:
            input_fn()
        except EOFError:
            print("\n[non-interactive input; continuing]", file=output)
        else:
            print(file=output)

    def report(stage: str, details: dict[str, Any]) -> None:
        print(file=output)
        if stage == "inputs":
            fixture = details["fixture"]
            previous = _filing_by_side(fixture, "previous")
            current = _filing_by_side(fixture, "current")
            previous_item_chunks = sum(
                chunk.get("section_key") == "item_1a_risk_factors"
                for chunk in previous["chunks"]
            )
            current_item_chunks = sum(
                chunk.get("section_key") == "item_1a_risk_factors"
                for chunk in current["chunks"]
            )
            print("STEP 1/6 - Filing pair", file=output)
            print(
                f"  Company: {fixture['company_name']} (fictional)",
                file=output,
            )
            print(
                "  Previous: "
                f"{previous['period_end']} | {previous['source_name']} | "
                f"Item 1A chunks={previous_item_chunks}",
                file=output,
            )
            print(
                "  Current:  "
                f"{current['period_end']} | {current['source_name']} | "
                f"Item 1A chunks={current_item_chunks}",
                file=output,
            )
            print(
                "  Expected safety behavior: the missing current Item 1A must "
                "produce one undetermined result, never mass removals.",
                file=output,
            )
            wait_for_next()
            return

        if stage == "detection":
            comparison = details["comparison"]
            worker = details["worker"]
            result = details["result"]
            print("STEP 2/6 - Durable detection", file=output)
            print(
                "  Protected API: created "
                f"{comparison['comparisonId']} and queued {details['job_id']}.",
                file=output,
            )
            print(
                "  One-shot worker: "
                f"{worker['job_status']} | attempt={worker['attempt_id']} | "
                f"workflow={comparison['workflowVersion']}",
                file=output,
            )
            changes = result.get("changes") or []
            print(
                f"  Detector result: {len(changes)} structured change(s).",
                file=output,
            )
            for change in changes:
                _print_change(change, output=output)
            wait_for_next()
            return

        if stage == "governance":
            evaluation = details["evaluation"]
            print("STEP 3/6 - Governance decision", file=output)
            print(
                f"  Risk: {evaluation['riskLevel']} "
                f"(score={evaluation['riskScore']})",
                file=output,
            )
            print(f"  Decision: {evaluation['decision']}", file=output)
            print(
                "  Reasons: "
                f"{', '.join(evaluation['reasonCodes']) or 'none'}",
                file=output,
            )
            print(
                "  Meaning: uncertainty is routed to review instead of being "
                "published as a confident filing change.",
                file=output,
            )
            wait_for_next()
            return

        if stage == "release_refused":
            detail = details["detail"]
            print("STEP 4/6 - Release gate before review", file=output)
            print(
                "  Release gate before review: refused "
                f"(HTTP {details['status_code']}, "
                f"code={detail.get('code')}).",
                file=output,
            )
            print(f"  Message: {detail.get('message')}", file=output)
            print(
                "  Meaning: a client cannot bypass the persisted review state.",
                file=output,
            )
            wait_for_next()
            return

        if stage == "review":
            decision = details["decision"]
            print("STEP 5/6 - Authenticated human review", file=output)
            print(f"  Review: {details['review_id']}", file=output)
            print(
                f"  Reviewer: {decision['reviewerId']} "
                f"(identity basis={decision['reviewerIdBasis']})",
                file=output,
            )
            print(
                f"  Decision: {decision['action']} | "
                f"reason={decision['reasonCode']}",
                file=output,
            )
            print(f"  Note: {decision['reviewerNote']}", file=output)
            wait_for_next()
            return

        if stage == "export":
            exported = details["export"]
            print("STEP 6/6 - Released structured export", file=output)
            print(
                f"  Export: {exported['export_id']} | "
                f"schema={exported['export_schema_version']}",
                file=output,
            )
            print(
                f"  Release basis: {exported['release_basis']}",
                file=output,
            )
            print(
                f"  Final result hash: {exported['final_result_hash']}",
                file=output,
            )
            print(
                "  Persistence check: export payload round-tripped unchanged.",
                file=output,
            )
            if show_json:
                print("\nFull export JSON:", file=output)
                print(
                    json.dumps(exported, indent=2, ensure_ascii=False),
                    file=output,
                )
            return

        raise RuntimeError(f"unknown comparison demo progress stage: {stage}")

    return report


def run_comparison_demo(
    *,
    workdir: Path | None = None,
    stream: TextIO | None = None,
    pause_between_steps: bool = False,
    show_json: bool = False,
    input_fn: Callable[[], str] | None = None,
) -> dict[str, Any]:
    """Run and narrate the isolated workflow, then return its full summary."""
    output = stream or sys.stdout
    reader = input_fn or input
    progress = _progress_printer(
        output=output,
        pause_between_steps=pause_between_steps,
        show_json=show_json,
        input_fn=reader,
    )

    line = "=" * 72
    print(
        f"{line}\n"
        " STRUCTURED FILING COMPARISON - OFFLINE WALKTHROUGH\n"
        f"{line}\n",
        file=output,
    )
    print(
        "[staging] Uses a controlled synthetic missing-section fixture and the "
        "real protected API, durable worker, governance, review, and export "
        "code. No AWS, model, or network call; this demonstrates workflow "
        "behavior, not real-filing accuracy.",
        file=output,
    )

    if workdir is None:
        with tempfile.TemporaryDirectory(prefix="fdia-comparison-demo-") as tmp:
            summary = _run_in_directory(Path(tmp), on_progress=progress)
        state_path = None
        state_message = (
            "temporary registry, Chroma, and SQLite data removed."
        )
    else:
        workdir = workdir.resolve()
        workdir.mkdir(parents=True, exist_ok=True)
        summary = _run_in_directory(workdir, on_progress=progress)
        state_path = str(workdir)
        state_message = f"state retained at {state_path}"

    summary["state_path"] = state_path
    print(f"\nState: {state_message}", file=output)
    print(
        "Outcome: uncertainty was held, pre-review release was refused, and "
        "the reviewed result was exported only after authenticated approval.",
        file=output,
    )
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--no-pause",
        action="store_true",
        help="Run all six narrated steps without waiting for Enter.",
    )
    parser.add_argument(
        "--show-json",
        action="store_true",
        help="Print the complete released comparison.export.v1 artifact.",
    )
    parser.add_argument(
        "--keep-state",
        action="store_true",
        help="Retain the temporary registry, Chroma data, and SQLite database.",
    )
    args = parser.parse_args(argv)

    retained_workdir = (
        Path(tempfile.mkdtemp(prefix="fdia-comparison-demo-retained-"))
        if args.keep_state
        else None
    )
    try:
        run_comparison_demo(
            workdir=retained_workdir,
            pause_between_steps=not args.no_pause,
            show_json=args.show_json,
        )
    except KeyboardInterrupt:
        print("\n\nDemo stopped.", file=sys.stderr)
        if retained_workdir is not None:
            print(f"Retained state: {retained_workdir}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
