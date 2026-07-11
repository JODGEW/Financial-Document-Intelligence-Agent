import { useEffect, useRef } from "react";

type ConfirmDialogProps = {
  title: string;
  body: React.ReactNode;
  confirmLabel: string;
  isBusy: boolean;
  onConfirm: () => void;
  onCancel: () => void;
};

const FOCUSABLE_SELECTOR =
  'button:not([disabled]), [href], input:not([disabled]), textarea:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])';

/**
 * Modal confirmation dialog with the focus contract the review actions need:
 * focus moves onto the dialog when it opens, Tab cycles inside it while open,
 * and focus returns to the triggering element when it closes (unmounts).
 */
export function ConfirmDialog({
  title,
  body,
  confirmLabel,
  isBusy,
  onConfirm,
  onCancel
}: ConfirmDialogProps) {
  const panelRef = useRef<HTMLDivElement>(null);
  const cancelRef = useRef<HTMLButtonElement>(null);
  const restoreFocusRef = useRef<HTMLElement | null>(null);

  useEffect(() => {
    restoreFocusRef.current =
      document.activeElement instanceof HTMLElement
        ? document.activeElement
        : null;
    cancelRef.current?.focus();
    return () => {
      restoreFocusRef.current?.focus();
    };
  }, []);

  function handleKeyDown(event: React.KeyboardEvent<HTMLDivElement>) {
    if (event.key === "Escape") {
      event.stopPropagation();
      if (!isBusy) {
        onCancel();
      }
      return;
    }
    if (event.key !== "Tab") {
      return;
    }
    const panel = panelRef.current;
    if (!panel) {
      return;
    }
    const focusable = Array.from(
      panel.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR)
    );
    if (focusable.length === 0) {
      event.preventDefault();
      return;
    }
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    const active = document.activeElement;
    if (event.shiftKey && (active === first || !panel.contains(active))) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && active === last) {
      event.preventDefault();
      first.focus();
    }
  }

  return (
    <div
      className="fixed inset-0 z-50 grid place-items-center p-4"
      onKeyDown={handleKeyDown}
    >
      <div
        className="fixed inset-0 bg-black/40"
        onClick={isBusy ? undefined : onCancel}
        aria-hidden="true"
      />
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="confirm-dialog-title"
        className="relative w-full max-w-md rounded-lg border border-line bg-surface p-4 shadow-lg"
      >
        <h2
          id="confirm-dialog-title"
          className="text-sm font-semibold text-ink"
        >
          {title}
        </h2>
        <div className="mt-2 text-sm leading-6 text-muted">{body}</div>
        <div className="mt-4 flex justify-end gap-2">
          <button
            ref={cancelRef}
            className="h-8 rounded-md border border-line px-3 text-xs font-medium text-muted hover:bg-raised hover:text-ink focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ink/40 disabled:cursor-not-allowed disabled:opacity-50"
            onClick={onCancel}
            type="button"
            disabled={isBusy}
          >
            Cancel
          </button>
          <button
            className="h-8 rounded-md bg-accent px-3 text-xs font-medium text-on-accent hover:opacity-90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ink/40 disabled:cursor-not-allowed disabled:opacity-60"
            onClick={onConfirm}
            type="button"
            disabled={isBusy}
          >
            {isBusy ? "Recording decision..." : confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}

export default ConfirmDialog;
