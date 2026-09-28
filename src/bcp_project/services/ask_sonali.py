"""Ask Sonali Bank — governed Q&A with mandatory citations (R7)."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from ..access_control import write_audit
from ..observability import Timer, incr
from ..policy import Principal, Scope, require_scope
from ..ports.adapters import get_llm_client
from . import document_query

logger = logging.getLogger("bcp_project.services.ask_sonali")

SYSTEM_PROMPT = """You are Ask Sonali Bank, a governed institutional-memory assistant.
Rules (non-negotiable):
1. Answer ONLY using the provided SOURCE excerpts.
2. Every substantive claim must cite at least one source as {doc_id, page}.
3. If sources are insufficient, set refused=true and do not invent facts.
4. Never claim to be the final authority; humans decide.
5. Return ONLY valid JSON matching the schema.
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
        if not evidence:
            incr("ask_sonali.refused_no_source")
            payload = {
                "refused": True,
                "refusal_reason": "No Source, No Answer — no authorized archive evidence matched this question.",
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

        sources_block = []
        for idx, row in enumerate(evidence, start=1):
            sources_block.append(
                f"[{idx}] doc_id={row['doc_id']} page={row.get('page')} type={row.get('doc_type')}\n"
                f"{row.get('snippet') or ''}"
            )
        user_prompt = (
            f"Question:\n{q}\n\nSOURCE excerpts (authorized only):\n"
            + "\n\n".join(sources_block)
            + "\n\nRespond with JSON only."
        )

        try:
            llm = get_llm_client()
            model_out = llm.chat_json(system=SYSTEM_PROMPT, user=user_prompt, temperature=0.0)
        except Exception as exc:
            logger.exception("Ask Sonali LLM failed")
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Ask Sonali model unavailable: {str(exc)[:160]}",
            ) from exc

        refused = bool(model_out.get("refused"))
        citations_raw = model_out.get("citations") or []
        allowed_ids = {str(r["doc_id"]) for r in evidence if r.get("doc_id")}
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
            "sources_considered": len(evidence),
            "evidence": evidence,
            "query": q,
            "human_in_the_loop": True,
            "disclaimer": "AI support only — humans retain final decision authority.",
        }
        await write_audit(
            db,
            username=principal.username_for_grants or principal.actor_id,
            action="ask_sonali",
            resource_type="ai",
            detail=f"refused={refused} cites={len(citations)} sources={len(evidence)} q={q[:120]}",
            ip_address=ip_address,
            actor_type=principal.actor_type,
            tool_name=tool_name or "ask_sonali",
            commit=True,
        )
        incr("ask_sonali.ok" if not refused else "ask_sonali.refused")
        return result
