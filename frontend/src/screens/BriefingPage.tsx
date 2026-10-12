import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useParams } from "react-router";
import { api, data, postForm } from "../api/client";
import type { BriefingPayload, BriefingSection } from "../api/types";
import { ErrorNote } from "../components/ErrorNote";
import { StatusNote } from "../components/StatusNote";
import { appPath } from "../session";

export function BriefingPage() {
  const { docId = "" } = useParams();
  const queryClient = useQueryClient();
  const queryKey = ["briefing", docId];

  const briefing = useQuery({
    queryKey,
    queryFn: () =>
      data<BriefingPayload>(api.GET("/api/documents/{doc_id}/briefing", { params: { path: { doc_id: docId }, query: { refs: true } } })),
    staleTime: 0,
    refetchInterval: (query) => {
      const status = query.state.data?.briefing?.status;
      return status === "pending" || status === "generating" ? 4000 : false;
    },
  });

  const act = useMutation({
    mutationFn: ({ path, fields }: { path: string; fields?: Record<string, string> }) =>
      postForm(`/documents/${encodeURIComponent(docId)}/briefing${path}`, fields),
    onSettled: () => void queryClient.invalidateQueries({ queryKey }),
  });

  if (briefing.isPending) return <p className="muted">Loading…</p>;
  if (briefing.error) return <ErrorNote error={briefing.error} />;
  const payload = briefing.data;
  const b = payload.briefing;
  const organizer = Boolean(payload.is_organizer);
  const draft = b?.state === "ai_draft";
  const ready = b?.status === "ready";
  const paper = appPath(payload.paper_url);

  return (
    <section>
      <div className="row spread">
        <div>
          <h1>Board briefing</h1>
          <p className="muted">
            {payload.title ?? docId} · {paper ? <Link to={paper}>Open the paper</Link> : <a href={payload.paper_url}>Open the paper</a>}
          </p>
        </div>
        {b && <span className={`chip ${b.state === "approved" ? "ok" : "warn"}`}>{b.state_label ?? b.state}</span>}
      </div>
      <StatusNote result={act.data} />
      {payload.stale && <p className="notice warn">This briefing was written for an earlier version of the paper (v{b?.version}).</p>}

      {!b && (
        <div className="card">
          <p>No briefing has been generated for this paper yet.</p>
          {organizer && (
            <button type="button" disabled={act.isPending} onClick={() => act.mutate({ path: "/generate" })}>
              Generate briefing
            </button>
          )}
        </div>
      )}
      {b && !ready && (
        <div className="card">
          {b.status === "failed" ? (
            <>
              <p className="error">Generation failed{b.error ? `: ${b.error}` : "."}</p>
              {organizer && (
                <button type="button" onClick={() => act.mutate({ path: "/generate" })}>
                  Try again
                </button>
              )}
            </>
          ) : (
            <p className="muted">Generating the briefing…</p>
          )}
        </div>
      )}

      {b && ready && (
        <>
          {b.sections.map((section) => (
            <Section
              key={section.key}
              section={section}
              editable={organizer && draft}
              busy={act.isPending}
              onSave={(text) => act.mutate({ path: `/sections/${section.key}`, fields: { text } })}
            />
          ))}

          {organizer && (
            <div className="card actions">
              {draft && (
                <button type="button" disabled={act.isPending} onClick={() => act.mutate({ path: "/review" })}>
                  Mark management reviewed
                </button>
              )}
              {b.state === "management_reviewed" && (
                <>
                  <button
                    type="button"
                    disabled={act.isPending || Boolean(payload.approval_blocker)}
                    title={payload.approval_blocker ?? undefined}
                    onClick={() => act.mutate({ path: "/approve" })}
                  >
                    Approve briefing
                  </button>
                  <button
                    type="button"
                    className="secondary"
                    onClick={() => {
                      const note = window.prompt("What needs to change?");
                      if (note !== null) act.mutate({ path: "/return", fields: { note } });
                    }}
                  >
                    Return to draft
                  </button>
                </>
              )}
              {b.state === "approved" && payload.can_amend && (
                <button
                  type="button"
                  className="secondary"
                  onClick={() => {
                    const reason = window.prompt("Reason for reopening the approved briefing");
                    if (reason) act.mutate({ path: "/reopen", fields: { reason } });
                  }}
                >
                  Reopen for amendment
                </button>
              )}
              {draft && (
                <button type="button" className="link" onClick={() => act.mutate({ path: "/generate" })}>
                  Regenerate
                </button>
              )}
              {payload.approval_blocker && b.state === "management_reviewed" && <p className="muted small">{payload.approval_blocker}</p>}
            </div>
          )}
          {b.sha256 && (
            <p className="mono small">
              Approved by {b.approved_by} · SHA-256 {b.sha256}
            </p>
          )}

          {(payload.decisions?.length ?? 0) > 0 && (
            <div className="card">
              <h2>Related Board decisions</h2>
              <ul className="plain">
                {payload.decisions!.map((decision) => (
                  <li key={decision.decision_id}>
                    {decision.text}
                    <div className="muted small">
                      {decision.meeting_title} · {decision.meeting_date}
                      {decision.minutes_doc_id && (
                        <>
                          {" · "}
                          <Link to={`/view/${encodeURIComponent(decision.minutes_doc_id)}`}>minutes</Link>
                        </>
                      )}
                    </div>
                  </li>
                ))}
              </ul>
            </div>
          )}
          {(payload.references?.length ?? 0) > 0 && (
            <div className="card">
              <h2>Historical references</h2>
              <ul className="plain">
                {payload.references!.map((ref) => (
                  <li key={ref.doc_id}>
                    <Link to={appPath(ref.view_url) ?? `/view/${encodeURIComponent(ref.doc_id)}`}>{ref.title}</Link>
                    {ref.snippet && <div className="muted small">{ref.snippet}</div>}
                  </li>
                ))}
              </ul>
            </div>
          )}
        </>
      )}
    </section>
  );
}

function Section({
  section,
  editable,
  busy,
  onSave,
}: {
  section: BriefingSection;
  editable: boolean;
  busy: boolean;
  onSave: (text: string) => void;
}) {
  const [editing, setEditing] = useState(false);
  const [text, setText] = useState(section.edit_text);

  return (
    <div className="card" id={section.key}>
      <div className="row spread">
        <h2>{section.label}</h2>
        {editable && !editing && (
          <button type="button" className="link" onClick={() => setEditing(true)}>
            Edit
          </button>
        )}
      </div>
      {editing ? (
        <form
          onSubmit={(event) => {
            event.preventDefault();
            onSave(text);
            setEditing(false);
          }}
        >
          <textarea value={text} onChange={(event) => setText(event.target.value)} rows={6} lang="bn-BD" />
          <p className="muted small">One point per line, each ending with its page: Text [p. 3]. Write [not found] if the paper has nothing.</p>
          <div className="actions">
            <button type="submit" disabled={busy}>
              Save
            </button>
            <button type="button" className="link" onClick={() => setEditing(false)}>
              Cancel
            </button>
          </div>
        </form>
      ) : section.not_found || section.points.length === 0 ? (
        <p className="muted">Not found in the paper.</p>
      ) : (
        <ul>
          {section.points.map((point, index) => (
            <li key={index}>
              {point.text}{" "}
              {point.page != null &&
                (appPath(point.url) ? (
                  <Link className="cite" to={appPath(point.url)!} title={point.quote ?? undefined}>
                    p. {point.page}
                  </Link>
                ) : (
                  <a className="cite" href={point.url} title={point.quote ?? undefined}>
                    p. {point.page}
                  </a>
                ))}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
