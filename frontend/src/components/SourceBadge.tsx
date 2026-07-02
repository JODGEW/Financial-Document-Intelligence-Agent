import type { RetrievedSource } from "../types";

type SourceBadgeProps = {
  source: RetrievedSource;
  isExpanded: boolean;
  onToggle: () => void;
};

/**
 * A numbered citation chip. Clicking it expands the retrieved snippet
 * below the chip row (see SourceList).
 */
export function SourceBadge({ source, isExpanded, onToggle }: SourceBadgeProps) {
  return (
    <button
      className={`inline-flex max-w-full items-center gap-1.5 rounded border px-2 py-1 text-xs leading-4 transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-zinc-900/40 ${
        isExpanded
          ? "border-zinc-300 bg-zinc-100 text-zinc-900"
          : "border-zinc-200 bg-white text-zinc-600 hover:border-zinc-300 hover:text-zinc-800"
      }`}
      onClick={onToggle}
      type="button"
      aria-expanded={isExpanded}
      title={source.source}
    >
      <span className="font-semibold tabular-nums text-emerald-800">
        {source.rank}
      </span>
      <span className="min-w-0 truncate font-medium">{source.source_name}</span>
      {source.page !== null && (
        <span className="shrink-0 tabular-nums text-zinc-400">
          p.{source.page}
        </span>
      )}
    </button>
  );
}

export default SourceBadge;
