import type { Config } from "tailwindcss";

// The design tokens from docs/specs/f14-frontend/design.md §1.2, expressed once, here.
// Values resolve through CSS custom properties (see src/styles/tokens.css) so the dark theme is a
// variable swap rather than a second set of utility classes. Nothing downstream writes a raw hex.
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        paper: "rgb(var(--paper) / <alpha-value>)",
        "paper-raised": "rgb(var(--paper-raised) / <alpha-value>)",
        ink: "rgb(var(--ink) / <alpha-value>)",
        "ink-muted": "rgb(var(--ink-muted) / <alpha-value>)",
        seal: "rgb(var(--seal) / <alpha-value>)",
        stamp: "rgb(var(--stamp) / <alpha-value>)",
        flag: "rgb(var(--flag) / <alpha-value>)",
        rule: "rgb(var(--rule) / <alpha-value>)",
      },
      fontFamily: {
        // One superfamily, three roles. The restraint is a performance decision: NFR-2 caps Latin
        // fonts at 120KB on 3G, and a superfamily buys three voices from one set of metrics.
        display: ['"IBM Plex Sans Condensed"', "system-ui", "sans-serif"],
        body: ['"IBM Plex Sans"', "system-ui", "sans-serif"],
        mono: ['"IBM Plex Mono"', "ui-monospace", "monospace"],
      },
      fontSize: {
        xs: ["0.8125rem", { lineHeight: "1.45" }], // 13 — captions, counters
        sm: ["0.9375rem", { lineHeight: "1.5" }], // 15 — UI controls
        base: ["1.0625rem", { lineHeight: "1.5" }], // 17 — body; read carefully, not skimmed
        lg: ["1.3125rem", { lineHeight: "1.3" }], // 21 — section headings
        xl: ["2rem", { lineHeight: "1.15" }], // 32 — display
      },
      borderRadius: {
        DEFAULT: "6px",
      },
      maxWidth: {
        thread: "68ch",
      },
      keyframes: {
        // The stamp press: an impression landing on paper, not a fade-in.
        press: {
          "0%": { opacity: "0", transform: "scale(1.04)" },
          "100%": { opacity: "1", transform: "scale(1)" },
        },
      },
      animation: {
        press: "press 120ms ease-out",
      },
    },
  },
  plugins: [],
} satisfies Config;
