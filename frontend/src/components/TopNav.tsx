import { Menu, Moon, PanelRight, ShieldCheck, Sun } from "lucide-react";
import { useTheme } from "../lib/theme";

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
  const { theme, toggleTheme } = useTheme();

  return (
    <header className="flex h-14 shrink-0 items-center justify-between gap-3 border-b border-line bg-surface px-3 sm:px-4">
      <div className="flex min-w-0 items-center gap-2">
        {!isSidebarOpen && (
          <button
            className="grid h-9 w-9 shrink-0 place-items-center rounded-md text-ink hover:bg-raised focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ink/40"
            onClick={onOpenSidebar}
            type="button"
            aria-label="Open sidebar"
          >
            <Menu size={18} />
          </button>
        )}
        <div className="min-w-0">
          <div className="truncate text-sm font-semibold text-ink">
            Document Q&A
          </div>
          <div className="hidden truncate text-xs text-muted sm:block">
            Internal evidence first. External context labeled separately.
          </div>
        </div>
      </div>

      <div className="flex shrink-0 items-center gap-2 sm:gap-3">
        <div
          className="hidden items-center gap-1.5 rounded-full border border-grounded/25 bg-grounded-bg px-2.5 py-1 text-[11px] font-medium text-grounded md:flex"
          title="Answers cite retrieved corpus evidence; external context is labeled separately."
        >
          <ShieldCheck size={13} />
          Source-grounded
        </div>

        {showEvidenceToggle && (
          <button
            className={`flex h-8 items-center gap-1.5 rounded-md border px-2.5 text-xs font-medium focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ink/40 ${
              isContextPanelOpen
                ? "border-line-strong bg-raised text-ink"
                : "border-line text-muted hover:bg-raised hover:text-ink"
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

        <button
          className="grid h-9 w-9 shrink-0 place-items-center rounded-md text-muted hover:bg-raised hover:text-ink focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ink/40"
          onClick={toggleTheme}
          type="button"
          aria-label={
            theme === "dark" ? "Switch to light theme" : "Switch to dark theme"
          }
          title={
            theme === "dark" ? "Switch to light theme" : "Switch to dark theme"
          }
        >
          {theme === "dark" ? <Sun size={17} /> : <Moon size={17} />}
        </button>
      </div>
    </header>
  );
}

export default TopNav;
