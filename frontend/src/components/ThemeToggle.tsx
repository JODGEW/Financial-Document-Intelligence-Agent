import { Moon, Sun } from "lucide-react";
import { useTheme } from "../lib/theme";

/** Light/dark toggle button, shared by the chat top bar and review header. */
export function ThemeToggle() {
  const { theme, toggleTheme } = useTheme();
  const label =
    theme === "dark" ? "Switch to light theme" : "Switch to dark theme";

  return (
    <button
      className="grid h-9 w-9 shrink-0 place-items-center rounded-md text-muted hover:bg-raised hover:text-ink focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ink/40"
      onClick={toggleTheme}
      type="button"
      aria-label={label}
      title={label}
    >
      {theme === "dark" ? <Sun size={17} /> : <Moon size={17} />}
    </button>
  );
}

export default ThemeToggle;
