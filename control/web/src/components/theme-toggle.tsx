import { useEffect, useState } from "react";
import { effectiveTheme, setTheme, watchSystemTheme } from "../lib/theme";
import { MoonIcon, SunIcon } from "./icons";

export function ThemeToggle() {
  const [theme, setCurrent] = useState(effectiveTheme);
  useEffect(() => watchSystemTheme(() => setCurrent(effectiveTheme())), []);
  const next = theme === "light" ? "dark" : "light";
  return (
    <button
      type="button"
      className="theme-toggle"
      aria-label={`Theme: ${theme}. Switch to ${next}`}
      onClick={() => {
        setTheme(next);
        setCurrent(next);
      }}
    >
      {theme === "light" ? <SunIcon /> : <MoonIcon />}
    </button>
  );
}
