import createClient, { type Middleware } from "openapi-fetch";
import type { paths } from "./schema";

/** The CSRF double-submit token: the server sets this readable cookie next to the httpOnly session cookie. */
export function csrfToken(): string {
  const match = document.cookie.match(/(?:^|;\s*)csrf_token=([^;]+)/);
  return match ? decodeURIComponent(match[1] ?? "") : "";
}

export function loginUrl(): string {
  return `/login?next=${encodeURIComponent(window.location.pathname + window.location.search)}`;
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
  }
}

function withSpaHeaders(headers: Headers, method: string): void {
  headers.set("X-BCP-Client", "spa");
  headers.set("Accept", "application/json");
  if (method !== "GET" && method !== "HEAD") headers.set("X-CSRF-Token", csrfToken());
}

const spa: Middleware = {
  onRequest({ request }) {
    withSpaHeaders(request.headers, request.method);
    return request;
  },
  onResponse({ response }) {
    if (response.status === 401) window.location.assign(loginUrl());
    return response;
  },
};

/** Typed against FastAPI's OpenAPI schema (paths and parameters); run `npm run gen:api` after API changes. */
export const api = createClient<paths>({ baseUrl: "", credentials: "same-origin" });
api.use(spa);

function detailOf(body: unknown, fallback: string): string {
  if (body && typeof body === "object" && "detail" in body) {
    const detail = (body as { detail: unknown }).detail;
    return typeof detail === "string" ? detail : JSON.stringify(detail);
  }
  return fallback;
}

/** Resolve an openapi-fetch call to its JSON body, throwing ApiError with the server's detail. */
export async function data<T>(call: Promise<{ data?: unknown; error?: unknown; response: Response }>): Promise<T> {
  const { data: body, error, response } = await call;
  if (!response.ok) throw new ApiError(response.status, detailOf(error, response.statusText));
  return body as T;
}

export interface FormResult {
  ok: boolean;
  status: string | null;
  error: string | null;
  detail?: string;
}

/**
 * POST to one of the server's HTML form routes. The server answers SPA requests with
 * {ok, status, error} instead of a 303, so both UIs share one implementation of each workflow.
 */
export async function postForm(url: string, fields: Record<string, string | Blob> = {}): Promise<FormResult> {
  const body = new FormData();
  for (const [key, value] of Object.entries(fields)) body.append(key, value);
  const headers = new Headers();
  withSpaHeaders(headers, "POST");
  const response = await fetch(url, { method: "POST", body, headers, credentials: "same-origin" });
  if (response.status === 401) {
    window.location.assign(loginUrl());
    return { ok: false, status: null, error: "Sign-in required" };
  }
  const payload = (await response.json().catch(() => ({}))) as Partial<FormResult> & { detail?: unknown };
  if (response.ok || response.status === 409) {
    return {
      ok: Boolean(payload.ok ?? response.ok),
      status: payload.status ?? null,
      error: payload.error ?? (payload.detail ? detailOf(payload, "") : null),
    };
  }
  return { ok: false, status: null, error: detailOf(payload, response.statusText) };
}

/** JSON POST for the /api/v1 endpoints (Ask). */
export async function postJson<T>(url: string, body: unknown): Promise<T> {
  const headers = new Headers({ "Content-Type": "application/json" });
  withSpaHeaders(headers, "POST");
  const response = await fetch(url, { method: "POST", body: JSON.stringify(body), headers, credentials: "same-origin" });
  if (response.status === 401) window.location.assign(loginUrl());
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new ApiError(response.status, detailOf(payload, response.statusText));
  return payload as T;
}
