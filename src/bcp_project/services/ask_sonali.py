"""Ask Sonali Bank — governed Q&A with mandatory citations (R7)."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from .. import ai
from ..ai.guard import with_rules, wrap_untrusted
from ..access_control import get_sensitivity_for_docs, write_audit
from ..observability import Timer, incr
from ..policy import Principal, Scope, require_scope
from ..ports.adapters import get_llm_client
from . import document_query
from .search import citation_url, grounded_passages

logger = logging.getLogger("bcp_project.services.ask_sonali")

PASSAGES = 8
PASSAGE_CHARS = 1200

SYSTEM_PROMPT = """You are Ask Sonali Bank, a governed institutional-memory assistant.
Rules (non-negotiable):
1. Answer ONLY using the provided SOURCE excerpts.
2. Every substantive claim must cite at least one source as {doc_id, page}.
3. If sources are insufficient, set refused=true and do not invent facts.
4. Never claim to be the final authority; humans decide.
5. Return ONLY valid JSON matching the schema.
6. Write the answer in the language of the question (Bangla or English); keep quotes in the source's language.
Schema:
{
  "refused": false,
  "refusal_reason": null,
  "answer": "markdown answer",
  "citations": [{"doc_id": "SB-...", "page": 1, "quote": "short quote"}]
}
page may be null when unknown. quote must be short (<=160 chars) from the source text.
"""


def _evidence_rows(search_payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows = []
    for item in search_payload.get("results") or []:
        if not item.get("can_view"):
            continue
        rows.append(
            {
                "doc_id": item.get("doc_id"),
                "doc_type": item.get("doc_type"),
                "snippet": (item.get("snippet") or "")[:400],
                "page": item.get("page"),
                "match_label": item.get("match_label") or item.get("relevance_label"),
            }
        )
    return rows


async def ask_sonali_for_principal(
    db: AsyncSession,
    principal: Principal,
    question: str,
    *,
    lang: Optional[str] = None,
    limit: int = 8,
    ip_address: Optional[str] = None,
    tool_name: Optional[str] = None,
) -> Dict[str, Any]:
    require_scope(principal, Scope.ask_sonali)
    q = (question or "").strip()
    if not q:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="question required")

    with Timer("ask_sonali"):
        search = await document_query.search_documents_for_principal(
            db,
            principal,
            q,
            lang=lang,
            limit=limit,
            ip_address=ip_address,
            tool_name=tool_name or "ask_sonali",
        )
        evidence = _evidence_rows(search)
        levels = await get_sensitivity_for_docs(db, {str(r.get("doc_id") or "") for r in evidence})
        held_back = [r for r in evidence if not ai.chat_allowed("ask", levels.get(str(r.get("doc_id"))))]
        if held_back:
            incr("ask_sonali.evidence_withheld_egress")
            evidence = [r for r in evidence if r not in held_back]
        if not evidence:
            incr("ask_sonali_answers", labels={"outcome": "no_source"})
            payload = {
                "refused": True,
                "refusal_reason": (
                    "No Source, No Answer — the matching documents are classified for on-premise AI only."
                    if held_back
                    else "No Source, No Answer — no authorized archive evidence matched this question."
                ),
                "answer": None,
                "citations": [],
                "sources_considered": 0,
                "query": q,
            }
            await write_audit(
                db,
                username=principal.username_for_grants or principal.actor_id,
                action="ask_sonali",
                resource_type="ai",
                detail=f"refused=no_source q={q[:120]}",
                ip_address=ip_address,
                actor_type=principal.actor_type,
                tool_name=tool_name or "ask_sonali",
                commit=True,
            )
            return payload

        acl = await document_query.acl_scope_for_principal(db, principal)
        passages = await grounded_passages(
            acl, q, [str(r["doc_id"]) for r in evidence if r.get("doc_id")], k=PASSAGES, levels=levels
        )
        types = {str(r.get("doc_id")): r.get("doc_type") for r in evidence}
        sources = (
            [
                {"doc_id": p["doc_id"], "page": p.get("page"), "doc_type": types.get(p["doc_id"]), "text": p["text"][:PASSAGE_CHARS]}
                for p in passages
                if p.get("text")
            ]
            or [{**r, "text": r.get("snippet") or ""} for r in evidence]
        )
        sources_block = []
        for idx, row in enumerate(sources, start=1):
            sources_block.append(
                f"[{idx}] doc_id={row['doc_id']} page={row.get('page')} type={row.get('doc_type')}\n"
                f"{wrap_untrusted(row.get('text') or '', 'archive_excerpt')}"
            )
        user_prompt = (
            f"Question:\n{q}\n\nSOURCE excerpts (authorized only):\n"
            + "\n\n".join(sources_block)
            + "\n\nRespond with JSON only."
        )

        try:
            llm = get_llm_client()
            model_out = llm.chat_json(
                system=with_rules(SYSTEM_PROMPT),
                user=user_prompt,
                temperature=0.0,
                task="ask",
                sensitivity=ai.max_sensitivity(levels.get(str(r.get("doc_id"))) for r in evidence),
            )
            call = ai.last_call() or {}
        except Exception as exc:
            logger.exception("Ask Sonali LLM failed")
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Ask Sonali model unavailable: {str(exc)[:160]}",
            ) from exc

        refused = bool(model_out.get("refused"))
        citations_raw = model_out.get("citations") or []
        allowed_ids = {str(r["doc_id"]) for r in sources if r.get("doc_id")}
        citations: List[Dict[str, Any]] = []
        for cite in citations_raw:
            if not isinstance(cite, dict):
                continue
            doc_id = str(cite.get("doc_id") or "")
            if doc_id not in allowed_ids:
                continue
            page = cite.get("page")
            try:
                page_val = int(page) if page is not None and str(page).strip() != "" else None
            except (TypeError, ValueError):
                page_val = None
            citations.append(
                {
                    "doc_id": doc_id,
                    "page": page_val,
                    "quote": str(cite.get("quote") or "")[:160],
                    "url": citation_url(doc_id, page_val),
                }
            )

        answer = (model_out.get("answer") or "").strip() or None
        # Enforce No Source, No Answer for substantive answers.
        if not refused and (not answer or not citations):
            refused = True
            answer = None
            citations = []
            refusal_reason = "No Source, No Answer — model response lacked required Document+Page citations."
        else:
            refusal_reason = model_out.get("refusal_reason") if refused else None

        result = {
            "refused": refused,
            "refusal_reason": refusal_reason,
            "answer": answer,
            "citations": citations,
            "sources_considered": len(sources),
            "evidence": [
                {
                    "doc_id": r["doc_id"],
                    "page": r.get("page"),
                    "doc_type": r.get("doc_type"),
                    "excerpt": (r.get("text") or "")[:400],
                    "url": citation_url(str(r["doc_id"]), r.get("page")),
                }
                for r in sources
            ],
            "answered_by": {"provider": call.get("provider"), "model": call.get("model"), "route": call.get("source")},
            "query": q,
            "human_in_the_loop": True,
            "disclaimer": "AI support only — humans retain final decision authority.",
        }
        await write_audit(
            db,
            username=principal.username_for_grants or principal.actor_id,
            action="ask_sonali",
            resource_type="ai",
            detail=(
                f"refused={refused} cites={len(citations)} sources={len(sources)} "
                f"model={call.get('provider')}/{call.get('model')} q={q[:120]}"
            ),
            ip_address=ip_address,
            actor_type=principal.actor_type,
            tool_name=tool_name or "ask_sonali",
            commit=True,
        )
        incr("ask_sonali_answers", labels={"outcome": "refused" if refused else "answered"})
        return result
