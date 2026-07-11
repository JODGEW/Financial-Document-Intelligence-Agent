import { useEffect, useState } from "react";
import {
  CheckCircle2,
  CornerUpLeft,
  ExternalLink,
  EyeOff,
  HelpCircle,
  Loader2,
  XCircle
} from "lucide-react";
import type { ReviewDetail, SafeReviewSource } from "../types";
import {
  deliveryState,
  formatTimestamp,
  riskLine,
  riskReasonLabel
} from "../lib/reviews";
import AnswerSections from "./AnswerSections";
import GovernanceReportPanel from "./GovernanceReport";
import { StatusTag } from "./ReviewList";

type ReviewDetailPanelProps = {
  detail: ReviewDetail | null;
  state: "idle" | "loading" | "error" | "notfound" | "ready";
  error: string | null;
  /** Outcome message from the last action attempt (409, write failure). */
  actionNotice: string | null;
  isSubmitting: boolean;
  onRetry: () => void;
  onRequestAction: (action: "approve" | "reject", note: string | null) => void;
};

function Section({
  title,
  children
}: {
  title: string;
  children: React.ReactNode;
}) {
  return (
    <section className="mt-5">
      <h3 className="mb-1.5 text-xs font-semibold text-muted">{title}</h3>
      {children}
    </section>
  );
}

function DeliveryLine({ wasWithheld }: { wasWithheld: boolean | null }) {
  const { full } = deliveryState(wasWithheld);
  const Icon =
    wasWithheld === true
      ? EyeOff
      : wasWithheld === false
        ? CornerUpLeft
        : HelpCircle;
  return (
    <span className="inline-flex items-center gap-1.5 text-xs text-muted">
      <Icon size={12} />
      {full}
    </span>
  );
}

function IdRow({
  label,
  value,
  title
}: {
  label: string;
  value: string;
  title?: string;
}) {
  return (
    <div className="flex flex-wrap items-baseline gap-x-3 gap-y-0.5">
      <span className="w-24 shrink-0 text-muted">{label}</span>
      <span className="break-all font-mono text-ink" title={title}>
        {value}
      </span>
    </div>
  );
}

function SourceCard({ source }: { source: SafeReviewSource }) {
  const name = source.sourceName ?? source.sourcePath ?? "Unnamed source";
  return (
    <div className="rounded-md border-y border-r border-l-[3px] border-line border-l-grounded bg-surface p-3 text-xs leading-5">
      <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
        <span className="min-w-0 font-medium text-ink">
          {source.rank !== null && (
            <span className="font-mono">Source {source.rank}: </span>
          )}
          {source.documentUrl ? (
            <a
              className="inline-flex items-center gap-1 text-grounded hover:underline"
              href={source.documentUrl}
              rel="noreferrer"
              target="_blank"
              title={`Open ${name}`}
            >
              {name}
              <ExternalLink size={11} className="shrink-0" />
            </a>
          ) : (
            <span>{name}</span>
          )}
        </span>
        {source.sourcePath && (
          <span className="break-all font-mono text-[11px] text-faint">
            {source.sourcePath}
          </span>
        )}
      </div>
      {(source.sectionTitle || source.page !== null) && (
        <div className="mt-0.5 flex flex-wrap gap-x-3 gap-y-0.5 text-[11px] text-muted">
          {source.sectionTitle && <span>{source.sectionTitle}</span>}
          {source.page !== null && (
            <span className="font-mono tabular-nums">
              Metadata page {source.page} · 0-based
            </span>
          )}
        </div>
      )}
      {source.excerpt && <p className="mt-1.5 text-muted">{source.excerpt}</p>}
    </div>
  );
}

/**
 * Right column of the review workspace. Sections render in a fixed vertical
 * order: state line, risk reasons, question, draft, governance report,
 * retrieved context, identifiers, reviewer note, actions.
 */
