#!/usr/bin/env python
"""Guided end-to-end demo: governed RAG plus structured filing comparison.

Runs the current workflow story in four acts:

  Act 1  Ask a grounded question. Show the answer plus the governance report
         behind it: context-policy admission (chunks admitted/dropped and why),
         grounding score, citation coverage, risk score, and the routing decision.
  Act 2  The hold path under the normal policy. A deliberately ungrounded draft
         (fabricated numbers, citation to a file that was never retrieved) is
         validated against real retrieved evidence by the production pipeline —
         grounding validation, risk scoring, decision routing, review queue —
         with no thresholds changed. The grounding floor
         (require_review_below_grounding) holds it: the user-facing answer is
         the held notice, the draft lands in the review queue with its evidence
         and reason codes, and the reviewer CLI resolves it.
  Act 3  Stage drift. The eval baseline is diffed against a perturbed copy in a
         temp file (the real eval/baseline.json is never touched), producing the
         exact scripts/eval_diff.py output an operator sees when a metric moves,
         a case flips pass/fail, or latency shifts.
  Act 4  Run the structured Item 1A comparison path over an isolated controlled
         synthetic filing pair: protected API create/enqueue, durable one-shot
         worker, deterministic detection, governance hold, authenticated review,
         and release-gated export.

Staging is explicit: the script prints what it substitutes before each act (the
ungrounded draft in Act 2, the perturbed baseline copy in Act 3, and the
controlled synthetic pair in Act 4). Policy is never touched: thresholds,
weights, and the hold mode all run at their shipped values. The one persistent
side effect is intentional: Act 2 writes a real item into review_queue/
(gitignored runtime state). Reset or rebuild local demo queue data with:
python scripts/review_queue.py seed --reset

Requirements: AWS credentials for Bedrock (same as cli.py) and an ingested
corpus (python ingest.py) for Acts 1-2. Acts 3-4 need neither; Act 4 uses only
temporary state and makes no network or model calls.

Usage:
    python scripts/demo_walkthrough.py               # paused between acts
    python scripts/demo_walkthrough.py --no-pause    # straight through
    python scripts/demo_walkthrough.py --live-eval   # Act 3 runs the real eval
"""

from __future__ import annotations

import argparse
import copy
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

GROUNDED_QUESTION = (
    "What does the compliance policy say about blackout periods for personal trading?"
)
HELD_QUESTION = (
    "How do the cybersecurity risks in the 10-K compare to the trends described "
    "in the internal research note?"
)

# Act 2's deliberately ungrounded draft: numbers that appear in no retrieved
# chunk, cited to a file that was never retrieved. The temperature-0 agent on
# this clean corpus does not produce answers like this on demand, so the demo
# substitutes the draft and lets the real pipeline judge it under the real
# policy. Format matches the agent's mandated Result Summary shape.
UNGROUNDED_DRAFT = (
    "## Result Summary\n\n"
    '<span style="color: #2e7d32; font-weight: bold;">Internal Corpus Answer:</span> '
    "Available. Acme Corporation's cybersecurity spending rose 63% year over year "
    "while peer incidents fell 41%, and remediation costs reached $512 million "
    "[acme-shadow-report.pdf p.9].\n\n"
    '<span style="color: #1565c0; font-style: italic;">External Context:</span> '
    "Unavailable."
)

PAUSE = True


def banner(title: str) -> None:
    line = "=" * 72
    print(f"\n{line}\n {title}\n{line}\n")


def stage_note(text: str) -> None:
    print(f"[staging] {text}\n")


def pause(message: str = "-- Press Enter to continue --") -> None:
    if PAUSE:
        input(f"\n{message}")
    print()


def fmt_pct(value: float | None) -> str:
    return "N/A" if value is None else f"{value:.0%}"


_SPAN_TAG_RE = re.compile(r"</?span[^>]*>")


def strip_html(text: str) -> str:
    """Display-only: drop the UI span styling so terminal output stays clean.

    The answer itself, the queue item, and the audit record keep the original
    markup; only what this script prints is cleaned.
    """
    return _SPAN_TAG_RE.sub("", text or "")


