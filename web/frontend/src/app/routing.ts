import type { UserRole } from "../api/types";

export const APP_VIEWS = ["chat", "logs", "runs", "files", "analysis", "memory", "status", "settings"] as const;
export type AppView = typeof APP_VIEWS[number];
export const ADMIN_VIEWS = new Set<AppView>(["logs", "runs"]);

export function isViewAllowed(view: AppView, role: UserRole) {
  return !ADMIN_VIEWS.has(view) || role === "admin";
}

export function normalizeView(value: string | null | undefined, role: UserRole): AppView {
  const clean = String(value || "").replace(/^#\/?/, "") as AppView;
  return APP_VIEWS.includes(clean) && isViewAllowed(clean, role) ? clean : "chat";
}

export function initialView(role: UserRole): AppView {
  const hashView = window.location.hash ? normalizeView(window.location.hash, role) : null;
  return hashView || normalizeView(localStorage.getItem("mainView"), role);
}

export function navigateTo(view: AppView, role: UserRole) {
  const allowed = isViewAllowed(view, role) ? view : "chat";
  localStorage.setItem("mainView", allowed);
  if (window.location.hash !== `#/${allowed}`) window.location.hash = `/${allowed}`;
}
