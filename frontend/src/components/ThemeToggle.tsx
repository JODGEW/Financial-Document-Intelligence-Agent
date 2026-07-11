import { Monitor, Moon, Sun } from "lucide-react";
import { useTheme } from "../lib/theme";
import type { ThemePreference } from "../lib/theme";

/** The icon shows the current preference; the label names the next stop. */
const ICONS: Record<ThemePreference, typeof Sun> = {
  light: Sun,
  dark: Moon,
  auto: Monitor
};

const NEXT_LABELS: Record<ThemePreference, string> = {
  light: "Switch to dark theme",
  dark: "Switch to auto theme (follow system)",
  auto: "Switch to light theme"
};

/**
 * Theme cycle button (light -> dark -> auto), shared by the chat top bar and
 * the review header.
 */
export function ThemeToggle() {
  const { preference, cycleTheme } = useTheme();
  const Icon = ICONS[preference];
  const label = NEXT_LABELS[preference];

  return (
    <button
      className="grid h-9 w-9 shrink-0 place-items-center rounded-md text-muted hover:bg-raised hover:text-ink focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ink/40"
      onClick={cycleTheme}
      type="button"
      aria-label={label}
      title={label}
    >
      <Icon size={17} />
    </button>
  );
}

export default ThemeToggle;
