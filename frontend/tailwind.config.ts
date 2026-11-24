import type { Config } from "tailwindcss";

export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        paper: "var(--canvas)",
        ink: "rgb(var(--ink-rgb) / <alpha-value>)",
        river: "rgb(var(--accent-rgb) / <alpha-value>)",
      },
      fontFamily: {
        sans: ["var(--font-sans)"],
        serif: ["var(--font-serif)"],
        mono: ["var(--font-mono)"],
      },
      boxShadow: {
        card: "var(--shadow-sheet)",
      },
    },
  },
  plugins: [],
} satisfies Config;
