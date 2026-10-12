import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router";
import { api, data } from "../api/client";
import type { MeetingSummary } from "../api/types";
import { ErrorNote } from "../components/ErrorNote";
import { useSession } from "../session";

export function MeetingsPage() {
  const me = useSession();
  const meetings = useQuery({
    queryKey: ["meetings"],
    queryFn: () => data<{ meetings: MeetingSummary[] }>(api.GET("/api/v1/meetings", { params: { query: { limit: 50 } } })),
  });

  return (
    <section>
      <div className="row spread">
        <h1>Board meetings</h1>
        {me.permissions.organize_meetings && (
          <a className="button" href="/meetings?classic=1">
            Schedule a meeting
          </a>
        )}
      </div>
      {meetings.isPending && <p className="muted">Loading…</p>}
      {meetings.error && <ErrorNote error={meetings.error} />}
      {meetings.data && meetings.data.meetings.length === 0 && <p className="card">No meetings yet.</p>}
      {meetings.data && meetings.data.meetings.length > 0 && (
        <ul className="cards">
          {meetings.data.meetings.map((meeting) => (
            <li key={meeting.id} className="card">
              <Link to={`/meetings/${meeting.id}`}>{meeting.title}</Link>
              <p className="muted small">
                {meeting.scheduled_at?.slice(0, 16).replace("T", " ")}
                {meeting.location ? ` · ${meeting.location}` : ""} · {meeting.status}
              </p>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
