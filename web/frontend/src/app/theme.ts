export type Theme = "light" | "dark";

export const THEME_STORAGE_KEY = "taleclawTheme";
export const THEME_COLORS: Record<Theme, string> = {
  light: "#f4f7fa",
  dark: "#080e18",
};

export function isTheme(value: unknown): value is Theme {
  return value === "light" || value === "dark";
}

export function readStoredTheme(storage?: Storage): Theme | null {
  let target = storage;
  if (!target && typeof window !== "undefined") {
    try {
      target = window.localStorage;
    } catch {
      return null;
    }
  }
  if (!target) return null;
  try {
    const value = target.getItem(THEME_STORAGE_KEY);
    return isTheme(value) ? value : null;
  } catch {
    return null;
  }
}

export function resolveTheme(stored: unknown, prefersDark: boolean): Theme {
  return isTheme(stored) ? stored : prefersDark ? "dark" : "light";
}

export function applyTheme(theme: Theme, root?: HTMLElement): void {
  if (typeof document === "undefined") return;
  const target = root || document.documentElement;
  target.dataset.theme = theme;
  target.style.colorScheme = theme;
  const meta = document.querySelector<HTMLMetaElement>('meta[name="theme-color"]');
  if (meta) meta.content = THEME_COLORS[theme];
}

export function prefersDarkTheme(): boolean {
  return typeof window !== "undefined" && typeof window.matchMedia === "function"
    ? window.matchMedia("(prefers-color-scheme: dark)").matches
    : false;
}

