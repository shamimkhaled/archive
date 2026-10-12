import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { appPath } from "../session";
import { describeStatus } from "../components/StatusNote";
import { csrfToken, loginUrl, postForm } from "./client";

const assign = vi.fn();

beforeEach(() => {
  vi.stubGlobal("document", { cookie: "theme=dark; csrf_token=abc%2B123; other=1" });
  vi.stubGlobal("window", { location: { pathname: "/app/view/BCP-1", search: "?page=3", assign } });
});

afterEach(() => {
  vi.unstubAllGlobals();
  assign.mockReset();
});

describe("csrf and login helpers", () => {
  it("reads the double-submit cookie", () => {
    expect(csrfToken()).toBe("abc+123");
  });

  it("returns to the current screen after sign-in", () => {
    expect(loginUrl()).toBe("/login?next=%2Fapp%2Fview%2FBCP-1%3Fpage%3D3");
  });
});

describe("postForm bridge", () => {
  it("sends the SPA marker and CSRF header and reads the bridged result", async () => {
    const fetchMock = vi.fn(async (_url: string, _init: RequestInit) =>
      new Response(JSON.stringify({ ok: true, status: "minutes_saved", error: null }), { status: 200 }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const result = await postForm("/meetings/4/minutes", { body_md: "x" });
    expect(result).toEqual({ ok: true, status: "minutes_saved", error: null });
    const init = fetchMock.mock.calls[0]![1];
    const headers = init.headers as Headers;
    expect(headers.get("X-BCP-Client")).toBe("spa");
    expect(headers.get("X-CSRF-Token")).toBe("abc+123");
    expect(init.credentials).toBe("same-origin");
    expect((init.body as FormData).get("body_md")).toBe("x");
  });

  it("reports a workflow refusal from the 409 bridge response", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(JSON.stringify({ ok: false, status: "minutes_four_eyes", error: null }), { status: 409 })),
    );
    const result = await postForm("/meetings/4/minutes/approve");
    expect(result.ok).toBe(false);
    expect(result.status).toBe("minutes_four_eyes");
  });

  it("surfaces HTTPException detail on a 409", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ detail: "Version is indexed" }), { status: 409 })));
    const result = await postForm("/review/X/v/1/approve");
    expect(result).toMatchObject({ ok: false, error: "Version is indexed" });
  });

  it("sends the user to sign in on 401", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("{}", { status: 401 })));
    const result = await postForm("/documents/X/briefing/review");
    expect(result.ok).toBe(false);
    expect(assign).toHaveBeenCalledWith("/login?next=%2Fapp%2Fview%2FBCP-1%3Fpage%3D3");
  });
});

describe("links", () => {
  it("maps server citation URLs onto app routes", () => {
    expect(appPath("/view/BCP-2024-001?page=4")).toBe("/view/BCP-2024-001?page=4");
    expect(appPath("/documents/BCP-9/briefing")).toBe("/briefing/BCP-9");
    expect(appPath("/board/meetings/3/documents/7/view")).toBeNull();
    expect(appPath(null)).toBeNull();
  });

  it("describes known and unknown status codes", () => {
    expect(describeStatus("minutes_approved")).toMatch(/Approved/);
    expect(describeStatus("something_new")).toBe("something new");
  });
});
