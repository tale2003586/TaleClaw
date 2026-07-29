import { useCallback, useEffect, useState } from "react";
import type { UserRole } from "../api/types";
import { initialView, navigateTo, normalizeView, type AppView } from "../app/routing";

export function useAppView(role: UserRole) {
  const [view, setView] = useState<AppView>(() => initialView(role));
  useEffect(() => {
    const sync = () => {
      const next = normalizeView(window.location.hash || localStorage.getItem("mainView"), role);
      setView(next); localStorage.setItem("mainView", next);
      if (window.location.hash !== `#/${next}`) window.history.replaceState(null, "", `#/${next}`);
    };
    sync(); window.addEventListener("hashchange", sync);
    return () => window.removeEventListener("hashchange", sync);
  }, [role]);
  const navigate = useCallback((next: AppView) => navigateTo(next, role), [role]);
  return { view, navigate };
}
