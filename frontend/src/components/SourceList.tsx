import { useState } from "react";
import type { RetrievedSource } from "../types";
import SourceBadge from "./SourceBadge";
import SourceSnippet from "./SourceSnippet";

type SourceListProps = {
  sources: RetrievedSource[];
  /** Compact variant used inside the context panel. */
  dense?: boolean;
};

function sourceKey(source: RetrievedSource) {
  return `${source.rank}-${source.source}-${source.page ?? "n/a"}`;
}

/**
 * Citation chips for an answer, with per-source expandable snippets.
 * Renders nothing when there are no sources — absence is handled by the
 * caller so it can decide whether a "no source metadata" notice applies.
 */
export function SourceList({ sources, dense = false }: SourceListProps) {
  const [expandedKey, setExpandedKey] = useState<string | null>(null);

  if (sources.length === 0) {
    return null;
  }

  const expandedSource = sources.find(
    (source) => sourceKey(source) === expandedKey
  );

  return (
    <div>
      {!dense && (
        <div className="mb-2 flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wide text-muted">
          Retrieved evidence
          <span className="font-mono font-medium tabular-nums text-faint">
            ({sources.length})
          </span>
        </div>
      )}
      <div className="flex flex-wrap gap-1.5">
        {sources.map((source) => {
          const key = sourceKey(source);
          return (
            <SourceBadge
              key={key}
              source={source}
              isExpanded={expandedKey === key}
              onToggle={() =>
                setExpandedKey((current) => (current === key ? null : key))
              }
            />
          );
        })}
      </div>
      {expandedSource && (
        <div className="mt-2">
          <SourceSnippet source={expandedSource} />
        </div>
      )}
    </div>
  );
}

export default SourceList;
