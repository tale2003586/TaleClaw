import { describe, expect, it } from "vitest";
import { isViewAllowed, navigateTo, normalizeView } from "../app/routing";

describe("hash routing", () => {
  it("accepts all admin views and rejects unknown routes", () => {
    expect(normalizeView("#/runs", "admin")).toBe("runs");
    expect(normalizeView("#/not-real", "admin")).toBe("chat");
  });

  it("protects observability routes for regular users", () => {
    expect(isViewAllowed("logs", "user")).toBe(false);
    expect(normalizeView("#/runs", "user")).toBe("chat");
  });

  it("persists legal navigation in the hash", () => {
    navigateTo("memory", "user");
    expect(window.location.hash).toBe("#/memory");
    expect(localStorage.getItem("mainView")).toBe("memory");
  });
});
