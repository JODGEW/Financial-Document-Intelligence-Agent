import { FolderOpen } from "lucide-react";
import type { CorpusStatus } from "../types";

/**
 * Honest corpus states for the sidebar and context panel. Upload is not part
 * of the backend, so there is no upload CTA — the copy points at the real
 * ways this app gets documents (docs/ + ingestion, backend availability).
 */
export function EmptyCorpusState({ status }: { status: CorpusStatus }) {
  if (status === "loading") {
    return (
      <div className="px-2 py-1.5 text-xs text-faint">Loading corpus…</div>
    );
  }

  if (status === "error") {
    return (
      <div className="mx-1 rounded-md border border-dashed border-line-strong px-3 py-3 text-xs leading-5 text-muted">
        <div className="mb-1 flex items-center gap-1.5 font-medium text-ink">
          <FolderOpen size={14} />
          Document list unavailable
        </div>
        Couldn't reach the corpus API. Check that the backend is running, then
        reload.
      </div>
    );
  }

  return (
    <div className="mx-1 rounded-md border border-dashed border-line-strong px-3 py-3 text-xs leading-5 text-muted">
      <div className="mb-1 flex items-center gap-1.5 font-medium text-ink">
        <FolderOpen size={14} />
        No corpus loaded
      </div>
      Add documents to the corpus directory and run ingestion to make them
      searchable here.
    </div>
  );
}

export default EmptyCorpusState;
