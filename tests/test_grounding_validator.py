"""Tests for the runtime grounding validator.

The core contract: the runtime validator must agree with eval_runner's rule-based
grounding on identical inputs. These tests pin the two implementations together so
the copied primitives in governance/grounding_validator.py cannot drift from
eval_runner.py without failing the suite.
"""

import eval_runner
from governance.grounding_validator import validate

# A chunk longer than the audit excerpt cap: the closing figure sits past
# position 700, so it is invisible to excerpt-only scoring but present in the
# full text that rides the `content` key (as attached at scoring time).
_PAST_CAP_TEXT = (
    "Management discusses liquidity and capital resources at length in this "
    "section, covering cash flow from operations, working capital movements, "
    "and contractual obligations for the coming periods. " * 4
    + "Net revenue retention was 118% for fiscal 2025, up 4 points."
)

# Each chunk mirrors the parsed source dict the agent passes at runtime.
_CHUNKS = [
    {
        "source_name": "compliance-policy-personal-trading.md",
        "source": "docs/compliance-policy-personal-trading.md",
        "page": None,
        "excerpt": "Employees may not trade during the 30 day blackout window.",
    },
    {
        "source_name": "acme-corp-10k-excerpt-2025.pdf",
        "source": "docs/acme-corp-10k-excerpt-2025.pdf",
        "page": 2,
        "excerpt": "Total revenue was $5 million for fiscal year 2025.",
    },
    {
        "source_name": "acme-corp-10k-excerpt-2025.pdf",
        "source": "docs/acme-corp-10k-excerpt-2025.pdf",
        "page": 4,
        "excerpt": _PAST_CAP_TEXT[:700],
        "content": _PAST_CAP_TEXT,
    },
]

# A spread of answers: fully grounded, miscited, and an unsupported numeric claim.
# The p.2 / p.9 entries exercise the prompt's "[file.pdf p.N]" bracket citation
# form, which both parsers must resolve at page level (p.2 matches the chunk,
# p.9 must not).
_ANSWERS = [
    "Employees may not trade during the 30 day blackout window "
    "[compliance-policy-personal-trading.md].",
    "Total revenue was $5 million [acme-corp-10k-excerpt-2025.pdf p.2].",
    "Total revenue was $5 million [acme-corp-10k-excerpt-2025.pdf p.9].",
    "Revenue grew 42% last year [ghost-report.pdf].",
    "The policy covers preclearance and reporting [compliance-policy-personal-trading.md].",
    "Net revenue retention was 118% [acme-corp-10k-excerpt-2025.pdf p.4].",
]


def test_citation_coverage_matches_eval_runner():
    """citation_coverage must equal eval_runner.citation_accuracy on each answer."""
    for answer in _ANSWERS:
        result = validate(answer, _CHUNKS)
        expected = round(eval_runner.citation_accuracy(answer, _CHUNKS), 4)
        assert result["citation_coverage"] == expected, answer


def test_unsupported_claim_rate_matches_eval_runner():
    """The validator's unsupported share must equal eval_runner's claim rate."""
    evidence = eval_runner.evidence_text(_CHUNKS)
    for answer in _ANSWERS:
        result = validate(answer, _CHUNKS)
        claims = eval_runner.extract_claim_tokens(answer)
        runtime_rate = (
            result["unsupported_claim_count"] / len(claims) if claims else 0.0
        )
        expected_rate = eval_runner.unsupported_claim_rate(answer, evidence)
        assert runtime_rate == expected_rate, answer


def test_fully_grounded_answer_scores_one():
    """A cited, fully supported answer scores 1.0 with no unsupported claims."""
    answer = (
        "Employees may not trade during the 30 day blackout window "
        "[compliance-policy-personal-trading.md]."
    )
    result = validate(answer, _CHUNKS)
    assert result["citation_coverage"] == 1.0
    assert result["grounding_score"] == 1.0
    assert result["unsupported_claims"] == []
    assert result["unsupported_claim_count"] == 0


def test_claim_past_excerpt_cap_needs_full_content():
    """A number past the excerpt cap is supported only through full chunk text.

    Pins the excerpt-cap fix in both directions: an excerpt-only chunk still
    flags the claim, and the same chunk carrying `content` clears it, with
    both twins agreeing on each input.
    """
    answer = "Net revenue retention was 118% [acme-corp-10k-excerpt-2025.pdf p.4]."
    past_cap_chunk = _CHUNKS[2]
    assert "118" not in past_cap_chunk["excerpt"]

    excerpt_only = {k: v for k, v in past_cap_chunk.items() if k != "content"}
    capped = validate(answer, [excerpt_only])
    assert "118" in capped["unsupported_claims"]
    assert (
        eval_runner.unsupported_claim_rate(
            answer, eval_runner.evidence_text([excerpt_only])
        )
        > 0.0
    )

    full = validate(answer, [past_cap_chunk])
    assert full["unsupported_claims"] == []
    assert full["citation_coverage"] == 1.0
    assert full["grounding_score"] == 1.0
    assert (
        eval_runner.unsupported_claim_rate(
            answer, eval_runner.evidence_text([past_cap_chunk])
        )
        == 0.0
    )


def test_ungrounded_answer_flags_unsupported_claim_and_low_score():
    """A miscited answer with an unsupported number scores low and lists the claim."""
    answer = "Revenue grew 42% last year [ghost-report.pdf]."
    result = validate(answer, _CHUNKS)
    assert result["citation_coverage"] == 0.0
    # eval_runner's claim regex extracts the bare number "42" (the trailing "%"
    # falls outside its word boundary); the runtime validator mirrors that.
    assert "42" in result["unsupported_claims"]
    assert result["unsupported_claim_count"] == 1
    assert result["grounding_score"] < 0.5
