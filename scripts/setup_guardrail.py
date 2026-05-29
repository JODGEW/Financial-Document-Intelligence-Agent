"""Provision the Bedrock Guardrail defined in policies/guardrails-policy.md.

Idempotent: looks up an existing guardrail by name and updates it; otherwise
creates a new one. Prints the active guardrail id and version on success so
the operator can copy them into the deployment environment.

Usage:
    python scripts/setup_guardrail.py
    python scripts/setup_guardrail.py --bump-version
    python scripts/setup_guardrail.py --name react-rag-guardrail-staging

Requires:
    - boto3 in the active environment
    - AWS credentials with `bedrock:CreateGuardrail`, `bedrock:UpdateGuardrail`,
      `bedrock:CreateGuardrailVersion`, and `bedrock:ListGuardrails`.

Side effects:
    Creates or updates a billable AWS Bedrock guardrail in the target region.
    This script is intentionally not run automatically; an operator runs it
    once per environment after reviewing the policy.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow `python scripts/setup_guardrail.py` from the project root: ensure the
# repo root (which holds config.py) is importable.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import boto3

import config


GUARDRAIL_NAME_DEFAULT = "react-rag-guardrail"

DENIED_TOPICS = [
    {
        "name": "personalized_investment_advice",
        "definition": (
            "Personalized buy/sell/hold recommendations or future-price "
            "predictions directed at the user. Excludes factual lookup of "
            "disclosed filings or historical figures."
        ),
        "examples": [
            "Should I buy NVDA next week?",
            "What stocks should I put my 401k into?",
            "Is now a good time to load up on tech stocks?",
            "Will the stock price rise after earnings?",
        ],
        "type": "DENY",
    },
    {
        "name": "legal_opinion",
        "definition": (
            "Providing a legal opinion on the adequacy, compliance, or "
            "liability exposure of a specific filing or disclosure."
        ),
        "examples": [
            "Does Acme's risk-factor section satisfy SEC Reg S-K?",
            "Is this disclosure legally sufficient?",
        ],
        "type": "DENY",
    },
    # Note: an `mnpi_request` topic was attempted here but removed.
    # Bedrock's topic classifier keyed strongly on tokens like
    # "earnings per share" / "filings" regardless of the definition's
    # "filed/disclosed reports are NOT in scope" carve-out, causing false
    # positives on legitimate 10-K questions. This corpus contains only
    # public-style content (sample 10-K excerpts, a compliance policy),
    # so MNPI risk is essentially zero in practice. Re-introduce only if
    # the corpus starts holding drafts or pre-release material, and at
    # that point use input-side classification rather than topic policy.
]

PII_ENTITIES = [
    {"type": "EMAIL", "action": "ANONYMIZE"},
    {"type": "PHONE", "action": "ANONYMIZE"},
    {"type": "US_SOCIAL_SECURITY_NUMBER", "action": "ANONYMIZE"},
    {"type": "CREDIT_DEBIT_CARD_NUMBER", "action": "ANONYMIZE"},
    {"type": "ADDRESS", "action": "ANONYMIZE"},
]

CONTEXTUAL_GROUNDING_FILTERS = [
    {"type": "GROUNDING", "threshold": 0.65},
    {"type": "RELEVANCE", "threshold": 0.50},
]

BLOCKED_INPUT_MESSAGE = (
    "This request was blocked by the ReAct-RAG safety policy. "
    "See policies/guardrails-policy.md for the full rule set."
)
BLOCKED_OUTPUT_MESSAGE = (
    "The model's response was blocked by the ReAct-RAG safety policy. "
    "See policies/guardrails-policy.md for the full rule set."
)


def _client():
    return boto3.client("bedrock", region_name=config.AWS_REGION)


def _find_guardrail(client, name: str) -> dict | None:
    paginator = client.get_paginator("list_guardrails")
    for page in paginator.paginate():
        for entry in page.get("guardrails", []):
            if entry.get("name") == name:
                return entry
    return None


def _common_kwargs(name: str) -> dict:
    return {
        "name": name,
        "description": "ReAct-RAG safety policy. See policies/guardrails-policy.md.",
        "topicPolicyConfig": {"topicsConfig": DENIED_TOPICS},
        "sensitiveInformationPolicyConfig": {"piiEntitiesConfig": PII_ENTITIES},
        "contextualGroundingPolicyConfig": {"filtersConfig": CONTEXTUAL_GROUNDING_FILTERS},
        "blockedInputMessaging": BLOCKED_INPUT_MESSAGE,
        "blockedOutputsMessaging": BLOCKED_OUTPUT_MESSAGE,
    }


def create_or_update(name: str, bump_version: bool) -> tuple[str, str]:
    """Create or update the guardrail; return (guardrail_id, version)."""
    client = _client()
    existing = _find_guardrail(client, name)

    if existing is None:
        response = client.create_guardrail(**_common_kwargs(name))
        guardrail_id = response["guardrailId"]
        version = response.get("version", "DRAFT")
        print(f"Created guardrail '{name}' id={guardrail_id} version={version}")
        return guardrail_id, version

    guardrail_id = existing["id"]
    client.update_guardrail(guardrailIdentifier=guardrail_id, **_common_kwargs(name))
    print(f"Updated guardrail '{name}' id={guardrail_id} (DRAFT)")

    if bump_version:
        version_response = client.create_guardrail_version(
            guardrailIdentifier=guardrail_id,
            description="Published from setup_guardrail.py --bump-version",
        )
        version = version_response["version"]
        print(f"Published new version: {version}")
    else:
        version = "DRAFT"

    return guardrail_id, version


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", default=GUARDRAIL_NAME_DEFAULT, help="Guardrail name")
    parser.add_argument(
        "--bump-version",
        action="store_true",
        help="Publish a new immutable version after the update.",
    )
    args = parser.parse_args()

    try:
        guardrail_id, version = create_or_update(args.name, args.bump_version)
    except Exception as exc:  # noqa: BLE001  - operator-facing CLI; surface anything
        print(f"ERROR provisioning guardrail: {exc}", file=sys.stderr)
        return 1

    print()
    print("Set these in your environment to enable enforcement:")
    print(f"  export BEDROCK_GUARDRAIL_ID={guardrail_id}")
    print(f"  export BEDROCK_GUARDRAIL_VERSION={version}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
