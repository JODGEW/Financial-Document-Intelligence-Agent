import type { ChatMessage } from "../types";
import AnswerSections from "./AnswerSections";
import GovernanceReportPanel from "./GovernanceReport";
import LoadingIndicator from "./LoadingIndicator";
import SourceList from "./SourceList";

type ChatMessageViewProps = {
  message: ChatMessage;
  /** True while this message is the one currently streaming. */
  isStreaming: boolean;
  streamingStatus: string | null;
};

/** Decisions where an answer legitimately carries no retrieved sources. */
const NO_SOURCE_DECISIONS = new Set(["blocked", "held_for_review"]);

export function ChatMessageView({
  message,
  isStreaming,
  streamingStatus
}: ChatMessageViewProps) {
  if (message.role === "user") {
    return (
      <article className="flex justify-end">
        <div className="min-w-0 max-w-[85%] rounded-lg bg-raised px-4 py-2.5 text-sm leading-6 text-ink sm:max-w-[75%]">
          <p className="whitespace-pre-wrap break-words">{message.content}</p>
        </div>
      </article>
    );
  }

  const decision = message.governance_report?.decision ?? null;
  const showNoSourceNotice =
    !isStreaming &&
    message.content.length > 0 &&
    (message.sources?.length ?? 0) === 0 &&
    (decision === null || !NO_SOURCE_DECISIONS.has(decision));

  return (
    <article className="flex justify-start">
      <div className="w-full min-w-0 text-sm leading-6 text-ink">
        {message.content ? (
          <AnswerSections content={message.content} />
        ) : isStreaming ? (
          <LoadingIndicator status={streamingStatus} />
        ) : (
          <div className="text-sm text-muted">
            No answer text was returned for this query.
          </div>
        )}

        {message.sources && message.sources.length > 0 && (
          <div className="mt-4 border-t border-line pt-3">
            <SourceList sources={message.sources} />
          </div>
        )}

        {showNoSourceNotice && (
          <div className="mt-4 border-t border-line pt-3 text-xs text-faint">
            No source metadata returned for this answer.
          </div>
        )}

        {message.governance_report && (
          <GovernanceReportPanel report={message.governance_report} />
        )}

        {message.audit_id && (
          <div className="mt-2 break-all font-mono text-[11px] text-faint">
            Audit ID: {message.audit_id}
          </div>
        )}
      </div>
    </article>
  );
}

export default ChatMessageView;
