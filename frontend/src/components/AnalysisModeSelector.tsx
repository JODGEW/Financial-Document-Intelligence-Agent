import { ChevronDown } from "lucide-react";
import { ANALYSIS_MODES, type AnalysisModeId } from "../modes";

type AnalysisModeSelectorProps = {
  activeMode: AnalysisModeId;
  onSelect: (id: AnalysisModeId) => void;
};

/**
 * Frontend-only analysis modes: selecting one retitles the workspace and
 * seeds the input with a prompt scaffold. The backend always receives free
 * text, so this never changes the API contract.
 */
export function AnalysisModeSelector({
  activeMode,
  onSelect
}: AnalysisModeSelectorProps) {
  return (
    <>
      {/* Segmented control on wide screens */}
      <div
        className="hidden items-center gap-0.5 rounded-md border border-line bg-raised p-0.5 lg:flex"
        role="tablist"
        aria-label="Analysis mode"
      >
        {ANALYSIS_MODES.map((mode) => (
          <button
            className={`rounded px-2.5 py-1 text-xs font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ink/40 ${
              mode.id === activeMode
                ? "bg-surface text-ink shadow-sm ring-1 ring-line"
                : "text-muted hover:text-ink"
            }`}
            key={mode.id}
            onClick={() => onSelect(mode.id)}
            type="button"
            role="tab"
            aria-selected={mode.id === activeMode}
            title={mode.subtitle}
          >
            {mode.label}
          </button>
        ))}
      </div>

      {/* Compact select below lg */}
      <label className="relative lg:hidden">
        <span className="sr-only">Analysis mode</span>
        <select
          className="h-8 appearance-none rounded-md border border-line bg-raised pl-2.5 pr-7 text-xs font-medium text-ink focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ink/40"
          value={activeMode}
          onChange={(event) => onSelect(event.target.value as AnalysisModeId)}
        >
          {ANALYSIS_MODES.map((mode) => (
            <option key={mode.id} value={mode.id}>
              {mode.label}
            </option>
          ))}
        </select>
        <ChevronDown
          size={13}
          className="pointer-events-none absolute right-2 top-1/2 -translate-y-1/2 text-faint"
        />
      </label>
    </>
  );
}

export default AnalysisModeSelector;
