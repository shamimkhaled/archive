import json
import re
from typing import Any, Dict, List

from pydantic import BaseModel, Field

from .config import load_environment, openai_chat_model, resolve_openai_credentials

load_environment()

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None


class DocumentSummary(BaseModel):
    core_info: Dict[str, Any] = Field(default_factory=dict)
    key_personnel: List[Dict[str, str]] = Field(default_factory=list)
    major_projects: List[Dict[str, str]] = Field(default_factory=list)
    finance_and_admin: List[str] = Field(default_factory=list)
    searchable_keywords: List[str] = Field(default_factory=list)


def _strip_code_fence(text: str) -> str:
    text = re.sub(r"```(?:json)?\s*", "", text)
    text = text.replace("```", "")
    return text.strip()


def _remove_trailing_commas(json_text: str) -> str:
    return re.sub(r",\s*(?=[}\]])", "", json_text)


def _close_json_structures(text: str) -> str:
    if text.count('"') % 2 != 0:
        text += '"'

    stack: list[str] = []
    in_string = False
    escaped = False
    for ch in text:
        if ch == '"' and not escaped:
            in_string = not in_string
        if in_string:
            escaped = ch == '\\' and not escaped
            continue
        if ch == '{':
            stack.append('{')
        elif ch == '[':
            stack.append('[')
        elif ch == '}' and stack and stack[-1] == '{':
            stack.pop()
        elif ch == ']' and stack and stack[-1] == '[':
            stack.pop()
    while stack:
        opener = stack.pop()
        text += '}' if opener == '{' else ']'
    return text


def _repair_incomplete_json(candidate: str) -> str:
    repaired = re.sub(r',\s*"[^"\n]*$', '', candidate)
    repaired = _remove_trailing_commas(repaired)
    repaired = _close_json_structures(repaired)
    return repaired


def _extract_json(text: str) -> str:
    text = _strip_code_fence(text)
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or start > end:
        raise ValueError("Could not extract JSON object from model output")
    candidate = text[start : end + 1]
    candidate = _remove_trailing_commas(candidate)
    return candidate


def _build_prompt(text: str) -> str:
    return (
        "You are building a structured executive summary for a PDF document.\n"
        "Return only valid JSON.\n"
        "Do not add markdown, code fences, commentary, or any text outside the JSON object.\n"
        "If a field is missing, use empty strings or empty lists, not null.\n"
        "The document may contain English, Bangla (বাংলা / Bengali script), or both.\n"
        "Preserve meaningful Bangla terms in their original script when present.\n"
        "For searchable_keywords, include BOTH English and Bangla variants when available "
        "(e.g. \"Board Meeting\" and \"বোর্ড সভা\") so bilingual search works.\n"
        "Output must exactly match the schema below.\n"
        "Example output:\n"
        "{\n"
        "  \"core_info\": {\n"
        "    \"organization\": \"\",\n"
        "    \"meeting_title\": \"\",\n"
        "    \"date\": \"\",\n"
        "    \"status\": \"\"\n"
        "  },\n"
        "  \"key_personnel\": [\n"
        "    {\"name\": \"\", \"role\": \"\"}\n"
        "  ],\n"
        "  \"major_projects\": [\n"
        "    {\"project_name\": \"\", \"brief_context\": \"\"}\n"
        "  ],\n"
        "  \"finance_and_admin\": [\"\"],\n"
        "  \"searchable_keywords\": [\"\"]\n"
        "}\n"
        "Document text:\n" + text
    )


def extract_document_summary(text: str) -> DocumentSummary:
    if OpenAI is None:
        raise RuntimeError("The openai package is required to extract document summaries.")
    api_key, base_url = resolve_openai_credentials()
    client = OpenAI(api_key=api_key, base_url=base_url) if base_url else OpenAI(api_key=api_key)
    prompt = _build_prompt(text)
    response = client.chat.completions.create(
        model=openai_chat_model(),
        messages=[
            {"role": "user", "content": prompt},
        ],
        temperature=0,
        max_tokens=1500,
    )
    raw = response.choices[0].message.content
    payload = _extract_json(raw)
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError as exc:
        repaired = _repair_incomplete_json(payload)
        try:
            parsed = json.loads(repaired)
        except json.JSONDecodeError:
            raise ValueError(
                f"Could not parse JSON from model response.\nRaw output:\n{raw}\n\nExtracted payload:\n{payload}\n\nRepaired payload:\n{repaired}\n\nJSON error: {exc}"
            ) from exc
    return DocumentSummary.parse_obj(parsed)
