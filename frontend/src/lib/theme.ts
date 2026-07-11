import { useEffect, useState } from "react";

export type ThemePreference = "light" | "dark" | "auto";

const STORAGE_KEY = "fdia-theme";

/** Cycle order for the toggle button. */
const CYCLE: ThemePreference[] = ["light", "dark", "auto"];

/**
 * The preference currently applied to <html> (set pre-paint by the inline
 * script in index.html). A forced choice carries the matching class; auto
 * carries neither, so the prefers-color-scheme CSS follows the OS live.
 */
function currentPreference(): ThemePreference {
  const classes = document.documentElement.classList;
  if (classes.contains("dark")) {
    return "dark";
  }
  if (classes.contains("light")) {
    return "light";
  }
  return "auto";
}

function applyPreference(preference: ThemePreference) {
  const root = document.documentElement;
  root.classList.toggle("dark", preference === "dark");
  root.classList.toggle("light", preference === "light");
}

/**
 * Theme preference cycling light -> dark -> auto, backed by localStorage.
 * The inline script in index.html has already resolved and applied the
 * initial state, so this hook only reads it and lets the user cycle.
 */
export function useTheme(): {
  preference: ThemePreference;
  cycleTheme: () => void;
} {
  const [preference, setPreference] = useState<ThemePreference>(() =>
    currentPreference()
  );

  useEffect(() => {
    applyPreference(preference);
    try {
      localStorage.setItem(STORAGE_KEY, preference);
    } catch {
      // Private-mode storage failures are non-fatal; the class is still applied.
    }
  }, [preference]);

  function cycleTheme() {
    setPreference(
      (current) => CYCLE[(CYCLE.indexOf(current) + 1) % CYCLE.length]
    );
  }

  return { preference, cycleTheme };
}