export function ReviewDetailPanel({
  detail,
  state,
  error,
  actionNotice,
  isSubmitting,
  onRetry,
  onRequestAction
}: ReviewDetailPanelProps) {
  const [note, setNote] = useState("");

  // A new item means a fresh decision; never carry a draft note across items.
  useEffect(() => {
    setNote("");
  }, [detail?.reviewId]);

  if (state === "idle") {
    return (
      <p className="text-sm text-muted">
        Select a review item from the queue to see its full context.
      </p>
    );
  }

  if (state === "loading") {
    return (
      <div className="flex items-center gap-2 text-sm text-muted">
        <Loader2 size={15} className="animate-spin" />
        Loading review...
      </div>
    );
  }

  if (state === "error") {
    return (
      <div className="text-sm leading-6">
        <p className="text-blocked">The review could not be loaded: {error}</p>
        <button
          className="mt-2 rounded-md border border-line px-3 py-1 text-xs font-medium text-muted hover:bg-raised hover:text-ink focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ink/40"
          onClick={onRetry}
          type="button"
        >
          Retry
        </button>
      </div>
    );
  }

  if (state === "notfound") {
    return (
      <p className="text-sm text-muted">
        This review item was not found. The queue list has been refreshed.
      </p>
    );
  }

  if (!detail) {
    return null;
  }

  const delivery = deliveryState(detail.wasWithheld);
  const trimmedNote = note.trim();

  return (
    <div>
      {/* 1. Status, risk, delivery state */}
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <StatusTag status={detail.reviewStatus} />
        <span className="text-xs font-medium text-muted">
          {riskLine(detail.riskLevel, detail.riskScore)}
        </span>
        <DeliveryLine wasWithheld={detail.wasWithheld} />
      </div>

      {actionNotice && (
        <div className="mt-3 rounded-md border border-held/25 bg-held-bg px-3 py-2 text-xs text-held">
          {actionNotice}
        </div>
      )}

      {/* 2. Risk reasons */}
      <Section title="Risk reasons">
        {detail.riskReasons.length === 0 ? (
          <p className="text-xs text-muted">None recorded.</p>
        ) : (
          <div className="flex flex-wrap gap-1">
            {detail.riskReasons.map((reason) => (
              <span
                className="rounded-full border border-held/25 bg-held-bg px-2 py-0.5 text-[11px] font-medium text-held"
                key={reason}
              >
                {riskReasonLabel(reason)}
              </span>
            ))}
          </div>
        )}
      </Section>

      {/* 3. Original question */}
      <Section title="Question">
        <p className="rounded-md bg-raised px-3 py-2 text-sm leading-6 text-ink">
          {detail.question}
        </p>
      </Section>

      {/* 4. Read-only draft, rendered with the chat answer rules */}
      <Section title="Draft answer">
        <div className="rounded-md border border-line bg-surface p-3.5 text-sm leading-6 text-ink">
          <AnswerSections content={detail.draftAnswer} />
        </div>
      </Section>

      {/* 5. Governance report (the panel renders its own header) */}
      {detail.governanceReport ? (
        <GovernanceReportPanel report={detail.governanceReport} />
      ) : (
        <div className="mt-5 border-t border-line pt-3 text-xs text-faint">
          Governance report unavailable
        </div>
      )}

      {/* 6. Retrieved context */}
      <Section title="Retrieved context">
        {detail.retrievedSources.length === 0 ? (
          <p className="text-xs text-muted">
            No retrieved context was captured for this item.
          </p>
        ) : (
          <div className="space-y-2">
            {detail.retrievedSources.map((source, index) => (
              <SourceCard
                key={`${source.rank ?? index}-${source.sourcePath ?? index}`}
                source={source}
              />
            ))}
          </div>
        )}
      </Section>

      {/* 7. Identifiers */}
      <Section title="Identifiers">
        <div className="space-y-1 text-xs">
          <IdRow label="Review ID" value={detail.reviewId} />
          {detail.auditId && <IdRow label="Audit ID" value={detail.auditId} />}
          <IdRow
            label="Created at"
            value={formatTimestamp(detail.createdAt) ?? "Unknown"}
            title={detail.createdAt}
          />
          {detail.reviewedAt && (
            <IdRow
              label="Reviewed at"
              value={formatTimestamp(detail.reviewedAt) ?? detail.reviewedAt}
              title={detail.reviewedAt}
            />
          )}
        </div>
      </Section>

      {/* 8. Reviewer note */}
      {detail.reviewerNote && (
        <Section title="Reviewer note">
          <p className="rounded-md bg-raised px-3 py-2 text-sm leading-6 text-ink">
            {detail.reviewerNote}
          </p>
        </Section>
      )}

      {/* 9. Actions, pending items only */}
      {detail.reviewStatus === "pending" && (
        <Section title="Decision">
          <label className="block text-xs text-muted" htmlFor="reviewer-note">
            Reviewer note (optional)
          </label>
          <textarea
            className="mt-1 w-full rounded-md border border-line bg-surface px-2.5 py-1.5 text-sm leading-6 text-ink placeholder:text-faint focus:outline-none focus:ring-2 focus:ring-ink/30 disabled:cursor-not-allowed disabled:opacity-60"
            id="reviewer-note"
            value={note}
            onChange={(event) => setNote(event.target.value)}
            rows={2}
            disabled={isSubmitting}
            placeholder="Context for the audit trail"
          />
          <div className="mt-2.5 flex flex-wrap gap-2">
            <button
              className="inline-flex h-8 items-center gap-1.5 rounded-md bg-accent px-3 text-xs font-medium text-on-accent hover:opacity-90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ink/40 disabled:cursor-not-allowed disabled:opacity-60"
              onClick={() =>
                onRequestAction("approve", trimmedNote ? trimmedNote : null)
              }
              type="button"
              disabled={isSubmitting}
            >
              <CheckCircle2 size={14} />
              Approve review
            </button>
            <button
              className="inline-flex h-8 items-center gap-1.5 rounded-md border border-line px-3 text-xs font-medium text-muted hover:bg-raised hover:text-ink focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ink/40 disabled:cursor-not-allowed disabled:opacity-50"
              onClick={() =>
                onRequestAction("reject", trimmedNote ? trimmedNote : null)
              }
              type="button"
              disabled={isSubmitting}
            >
              <XCircle size={14} />
              Reject review
            </button>
          </div>
          <p className="mt-2 text-xs leading-5 text-faint">
            {delivery.caption}
          </p>
        </Section>
      )}
    </div>
  );
}

export default ReviewDetailPanel;
