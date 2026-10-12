import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type FormEvent } from "react";
import { Link, useParams } from "react-router";
import { api, data, postForm } from "../api/client";
import type { MeetingDetail, MinutesWorkflow } from "../api/types";
import { ErrorNote } from "../components/ErrorNote";
import { StatusNote } from "../components/StatusNote";

const STATE_LABELS: Record<string, string> = { draft: "Draft", in_review: "Awaiting approval", approved: "Approved" };
const ACTION_STATUSES = ["open", "in_progress", "done", "cancelled"];

function formFields(event: FormEvent<HTMLFormElement>): Record<string, string> {
  event.preventDefault();
  const fields: Record<string, string> = {};
  for (const [key, value] of new FormData(event.currentTarget).entries()) if (typeof value === "string") fields[key] = value;
  return fields;
}

export function MeetingDetailPage() {
  const { meetingId = "" } = useParams();
  const id = Number(meetingId);
  const queryClient = useQueryClient();

  const meeting = useQuery({
    queryKey: ["meeting", id],
    queryFn: () => data<MeetingDetail>(api.GET("/api/app/meetings/{meeting_id}", { params: { path: { meeting_id: id } } })),
    refetchInterval: (query) => {
      const minutes = query.state.data?.minutes;
      return minutes && "generating" in minutes && minutes.generating ? 5000 : false;
    },
  });
  const workflow = useQuery({
    queryKey: ["minutes-workflow", id],
    enabled: Boolean(meeting.data?.is_organizer),
    queryFn: () =>
      data<MinutesWorkflow>(api.GET("/api/meetings/{meeting_id}/minutes/workflow", { params: { path: { meeting_id: id } } })),
  });

  const act = useMutation({
    mutationFn: ({ path, fields }: { path: string; fields?: Record<string, string> }) => postForm(`/meetings/${id}${path}`, fields),
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: ["meeting", id] });
      void queryClient.invalidateQueries({ queryKey: ["minutes-workflow", id] });
    },
  });

  if (meeting.isPending) return <p className="muted">Loading…</p>;
  if (meeting.error) return <ErrorNote error={meeting.error} />;
  const m = meeting.data;
  const minutes = m.minutes;
  const flow = workflow.data?.workflow;
  const editable = m.is_organizer && flow?.state === "draft";

  return (
    <section>
      <div className="row spread">
        <div>
          <h1>{m.title}</h1>
          <p className="muted">
            {m.scheduled_at?.slice(0, 16).replace("T", " ")}
            {m.location ? ` · ${m.location}` : ""} · {m.status}
          </p>
        </div>
        <a href={m.classic_url} className="muted">
          {m.is_organizer ? "Invitations, attendance, and recording" : "Sign attendance"}
        </a>
      </div>
      <StatusNote result={act.data} />

      {m.agenda_items.length > 0 && (
        <div className="card">
          <h2>Agenda</h2>
          <ol>
            {m.agenda_items.map((item, index) => (
              <li key={index}>{item}</li>
            ))}
          </ol>
        </div>
      )}

      <div className="card">
        <h2>Board papers</h2>
        {m.documents.length === 0 && <p className="muted">No papers uploaded.</p>}
        <ul className="plain">
          {m.documents.map((doc) => (
            <li key={doc.id}>
              {doc.archive_doc_id && doc.archive_status === "indexed" ? (
                <Link to={`/view/${encodeURIComponent(doc.archive_doc_id)}`}>{doc.title}</Link>
              ) : (
                <a href={doc.file_url.replace(/\/file$/, "/view")}>{doc.title}</a>
              )}
              {doc.archive_status && doc.archive_status !== "indexed" && <span className="chip">{doc.archive_status}</span>}
              {doc.archive_doc_id && (doc.briefed || m.is_organizer) && (
                <Link className="small" to={`/briefing/${encodeURIComponent(doc.archive_doc_id)}`}>
                  {doc.briefed ? "Approved briefing" : "Briefing"}
                </Link>
              )}
            </li>
          ))}
        </ul>
      </div>

      <div className="card" id="minutes">
        <h2>
          Minutes <span className="chip">{STATE_LABELS[flow?.state ?? ("state" in minutes ? minutes.state : "draft")] ?? "Draft"}</span>
        </h2>
        {!m.is_organizer &&
          ("doc_id" in minutes && minutes.doc_id ? (
            <Link to={`/view/${encodeURIComponent(minutes.doc_id)}`}>Read the approved minutes</Link>
          ) : (
            <p className="muted">Minutes are shared here once the Board Secretary approves them.</p>
          ))}

        {m.is_organizer && "body_md" in minutes && (
          <>
            {flow?.review_note && <p className="notice warn">Returned: {flow.review_note}</p>}
            {minutes.generating && <p className="muted">Drafting minutes from the transcript…</p>}
            <form onSubmit={(event) => act.mutate({ path: "/minutes", fields: formFields(event) })}>
              <textarea name="body_md" rows={16} defaultValue={minutes.body_md ?? ""} key={`${flow?.version}-${minutes.body_md?.length}`} readOnly={!editable} lang="bn-BD" />
              <div className="actions">
                {editable && (
                  <>
                    <button type="submit" disabled={act.isPending}>
                      Save draft
                    </button>
                    <button type="button" className="secondary" disabled={act.isPending} onClick={() => act.mutate({ path: "/minutes/regenerate" })}>
                      Redraft from transcript
                    </button>
                    <button type="button" disabled={act.isPending} onClick={() => act.mutate({ path: "/minutes/submit" })}>
                      Submit for approval
                    </button>
                  </>
                )}
                {flow?.state === "in_review" && (
                  <>
                    <button
                      type="button"
                      disabled={act.isPending || Boolean(flow.approve_blocker)}
                      title={flow.approve_blocker ?? undefined}
                      onClick={() => act.mutate({ path: "/minutes/approve" })}
                    >
                      Approve and archive
                    </button>
                    <button
                      type="button"
                      className="secondary"
                      disabled={act.isPending}
                      onClick={() => {
                        const note = window.prompt("Why are you returning the minutes?");
                        if (note !== null) act.mutate({ path: "/minutes/reject", fields: { note } });
                      }}
                    >
                      Return to draft
                    </button>
                  </>
                )}
                {flow?.state === "approved" && flow.doc_id && (
                  <Link to={`/view/${encodeURIComponent(flow.doc_id)}`}>Archived minutes</Link>
                )}
              </div>
              {flow?.approve_blocker && flow.state === "in_review" && <p className="muted small">{flow.approve_blocker}</p>}
              {flow?.sha256 && <p className="mono small">SHA-256 {flow.sha256}</p>}
            </form>
          </>
        )}
      </div>

      {m.is_organizer && workflow.data && (
        <div className="card">
          <h2>Decisions</h2>
          <ol>
            {workflow.data.decisions.map((decision) => (
              <li key={decision.id}>
                {decision.agenda_item && <strong>{decision.agenda_item}: </strong>}
                {decision.text}
                {editable && (
                  <button type="button" className="link" onClick={() => act.mutate({ path: `/decisions/${decision.id}/delete` })}>
                    Remove
                  </button>
                )}
              </li>
            ))}
          </ol>
          {editable && (
            <form className="inline-form" onSubmit={(event) => act.mutate({ path: "/decisions", fields: formFields(event) })}>
              <input name="agenda_item" placeholder="Agenda item (optional)" />
              <input name="text" placeholder="Decision" required minLength={3} />
              <button type="submit">Add decision</button>
            </form>
          )}

          <h2>Actions</h2>
          <table className="table">
            <tbody>
              {workflow.data.actions.map((action) => (
                <tr key={action.id}>
                  <td>{action.description}</td>
                  <td>{action.owner ?? "—"}</td>
                  <td>{action.due_date ?? "—"}</td>
                  <td>
                    <select
                      value={action.status}
                      onChange={(event) => act.mutate({ path: `/actions/${action.id}/status`, fields: { status: event.target.value } })}
                      aria-label="Action status"
                    >
                      {ACTION_STATUSES.map((s) => (
                        <option key={s} value={s}>
                          {s.replace("_", " ")}
                        </option>
                      ))}
                    </select>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {editable && (
            <form className="inline-form" onSubmit={(event) => act.mutate({ path: "/actions", fields: formFields(event) })}>
              <input name="description" placeholder="Action" required minLength={3} />
              <input name="owner" placeholder="Owner" />
              <input name="due_date" type="date" />
              <button type="submit">Add action</button>
            </form>
          )}
        </div>
      )}
    </section>
  );
}
