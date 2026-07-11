import { useCallback, useEffect, useRef, useState } from "react";
import type {
  ReviewDetail,
  ReviewStatusFilter,
  ReviewSummary
} from "../types";
import {
  ReviewApiError,
  fetchReviewDetail,
  fetchReviewList,
  resolveReview
} from "../lib/reviews";
import ConfirmDialog from "./ConfirmDialog";
import ReviewDetailPanel from "./ReviewDetailPanel";
import ReviewList from "./ReviewList";
import ThemeToggle from "./ThemeToggle";

type ReviewWorkspaceProps = {
  /** The Chat / Review Queue switcher, rendered in the header. */
  workspaceNav: React.ReactNode;
};

type PendingAction = {
  action: "approve" | "reject";
  note: string | null;
};

function errorMessage(error: unknown): string {
  return error instanceof Error && error.message
    ? error.message
    : "The request failed.";
}

/**
 * Reviewer workspace: queue list on the left, item detail on the right.
 * Plain local state and fetch, same as the chat workspace. Approve/reject
 * records the decision only; nothing is delivered to the original requester.
 */
export function ReviewWorkspace({ workspaceNav }: ReviewWorkspaceProps) {
  const [filter, setFilter] = useState<ReviewStatusFilter>("pending");
  const [items, setItems] = useState<ReviewSummary[]>([]);
  const [listState, setListState] = useState<"loading" | "error" | "ready">(
    "loading"
  );
  const [listError, setListError] = useState<string | null>(null);

  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<ReviewDetail | null>(null);
  const [detailState, setDetailState] = useState<
    "idle" | "loading" | "error" | "notfound" | "ready"
  >("idle");
  const [detailError, setDetailError] = useState<string | null>(null);

  const [pendingAction, setPendingAction] = useState<PendingAction | null>(
    null
  );
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [actionNotice, setActionNotice] = useState<string | null>(null);

  // Guard against out-of-order responses when the filter or selection changes
  // while an earlier request is still in flight.
  const listRequestRef = useRef(0);
  const detailRequestRef = useRef(0);
  // Entry auto-open is armed once per mount; the first pending list load or
  // any manual filter change disarms it.
  const autoSelectRef = useRef(true);

  const loadList = useCallback(async (target: ReviewStatusFilter) => {
    const requestId = ++listRequestRef.current;
    setListState("loading");
    setListError(null);
    try {
      const data = await fetchReviewList(target);
      if (requestId !== listRequestRef.current) {
        return;
      }
      setItems(data);
      setListState("ready");
    } catch (error) {
      if (requestId !== listRequestRef.current) {
        return;
      }
      setListError(errorMessage(error));
      setListState("error");
    }
  }, []);

  const loadDetail = useCallback(
    async (reviewId: string) => {
      const requestId = ++detailRequestRef.current;
      setDetailState("loading");
      setDetailError(null);
      try {
        const data = await fetchReviewDetail(reviewId);
        if (requestId !== detailRequestRef.current) {
          return;
        }
        setDetail(data);
        setDetailState("ready");
      } catch (error) {
        if (requestId !== detailRequestRef.current) {
          return;
        }
        if (error instanceof ReviewApiError && error.status === 404) {
          setDetail(null);
          setDetailState("notfound");
          void loadList(filter);
        } else {
          setDetailError(errorMessage(error));
          setDetailState("error");
        }
      }
    },
    [filter, loadList]
  );

  useEffect(() => {
    void loadList(filter);
  }, [filter, loadList]);

  // Open the oldest pending item on workspace entry so review work starts
  // without an extra click. Pinned conditions: only the pending filter, only
  // with nothing selected, and only the first pending list load of this
  // mount. Filter switches and post-action refreshes never re-trigger it;
  // an empty pending queue keeps the idle copy.
  useEffect(() => {
    if (!autoSelectRef.current || listState !== "ready" || filter !== "pending") {
      return;
    }
    autoSelectRef.current = false;
    if (items.length > 0 && selectedId === null) {
      selectItem(items[0].reviewId);
    }
  }, [listState, items, filter, selectedId]);

  function changeFilter(next: ReviewStatusFilter) {
    // Manual navigation ends the entry auto-open window.
    autoSelectRef.current = false;
    setFilter(next);
  }

  function selectItem(reviewId: string) {
    if (reviewId === selectedId && detailState === "ready") {
      return;
    }
    setSelectedId(reviewId);
    setActionNotice(null);
    void loadDetail(reviewId);
  }

  async function runPendingAction() {
    if (!pendingAction || !detail) {
      return;
    }
    setIsSubmitting(true);
    setActionNotice(null);
    try {
      const updated = await resolveReview(
        detail.reviewId,
        pendingAction.action,
        pendingAction.note
      );
      // The POST response is the full final item; no second GET needed.
      setDetail(updated);
      setDetailState("ready");
      setPendingAction(null);
      void loadList(filter);
    } catch (error) {
      setPendingAction(null);
      if (error instanceof ReviewApiError && error.status === 409) {
        // Someone else resolved it first. Show the server's decision;
        // never overwrite it client-side.
        setActionNotice("This review has already been resolved.");
        if (selectedId) {
          void loadDetail(selectedId);
        }
        void loadList(filter);
      } else if (error instanceof ReviewApiError && error.status === 404) {
        setDetail(null);
        setDetailState("notfound");
        void loadList(filter);
      } else {
        setActionNotice(
          `The decision was not recorded: ${errorMessage(error)}`
        );
      }
    } finally {
      setIsSubmitting(false);
    }
  }

  const dialogTitle =
    pendingAction?.action === "approve" ? "Approve review" : "Reject review";

  return (
    <div className="flex min-w-0 flex-1 flex-col">
      <header className="flex h-14 shrink-0 items-center justify-between gap-3 border-b border-line bg-surface px-3 sm:px-4">
        <div className="flex min-w-0 items-center gap-3">
          {workspaceNav}
          <div className="hidden min-w-0 sm:block">
            <div className="truncate text-sm font-semibold text-ink">
              Review Queue
            </div>
            <div className="truncate text-xs text-muted">
              Held answers wait here for an approve or reject decision.
            </div>
          </div>
        </div>
        <ThemeToggle />
      </header>

      <div
        className="shrink-0 border-b border-held/25 bg-held-bg px-3 py-1.5 text-xs font-medium text-held sm:px-4"
        role="note"
      >
        Local demo · No authentication
      </div>

      <div className="flex min-h-0 flex-1">
        <div className="flex w-[300px] shrink-0 flex-col border-r border-line bg-surface">
          <ReviewList
            filter={filter}
            onFilterChange={changeFilter}
            items={items}
            state={listState}
            error={listError}
            selectedId={selectedId}
            onSelect={selectItem}
            onRetry={() => void loadList(filter)}
          />
        </div>

        <div className="min-w-0 flex-1 overflow-y-auto bg-bg">
          <div className="mx-auto w-full max-w-3xl px-4 py-5 sm:px-6">
            <ReviewDetailPanel
              detail={detail}
              state={detailState}
              error={detailError}
              actionNotice={actionNotice}
              isSubmitting={isSubmitting}
              onRetry={() => {
                if (selectedId) {
                  void loadDetail(selectedId);
                }
              }}
              onRequestAction={(action, note) =>
                setPendingAction({ action, note })
              }
            />
          </div>
        </div>
      </div>

      {pendingAction && detail && (
        <ConfirmDialog
          title={dialogTitle}
          body={
            <>
              <p>
                This marks the review as{" "}
                {pendingAction.action === "approve" ? "approved" : "rejected"}{" "}
                and records it in the queue.
              </p>
              <p className="mt-2">
                {pendingAction.note
                  ? `Reviewer note: "${pendingAction.note}"`
                  : "No reviewer note added."}
              </p>
            </>
          }
          confirmLabel={dialogTitle}
          isBusy={isSubmitting}
          onConfirm={() => void runPendingAction()}
          onCancel={() => {
            if (!isSubmitting) {
              setPendingAction(null);
            }
          }}
        />
      )}
    </div>
  );
}

export default ReviewWorkspace;
