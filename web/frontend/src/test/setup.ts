import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach, vi } from "vitest";

afterEach(() => {
  cleanup();
  localStorage.clear();
  document.documentElement.removeAttribute("data-theme");
  document.documentElement.style.colorScheme = "";
  document.querySelector('meta[name="theme-color"]')?.remove();
  vi.unstubAllGlobals();
  window.history.replaceState(null, "", "/");
});
