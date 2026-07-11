import type {
  ReviewDetail,
  ReviewStatus,
  ReviewStatusFilter,
  ReviewSummary
} from "../types";

/** API error carrying the HTTP status so callers can branch on 404/409. */
export class ReviewApiError extends Error {
  status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "ReviewApiError";
    this.status = status;
  }
}

async function errorDetail(response: Response): Promise<string> {
  try {
    const data = (await response.json()) as { detail?: unknown };
    if (typeof data.detail === "string" && data.detail.length > 0) {
      return data.detail;
    }
  } catch {
    // Non-JSON error body; fall through to the generic message.
  }
  return `Request failed with status ${response.status}`;
}

async function requestJson<T>(input: string, init?: RequestInit): Promise<T> {
  const response = await fetch(input, init);
  if (!response.ok) {
    throw new ReviewApiError(response.status, await errorDetail(response));
  }
  return (await response.json()) as T;
}

/** List review items. The server's ordering is authoritative; never re-sort. */
export function fetchReviewList(
  filter: ReviewStatusFilter
): Promise<ReviewSummary[]> {
  return requestJson<ReviewSummary[]>(`/api/reviews?status=${filter}`);
}

export function fetchReviewDetail(reviewId: string): Promise<ReviewDetail> {
  return requestJson<ReviewDetail>(
    `/api/reviews/${encodeURIComponent(reviewId)}`
  );
}

export function resolveReview(
  reviewId: string,
  action: "approve" | "reject",
  note: string | null
): Promise<ReviewDetail> {
  return requestJson<ReviewDetail>(
    `/api/reviews/${encodeURIComponent(reviewId)}/${action}`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ note })
    }
  );
}

export const REVIEW_FILTERS: { id: ReviewStatusFilter; label: string }[] = [
  { id: "pending", label: "Pending" },
  { id: "approved", label: "Approved" },
  { id: "rejected", label: "Rejected" },
  { id: "all", label: "All" }
];

export const REVIEW_STATUS_LABELS: Record<ReviewStatus, string> = {
  pending: "Pending",
  approved: "Approved",
  rejected: "Rejected"
};

const RISK_REASON_LABELS: Record<string, string> = {
  grounding_score_below_target: "Grounding score below target",
  external_context_used: "External context used",
  guardrail_blocked: "Guardrail blocked the answer",
  pii_anonymized: "PII anonymized by the guardrail"
};

function capitalize(value: string) {
  return value.charAt(0).toUpperCase() + value.slice(1);
}

/** Readable label for a scorer risk reason, never raw snake_case. */
export function riskReasonLabel(reason: string): string {
  return RISK_REASON_LABELS[reason] ?? capitalize(reason.replace(/_/g, " "));
}

export function riskLine(riskLevel: string, riskScore: number): string {
  return `${capitalize(riskLevel)} risk · ${riskScore.toFixed(2)}`;
}

export type DeliveryState = {
  /** Short text for list rows. */
  short: string;
  /** Full line for the detail header. */
  full: string;
  /** Caption under the approve/reject buttons. */
  caption: string;
};

/**
 * What happened to the original answer, snapshotted at enqueue time.
 * Items written before the wasWithheld field read as null (unknown).
 */
export function deliveryState(wasWithheld: boolean | null): DeliveryState {
  if (wasWithheld === true) {
    return {
      short: "Withheld",
      full: "Withheld before delivery",
      caption:
        "Decision will be recorded only. It will not be delivered to the original requester automatically."
    };
  }
  if (wasWithheld === false) {
    return {
      short: "Returned",
      full: "Answer already returned · Flag mode",
      caption:
        "Decision recorded for audit purposes only. The answer was already returned in flag mode."
    };
  }
  return {
    short: "Unknown",
    full: "Delivery state unknown · Legacy review item",
    caption:
      "Decision recorded only. Automatic delivery is not configured; original delivery state is unknown."
  };
}

/** Local, readable timestamp; falls back to the raw value when unparsable. */
export function formatTimestamp(iso: string | null): string | null {
  if (!iso) {
    return null;
  }
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) {
    return iso;
  }
  return date.toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit"
  });
}
