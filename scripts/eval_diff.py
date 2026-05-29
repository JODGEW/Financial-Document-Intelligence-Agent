"""Run the eval and compare against a saved baseline; report drift.

First run (seed the baseline):
    python scripts/eval_diff.py --update-baseline

Subsequent runs (compare):
    python scripts/eval_diff.py

Reports metric moves > 5 percentage points, per-case pass/fail flips, and
latency changes > 30%. Useful for catching drift in the Bedrock guardrail
classifier or the agent's grounding behavior over time.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BASELINE = _REPO_ROOT / "eval" / "baseline.json"

METRIC_THRESHOLD = 0.05      # 5 percentage points
LATENCY_THRESHOLD = 0.30     # 30%
LATENCY_ABS_FLOOR = 0.5      # seconds; AND-gated with the % threshold so
                             # sub-second jitter on fast workflows (e.g.
                             # guardrail_block ~0.7s) can't trip a false alert

PCT_METRICS = (
    "retrieval_hit_rate",
    "grounded_answer_rate",
    "unsupported_claim_rate",
    "citation_accuracy",
    "tool_routing_accuracy",
    "local_refusal_accuracy",
    "guardrail_block_accuracy",
)


def run_eval() -> dict:
    """Invoke the eval runner and return its JSON output."""
    completed = subprocess.run(
        [sys.executable, str(_REPO_ROOT / "eval_runner.py"), "--json"],
        cwd=str(_REPO_ROOT),
        check=True,
        capture_output=True,
        text=True,
    )
    # Strip any leading non-JSON (e.g. UserWarning lines from agent.py)
    output = completed.stdout
    brace = output.find("{")
    if brace > 0:
        output = output[brace:]
    return json.loads(output)


def _diff_metric(name: str, old: float | None, new: float | None) -> str | None:
    if old is None and new is None:
        return None
    if old is None or new is None:
        return f"  {name}: {old} → {new} (one side missing)"
    if abs(new - old) > METRIC_THRESHOLD:
        return f"  {name}: {old:.2%} → {new:.2%} (Δ {new - old:+.2%})"
    return None


def _diff_latency(label: str, old: float | None, new: float | None) -> str | None:
    if old is None or new is None:
        return None
    if old <= 0:
        return None
    rel = (new - old) / old
    if abs(rel) > LATENCY_THRESHOLD and abs(new - old) > LATENCY_ABS_FLOOR:
        return f"  {label}: {old:.2f}s → {new:.2f}s ({rel:+.0%})"
    return None


def diff_reports(baseline: dict, current: dict) -> list[str]:
    lines: list[str] = []

    base_summary = baseline.get("summary", {})
    cur_summary = current.get("summary", {})

    metric_lines = [
        _diff_metric(name, base_summary.get(name), cur_summary.get(name))
        for name in PCT_METRICS
    ]
    metric_lines = [m for m in metric_lines if m]
    if metric_lines:
        lines.append("Metrics moved > 5pp:")
        lines.extend(metric_lines)

    base_lat = base_summary.get("latency_by_workflow_type", {}) or {}
    cur_lat = cur_summary.get("latency_by_workflow_type", {}) or {}
    latency_lines = [
        _diff_latency(workflow, base_lat.get(workflow), cur_lat.get(workflow))
        for workflow in sorted(set(base_lat) | set(cur_lat))
    ]
    latency_lines = [l for l in latency_lines if l]
    if latency_lines:
        lines.append("Latency changed > 30%:")
        lines.extend(latency_lines)

    base_results = {r["case_id"]: r for r in baseline.get("results", [])}
    cur_results = {r["case_id"]: r for r in current.get("results", [])}
    flips: list[str] = []
    for case_id in sorted(set(base_results) | set(cur_results)):
        b = base_results.get(case_id)
        c = cur_results.get(case_id)
        if b is None:
            flips.append(f"  {case_id}: NEW (grounded={c.get('grounded_answer')})")
            continue
        if c is None:
            flips.append(f"  {case_id}: REMOVED (was grounded={b.get('grounded_answer')})")
            continue
        if b.get("grounded_answer") != c.get("grounded_answer"):
            flips.append(
                f"  {case_id}: grounded {b.get('grounded_answer')} → {c.get('grounded_answer')}"
            )
    if flips:
        lines.append("Case pass/fail flips:")
        lines.extend(flips)

    return lines


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--baseline",
        default=str(DEFAULT_BASELINE),
        help=f"Path to baseline JSON (default: {DEFAULT_BASELINE.relative_to(_REPO_ROOT)})",
    )
    parser.add_argument(
        "--update-baseline",
        action="store_true",
        help="Run the eval and overwrite the baseline. Use to seed or refresh.",
    )
    args = parser.parse_args()

    baseline_path = Path(args.baseline)
    current = run_eval()

    if args.update_baseline:
        baseline_path.parent.mkdir(parents=True, exist_ok=True)
        baseline_path.write_text(json.dumps(current, indent=2))
        print(f"Baseline written: {baseline_path.relative_to(_REPO_ROOT)}")
        return 0

    if not baseline_path.exists():
        print(
            f"No baseline at {baseline_path}. Seed it with:\n"
            f"  python scripts/eval_diff.py --update-baseline",
            file=sys.stderr,
        )
        return 1

    baseline = json.loads(baseline_path.read_text())
    drift_lines = diff_reports(baseline, current)

    if not drift_lines:
        print("No drift. All metrics within thresholds (5pp / 30% latency).")
        return 0

    print("Drift detected vs baseline:")
    print(f"  baseline file: {baseline_path.relative_to(_REPO_ROOT)}")
    print()
    for line in drift_lines:
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
