export type AnalysisModeId =
  | "qa"
  | "summary"
  | "risk"
  | "metrics"
  | "compare"
  | "compliance";

export type AnalysisMode = {
  id: AnalysisModeId;
  /** Short label for the mode selector. */
  label: string;
  /** Workspace title shown in the top bar. */
  title: string;
  /** One-line description shown under the title. */
  subtitle: string;
  placeholder: string;
  /**
   * Prompt scaffold inserted into the input when the mode is selected.
   * Angle-bracket placeholders are meant to be edited before sending.
   * Empty string means free-form — nothing is inserted.
   */
  template: string;
};

export const ANALYSIS_MODES: AnalysisMode[] = [
  {
    id: "qa",
    label: "Q&A",
    title: "Document Q&A",
    subtitle: "Internal evidence first. External context labeled separately.",
    placeholder:
      "Ask a question about your documents...",
    template: ""
  },
  {
    id: "summary",
    label: "Summary",
    title: "Summary",
    subtitle: "Condense a document with the sections each point comes from.",
    placeholder: "Name a document or topic to summarize with citations…",
    template:
      "Summarize <document or topic> and cite the source section for each point."
  },
  {
    id: "risk",
    label: "Risk",
    title: "Risk Review",
    subtitle: "Surface disclosed risk factors and material changes, cited.",
    placeholder: "Name a filing or topic to review for risk factors…",
    template:
      "List the key risk factors disclosed about <company or topic>, cite the source for each, and note any material changes."
  },
  {
    id: "metrics",
    label: "Metrics",
    title: "Metrics Extraction",
    subtitle: "Pull reported figures with the source section for each number.",
    placeholder: "Name the company and period for the figures you need…",
    template:
      "Extract the reported revenue, margins, and EPS for <company and period>, citing the source section for each figure."
  },
  {
    id: "compare",
    label: "Compare",
    title: "Compare",
    subtitle: "Set statements or periods side by side, with citations.",
    placeholder: "Name what to compare and across which documents or periods…",
    template:
      "Compare <topic> across <document or period A> and <document or period B>, citing the source for each claim."
  },
  {
    id: "compliance",
    label: "Compliance",
    title: "Compliance Q&A",
    subtitle: "Ask what internal policy documents say, with citations.",
    placeholder: "Ask what the compliance policy says about a topic…",
    template:
      "What does the compliance policy say about <topic>? Answer using cited internal sources only."
  }
];

export const DEFAULT_MODE = ANALYSIS_MODES[0];

export function getMode(id: AnalysisModeId): AnalysisMode {
  return ANALYSIS_MODES.find((mode) => mode.id === id) ?? DEFAULT_MODE;
}

export type QuickAction = {
  label: string;
  prompt: string;
};

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
