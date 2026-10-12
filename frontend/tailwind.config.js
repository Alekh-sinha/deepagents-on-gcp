/** Colours come from CSS variables (src/index.css) so the palette lives in one place and could be themed. */
const tok = (name) => ({ opacityValue }) =>
  opacityValue === undefined ? `var(${name})` : `color-mix(in srgb, var(${name}) calc(${opacityValue} * 100%), transparent)`;

/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        canvas: tok("--canvas"), chrome: tok("--chrome"), chromeHover: tok("--chrome-hover"), panel: tok("--panel"), paper: tok("--paper"),
        ink: tok("--ink"), muted: tok("--muted"), faint: tok("--faint"), line: tok("--line"), lineStrong: tok("--line-strong"),
        accent: tok("--accent"), accentSoft: tok("--accent-soft"), accentInk: tok("--accent-ink"),
        ok: tok("--ok"), okSoft: tok("--ok-soft"), warn: tok("--warn"), warnSoft: tok("--warn-soft"), danger: tok("--danger"), dangerSoft: tok("--danger-soft"),
      },
      fontFamily: {
        sans: ["Inter", "ui-sans-serif", "system-ui", "-apple-system", "Segoe UI", "Roboto", "sans-serif"],
        mono: ["ui-monospace", "SFMono-Regular", "Menlo", "Consolas", "monospace"],
      },
      boxShadow: { card: "0 1px 2px rgba(28,25,23,0.04), 0 1px 3px rgba(28,25,23,0.06)", pop: "0 8px 30px rgba(28,25,23,0.12)" },
      borderRadius: { xl2: "18px" },
    },
  },
  plugins: [],
};
