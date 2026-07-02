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
        className="hidden items-center gap-0.5 rounded-md border border-zinc-200 bg-zinc-50 p-0.5 lg:flex"
        role="tablist"
        aria-label="Analysis mode"
      >
        {ANALYSIS_MODES.map((mode) => (
          <button
            className={`rounded px-2.5 py-1 text-xs font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-zinc-900/40 ${
              mode.id === activeMode
                ? "bg-white text-zinc-900 shadow-sm ring-1 ring-zinc-200"
                : "text-zinc-500 hover:text-zinc-800"
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
          className="h-8 appearance-none rounded-md border border-zinc-200 bg-zinc-50 pl-2.5 pr-7 text-xs font-medium text-zinc-700 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-zinc-900/40"
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
          className="pointer-events-none absolute right-2 top-1/2 -translate-y-1/2 text-zinc-400"
        />
      </label>
    </>
  );
}

export default AnalysisModeSelector;
