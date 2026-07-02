import { ExternalLink } from "lucide-react";
import type { RetrievedSource } from "../types";
import { sourceDocumentUrl } from "../lib/answer";

/**
 * The expanded view of a retrieved chunk: excerpt text, file path, and a
 * link that opens the source document itself.
 */
export function SourceSnippet({ source }: { source: RetrievedSource }) {
  return (
    <div className="rounded-md border border-zinc-200 bg-zinc-50 p-3 text-xs leading-5">
      <div className="mb-1.5 flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
        <a
          className="inline-flex min-w-0 items-center gap-1 font-medium text-emerald-800 hover:text-emerald-900"
          href={sourceDocumentUrl(source.source_path)}
          rel="noreferrer"
          target="_blank"
          title={`Open ${source.source_name}`}
        >
          <span className="truncate">
            Source {source.rank}: {source.source_name}
            {source.page !== null ? `, page ${source.page}` : ""}
          </span>
          <ExternalLink size={12} className="shrink-0" />
        </a>
        <span className="break-all font-mono text-[11px] text-zinc-400">
          {source.source_path}
        </span>
      </div>
      <p className="text-zinc-600">{source.excerpt}</p>
    </div>
  );
}

export default SourceSnippet;
