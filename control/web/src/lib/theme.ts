export type Theme = "light" | "dark";

const STORAGE_KEY = "vonk-theme";
const LIGHT_QUERY = "(prefers-color-scheme: light)";

function isTheme(value: unknown): value is Theme {
  return value === "light" || value === "dark";
}

/** The owner's pick, or null while the system setting decides. */
export function storedTheme(): Theme | null {
  try {
    const value = localStorage.getItem(STORAGE_KEY);
    return isTheme(value) ? value : null;
  } catch {
    return null;
  }
}

export function systemTheme(): Theme {
  return window.matchMedia?.(LIGHT_QUERY).matches ? "light" : "dark";
}

export function effectiveTheme(): Theme {
  return storedTheme() ?? systemTheme();
}

/** Sets data-theme from the owner's pick; with none, the stylesheet follows the system. */
export function applyStoredTheme(): void {
  const theme = storedTheme();
  if (theme) document.documentElement.dataset.theme = theme;
  else delete document.documentElement.dataset.theme;
}

export function setTheme(theme: Theme): void {
  try {
    localStorage.setItem(STORAGE_KEY, theme);
  } catch {
    // The pick still applies to this page without storage.
  }
  document.documentElement.dataset.theme = theme;
}

/** Calls back when the system setting changes; returns the unsubscribe. */
export function watchSystemTheme(onChange: () => void): () => void {
  const media = window.matchMedia?.(LIGHT_QUERY);
  media?.addEventListener("change", onChange);
  return () => media?.removeEventListener("change", onChange);
}
