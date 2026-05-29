"""Terminal interface for the document intelligence agent."""

import argparse
import sys

from agent import query


def _governance_line(report: dict | None) -> str | None:
    """Format the one-line governance summary printed after each answer."""
    if not report:
        return None
    validation = report.get("validation", {})
    risk = report.get("risk", {})

    def fmt_pct(value: float | None) -> str:
        return "N/A" if value is None else f"{value:.0%}"

    grounding = fmt_pct(validation.get("groundingScore"))
    citations = fmt_pct(validation.get("citationCoverage"))
    review = "yes" if risk.get("humanReviewRequired") else "no"
    return (
        f"Governance: risk={risk.get('riskLevel', 'n/a')} | "
        f"grounding={grounding} | citations={citations} | review={review}"
    )


def interactive():
    """Run an interactive CLI session."""
    print("Financial Document Intelligence Agent")
    print("Type 'quit' or 'exit' to stop.\n")

    chat_history = []
    while True:
        try:
            question = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye.")
            break

        if not question:
            continue
        if question.lower() in ("quit", "exit"):
            print("Bye.")
            break

        result = query(question, chat_history)
        answer = result["output"]
        print(f"\nAgent: {answer}\n")

        governance_line = _governance_line(result.get("governance_report"))
        if governance_line:
            print(f"{governance_line}\n")

        chat_history.extend([
            ("human", question),
            ("ai", answer),
        ])


def main():
    parser = argparse.ArgumentParser(description="Document Intelligence Agent CLI")
    parser.add_argument("--query", "-q", type=str, help="Single question to answer")
    args = parser.parse_args()

    if args.query:
        result = query(args.query)
        print(result["output"])
        governance_line = _governance_line(result.get("governance_report"))
        if governance_line:
            print(f"\n{governance_line}")
    else:
        interactive()


if __name__ == "__main__":
    main()
