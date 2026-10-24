import type { Config } from "tailwindcss";

export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        paper: "#f6f4ee",
        ink: "#1f2933",
        river: "#0f6b5c",
      },
      fontFamily: {
        sans: [
          '"Noto Sans SC"',
          '"Source Han Sans SC"',
          '"Microsoft YaHei"',
          "system-ui",
          "sans-serif",
        ],
      },
      boxShadow: {
        card: "0 24px 60px -34px rgba(31, 41, 51, 0.45)",
      },
    },
  },
  plugins: [],
} satisfies Config;
