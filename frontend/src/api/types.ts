// Response bodies of the JSON routes the screens use. FastAPI declares most of these as plain
// dicts, so the generated schema types the paths and parameters while these types cover the bodies.

export interface Session {
  username: string;
  role: "admin" | "board_secretary" | "board_member" | "uploader";
  permissions: { search: boolean; review: boolean; organize_meetings: boolean; admin: boolean };
  screens: string[];
}

export interface SearchResult {
  doc_id: string;
  title?: string | null;
  doc_type?: string | null;
  doc_date?: string | null;
  snippet?: string;
  page?: number | null;
  score?: number;
  source?: string;
  match_reasons?: string[];
  can_view: boolean;
  can_download: boolean;
  access_status: "privileged" | "approved_download" | "approved_view" | "pending" | "none";
  department?: string | null;
  reference_number?: string | null;
}

export interface SearchResponse {
  results: SearchResult[];
  count: number;
  query: string;
  mode?: string;
  degraded?: boolean;
  reranked?: boolean;
}

export interface Citation {
  doc_id: string;
  page: number | null;
  quote?: string;
  url?: string;
}

export interface AskResponse {
  refused: boolean;
  refusal_reason: string | null;
  answer: string | null;
  citations: Citation[];
  sources_considered: number;
  evidence?: { doc_id: string; page: number | null; doc_type?: string; excerpt: string; url: string }[];
  answered_by?: { provider?: string; model?: string };
}

export interface DocumentInfo {
  doc_id: string;
  title: string | null;
  doc_type: string;
  doc_date: string | null;
  department: string | null;
  reference_number: string | null;
  page_count: number | null;
  current_version: number;
  provenance: string | null;
  metadata_status: "confirmed" | "ai_unconfirmed" | null;
  seal_text: string;
  file_url: string;
  download_url: string | null;
  can_see_versions: boolean;
  has_briefing: boolean;
}

export interface RelatedDocuments {
  related: { doc_id: string; doc_type: string; doc_date: string; score: number; reasons: string[] }[];
}

export interface DocumentVersions {
  current_version: number;
  versions: {
    version: number;
    status: string;
    is_current: boolean;
    sha256: string;
    size_bytes: number | null;
    note: string | null;
    created_by: string | null;
    created_at: string | null;
    view_url: string;
  }[];
}

export interface MeetingDetail {
  id: number;
  title: string;
  scheduled_at: string | null;
  location: string | null;
  status: string;
  attendance_open: boolean;
  agenda_items: string[];
  is_organizer: boolean;
  classic_url: string;
  documents: {
    id: number;
    title: string;
    uploaded_at: string | null;
    archive_doc_id: string | null;
    archive_status: string | null;
    briefed: boolean;
    file_url: string;
  }[];
  minutes: (MinutesView & { workflow?: MinutesWorkflow["workflow"]; generating?: boolean }) | { state: string; doc_id: string | null };
  records: MeetingRecordItem[];
}

export interface MeetingRecordItem {
  id: number;
  kind: "recording" | "transcript" | "minutes_draft" | "minutes_approved" | string;
  label: string;
  version: number;
  sha256: string;
  size_bytes: number | null;
  content_type: string | null;
  filename: string | null;
  archive_doc_id: string | null;
  created_by: string | null;
  created_at: string | null;
  download_url: string;
  view_url: string | null;
}

export interface ReviewItem {
  doc_id: string;
  version: number;
  doc_type: string;
  title: string | null;
  sensitivity: string;
  ocr_confidence: number | null;
  review_reasons: string[];
  page_count: number | null;
  uploaded_by: string | null;
  uploaded_at: string | null;
  is_new_version: boolean;
}

export interface ReviewDetail {
  doc_id: string;
  version: number;
  status: string;
  doc_type: string;
  sensitivity: string;
  ocr_confidence: number | null;
  uploaded_by: string | null;
  fields: string[];
  metadata: Record<string, string>;
  suggested_source?: string | null;
  suggested: { doc_type?: string; doc_date?: string; keywords?: string[] };
  review_reasons: string[];
  pages: { page: number; text: string; ocr_confidence: number | null; source: string }[];
  four_eyes_problem: string | null;
  can_act: boolean;
}

export interface MeetingSummary {
  id: number;
  title: string;
  scheduled_at: string | null;
  location: string | null;
  status: string;
}