def print_governance_report(report: dict | None) -> None:
    """Human-readable walk through one governance report, then the raw record."""
    if not report:
        print("No governance report attached to this answer.")
        return

    ctx = report.get("contextPolicy", {})
    val = report.get("validation", {})
    risk = report.get("risk", {})
    usage = report.get("sourceUsage", {})

    selected = ctx.get("selectedChunks", 0)
    dropped = ctx.get("droppedChunks", 0)
    reasons = ", ".join(ctx.get("dropReasons") or []) or "none"

    print("Governance report")
    print(f"  Audit ID:        {report.get('auditId')}")
    print(f"  Context policy:  admitted {selected} chunks, dropped {dropped} "
          f"(drop reasons: {reasons})")
    print(f"                   prompt tokens: {ctx.get('internalTokens', 0)} internal "
          f"+ {ctx.get('externalTokens', 0)} external "
          f"= {ctx.get('totalPromptTokens', 0)} total")
    print(f"  Grounding:       score {fmt_pct(val.get('groundingScore'))}, "
          f"citation coverage {fmt_pct(val.get('citationCoverage'))}, "
          f"unsupported claims: {val.get('unsupportedClaims', 0)}")
    print(f"  Guardrail:       {val.get('guardrailOutcome')}")
    print(f"  Sources:         {usage.get('internalSourcesUsed', 0)} internal, "
          f"{usage.get('externalSourcesUsed', 0)} external")
    print(f"  Risk:            {risk.get('riskLevel')} "
          f"(score {risk.get('riskScore')}, "
          f"human review required: {risk.get('humanReviewRequired')})")
    print(f"  Decision:        {report.get('decision')}")
    print("\nFull report (as nested into the JSONL audit record):\n")
    print(json.dumps(report, indent=2))


def act_1_grounded_query() -> None:
    banner("ACT 1 - A grounded answer and the governance report behind it")
    print(f"Question: {GROUNDED_QUESTION}\n")
    print("Running the agent (Bedrock + Chroma retrieval)...\n")

    from agent import query

    result = query(GROUNDED_QUESTION)
    print("Answer:\n")
    print(strip_html(result["output"]))
    print()
    print_governance_report(result.get("governance_report"))


