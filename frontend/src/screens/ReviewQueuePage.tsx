import { useQuery } from "@tanstack/react-query";
import { Link, useSearchParams } from "react-router";
import { api, data } from "../api/client";
import type { ReviewItem } from "../api/types";
import { ErrorNote } from "../components/ErrorNote";

const DONE: Record<string, string> = {
  approved: "Approved. The document is being indexed with the corrected text.",
  rejected: "Rejected. The version will not be indexed.",
};

export function ReviewQueuePage() {
  const [params] = useSearchParams();
  const queue = useQuery({
    queryKey: ["review-queue"],
    queryFn: () => data<{ items: ReviewItem[]; four_eyes: boolean }>(api.GET("/api/review")),
    staleTime: 0,
  });
  const done = DONE[params.get("status") ?? ""];

  return (
    <section>
      <h1>OCR verification</h1>
      <p className="muted">
        Low-confidence scans wait here until someone checks the text and confirms the metadata.
        {queue.data?.four_eyes ? " The uploader of a version cannot approve it." : ""}
      </p>
      {done && <p className="notice">{done}</p>}
      {queue.isPending && <p className="muted">Loading…</p>}
      {queue.error && <ErrorNote error={queue.error} />}
      {queue.data && queue.data.items.length === 0 && <p className="card">Nothing is waiting for verification.</p>}
      {queue.data && queue.data.items.length > 0 && (
        <table className="table">
          <thead>
            <tr>
              <th>Document</th>
              <th>Type</th>
              <th>OCR confidence</th>
              <th>Pages</th>
              <th>Uploaded</th>
            </tr>
          </thead>
          <tbody>
            {queue.data.items.map((item) => (
              <tr key={`${item.doc_id}-${item.version}`}>
                <td>
                  <Link to={`/review/${encodeURIComponent(item.doc_id)}?v=${item.version}`}>{item.title || item.doc_id}</Link>
                  {item.is_new_version && <span className="chip">v{item.version}</span>}
                  {item.sensitivity === "restricted" && <span className="chip warn">Restricted</span>}
                  {item.review_reasons.length > 0 && <div className="muted small">{item.review_reasons.join("; ")}</div>}
                </td>
                <td>{item.doc_type}</td>
                <td>{item.ocr_confidence == null ? "—" : `${Math.round(item.ocr_confidence)}%`}</td>
                <td>{item.page_count ?? "—"}</td>
                <td>
                  {item.uploaded_by} <span className="muted small">{item.uploaded_at?.slice(0, 16).replace("T", " ")}</span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
