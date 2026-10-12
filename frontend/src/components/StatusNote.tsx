import type { FormResult } from "../api/client";

const MESSAGES: Record<string, string> = {
  minutes_saved: "Minutes saved.",
  minutes_generating: "Drafting minutes from the transcript…",
  minutes_no_transcript: "There is no transcript to draft from yet.",
  minutes_submitted: "Submitted for approval.",
  minutes_returned: "Returned to draft.",
  minutes_approved: "Approved and archived as a locked PDF.",
  minutes_amending: "Reopened for amendment.",
  minutes_archive_failed: "Approval stopped: the minutes could not be archived. Try again.",
  minutes_locked: "The minutes are locked in their current state.",
  minutes_four_eyes: "Someone other than the editor or submitter must approve.",
  minutes_input_required: "Please fill in the required text.",
  decision_saved: "Decision saved.",
  decision_deleted: "Decision removed.",
  action_saved: "Action saved.",
  action_deleted: "Action removed.",
  items_extracted: "Decisions and actions extracted from the minutes.",
  briefing_generating: "Generating the briefing; this page refreshes when it is ready.",
  briefing_saved: "Section saved.",
  briefing_unchanged: "No changes to save.",
  briefing_reviewed: "Marked as management reviewed.",
  briefing_approved: "Briefing approved and locked.",
  briefing_returned: "Returned to draft.",
  briefing_reopened: "Reopened for amendment.",
  briefing_locked: "The briefing is locked in its current state.",
  briefing_four_eyes: "Someone other than the editor or reviewer must approve.",
  briefing_input: "Each point needs its page citation, for example: Text [p. 3].",
  briefing_failed: "Generation failed; try again.",
};

export function describeStatus(code: string | null | undefined): string | null {
  if (!code) return null;
  return MESSAGES[code] ?? code.replace(/_/g, " ");
}

export function StatusNote({ result }: { result: FormResult | undefined }) {
  if (!result) return null;
  const text = result.error ?? describeStatus(result.status);
  if (!text) return null;
  return (
    <p className={result.ok ? "notice" : "error"} role="status">
      {text}
    </p>
  );
}
