import {
  useLayoutEffect,
  type FormEvent,
  type KeyboardEvent,
  type RefObject
} from "react";
import { ArrowUp, Loader2, RotateCcw } from "lucide-react";
import { QUICK_ACTIONS } from "../modes";

type CommandInputProps = {
  value: string;
  onChange: (value: string) => void;
  onSend: () => void;
  onReset: () => void;
  isLoading: boolean;
  placeholder: string;
  inputRef: RefObject<HTMLTextAreaElement>;
  /**
   * Quick-action chips appear only once a thread exists — in the empty state
   * the starter cards are the single suggestion surface.
   */
  showQuickActions: boolean;
};

const MAX_INPUT_HEIGHT = 200;

export function CommandInput({
  value,
  onChange,
  onSend,
  onReset,
  isLoading,
  placeholder,
  inputRef,
  showQuickActions
}: CommandInputProps) {
  const canSubmit = value.trim().length > 0 && !isLoading;

  useLayoutEffect(() => {
    const element = inputRef.current;
    if (!element) {
      return;
    }
    element.style.height = "auto";
    element.style.height = `${Math.min(element.scrollHeight, MAX_INPUT_HEIGHT)}px`;
  }, [value, inputRef]);

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (canSubmit) {
      onSend();
    }
  }

  function handleKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    // Enter during IME composition commits the candidate, not the message.
    if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      if (canSubmit) {
        onSend();
      }
    }
  }

  function applyQuickAction(prompt: string) {
    onChange(prompt);
    inputRef.current?.focus();
  }

  return (
    <div className="shrink-0 border-t border-line bg-surface px-3 pb-3 pt-2 sm:px-4">
      <div className="mx-auto w-full max-w-3xl">
        {showQuickActions && (
          <div className="mb-2 flex flex-wrap items-center gap-1.5">
            {QUICK_ACTIONS.map((action) => (
              <button
                className="rounded-full border border-line bg-raised px-2.5 py-1 text-[11px] font-medium text-muted transition-colors hover:border-line-strong hover:text-ink focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ink/40"
                key={action.label}
                onClick={() => applyQuickAction(action.prompt)}
                type="button"
                title={action.prompt}
              >
                {action.label}
              </button>
            ))}
          </div>
        )}

        <form
          className="flex items-end gap-2 rounded-lg border border-line-strong bg-surface p-1.5 shadow-sm focus-within:border-ink/40 focus-within:ring-2 focus-within:ring-ink/10"
          onSubmit={handleSubmit}
        >
          <textarea
            ref={inputRef}
            className="min-h-[40px] flex-1 resize-none bg-transparent px-2.5 py-2 text-sm leading-6 text-ink outline-none placeholder:text-faint"
            value={value}
            onChange={(event) => onChange(event.target.value)}
            onKeyDown={handleKeyDown}
            placeholder={placeholder}
            rows={1}
            aria-label="Question"
          />
          {/* Hidden until there is something to send; stays while streaming
              so the spinner remains visible. */}
          {(value.trim().length > 0 || isLoading) && (
            <button
              className="grid h-9 w-9 shrink-0 place-items-center rounded-md bg-accent text-on-accent transition hover:opacity-90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ink/40 disabled:cursor-not-allowed disabled:bg-line-strong disabled:text-faint"
              disabled={!canSubmit}
              type="submit"
              aria-label="Send question"
            >
              {isLoading ? (
                <Loader2 size={17} className="animate-spin" />
              ) : (
                <ArrowUp size={17} />
              )}
            </button>
          )}
          <button
            className="grid h-9 w-9 shrink-0 place-items-center rounded-md text-muted hover:bg-raised hover:text-ink focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ink/40"
            onClick={onReset}
            type="button"
            aria-label="Start new analysis"
            title={
              isLoading
                ? "Stop the current answer and start a new analysis"
                : "Start new analysis"
            }
          >
            <RotateCcw size={16} />
          </button>
        </form>
      </div>
    </div>
  );
}

export default CommandInput;
