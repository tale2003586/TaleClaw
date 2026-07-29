import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { applyTheme, readStoredTheme, resolveTheme, THEME_COLORS, THEME_STORAGE_KEY } from "../app/theme";
import { useTheme } from "../hooks/useTheme";
import { ThemeToggle } from "../components/ui/ThemeToggle";

function makeMediaQuery(matches = false) {
  const listeners = new Set<(event: MediaQueryListEvent) => void>();
  let currentMatches = matches;
  const media = {
    get matches() { return currentMatches; },
    media: "(prefers-color-scheme: dark)",
    addEventListener: (_type: string, listener: (event: MediaQueryListEvent) => void) => listeners.add(listener),
    removeEventListener: (_type: string, listener: (event: MediaQueryListEvent) => void) => listeners.delete(listener),
    addListener: (listener: (event: MediaQueryListEvent) => void) => listeners.add(listener),
    removeListener: (listener: (event: MediaQueryListEvent) => void) => listeners.delete(listener),
    emit(next: boolean) {
      currentMatches = next;
      listeners.forEach((listener) => listener({ matches: next } as MediaQueryListEvent));
    },
  };
  return media as unknown as MediaQueryList & { emit(next: boolean): void };
}

function Harness() {
  const controller = useTheme();
  return <ThemeToggle {...controller} />;
}

describe("theme utilities", () => {
  beforeEach(() => {
    document.documentElement.removeAttribute("data-theme");
    document.documentElement.style.colorScheme = "";
    localStorage.clear();
    document.querySelector('meta[name="theme-color"]')?.remove();
  });

  it("accepts only valid stored themes and resolves system fallback", () => {
    localStorage.setItem(THEME_STORAGE_KEY, "dark");
    expect(readStoredTheme()).toBe("dark");
    localStorage.setItem(THEME_STORAGE_KEY, "sepia");
    expect(readStoredTheme()).toBeNull();
    expect(readStoredTheme({ getItem: () => { throw new Error("denied"); } } as unknown as Storage)).toBeNull();
    expect(resolveTheme(null, true)).toBe("dark");
    expect(resolveTheme(null, false)).toBe("light");
  });

  it("applies root and browser theme colors", () => {
    const meta = document.createElement("meta");
    meta.name = "theme-color";
    document.head.append(meta);
    applyTheme("dark");
    expect(document.documentElement.dataset.theme).toBe("dark");
    expect(document.documentElement.style.colorScheme).toBe("dark");
    expect(meta.content).toBe(THEME_COLORS.dark);
  });
});

describe("ThemeToggle", () => {
  beforeEach(() => {
    document.documentElement.dataset.theme = "light";
    localStorage.clear();
  });

  it("follows the system until the user chooses a theme", async () => {
    const media = makeMediaQuery(false);
    vi.stubGlobal("matchMedia", vi.fn(() => media));
    render(<Harness />);
    expect(screen.getByRole("button")).toHaveAccessibleName("切换到暗色主题");
    media.emit(true);
    expect(await screen.findByRole("button")).toHaveAccessibleName("切换到浅色主题");
  });

  it("persists a manual choice and exposes a tooltip", async () => {
    const user = userEvent.setup();
    vi.stubGlobal("matchMedia", vi.fn(() => makeMediaQuery(false)));
    render(<Harness />);
    const button = screen.getByRole("button");
    await user.click(button);
    expect(localStorage.getItem(THEME_STORAGE_KEY)).toBe("dark");
    expect(document.documentElement.dataset.theme).toBe("dark");
    expect(button).toHaveAttribute("title", "切换到浅色主题");
  });
});
