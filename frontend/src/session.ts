import { useQuery } from "@tanstack/react-query";
import { createContext, useContext } from "react";
import { api, data } from "./api/client";
import type { Session } from "./api/types";

export function useSessionQuery() {
  return useQuery({
    queryKey: ["session"],
    queryFn: () => data<Session>(api.GET("/api/app/session")),
    staleTime: 5 * 60_000,
  });
}

export const SessionContext = createContext<Session | null>(null);

export function useSession(): Session {
  const session = useContext(SessionContext);
  if (!session) throw new Error("useSession outside the signed-in layout");
  return session;
}

/** Same server route, as a link inside this app (`/view/X?page=2` → router path) or null if it is not an app screen. */
export function appPath(serverUrl: string | undefined | null): string | null {
  if (!serverUrl) return null;
  const match = serverUrl.match(/^\/(view|review|meetings)\/(.+)$/);
  if (match) return `/${match[1]}/${match[2]}`;
  const briefing = serverUrl.match(/^\/documents\/([^/]+)\/briefing/);
  if (briefing) return `/briefing/${briefing[1]}`;
  return null;
}
