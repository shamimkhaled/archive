import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api, data, postForm } from "../api/client";
import type { AiOverview, ChainVerification, QueueOverview, SecurityStatus } from "../api/types";
import { ErrorNote } from "../components/ErrorNote";
import { useSession } from "../session";

export function AdminPage() {
  const me = useSession();
  if (!me.permissions.admin) return <p className="error">Administrators only.</p>;
  return (
    <section>
      <h1>Administration</h1>
      <p className="muted">
        Users, access requests, and departments stay in the <a href="/admin/users">classic admin pages</a>.
      </p>
      <Queues />
      <AiRoutes />
      <Security />
    </section>
  );
}

function Queues() {
  const queryClient = useQueryClient();
  const queues = useQuery({
    queryKey: ["admin-queues"],
    queryFn: () => data<QueueOverview>(api.GET("/api/admin/queues")),
    refetchInterval: 15_000,
  });
  const retry = useMutation({
    mutationFn: (entryId: string) => postForm(`/admin/queues/dead/${encodeURIComponent(entryId)}/retry`),
    onSettled: () => void queryClient.invalidateQueries({ queryKey: ["admin-queues"] }),
  });

  return (
    <div className="card">
      <h2>Job queues</h2>
      {queues.error && <ErrorNote error={queues.error} />}
      {queues.data && (
        <>
          {!queues.data.redis_ok && <p className="error">Redis is unreachable; stream statistics are unavailable.</p>}
          <table className="table">
            <thead>
              <tr>
                <th>Stream</th>
                <th>Length</th>
                <th>Pending</th>
                <th>Lag</th>
                <th>Consumers</th>
              </tr>
            </thead>
            <tbody>
              {Object.entries(queues.data.streams).map(([name, row]) => (
                <tr key={name}>
                  <td className="mono">{name}</td>
                  <td>{row.length}</td>
                  <td>{row.pending ?? "—"}</td>
                  <td>{row.lag ?? "—"}</td>
                  <td>{row.consumers ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="muted small">
            Outbox unpublished: {queues.data.outbox_unpublished} · Jobs:{" "}
            {Object.entries(queues.data.job_status_counts)
              .map(([k, v]) => `${k} ${v}`)
              .join(", ") || "none"}{" "}
            · Ingest:{" "}
            {Object.entries(queues.data.ingest_counts)
              .map(([k, v]) => `${k} ${v}`)
              .join(", ")}
          </p>
          {queues.data.dead_letters.length > 0 && (
            <>
              <h3>Dead letters</h3>
              <table className="table">
                <tbody>
                  {queues.data.dead_letters.map((entry) => (
                    <tr key={entry.entry_id}>
                      <td className="mono small">{entry.type}</td>
                      <td>{entry.reason}</td>
                      <td className="small">{entry.error?.slice(0, 160)}</td>
                      <td>
                        <button type="button" className="link" disabled={retry.isPending} onClick={() => retry.mutate(entry.entry_id)}>
                          Retry
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </>
          )}
        </>
      )}
    </div>
  );
}

function AiRoutes() {
  const queryClient = useQueryClient();
  const ai = useQuery({ queryKey: ["admin-ai"], queryFn: () => data<AiOverview>(api.GET("/api/admin/ai")) });
  const [task, setTask] = useState("chat.ask");
  const change = useMutation({
    mutationFn: ({ path, fields }: { path: string; fields?: Record<string, string> }) => postForm(path, fields),
    onSettled: () => void queryClient.invalidateQueries({ queryKey: ["admin-ai"] }),
  });

  return (
    <div className="card">
      <h2>AI model routes</h2>
      {ai.error && <ErrorNote error={ai.error} />}
      {change.data && !change.data.ok && <p className="error">{change.data.error}</p>}
      {change.data?.ok && change.data.status === "pending" && (
        <p className="muted">Change recorded. It takes effect when a second administrator approves it.</p>
      )}
      {ai.data && (
        <>
          {ai.data.changes.some((c) => c.status === "pending") && (
            <>
              <h3>Awaiting a second administrator</h3>
              <ul className="plain">
                {ai.data.changes
                  .filter((c) => c.status === "pending")
                  .map((c) => (
                    <li key={c.id}>
                      <span className="mono">{c.summary}</span> requested by {c.requested_by}{" "}
                      <button type="button" className="link" onClick={() => change.mutate({ path: `/admin/ai/changes/${c.id}/approve` })}>
                        Approve
                      </button>{" "}
                      <button type="button" className="link" onClick={() => change.mutate({ path: `/admin/ai/changes/${c.id}/reject` })}>
                        Reject
                      </button>
                    </li>
                  ))}
              </ul>
            </>
          )}
          <table className="table">
            <thead>
              <tr>
                <th>Task</th>
                <th>Provider / model</th>
                <th>Source</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {ai.data.routes.map((route) => (
                <tr key={route.task}>
                  <td className="mono">{route.task}</td>
                  <td>
                    {route.provider} / {route.model} {route.cloud && <span className="chip">cloud</span>}
                    {route.restricted_needs_local && <span className="chip warn">restricted stays local</span>}
                  </td>
                  <td>{route.source}</td>
                  <td>
                    {route.source !== "env" && ai.data.tasks.includes(route.task) && (
                      <button type="button" className="link" onClick={() => change.mutate({ path: `/admin/ai/route/${route.task}/reset` })}>
                        Reset to env
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <form
            className="inline-form"
            onSubmit={(event) => {
              event.preventDefault();
              const fields: Record<string, string> = {};
              for (const [k, v] of new FormData(event.currentTarget).entries()) if (typeof v === "string") fields[k] = v;
              change.mutate({ path: "/admin/ai/route", fields });
            }}
          >
            <select name="task" aria-label="Task" value={task} onChange={(event) => setTask(event.target.value)}>
              {ai.data.tasks.map((name) => (
                <option key={name}>{name}</option>
              ))}
            </select>
            <select name="provider" aria-label="Provider" key={task}>
              {(ai.data.providers[task === "rerank" || task === "stt" ? task : "chat"] ?? []).map((provider) => (
                <option key={provider} value={provider}>
                  {provider}
                  {ai.data.configured[provider] === false ? " (not configured)" : ""}
                </option>
              ))}
            </select>
            <input name="model" placeholder="Model" required />
            <input name="notes" placeholder="Reason" />
            <button type="submit" disabled={change.isPending}>
              Switch
            </button>
          </form>
          <p className="muted small">Embedding routes change only through a reindex behind the collection alias.</p>
          <p>
            Speaker identification:{" "}
            {ai.data.diarization.enabled ? (
              <>
                <span className="mono">{ai.data.diarization.provider}</span>
                {ai.data.diarization.detail && <span className="mono"> · {ai.data.diarization.detail}</span>}{" "}
                <span className={ai.data.diarization.ready ? "chip" : "chip warn"}>{ai.data.diarization.ready ? "configured" : "needs attention"}</span>
              </>
            ) : (
              <span className="muted">off (DIARIZATION_PROVIDER=none)</span>
            )}
          </p>
          {ai.data.diarization.issues.map((issue) => (
            <p key={issue} className="notice warn">
              {issue}
            </p>
          ))}
        </>
      )}
    </div>
  );
}

function Security() {
  const status = useQuery({ queryKey: ["admin-security"], queryFn: () => data<SecurityStatus>(api.GET("/api/admin/security/status")) });
  const verify = useMutation({
    mutationFn: () => data<ChainVerification>(api.POST("/api/admin/audit/verify")),
  });

  return (
    <div className="card">
      <h2>Audit and identity</h2>
      {status.error && <ErrorNote error={status.error} />}
      {status.data && (
        <ul className="plain">
          <li>
            Audit rows: {status.data.audit_rows} ({status.data.unsealed_rows} awaiting seal)
          </li>
          <li>Chain tip: #{status.data.chain?.last_id ?? "—"}</li>
          <li>
            SIEM forwarding: {status.data.siem.enabled ? `on, at #${status.data.siem.cursor?.last_id ?? 0}` : "off"}
          </li>
          <li className="mono small">{JSON.stringify(status.data.identity)}</li>
        </ul>
      )}
      <button type="button" disabled={verify.isPending} onClick={() => verify.mutate()}>
        {verify.isPending ? "Verifying…" : "Verify audit hash chain"}
      </button>
      {verify.error && <ErrorNote error={verify.error} />}
      {verify.data && (
        <p className={verify.data.ok ? "notice" : "error"}>
          {verify.data.ok
            ? `Chain intact: ${verify.data.linked ?? verify.data.sealed} rows linked.`
            : `Chain broken at #${verify.data.broken_at}: ${verify.data.reason}`}
        </p>
      )}
    </div>
  );
}