export interface MinutesWorkflow {
  workflow: {
    state: "draft" | "in_review" | "approved" | string;
    version: number;
    locked: boolean;
    four_eyes: boolean;
    edited_by: string | null;
    submitted_by: string | null;
    approved_by: string | null;
    approved_at: string | null;
    review_note: string | null;
    sha256: string | null;
    doc_id: string | null;
    approve_blocker: string | null;
  };
  decisions: { id: number; seq: number | null; agenda_item: string | null; text: string; source: string | null }[];
  actions: {
    id: number;
    decision_id: number | null;
    description: string;
    owner: string | null;
    division: string | null;
    due_date: string | null;
    status: string;
    source: string | null;
  }[];
}

export interface MinutesView {
  status: string;
  body_md?: string | null;
  generated_at?: string | null;
  source?: string | null;
}

export interface BriefingPoint {
  text: string;
  page: number | null;
  quote?: string | null;
  url?: string;
}

export interface BriefingSection {
  key: string;
  label: string;
  points: BriefingPoint[];
  not_found?: boolean;
  edit_text: string;
}

export interface BriefingPayload {
  briefing: null | {
    id: number;
    version: number;
    state: string;
    state_label?: string;
    status: string;
    error?: string | null;
    sections: BriefingSection[];
    sha256?: string | null;
    approved_by?: string | null;
    approved_at?: string | null;
    reviewed_by?: string | null;
    review_note?: string | null;
    [k: string]: unknown;
  };
  current_version: number;
  stale?: boolean;
  paper_url?: string;
  title?: string;
  doc_type?: string;
  is_organizer?: boolean;
  can_amend?: boolean;
  approval_blocker?: string | null;
  four_eyes?: boolean;
  references?: { doc_id: string; title: string; doc_type: string | null; page: number | null; snippet: string | null; view_url: string }[];
  decisions?: {
    decision_id: number;
    meeting_id: number;
    meeting_title: string;
    meeting_date: string | null;
    agenda_item: string | null;
    text: string;
    minutes_doc_id: string | null;
    actions: { description: string; owner: string | null; division?: string | null; due_date: string | null; status: string }[];
  }[];
}

export interface QueueOverview {
  redis_ok: boolean;
  streams: Record<string, { length: number; pending?: number; lag?: number | null; consumers?: number }>;
  dead_letters: {
    entry_id: string;
    job_id: string | null;
    type: string | null;
    attempts: number | null;
    reason: string | null;
    error: string | null;
    source_stream: string | null;
    dead_at: string | null;
  }[];
  job_status_counts: Record<string, number>;
  outbox_unpublished: number;
  ingest_counts: Record<string, number>;
  recent_failures: { id: number; job_type: string; status: string; attempts: number; resource: string; last_error: string; updated_at: string | null }[];
}

export interface SecurityStatus {
  audit_rows: number;
  unsealed_rows: number;
  oldest_unsealed_at: string | null;
  chain: { last_id: number; updated_at: string | null } | null;
  siem: { enabled: boolean; cursor: { last_id: number; updated_at: string | null } | null };
  identity: Record<string, unknown>;
}

export interface AiRoute {
  task: string;
  provider: string;
  model: string;
  source: string;
  cloud: boolean;
  locked?: boolean;
  restricted_needs_local?: boolean;
  env: { provider: string; model: string } | null;
}

export interface AiRouteChange {
  id: number;
  kind: "switch" | "reset" | "rollback";
  task: string;
  summary: string;
  notes: string | null;
  status: "pending" | "applied" | "rejected" | "cancelled";
  requested_by: string;
  requested_at: string | null;
  reviewed_by: string | null;
  review_note: string | null;
}

export interface AiOverview {
  four_eyes: boolean;
  changes: AiRouteChange[];
  egress_policy: Record<string, unknown> | string;
  routes: AiRoute[];
  tasks: string[];
  providers: Record<string, string[]>;
  configured: Record<string, boolean>;
  diarization: { provider: string; enabled: boolean; detail: string | null; ready: boolean; issues: string[] };
  history: {
    id: number;
    task: string;
    provider: string;
    model: string;
    is_active: boolean;
    notes: string | null;
    activated_by: string | null;
    activated_at: string | null;
  }[];
}

export interface ChainVerification {
  ok: boolean;
  sealed?: number;
  linked?: number;
  tip?: string;
  broken_at?: number | null;
  reason?: string;
}
