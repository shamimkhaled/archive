import { useMutation, useQuery } from "@tanstack/react-query";
import { type FormEvent, useState } from "react";
import { Link, useSearchParams } from "react-router";
import { api, data, postForm } from "../api/client";
import type { SearchResponse, SearchResult } from "../api/types";
import { ErrorNote } from "../components/ErrorNote";

type Facets = { departments?: string[]; categories?: string[]; doc_types?: string[] };

const FILTERS = ["lang", "doc_type", "department", "category", "reference_number", "date_from", "date_to"] as const;
type Filter = (typeof FILTERS)[number];

export function SearchPage() {
  const [params, setParams] = useSearchParams();
  const q = params.get("q") ?? "";
  const filters = Object.fromEntries(FILTERS.map((key) => [key, params.get(key) ?? ""])) as Record<Filter, string>;

  const facets = useQuery({
    queryKey: ["facets"],
    queryFn: () => data<Facets>(api.GET("/api/search/facets")),
    staleTime: 10 * 60_000,
  });

  const search = useQuery({
    queryKey: ["search", q, filters],
    enabled: q.trim().length > 0,
    queryFn: () => {
      const query: Record<string, string> = { q };
      for (const key of FILTERS) if (filters[key]) query[key] = filters[key];
      return data<SearchResponse>(api.GET("/api/search", { params: { query: query as never } }));
    },
  });

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const next = new URLSearchParams();
    for (const [key, value] of form.entries()) if (typeof value === "string" && value.trim()) next.set(key, value.trim());
    setParams(next);
  }

  const facetOptions = (name: keyof Facets) => (facets.data?.[name] ?? []).map((v) => <option key={v} value={v}>{v}</option>);

  return (
    <section>
      <h1>Search the archive</h1>
      <form className="search-form" onSubmit={submit} key={params.toString()}>
        <div className="search-bar">
          <input name="q" type="search" defaultValue={q} placeholder="Search in English or বাংলা" aria-label="Search" autoFocus />
          <button type="submit">Search</button>
        </div>
        <details className="filters" open={FILTERS.some((key) => filters[key])}>
          <summary>Filters</summary>
          <div className="filter-grid">
            <label>
              Language
              <select name="lang" defaultValue={filters.lang}>
                <option value="">Auto</option>
                <option value="en">English</option>
                <option value="bn">বাংলা</option>
              </select>
            </label>
            <label>
              Document type
              <select name="doc_type" defaultValue={filters.doc_type}>
                <option value="">Any</option>
                {facetOptions("doc_types")}
              </select>
            </label>
            <label>
              Department
              <select name="department" defaultValue={filters.department}>
                <option value="">Any</option>
                {facetOptions("departments")}
              </select>
            </label>
            <label>
              Category
              <select name="category" defaultValue={filters.category}>
                <option value="">Any</option>
                {facetOptions("categories")}
              </select>
            </label>
            <label>
              Reference number
              <input name="reference_number" defaultValue={filters.reference_number} />
            </label>
            <label>
              From
              <input name="date_from" type="date" defaultValue={filters.date_from} />
            </label>
            <label>
              To
              <input name="date_to" type="date" defaultValue={filters.date_to} />
            </label>
          </div>
        </details>
      </form>

      {search.isFetching && <p className="muted">Searching…</p>}
      {search.error && <ErrorNote error={search.error} />}
      {search.data && (
        <>
          <p className="muted">
            {search.data.count} result{search.data.count === 1 ? "" : "s"}
            {search.data.degraded ? " · keyword-only (semantic search unavailable)" : ""}
            {search.data.reranked ? " · reranked" : ""}
          </p>
          <ol className="results">
            {search.data.results.map((result) => (
              <ResultCard key={`${result.doc_id}-${result.page ?? 0}`} result={result} />
            ))}
          </ol>
        </>
      )}
    </section>
  );
}

function ResultCard({ result }: { result: SearchResult }) {
  const [requesting, setRequesting] = useState(false);
  const request = useMutation({
    mutationFn: (purpose: string) =>
      postForm(`/api/documents/${encodeURIComponent(result.doc_id)}/access-requests`, { purpose, requested_mode: "view_only" }),
  });
  const viewHref = `/view/${encodeURIComponent(result.doc_id)}${result.page ? `?page=${result.page}` : ""}`;
  const pending = result.access_status === "pending" || request.data?.ok;

  return (
    <li className="card result">
      <div className="result-head">
        {result.can_view ? <Link to={viewHref}>{result.title || result.doc_id}</Link> : <strong>{result.title || "Restricted document"}</strong>}
        <span className="chip">{result.doc_type}</span>
        {result.doc_date && <span className="muted">{result.doc_date}</span>}
        {result.page ? <span className="muted">p. {result.page}</span> : null}
      </div>
      {result.snippet && <p className="snippet">{result.snippet}</p>}
      {result.match_reasons?.length ? <p className="muted small">Matched: {result.match_reasons.join(", ")}</p> : null}
      <div className="actions">
        {result.can_view && <Link to={viewHref}>View</Link>}
        {result.can_download && <a href={`/download/${encodeURIComponent(result.doc_id)}`}>Download</a>}
        {!result.can_view && pending && <span className="chip warn">Access requested</span>}
        {!result.can_view && !pending && !requesting && (
          <button type="button" className="link" onClick={() => setRequesting(true)}>
            Request access
          </button>
        )}
      </div>
      {requesting && !pending && (
        <form
          className="inline-form"
          onSubmit={(event) => {
            event.preventDefault();
            const purpose = String(new FormData(event.currentTarget).get("purpose") ?? "").trim();
            if (purpose) request.mutate(purpose);
          }}
        >
          <input name="purpose" placeholder="Purpose of access" required />
          <button type="submit" disabled={request.isPending}>Send</button>
          {request.data && !request.data.ok && <span className="error">{request.data.error}</span>}
        </form>
      )}
    </li>
  );
}
