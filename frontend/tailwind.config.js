/** @type {import('tailwindcss').Config} */

// Every semantic color reads a CSS variable defined in index.css. Channels are
// space-separated RGB so `<alpha-value>` opacity modifiers (text-ink/70,
// ring-ink/40) keep working. Theme flips by toggling `.dark` on <html>.
const token = (name) => `rgb(var(--${name}) / <alpha-value>)`;

export default {
  darkMode: "class",
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      fontFamily: {
        sans: [
          "Inter",
          "ui-sans-serif",
          "system-ui",
          "-apple-system",
          "BlinkMacSystemFont",
          "Segoe UI",
          "sans-serif"
        ],
        mono: [
          "ui-monospace",
          "SFMono-Regular",
          "Menlo",
          "Consolas",
          "monospace"
        ]
      },
      colors: {
        bg: token("bg"),
        surface: token("surface"),
        raised: token("raised"),
        line: token("line"),
        "line-strong": token("line-strong"),
        ink: token("ink"),
        muted: token("muted"),
        faint: token("faint"),
        accent: token("accent"),
        "on-accent": token("on-accent"),
        grounded: {
          DEFAULT: token("grounded"),
          bg: token("grounded-bg")
        },
        external: {
          DEFAULT: token("external"),
          bg: token("external-bg")
        },
        held: {
          DEFAULT: token("held"),
          bg: token("held-bg")
        },
        blocked: {
          DEFAULT: token("blocked"),
          bg: token("blocked-bg")
        }
      }
    }
  },
  plugins: []
};
