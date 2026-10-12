import { lazy, Suspense } from "react";
import { createBrowserRouter, Navigate } from "react-router";
import { Layout } from "./components/Layout";
import { AdminPage } from "./screens/AdminPage";
import { AskPage } from "./screens/AskPage";
import { BriefingPage } from "./screens/BriefingPage";
import { MeetingDetailPage } from "./screens/MeetingDetailPage";
import { MeetingsPage } from "./screens/MeetingsPage";
import { ReviewDetailPage } from "./screens/ReviewDetailPage";
import { ReviewQueuePage } from "./screens/ReviewQueuePage";
import { SearchPage } from "./screens/SearchPage";

// pdf.js is most of the bundle; only the viewer needs it.
const ViewerPage = lazy(() => import("./screens/ViewerPage").then((m) => ({ default: m.ViewerPage })));

const viewer = (
  <Suspense fallback={<p className="muted">Loading viewer…</p>}>
    <ViewerPage />
  </Suspense>
);

export const router = createBrowserRouter(
  [
    {
      path: "/",
      element: <Layout />,
      children: [
        { index: true, element: <Navigate to="/search" replace /> },
        { path: "search", element: <SearchPage /> },
        { path: "ask", element: <AskPage /> },
        { path: "view/:docId", element: viewer },
        { path: "review", element: <ReviewQueuePage /> },
        { path: "review/:docId", element: <ReviewDetailPage /> },
        { path: "meetings", element: <MeetingsPage /> },
        { path: "meetings/:meetingId", element: <MeetingDetailPage /> },
        { path: "briefing/:docId", element: <BriefingPage /> },
        { path: "admin", element: <AdminPage /> },
        { path: "*", element: <Navigate to="/search" replace /> },
      ],
    },
  ],
  { basename: "/app" },
);
