import { createContext, useContext, type ReactNode } from "react";
import type { CurrentUser, HealthResponse } from "../api/types";
import type { ChatRunsController } from "../hooks/useChatRuns";
import type { SessionsController } from "../hooks/useSessions";

export interface AppContextValue {
  user: CurrentUser;
  health: HealthResponse;
  codingWorkspace: string;
  setCodingWorkspace(value: string): void;
  refreshHealth?(): Promise<void>;
}

const AppContext = createContext<AppContextValue | null>(null);
export function AppContextProvider({ value, children }: { value: AppContextValue; children: ReactNode }) {
  return <AppContext.Provider value={value}>{children}</AppContext.Provider>;
}
export function useAppContext() {
  const value = useContext(AppContext);
  if (!value) throw new Error("AppContext is unavailable");
  return value;
}

const SessionsContext = createContext<SessionsController | null>(null);
export function SessionsContextProvider({ value, children }: { value: SessionsController; children: ReactNode }) {
  return <SessionsContext.Provider value={value}>{children}</SessionsContext.Provider>;
}
export function useSessionsContext() {
  const value = useContext(SessionsContext);
  if (!value) throw new Error("SessionsContext is unavailable");
  return value;
}

const ChatRunsContext = createContext<ChatRunsController | null>(null);
export function ChatRunsContextProvider({ value, children }: { value: ChatRunsController; children: ReactNode }) {
  return <ChatRunsContext.Provider value={value}>{children}</ChatRunsContext.Provider>;
}
export function useChatRunsContext() {
  const value = useContext(ChatRunsContext);
  if (!value) throw new Error("ChatRunsContext is unavailable");
  return value;
}
