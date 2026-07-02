type StarterPrompt = {
  /** Analysis category tag shown on the card. */
  tag: string;
  prompt: string;
};

/**
 * Starter prompts are editable scaffolds — clicking one fills the input so
 * the analyst can name the document, company, or period before sending.
 */
const STARTER_PROMPTS: StarterPrompt[] = [
  {
    tag: "Summary",
    prompt: "Summarize <filing> with cited sections."
  },
  {
    tag: "Metrics",
    prompt: "Extract revenue, margin, and EPS trends with the source for each figure."
  },
  {
    tag: "Risk",
    prompt: "Find key risk factors and material changes, citing each source."
  },
  {
    tag: "Compare",
    prompt: "Compare management commentary across periods, citing each source."
  }
];

type EmptyStateProps = {
  onUsePrompt: (prompt: string) => void;
};

export function EmptyState({ onUsePrompt }: EmptyStateProps) {
  return (
    // my-auto centers when there is spare height but stays scrollable when the
    // cards overflow small viewports (justify-center would clip the top).
    <section className="mx-auto my-auto w-full max-w-3xl py-8">
      <div className="mb-7">
        <h2 className="text-2xl font-semibold tracking-tight text-zinc-950">
          Ask questions across your financial document corpus
        </h2>
        <p className="mt-2.5 max-w-2xl text-sm leading-6 text-zinc-600">
          Query filings, policies, research notes, and extracted financial
          sections. Answers keep internal evidence and external context
          separate, with citations when source metadata is available.
        </p>
      </div>

      <div className="grid gap-2.5 sm:grid-cols-2">
        {STARTER_PROMPTS.map((starter) => (
          <button
            className="group flex min-h-[96px] flex-col justify-between gap-2 rounded-md border border-zinc-200 bg-white p-3.5 text-left shadow-sm transition-colors hover:border-zinc-300 hover:bg-zinc-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-zinc-900/40"
            key={starter.prompt}
            onClick={() => onUsePrompt(starter.prompt)}
            type="button"
          >
            <span className="text-[13px] leading-5 text-zinc-700">
              {starter.prompt}
            </span>
            <span className="text-[10px] font-semibold uppercase tracking-wide text-zinc-400 group-hover:text-zinc-700">
              {starter.tag}
            </span>
          </button>
        ))}
      </div>
      <p className="mt-3 text-xs text-zinc-400">
        Starter prompts fill the input so you can name the document, company,
        or period before sending.
      </p>
    </section>
  );
}

export default EmptyState;