def act_2_held_answer(auto_approve: bool) -> None:
    banner("ACT 2 - An ungrounded answer is held for human review (normal policy)")

    import config
    from agent import _finalize_query_result
    from governance import review_queue, risk_scorer
    from tools import local_search

    threshold = risk_scorer.THRESHOLDS["require_review_at_or_above"]
    floor = risk_scorer.THRESHOLDS["require_review_below_grounding"]
    stage_note(
        f"No thresholds are changed. The temperature-0 agent on this clean corpus "
        f"does not produce ungrounded answers on demand, so this act substitutes a "
        f"deliberately ungrounded draft (fabricated numbers, citation to a file "
        f"that was never retrieved) and runs it through the real production "
        f"pipeline: grounding validation -> risk scoring -> decision routing -> "
        f"review queue, under the shipped policy (weighted review threshold "
        f"{threshold}, mandatory grounding floor {floor})."
    )
    print(f"Question: {HELD_QUESTION}\n")
    print("Retrieving real evidence chunks from Chroma...\n")
    tool_output = local_search.invoke(HELD_QUESTION)
    result_messages = [
        {"type": "tool", "name": "local_search", "content": tool_output}
    ]

    print("The ungrounded draft under review:\n")
    print("  " + strip_html(UNGROUNDED_DRAFT).replace("\n", "\n  "))
    print("\nValidating the draft against the retrieved evidence...\n")
    result = _finalize_query_result(
        question=HELD_QUESTION,
        output=UNGROUNDED_DRAFT,
        result_messages=result_messages,
        trace_messages=result_messages,
        guardrail_outcome=None,
    )

    report = result.get("governance_report") or {}
    risk = report.get("risk", {})
    if config.HUMAN_REVIEW_HOLD:
        print("What the user sees (the draft answer is withheld):\n")
    else:
        print("HUMAN_REVIEW_HOLD=false (flag mode): the answer returns, but the "
              "item is still enqueued. What the user sees:\n")
    print(strip_html(result["output"]))
    print(f"\nDecision in the governance report: {report.get('decision')}")
    print(f"Risk reasons in the report:         "
          f"{', '.join(risk.get('riskReasons') or []) or 'none'}")

    audit_id = result.get("audit_id")
    item = next(
        (i for i in review_queue.list_pending(config.REVIEW_QUEUE_DIR)
         if i.get("auditId") == audit_id),
        None,
    )
    if item is None:
        print("\nNo matching review item found. Inspect with: "
              "python scripts/review_queue.py list")
        return

    draft = strip_html(item.get("draftAnswer", ""))
    preview = draft[:400] + ("..." if len(draft) > 400 else "")
    print("\nMeanwhile, the full draft is preserved in the review queue:\n")
    print(f"  Review ID: {item.get('reviewId')}")
    print(f"  Risk:      {item.get('riskLevel')} (score {item.get('riskScore')}) - "
          f"the weighted score is untouched; the hold comes from the explicit "
          f"grounding floor, not from a score adjustment")
    print(f"  Reasons:   {', '.join(item.get('riskReasons') or []) or 'none'}")
    print(f"  Sources:   {len(item.get('retrievedSources') or [])} retrieved chunks")
    print(f"  Draft (preview):\n    {preview}")

    review_id = item.get("reviewId")
    approve_cmd = [
        sys.executable, "scripts/review_queue.py", "approve",
        "--review-id", str(review_id),
        "--note", "Demo: verified against retrieved sources.",
    ]
    print("\nA reviewer resolves it with the queue CLI:")
    print(f"  python scripts/review_queue.py show --review-id {review_id}")
    print(f"  python scripts/review_queue.py approve --review-id {review_id}")

    if not auto_approve:
        pause("-- Press Enter to run the approve command now --")
    print("$ " + " ".join(approve_cmd[1:]))
    subprocess.run(approve_cmd, cwd=str(_REPO_ROOT), check=False)


def perturb_baseline(baseline: dict) -> tuple[dict, list[str]]:
    """Return a perturbed copy of the baseline plus a list of what was changed.

    The perturbed copy claims grounding was better and latency lower than the
    current numbers, so the diff reads as a regression - which is what drift
    looks like when it happens for real.
    """
    perturbed = copy.deepcopy(baseline)
    changes: list[str] = []
    summary = perturbed.setdefault("summary", {})

    rate = summary.get("grounded_answer_rate") or 0.0
    if rate < 0.95:
        new_rate, flip_to = min(1.0, rate + 0.2), True
    else:
        new_rate, flip_to = max(0.0, rate - 0.2), False
    summary["grounded_answer_rate"] = new_rate
    changes.append(f"grounded_answer_rate {rate} -> {new_rate}")

    for case in perturbed.get("results", []):
        if case.get("grounded_answer") == (not flip_to):
            case["grounded_answer"] = flip_to
            changes.append(
                f"case '{case.get('case_id')}' grounded_answer "
                f"{not flip_to} -> {flip_to}"
            )
            break

    latencies = summary.get("latency_by_workflow_type") or {}
    if latencies:
        slowest = max(latencies, key=latencies.get)
        old = latencies[slowest]
        latencies[slowest] = round(old / 2, 4)
        changes.append(f"latency[{slowest}] {old:.2f}s -> {old / 2:.2f}s")

    return perturbed, changes


