import { useEffect, useState } from "react";

export type Theme = "light" | "dark";

const STORAGE_KEY = "fdia-theme";

/** The theme currently applied to <html> (set pre-paint by the inline script). */
function currentTheme(): Theme {
  return document.documentElement.classList.contains("dark") ? "dark" : "light";
}

function applyTheme(theme: Theme) {
  const root = document.documentElement;
  root.classList.toggle("dark", theme === "dark");
  root.classList.toggle("light", theme === "light");
}

/**
 * Light/dark toggle backed by localStorage. The inline script in index.html
 * has already resolved and applied the initial theme, so this hook only reads
 * that state and lets the user flip it.
 */
export function useTheme(): { theme: Theme; toggleTheme: () => void } {
  const [theme, setTheme] = useState<Theme>(() => currentTheme());

  useEffect(() => {
    applyTheme(theme);
    try {
      localStorage.setItem(STORAGE_KEY, theme);
    } catch {
      // Private-mode storage failures are non-fatal; the class is still applied.
    }
  }, [theme]);

  function toggleTheme() {
    setTheme((current) => (current === "dark" ? "light" : "dark"));
  }

  return { theme, toggleTheme };
}
