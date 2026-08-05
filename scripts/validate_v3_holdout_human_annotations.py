"""Validate the v3 holdout review workspace and human-completed annotations.

    python scripts/validate_v3_holdout_human_annotations.py --workspace
    python scripts/validate_v3_holdout_human_annotations.py --pair <pair_id>
    python scripts/validate_v3_holdout_human_annotations.py
    python scripts/validate_v3_holdout_human_annotations.py --json

Offline and credential-free: it reads only the committed reports under
``benchmarks/real_filing_v3_holdout_v1/`` and the gitignored corpus under
``benchmark_data/real_filing_v3_holdout_v1/``. It never contacts the network,
never opens Chroma, never runs the parser or detector, and never imports or
invokes the gold evaluator.

Three modes over the same frozen-identity checks:

- ``--workspace``: pre-review integrity. Every packet, machine proposal,
  build record, section text, source body, detection result, review record,
  and the queue must match the committed inventory and the frozen manifest
  chain. Human annotations are reported but not required.
- ``--pair <pair_id>``: everything ``--workspace`` checks PLUS full admission
  for one named packet.
- default: everything ``--workspace`` checks PLUS full admission for every
  review-ready pair — the gate a future gold evaluation must pass first.

Admission requires, per pair: an explicit reviewer decision on every canonical
``side:sequence:unit_key`` subject, an explicit reviewer completion marker, and
a human annotation that is explicitly ``human_verified`` with a non-placeholder
annotator id, an explicit-UTC verification timestamp postdating packet
generation, canonical label ids, exactly-once closure over the build's
canonical unit inventory, evidence sides consistent with the sides each label
binds, a reason code on every undetermined label, and bounded notes carrying
no filing excerpt, no absolute path, and no credential material.

This command is strictly read-only. It never edits an annotation, never sets a
status, never selects or accepts a label, never copies a machine proposal into
a human decision field, never computes an accuracy metric, and never measures
machine/human agreement. Gold evaluation is a separate later phase; this
validator only decides whether the human-completed files are fit to enter it.
**Validator acceptance is necessary but does not establish that the labels are
correct**, and it creates no accuracy or generalization claim.

Output is deterministic and bounded: stable check ids, stable failure codes,
fixed ordering (frozen-identity checks, then the queue, then pairs in committed
inventory order, then directory closure), no filing text, no absolute paths,
and no raw exception text.

Exit codes: 0 all checks pass, 1 one or more checks fail, 2 the committed
reports or manifest are unreadable, internally inconsistent, the named pair is
unknown or blocked, or the validator itself failed.
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Validate the v3 holdout human-review workspace (--workspace), one "
            "completed packet (--pair), or the whole corpus (default). "
            "Offline; never edits, never decides, never scores."
        )
    )
    parser.add_argument(
        "--workspace",
        action="store_true",
        help="pre-review integrity only; human annotations not required",
    )
    parser.add_argument(
        "--pair",
        metavar="PAIR_ID",
        help="require full admission for exactly this pair",
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
    args = parser.parse_args(argv)

    if args.workspace and args.pair:
        parser.error("--workspace and --pair are mutually exclusive")

    manifest_path = Path(args.manifest or rfv3.default_v3_holdout_manifest_path())
    layout = rfb.CorpusLayout(args.corpus_dir or config.REAL_FILING_V3_HOLDOUT_DIR)
    report_dir = Path(args.report_dir or manifest_path.parent)

    if args.workspace:
        mode = hr.MODE_WORKSPACE
    elif args.pair:
        mode = hr.MODE_PAIR
    else:
        mode = hr.MODE_CORPUS

    try:
        findings = hr.run_validation(
            layout=layout,
            manifest_path=manifest_path,
            report_dir=report_dir,
            mode=mode,
            pair_id=args.pair,
        )
    except rfb.BenchmarkError as exc:
        print(f"Validation refused [{exc.code}]: {exc.message}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 — never leak raw exception text
        print(
            "Validation failed [v3_review_validator_internal_error]: "
            f"{type(exc).__name__}",
            file=sys.stderr,
        )
        return 2

    failed = findings.failed
    if args.json:
        print(
            json.dumps(
                {
                    "mode": mode,
                    "pair_id": args.pair,
                    "checks": findings.rows,
                    "failed_count": len(failed),
                    "failed_codes": findings.failed_codes,
                    "passed": not failed,
                    "gold_metrics_computed": False,
                    "human_verified_admitted": sum(
                        1
                        for row in findings.rows
                        if row["check"] == "human_annotation_admitted" and row["ok"]
                    ),
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 1 if failed else 0

    titles = {
        hr.MODE_WORKSPACE: "workspace integrity",
        hr.MODE_PAIR: f"admission for {args.pair}",
        hr.MODE_CORPUS: "corpus admission readiness",
    }
    print(f"v3 holdout human-annotation validation — {titles[mode]}")
    for row in findings.rows:
        marker = "ok  " if row["ok"] else "FAIL"
        scope = row["pair_id"] or "-"
        print(f"  [{marker}] {scope:<16} {row['check']}: {row['detail']}")
    print(
        f"\n  {len(findings.rows) - len(failed)}/{len(findings.rows)} checks "
        f"passed; {len(failed)} failed."
    )
    if failed:
        print(
            "  This command changes nothing. Fix the files above and rerun; "
            "only explicitly human_verified annotations can ever enter a gold "
            "metric."
        )
    else:
        print(
            "  No metric was computed and no accuracy or generalization claim "
            "exists. Passing this gate proves identity, closure, and hygiene — "
            "not that a reviewer's judgement is correct."
        )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
