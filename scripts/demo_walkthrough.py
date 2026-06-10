#!/usr/bin/env python
"""Guided end-to-end demo: query -> governance report -> held answer -> drift diff.

Runs the full governance story in three acts, in one pass, against the real
pipeline (Bedrock agent, Chroma retrieval, review queue, eval baseline):

  Act 1  Ask a grounded question. Show the answer plus the governance report
         behind it: context-policy admission (chunks admitted/dropped and why),
         grounding score, citation coverage, risk score, and the routing decision.
  Act 2  Stage the hold path. The review threshold (normally 0.75) is lowered
         in-process for one query so the answer trips human review: the user
         sees a held notice instead of the draft, the draft lands in the review
         queue with its evidence, and the reviewer CLI resolves it.
  Act 3  Stage drift. The eval baseline is diffed against a perturbed copy in a
         temp file (the real eval/baseline.json is never touched), producing the
         exact scripts/eval_diff.py output an operator sees when a metric moves,
         a case flips pass/fail, or latency shifts.

Staging is explicit: the script prints what it changes before each act, and the
changes do not survive the run (the threshold patch is reverted, the perturbed
baseline is a temp file). The one persistent side effect is intentional: Act 2
writes a real item into review_queue/. Reset with: git checkout -- review_queue/

Requirements: AWS credentials for Bedrock (same as cli.py) and an ingested
corpus (python ingest.py) for Acts 1-2. Act 3 in default mode needs neither.

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
    banner("ACT 2 - A high-risk answer is held for human review")

    import config
    from agent import query
    from governance import review_queue, risk_scorer

    real_threshold = risk_scorer.THRESHOLDS["require_review_at_or_above"]
    stage_note(
        f"For this one query, the review trigger is lowered in-process from "
        f"{real_threshold} to 0.0 so you can watch the hold path fire. In normal "
        f"operation only answers scoring >= {real_threshold} are held. The patch "
        f"is reverted immediately after the query."
    )
    print(f"Question: {HELD_QUESTION}\n")
    print("Running the agent...\n")

    real_hold = config.HUMAN_REVIEW_HOLD
    risk_scorer.THRESHOLDS["require_review_at_or_above"] = 0.0
    config.HUMAN_REVIEW_HOLD = True
    try:
        result = query(HELD_QUESTION)
    finally:
        risk_scorer.THRESHOLDS["require_review_at_or_above"] = real_threshold
        config.HUMAN_REVIEW_HOLD = real_hold

    report = result.get("governance_report") or {}
    print("What the user sees (the draft answer is withheld):\n")
    print(strip_html(result["output"]))
    print(f"\nDecision in the governance report: {report.get('decision')}")

    audit_id = result.get("audit_id")
    item = next(
        (i for i in review_queue.list_pending(config.REVIEW_QUEUE_DIR)
         if i.get("auditId") == audit_id),
        None,
    )
    if item is None:
        print("\nNo matching review item found (was the answer blocked by a "
              "guardrail instead?). Inspect with: python scripts/review_queue.py list")
        return

    draft = strip_html(item.get("draftAnswer", ""))
    preview = draft[:400] + ("..." if len(draft) > 400 else "")
    print("\nMeanwhile, the full draft is preserved in the review queue:\n")
    print(f"  Review ID: {item.get('reviewId')}")
    print(f"  Risk:      {item.get('riskLevel')} (score {item.get('riskScore')})")
    print(f"  Reasons:   {', '.join(item.get('riskReasons') or []) or 'none'}")
    if (item.get("riskScore") or 0.0) < real_threshold:
        print(f"             (the score is the real signal - the scorer was never "
              f"touched. The '{item.get('riskLevel')}' label and the hold come "
              f"from the staged 0.0 threshold; in production only answers "
              f"scoring >= {real_threshold} are held, so this one would return "
              f"normally.)")
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
    print("Three acts: a grounded answer and its governance report, a high-risk\n"
          "answer held for human review, and eval_diff.py catching an induced\n"
          "drift. Staged steps are labeled [staging] and reverted after use.")

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
    except KeyboardInterrupt:
        print("\n\nDemo stopped. If a held item remains in the queue: "
              "python scripts/review_queue.py list")
        return 1

    banner("Done")
    print("Cleanup: Act 2 appended a real item to review_queue/ (tracked files).\n"
          "Reset with: git checkout -- review_queue/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
