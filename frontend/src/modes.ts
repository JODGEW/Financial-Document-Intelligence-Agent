// Prompt scaffolds for the chat input. These are frontend-only conveniences:
// the backend always receives free text, so nothing here changes the API
// contract. (A mode-selector UI once lived here but was never wired into the
// app; only the default placeholder and the quick actions are real.)

export const DEFAULT_MODE = {
  /** Placeholder shown in the chat textarea. */
  placeholder: "Ask a question about your documents..."
};

export type QuickAction = {
  label: string;
  prompt: string;
};

// Inserted into the input when clicked; angle-bracket placeholders are meant
// to be edited before sending. The "Compare periods" scaffold asks the agent
// for a free-text, citation-backed comparison in a single answer — there is
// no structured filing-comparison backend behind it.
export const QUICK_ACTIONS: QuickAction[] = [
  {
    label: "Summarize",
    prompt:
      "Summarize <document or topic> and cite the source section for each point."
  },
  {
    label: "Extract metrics",
    prompt:
      "Extract the reported revenue, margins, and EPS for <company and period>, citing the source section for each figure."
  },
  {
    label: "Find risks",
    prompt:
      "List the key risk factors disclosed about <company or topic>, cite the source for each, and note any material changes."
  },
  {
    label: "Compare periods",
    prompt:
      "Compare <topic> across <period A> and <period B>, citing the source for each claim."
  },
  {
    label: "Cite sources only",
    prompt: "Answer using cited internal corpus sources only: <question>"
  }
];
