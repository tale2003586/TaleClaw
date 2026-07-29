import { useCallback, useEffect, useState } from "react";
import { applyTheme, prefersDarkTheme, readStoredTheme, resolveTheme, THEME_STORAGE_KEY, type Theme } from "../app/theme";

export interface ThemeController {
  theme: Theme;
  toggleTheme(): void;
}

function initialTheme(): Theme {
  if (typeof document !== "undefined") {
    const attribute = document.documentElement.dataset.theme;
    if (attribute === "light" || attribute === "dark") return attribute;
  }
  return resolveTheme(readStoredTheme(), prefersDarkTheme());
}

export function useTheme(): ThemeController {
  const [theme, setTheme] = useState<Theme>(initialTheme);
  const [manual, setManual] = useState(() => readStoredTheme() !== null);

  useEffect(() => {
    applyTheme(theme);
  }, [theme]);

  useEffect(() => {
    if (manual || typeof window === "undefined" || typeof window.matchMedia !== "function") return undefined;
    const media = window.matchMedia("(prefers-color-scheme: dark)");
    const onChange = (event: MediaQueryListEvent) => setTheme(resolveTheme(null, event.matches));
    if (typeof media.addEventListener === "function") media.addEventListener("change", onChange);
    else media.addListener?.(onChange);
    return () => {
      if (typeof media.removeEventListener === "function") media.removeEventListener("change", onChange);
      else media.removeListener?.(onChange);
    };
  }, [manual]);

  const toggleTheme = useCallback(() => {
    setTheme((current) => {
      const next = current === "dark" ? "light" : "dark";
      try {
        window.localStorage.setItem(THEME_STORAGE_KEY, next);
      } catch {
        // Private browsing may deny localStorage; the in-memory toggle still works.
      }
      return next;
    });
    setManual(true);
  }, []);

  return { theme, toggleTheme };
}
