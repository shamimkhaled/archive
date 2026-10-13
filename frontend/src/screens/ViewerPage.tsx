import { useQuery } from "@tanstack/react-query";
import { Link, useParams, useSearchParams } from "react-router";
import { api, data } from "../api/client";
import type { DocumentInfo, DocumentVersions, RelatedDocuments } from "../api/types";
import { ErrorNote } from "../components/ErrorNote";
import { PdfViewer } from "../components/PdfViewer";

export function ViewerPage() {
  const { docId = "" } = useParams();
  const [params, setParams] = useSearchParams();
  const page = Math.max(1, Number(params.get("page") ?? window.location.hash.match(/page=(\d+)/)?.[1] ?? 1) || 1);

  const info = useQuery({
    queryKey: ["document", docId],
    queryFn: () => data<DocumentInfo>(api.GET("/api/app/documents/{doc_id}", { params: { path: { doc_id: docId } } })),
    staleTime: 0,
  });
  const related = useQuery({
    queryKey: ["related", docId],
    enabled: info.isSuccess,
    queryFn: () => data<RelatedDocuments>(api.GET("/api/documents/{doc_id}/related", { params: { path: { doc_id: docId }, query: {} } })),
  });
  const versions = useQuery({
    queryKey: ["versions", docId],
    enabled: Boolean(info.data?.can_see_versions),
    queryFn: () => data<DocumentVersions>(api.GET("/api/documents/{doc_id}/versions", { params: { path: { doc_id: docId } } })),
  });

  if (info.isPending) return <p className="muted">Loading…</p>;
  if (info.error) return <ErrorNote error={info.error} />;
  const doc = info.data;

  return (
    <section className="viewer">
      <header className="viewer-head">
        <div>
          <h1>{doc.title || doc.doc_id}</h1>
          <p className="muted">
            {[doc.doc_type, doc.doc_date, doc.department, doc.reference_number, doc.provenance].filter(Boolean).join(" · ")}
            {doc.metadata_status === "ai_unconfirmed" && (
              <span className="chip warn" title="Filled in by AI from the document text; not yet confirmed by a reviewer">
                AI-extracted metadata, unconfirmed
              </span>
            )}
          </p>
        </div>
        <div className="actions">
          {doc.has_briefing && <Link to={`/briefing/${encodeURIComponent(doc.doc_id)}`}>Board briefing</Link>}
          {doc.download_url && <a href={doc.download_url}>Download</a>}
        </div>
      </header>
      <div className="viewer-body">
        <PdfViewer
          url={doc.file_url}
          page={page}
          onPageChange={(next) => {
            const nextParams = new URLSearchParams(params);
            nextParams.set("page", String(next));
            setParams(nextParams, { replace: true });
          }}
        />
        <aside className="viewer-side">
          <p className="seal small">{doc.seal_text}</p>
          {related.data && related.data.related.length > 0 && (
            <>
              <h2>Related documents</h2>
              <ul className="plain">
                {related.data.related.map((item) => (
                  <li key={item.doc_id}>
                    <Link to={`/view/${encodeURIComponent(item.doc_id)}`}>{item.doc_id}</Link>
                    <span className="muted small"> {item.doc_type} {item.doc_date}</span>
                    {item.reasons.length > 0 && <div className="muted small">{item.reasons.slice(0, 3).join(", ")}</div>}
                  </li>
                ))}
              </ul>
            </>
          )}
          {versions.data && versions.data.versions.length > 1 && (
            <>
              <h2>Versions</h2>
              <ul className="plain">
                {versions.data.versions.map((version) => (
                  <li key={version.version}>
                    <a href={version.view_url} target="_blank" rel="noopener">
                      v{version.version}
                    </a>{" "}
                    <span className="muted small">
                      {version.is_current ? "current · " : ""}
                      {version.status} · {version.created_at?.slice(0, 10)}
                    </span>
                    <div className="mono small" title="SHA-256">
                      {version.sha256.slice(0, 16)}…
                    </div>
                  </li>
                ))}
              </ul>
            </>
          )}
        </aside>
      </div>
    </section>
  );
}
