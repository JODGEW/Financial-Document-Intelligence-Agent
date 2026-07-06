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
      className={`inline-flex max-w-full items-center gap-1.5 rounded border px-2 py-1 text-xs leading-4 transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ink/40 ${
        isExpanded
          ? "border-line-strong bg-raised text-ink"
          : "border-line bg-surface text-muted hover:border-line-strong hover:text-ink"
      }`}
      onClick={onToggle}
      type="button"
      aria-expanded={isExpanded}
      title={source.source}
    >
      <span className="font-mono font-semibold tabular-nums text-grounded">
        {source.rank}
      </span>
      <span className="min-w-0 truncate font-medium">{source.source_name}</span>
      {source.page !== null && (
        <span className="shrink-0 font-mono tabular-nums text-faint">
          p.{source.page}
        </span>
      )}
    </button>
  );
}

export default SourceBadge;
