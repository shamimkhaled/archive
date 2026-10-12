import { NavLink, Outlet } from "react-router";
import { SessionContext, useSessionQuery } from "../session";
import { UpdatePrompt } from "./UpdatePrompt";

const ROLE_LABELS: Record<string, string> = {
  admin: "Administrator",
  board_secretary: "Board Secretary",
  board_member: "Board Member",
  uploader: "Uploader",
};

export function Layout() {
  const session = useSessionQuery();

  if (session.isPending) return <div className="boot">Loading…</div>;
  if (session.isError || !session.data) {
    return (
      <div className="boot">
        <p>Could not load your session.</p>
        <a href="/login">Sign in again</a>
      </div>
    );
  }
  const me = session.data;
  const can = me.permissions;

  return (
    <SessionContext.Provider value={me}>
      <div className="shell">
        <header className="topbar">
          <a className="brand" href="/">
            <img src="/static/img/sonali-bank-logo.png" alt="" width={28} height={28} />
            <span>Sonali Bank Archive</span>
          </a>
          <nav className="nav" aria-label="Main">
            {can.search && <NavLink to="/search">Search</NavLink>}
            {can.search && <NavLink to="/ask">Ask Sonali</NavLink>}
            {can.review && <NavLink to="/review">Verify OCR</NavLink>}
            <NavLink to="/meetings">Meetings</NavLink>
            {can.admin && <NavLink to="/admin">Admin</NavLink>}
          </nav>
          <div className="me">
            <span title={me.username}>{ROLE_LABELS[me.role] ?? me.role}</span>
            <a href="/?classic=1" className="muted">Classic view</a>
            <a href="/logout">Sign out</a>
          </div>
        </header>
        <main className="content">
          <Outlet />
        </main>
        <UpdatePrompt />
      </div>
    </SessionContext.Provider>
  );
}
