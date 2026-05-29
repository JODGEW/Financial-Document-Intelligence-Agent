"""Gradio UI for the document intelligence agent."""

import gradio as gr

from agent import query


MAX_HISTORY_TURNS = 4


def respond(message: str, history: list[dict]) -> str:
    """Handle a chat message and return the agent response."""
    # Keep only the last N turns to prevent stale answers from influencing
    # the model's output format. Each turn = one user + one assistant message.
    recent = history[-(MAX_HISTORY_TURNS * 2):]

    chat_history = []
    for msg in recent:
        chat_history.append((msg["role"], msg["content"]))

    result = query(message, chat_history)
    return result["output"]


def build_app() -> gr.Blocks:
    """Build the Gradio interface."""
    with gr.Blocks(
        title="Financial Document Intelligence Agent",
        css="div.chat-container { min-height: 70vh; }",
        fill_height=True,
    ) as app:
        gr.Markdown("# Financial Document Intelligence Agent")
        gr.Markdown(
            "Ask questions about internal financial documents, compliance policies, "
            "and filings. The agent searches local documents first and can fall back "
            "to web search for supplemental context."
        )

        gr.ChatInterface(
            fn=respond,
            examples=[
                "Summarize Acme Corp's cybersecurity risk disclosures and cite the source document.",
                "What does the compliance policy say about blackout periods for personal trading?",
                "What were Acme Corp's fiscal year 2025 revenue and earnings per share?",
                "What does the internal research note say about cybersecurity disclosure trends?",
            ],
        )

    return app


if __name__ == "__main__":
    app = build_app()
    app.launch()
