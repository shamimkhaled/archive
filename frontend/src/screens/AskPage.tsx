import { useMutation } from "@tanstack/react-query";
import { type FormEvent } from "react";
import { Link } from "react-router";
import { postJson } from "../api/client";
import type { AskResponse } from "../api/types";
import { ErrorNote } from "../components/ErrorNote";

export function AskPage() {
  const ask = useMutation({
    mutationFn: (body: { question: string; lang: string }) => postJson<AskResponse>("/api/v1/ask", { ...body, limit: 8 }),
  });

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const question = String(form.get("question") ?? "").trim();
    if (question) ask.mutate({ question, lang: String(form.get("lang") ?? "auto") });
  }

  const result = ask.data;
  return (
    <section>
      <h1>Ask Sonali</h1>
      <p className="muted">Answers come only from archive documents you can view, with a citation for each claim.</p>
      <form className="ask-form" onSubmit={submit}>
        <textarea name="question" rows={3} placeholder="e.g. What did the Board decide on branch automation in 2024?" required />
        <div className="row">
          <select name="lang" defaultValue="auto" aria-label="Answer language">
            <option value="auto">Same language as question</option>
            <option value="en">English</option>
            <option value="bn">বাংলা</option>
          </select>
          <button type="submit" disabled={ask.isPending}>{ask.isPending ? "Thinking…" : "Ask"}</button>
        </div>
      </form>

      {ask.error && <ErrorNote error={ask.error} />}
      {result && result.refused && (
        <div className="card notice">
          <strong>No grounded answer.</strong> {result.refusal_reason ?? "The archive does not contain enough evidence."}
        </div>
      )}
      {result && !result.refused && (
        <article className="card answer">
          <p className="answer-text">{result.answer}</p>
          {result.citations.length > 0 && (
            <>
              <h2>Sources</h2>
              <ol className="citations">
                {result.citations.map((citation, index) => (
                  <li key={`${citation.doc_id}-${citation.page}-${index}`}>
                    <Link to={`/view/${encodeURIComponent(citation.doc_id)}${citation.page ? `?page=${citation.page}` : ""}`}>
                      {citation.doc_id}
                      {citation.page ? `, page ${citation.page}` : ""}
                    </Link>
                    {citation.quote && <blockquote>{citation.quote}</blockquote>}
                  </li>
                ))}
              </ol>
            </>
          )}
          <p className="muted small">
            {result.sources_considered} passages considered
            {result.answered_by?.model ? ` · ${result.answered_by.provider}/${result.answered_by.model}` : ""}
          </p>
        </article>
      )}
    </section>
  );
}
