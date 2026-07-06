import { X } from "lucide-react";
import type { ChatMessage } from "../types";
import SourceList from "./SourceList";

type DocumentContextPanelProps = {
  isOpen: boolean;
  onClose: () => void;
  messages: ChatMessage[];
  isLoading: boolean;
};

/** The most recent assistant answer, if any. */
function latestAnswer(messages: ChatMessage[]): ChatMessage | null {
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    const message = messages[i];
    if (message.role === "assistant") {
      return message;
    }
  }
  return null;
}

/** Decisions where an answer legitimately carries no retrieved sources. */
const NO_SOURCE_DECISIONS = new Set(["blocked", "held_for_review"]);

/**
 * Right-hand evidence rail: the chunks retrieved for the latest answer.
 * The corpus list lives in the sidebar only. Static column on xl screens,
 * overlay drawer below that. The caller renders this panel only once the
 * current thread has an answer with sources.
 */
export function DocumentContextPanel({
  isOpen,
  onClose,
  messages,
  isLoading
}: DocumentContextPanelProps) {
  const answer = latestAnswer(messages);
  const evidence = answer ? answer.sources ?? [] : null;
  const decision = answer?.governance_report?.decision ?? null;
  const withheldByPolicy = decision !== null && NO_SOURCE_DECISIONS.has(decision);

  return (
    <>
      {/* Backdrop for the drawer below xl */}
      {isOpen && (
        <div
          className="fixed inset-0 z-30 bg-black/40 xl:hidden"
          onClick={onClose}
          aria-hidden="true"
        />
      )}

      <aside
        className={`fixed inset-y-0 right-0 z-40 flex w-[320px] shrink-0 flex-col overflow-y-auto border-l border-line bg-surface transition-transform duration-200 xl:static xl:z-auto xl:transition-none ${
          isOpen ? "translate-x-0" : "translate-x-full xl:hidden"
        }`}
        aria-label="Retrieved sources"
      >
        <div className="flex h-14 shrink-0 items-center justify-between border-b border-line px-4">
          <div className="text-sm font-semibold text-ink">
            Retrieved evidence
          </div>
          <button
            className="grid h-8 w-8 place-items-center rounded-md text-muted hover:bg-raised hover:text-ink focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ink/40 xl:hidden"
            onClick={onClose}
            type="button"
            aria-label="Close sources panel"
          >
            <X size={16} />
          </button>
        </div>

        <div className="px-4 py-4">
          {evidence === null ? (
            <p className="text-xs leading-5 text-faint">
              Ask a question to see which corpus chunks were retrieved for the
              answer.
            </p>
          ) : isLoading ? (
            <p className="text-xs leading-5 text-faint">
              Waiting for the answer to finish…
            </p>
          ) : evidence.length > 0 ? (
            <div>
              <p className="mb-2 text-xs text-faint">For the latest answer:</p>
              <SourceList sources={evidence} dense />
            </div>
          ) : withheldByPolicy ? (
            <p className="text-xs leading-5 text-faint">
              The latest answer was{" "}
              {decision === "blocked" ? "blocked" : "held for review"}; sources
              aren't shown for it.
            </p>
          ) : (
            <p className="text-xs leading-5 text-faint">
              No source metadata returned for the latest answer.
            </p>
          )}
        </div>
      </aside>
    </>
  );
}

export default DocumentContextPanel;
