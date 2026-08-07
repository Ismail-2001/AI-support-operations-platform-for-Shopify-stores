/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  darkMode: "class",
  theme: {
    extend: {
      colors: {
        bg: { DEFAULT: "#EEF1F4", dark: "#0F171E" },
        surface: { DEFAULT: "#FFFFFF", dark: "#162230" },
        ink: {
          900: "#12202E",
          700: "#1E3245",
          600: "#46586B",
          400: "#7C8CA0",
          dark: {
            900: "#E8ECF0",
            700: "#C0C9D4",
            600: "#8A97A6",
            400: "#5A6778",
          },
        },
        line: { DEFAULT: "#DBE1E8", dark: "#253546" },
        gold: { DEFAULT: "#C08A2E", 100: "#F6E9D2", 700: "#8A6220" },
        teal: { DEFAULT: "#2E8C82", 100: "#DCEEEC", 700: "#1F615A" },
        rose: { DEFAULT: "#C4485A", 100: "#F5DCE0", 700: "#8E3242" },
        violet: { DEFAULT: "#6B5CA5", 100: "#E7E3F3", 700: "#4C4079" },
      },
      fontFamily: {
        display: ["Fraunces", "ui-serif", "Georgia", "serif"],
        sans: ["'IBM Plex Sans'", "ui-sans-serif", "system-ui", "sans-serif"],
        mono: ["'IBM Plex Mono'", "ui-monospace", "SFMono-Regular", "monospace"],
      },
      boxShadow: {
        panel: "0 1px 2px rgba(18,32,46,0.04), 0 8px 24px -12px rgba(18,32,46,0.12)",
        "panel-dark": "0 1px 2px rgba(0,0,0,0.2), 0 8px 24px -12px rgba(0,0,0,0.4)",
      },
      borderRadius: {
        xl2: "1.25rem",
      },
    },
  },
  plugins: [],
};