def act_3_drift(live_eval: bool) -> None:
    banner("ACT 3 - eval_diff.py catches an induced drift")

    baseline_path = _REPO_ROOT / "eval" / "baseline.json"
    baseline = json.loads(baseline_path.read_text())
    perturbed, changes = perturb_baseline(baseline)

    stage_note(
        "Writing a perturbed copy of eval/baseline.json to a temp file (the real "
        "baseline is untouched). The copy claims the system used to do better:"
    )
    for change in changes:
        print(f"  - {change}")
    print()

    with tempfile.NamedTemporaryFile(
        "w", suffix=".json", prefix="baseline_perturbed_", delete=False
    ) as handle:
        handle.write(json.dumps(perturbed, indent=2))
        temp_path = Path(handle.name)

    try:
        if live_eval:
            print("Running the real eval against Bedrock and diffing it against "
                  "the perturbed baseline (takes a minute)...\n")
            cmd = [sys.executable, "scripts/eval_diff.py", "--baseline", str(temp_path)]
            print("$ " + " ".join(cmd[1:]) + "\n")
            subprocess.run(cmd, cwd=str(_REPO_ROOT), check=False)
        else:
            print("Offline mode: diffing the saved eval report against the "
                  "perturbed baseline - no Bedrock calls. (--live-eval runs the "
                  "real eval instead.) This is the same comparator and output "
                  "scripts/eval_diff.py uses on the 2-week drift check:\n")
            from scripts.eval_diff import diff_reports

            lines = diff_reports(perturbed, baseline)
            if not lines:
                print("No drift. All metrics within thresholds (5pp / 30% latency).")
            else:
                print("Drift detected vs baseline:")
                print(f"  baseline file: {temp_path.name} (perturbed copy)")
                print()
                for line in lines:
                    print(line)
    finally:
        temp_path.unlink(missing_ok=True)

    print("\nThresholds: a metric must move > 5 percentage points, and latency "
          "> 30% AND > 0.5s, before the diff fires - so sub-threshold jitter "
          "stays quiet and a real regression names the metric, the case_id that "
          "flipped, and the workflow whose latency shifted.")


def act_4_structured_comparison() -> None:
    banner(
        "ACT 4 - Filing comparison: durable worker -> governance -> review -> export"
    )
    stage_note(
        "This act uses the tracked controlled synthetic missing-section fixture "
        "(fictional Corville Freight) in a temporary registry, Chroma collection, "
        "and SQLite database. It exercises the real protected API, durable "
        "one-shot worker, governance/review path, and export gate. It uses no "
        "AWS credentials, model calls, network access, or persistent repo state; "
        "it demonstrates workflow behavior, not real-SEC-filing accuracy."
    )
    from scripts.comparison_demo_walkthrough import run_comparison_demo

    run_comparison_demo(pause_between_steps=PAUSE)


def main() -> int:
    global PAUSE

    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--no-pause", action="store_true",
                        help="Run straight through without pausing between acts.")
    parser.add_argument("--live-eval", action="store_true",
                        help="Act 3 runs the real eval (Bedrock calls, ~1 min) "
                             "instead of the offline diff.")
    args = parser.parse_args()
    PAUSE = not args.no_pause

    banner("Financial Document Intelligence Agent - governance walkthrough")
    print("Four acts: a grounded answer and governance report, an ungrounded\n"
          "answer held for human review, eval_diff.py catching induced drift,\n"
          "and the structured filing-comparison workflow through a durable\n"
          "worker, review, and export. Staged inputs are labeled [staging].")

    if not (_REPO_ROOT / "chroma_db").exists():
        print("\nwarning: chroma_db/ not found - run `python ingest.py` first, "
              "or Acts 1-2 will retrieve nothing.", file=sys.stderr)

    try:
        pause("-- Press Enter to start Act 1 --")
        act_1_grounded_query()

        pause("-- Press Enter to start Act 2 --")
        act_2_held_answer(auto_approve=args.no_pause)

        pause("-- Press Enter to start Act 3 --")
        act_3_drift(live_eval=args.live_eval)

        pause("-- Press Enter to start Act 4 --")
        act_4_structured_comparison()
    except KeyboardInterrupt:
        print("\n\nDemo stopped. If a held item remains in the queue: "
              "python scripts/review_queue.py list")
        return 1

    banner("Done")
    print("Cleanup: Act 2 appended a real item to review_queue/ (gitignored\n"
          "runtime state); Act 4 already removed all of its temporary state.\n"
          "Reset or rebuild local generic-review demo data with:\n"
          "  python scripts/review_queue.py seed --reset")
    return 0


if __name__ == "__main__":
    sys.exit(main())
