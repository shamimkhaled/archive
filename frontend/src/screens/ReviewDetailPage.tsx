import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useState } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router";
import { api, data, postForm } from "../api/client";
import type { ReviewDetail } from "../api/types";
import { ErrorNote } from "../components/ErrorNote";

const FIELD_LABELS: Record<string, string> = {
  title: "Title",
  reference_number: "Reference number",
  department: "Department",
  subject: "Subject",
  category: "Category",
};

export function ReviewDetailPage() {
  const { docId = "" } = useParams();
  const [params] = useSearchParams();
  const version = params.get("v") ?? "";
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [page, setPage] = useState(1);
  const [rejecting, setRejecting] = useState(false);

  const detail = useQuery({
    queryKey: ["review", docId, version],
    queryFn: () =>
      data<ReviewDetail>(
        api.GET("/api/review/{doc_id}", { params: { path: { doc_id: docId }, query: version ? { v: Number(version) } : {} } }),
      ),
    staleTime: 0,
  });

  const act = useMutation({
    mutationFn: ({ url, fields }: { url: string; fields: Record<string, string> }) => postForm(url, fields),
    onSuccess: (result) => {
      if (!result.ok) return;
      void queryClient.invalidateQueries({ queryKey: ["review-queue"] });
      navigate(`/review?status=${result.status ?? "approved"}`);
    },
  });

  if (detail.isPending) return <p className="muted">Loading…</p>;
  if (detail.error) return <ErrorNote error={detail.error} />;
  const review = detail.data;
  const base = `/review/${encodeURIComponent(review.doc_id)}/v/${review.version}`;
  const current = review.pages.find((p) => p.page === page) ?? review.pages[0];

  function approve(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const fields: Record<string, string> = {};
    for (const [key, value] of new FormData(event.currentTarget).entries()) if (typeof value === "string") fields[key] = value;
    act.mutate({ url: `${base}/approve`, fields });
  }

  return (
    <section>
      <h1>Verify {review.doc_id}</h1>
      <p className="muted">
        Version {review.version} · {review.doc_type} · uploaded by {review.uploaded_by ?? "unknown"}
        {review.ocr_confidence != null ? ` · OCR confidence ${Math.round(review.ocr_confidence)}%` : ""}
      </p>
      {review.four_eyes_problem && <p className="notice warn">{review.four_eyes_problem}</p>}
      {review.review_reasons.length > 0 && <p className="notice">Sent for verification: {review.review_reasons.join("; ")}.</p>}
      {act.data && !act.data.ok && <p className="error">{act.data.error ?? "The action was not accepted."}</p>}

      <form onSubmit={approve} className="review-grid">
        <div className="review-page">
          <div className="pdf-toolbar">
            <button type="button" disabled={page <= 1} onClick={() => setPage(page - 1)} aria-label="Previous page">
              ‹
            </button>
            <span>
              Page {current?.page ?? page} of {review.pages.length}
            </span>
            <button type="button" disabled={page >= review.pages.length} onClick={() => setPage(page + 1)} aria-label="Next page">
              ›
            </button>
            {current?.ocr_confidence != null && <span className="chip">{Math.round(current.ocr_confidence)}%</span>}
          </div>
          {current && <img src={`${base}/page/${current.page}.png`} alt={`Page ${current.page} scan`} className="scan" />}
        </div>
        <div className="review-edit">
          <fieldset>
            <legend>Confirmed metadata</legend>
            <label>
              Document type
              {review.suggested.doc_type && review.suggested.doc_type.toLowerCase() !== review.doc_type.toLowerCase()
                ? ` (AI suggests ${review.suggested.doc_type})`
                : ""}
              <input name="doc_type" defaultValue={review.doc_type} disabled={!review.can_act} />
            </label>
            {review.fields.map((field) => (
              <label key={field}>
                {FIELD_LABELS[field] ?? field}
                <input name={field} defaultValue={review.metadata[field] ?? ""} disabled={!review.can_act} />
              </label>
            ))}
            {review.suggested_source && <p className="muted small">Suggested by {review.suggested_source}; confirm or correct.</p>}
          </fieldset>
          {review.pages.map((p) => (
            <label key={p.page} hidden={p.page !== (current?.page ?? 1)}>
              Text of page {p.page}
              <textarea name={`page_${p.page}`} defaultValue={p.text} rows={18} disabled={!review.can_act} lang="bn-BD" />
            </label>
          ))}
          {review.can_act && (
            <div className="actions">
              <button type="submit" disabled={act.isPending}>
                Approve and index
              </button>
              <button type="button" className="secondary" onClick={() => setRejecting(true)}>
                Reject
              </button>
            </div>
          )}
        </div>
      </form>

      {rejecting && (
        <form
          className="card inline-form"
          onSubmit={(event) => {
            event.preventDefault();
            const reason = String(new FormData(event.currentTarget).get("reason") ?? "");
            act.mutate({ url: `${base}/reject`, fields: { reason } });
          }}
        >
          <input name="reason" placeholder="Reason (unreadable scan, wrong file, …)" />
          <button type="submit" className="danger" disabled={act.isPending}>
            Reject version
          </button>
          <button type="button" className="link" onClick={() => setRejecting(false)}>
            Cancel
          </button>
        </form>
      )}
    </section>
  );
}
