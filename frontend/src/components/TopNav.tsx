import { Menu, PanelRight, ShieldCheck } from "lucide-react";

type TopNavProps = {
  isSidebarOpen: boolean;
  onOpenSidebar: () => void;
  /**
   * The evidence toggle appears only once the current thread has an answer
   * with retrieved sources — the empty state keeps the top bar as pure
   * context, not controls.
   */
  showEvidenceToggle: boolean;
  isContextPanelOpen: boolean;
  onToggleContextPanel: () => void;
};

export function TopNav({
  isSidebarOpen,
  onOpenSidebar,
  showEvidenceToggle,
  isContextPanelOpen,
  onToggleContextPanel
}: TopNavProps) {
  return (
    <header className="flex h-14 shrink-0 items-center justify-between gap-3 border-b border-zinc-200 bg-white px-3 sm:px-4">
      <div className="flex min-w-0 items-center gap-2">
        {!isSidebarOpen && (
          <button
            className="grid h-9 w-9 shrink-0 place-items-center rounded-md text-zinc-700 hover:bg-zinc-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-zinc-900/40"
            onClick={onOpenSidebar}
            type="button"
            aria-label="Open sidebar"
          >
            <Menu size={18} />
          </button>
        )}
        <div className="min-w-0">
          <div className="truncate text-sm font-semibold text-zinc-900">
            Document Q&A
          </div>
          <div className="hidden truncate text-xs text-zinc-500 sm:block">
            Internal evidence first. External context labeled separately.
          </div>
        </div>
      </div>

      <div className="flex shrink-0 items-center gap-2 sm:gap-3">
        <div
          className="hidden items-center gap-1.5 rounded-full border border-emerald-700/20 bg-emerald-50 px-2.5 py-1 text-[11px] font-medium text-emerald-800 md:flex"
          title="Answers cite retrieved corpus evidence; external context is labeled separately."
        >
          <ShieldCheck size={13} />
          Source-grounded
        </div>

        {showEvidenceToggle && (
          <button
            className={`flex h-8 items-center gap-1.5 rounded-md border px-2.5 text-xs font-medium focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-zinc-900/40 ${
              isContextPanelOpen
                ? "border-zinc-300 bg-zinc-100 text-zinc-900"
                : "border-zinc-200 text-zinc-600 hover:bg-zinc-50 hover:text-zinc-800"
            }`}
            onClick={onToggleContextPanel}
            type="button"
            aria-label={
              isContextPanelOpen
                ? "Hide retrieved sources"
                : "Show retrieved sources"
            }
            aria-pressed={isContextPanelOpen}
            title="Retrieved sources for the latest answer"
          >
            <PanelRight size={15} />
            Sources
          </button>
        )}
      </div>
    </header>
  );
}

export default TopNav;
