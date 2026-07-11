import {
  CheckCircle2,
  Clock,
  CornerUpLeft,
  EyeOff,
  HelpCircle,
  Loader2,
  XCircle
} from "lucide-react";
import type { ReviewStatus, ReviewStatusFilter, ReviewSummary } from "../types";
import {
  REVIEW_FILTERS,
  REVIEW_STATUS_LABELS,
  deliveryState,
  formatTimestamp,
  riskLine,
  riskReasonLabel
} from "../lib/reviews";

type ReviewListProps = {
  filter: ReviewStatusFilter;
  onFilterChange: (filter: ReviewStatusFilter) => void;
  items: ReviewSummary[];
  state: "loading" | "error" | "ready";
  error: string | null;
  selectedId: string | null;
  onSelect: (reviewId: string) => void;
  onRetry: () => void;
};

const EMPTY_COPY: Record<ReviewStatusFilter, string> = {
  pending: "No pending reviews.",
  approved: "No approved reviews.",
  rejected: "No rejected reviews.",
  all: "No review items."
};

/** Status is always icon + text, never color alone. */
export function StatusTag({ status }: { status: ReviewStatus }) {
  const label = REVIEW_STATUS_LABELS[status];
  if (status === "approved") {
    return (
      <span className="inline-flex items-center gap-1 text-[11px] font-semibold text-grounded">
        <CheckCircle2 size={12} />
        {label}
      </span>
    );
  }
  if (status === "rejected") {
    return (
      <span className="inline-flex items-center gap-1 text-[11px] font-semibold text-blocked">
        <XCircle size={12} />
        {label}
      </span>
    );
  }
  return (
    <span className="inline-flex items-center gap-1 text-[11px] font-semibold text-held">
      <Clock size={12} />
      {label}
    </span>
  );
}

/** Delivery snapshot for a list row: icon plus short text from wasWithheld. */
function DeliveryTag({ wasWithheld }: { wasWithheld: boolean | null }) {
  const { short } = deliveryState(wasWithheld);
  const Icon =
    wasWithheld === true
      ? EyeOff
      : wasWithheld === false
        ? CornerUpLeft
        : HelpCircle;
  return (
    <span className="inline-flex items-center gap-1 text-[11px] text-muted">
      <Icon size={11} />
      {short}
    </span>
  );
}

function reasonsLine(reasons: string[]): string | null {
  if (reasons.length === 0) {
    return null;
  }
  const shown = reasons.slice(0, 2).map(riskReasonLabel).join(" · ");
  const extra = reasons.length - 2;
  return extra > 0 ? `${shown} · +${extra} more` : shown;
}

/**
 * Left column of the review workspace: status filters plus the queue rows.
 * The server orders each status list; rows render in response order.
 */
export function ReviewList({
  filter,
  onFilterChange,
  items,
  state,
  error,
  selectedId,
  onSelect,
  onRetry
}: ReviewListProps) {
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="shrink-0 border-b border-line px-3 pb-2.5 pt-3">
        <div
          className="flex flex-wrap gap-1"
          role="group"
          aria-label="Filter reviews by status"
        >
          {REVIEW_FILTERS.map(({ id, label }) => {
            const isActive = filter === id;
            return (
              <button
                className={`rounded-md border px-2.5 py-1 text-xs font-medium focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ink/40 ${
                  isActive
                    ? "border-line-strong bg-raised text-ink"
                    : "border-line text-muted hover:bg-raised hover:text-ink"
                }`}
                key={id}
                onClick={() => onFilterChange(id)}
                type="button"
                aria-pressed={isActive}
              >
                {label}
              </button>
            );
          })}
        </div>
        {state === "ready" && (
          <p className="mt-2 text-[11px] text-faint">
            {items.length} {items.length === 1 ? "item" : "items"}
          </p>
        )}
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto p-2">
        {state === "loading" ? (
          <div className="flex items-center gap-2 px-2 py-3 text-xs text-muted">
            <Loader2 size={14} className="animate-spin" />
            Loading reviews...
          </div>
        ) : state === "error" ? (
          <div className="px-2 py-3 text-xs leading-5">
            <p className="text-blocked">
              The review list could not be loaded: {error}
            </p>
            <button
              className="mt-2 rounded-md border border-line px-2.5 py-1 font-medium text-muted hover:bg-raised hover:text-ink focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ink/40"
              onClick={onRetry}
              type="button"
            >
              Retry
            </button>
          </div>
        ) : items.length === 0 ? (
          <p className="px-2 py-3 text-xs text-muted">{EMPTY_COPY[filter]}</p>
        ) : (
          <div className="space-y-1">
            {items.map((item) => {
              const isSelected = item.reviewId === selectedId;
              const reasons = reasonsLine(item.riskReasons);
              return (
                <button
                  className={`w-full rounded-md border px-2.5 py-2 text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ink/40 ${
                    isSelected
                      ? "border-line-strong bg-raised"
                      : "border-transparent hover:bg-raised"
                  }`}
                  key={item.reviewId}
                  onClick={() => onSelect(item.reviewId)}
                  type="button"
                  aria-current={isSelected ? "true" : undefined}
                >
                  <div className="flex items-baseline justify-between gap-2">
                    <StatusTag status={item.reviewStatus} />
                    <span className="shrink-0 font-mono text-[10px] tabular-nums text-faint">
                      {formatTimestamp(item.createdAt)}
                    </span>
                  </div>
                  <p className="mt-1 line-clamp-2 text-[13px] leading-5 text-ink">
                    {item.question}
                  </p>
                  <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-0.5">
                    <span className="text-[11px] font-medium text-muted">
                      {riskLine(item.riskLevel, item.riskScore)}
                    </span>
                    <DeliveryTag wasWithheld={item.wasWithheld} />
                  </div>
                  {reasons && (
                    <p className="mt-0.5 line-clamp-1 text-[11px] text-faint">
                      {reasons}
                    </p>
                  )}
                </button>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}

export default ReviewList;
