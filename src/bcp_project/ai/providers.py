"""Concrete AI providers. Everything speaks the OpenAI-compatible wire format.

``openai``      api.openai.com (or OPENAI_BASE_URL); legacy OpenRouter key detection kept
``openrouter``  openrouter.ai with OPENROUTER_API_KEY
``local``       any OpenAI-compatible server on bank hardware: vLLM, Ollama, llama.cpp,
                TEI (embeddings and /rerank). LOCAL_LLM_BASE_URL, LOCAL_EMBED_BASE_URL,
                LOCAL_RERANK_BASE_URL, LOCAL_LLM_API_KEY
``stub``        deterministic, offline; tests and air-gapped demos only
"""

from __future__ import annotations

import hashlib
import math
import os
import re
import threading
from typing import Any, Dict, List, Optional, Protocol, Tuple

CLOUD_PROVIDERS = frozenset({"openai", "openrouter"})
LOCAL_PROVIDERS = frozenset({"local", "stub"})
ALL_PROVIDERS = CLOUD_PROVIDERS | LOCAL_PROVIDERS

OPENROUTER_BASE = "https://openrouter.ai/api/v1"

# Known embedding dimensions; AI_EMBED_DIM overrides for anything else.
EMBED_DIMENSIONS = {
    "text-embedding-3-small": 1536,
    "openai/text-embedding-3-small": 1536,
    "text-embedding-3-large": 3072,
    "openai/text-embedding-3-large": 3072,
    "text-embedding-ada-002": 1536,
    "bge-m3": 1024,
    "baai/bge-m3": 1024,
    "intfloat/multilingual-e5-large": 1024,
    "multilingual-e5-large": 1024,
    "intfloat/multilingual-e5-large-instruct": 1024,
    "stub-embed": 256,
}


class ChatProvider(Protocol):
    name: str

    def complete(
        self, *, model: str, messages: List[Dict[str, str]], temperature: float, max_tokens: int, json_mode: bool
    ) -> str: ...


class EmbeddingProvider(Protocol):
    name: str

    def embed(self, *, model: str, inputs: List[str]) -> List[List[float]]: ...


class RerankProvider(Protocol):
    name: str

    def rerank(self, *, model: str, query: str, documents: List[str]) -> List[float]: ...


class SttProvider(Protocol):
    name: str

    def transcribe(self, *, model: str, path: str, language: Optional[str]) -> List[Dict[str, Any]]: ...


def is_cloud(provider: str) -> bool:
    return provider in CLOUD_PROVIDERS


def _env(name: str) -> str:
    return (os.getenv(name) or "").strip().strip('"')


def credentials(provider: str, kind: str = "chat") -> Tuple[str, Optional[str]]:
    """(api_key, base_url) for an OpenAI-compatible provider."""
    if provider == "openai":
        from ..config import resolve_openai_credentials

        return resolve_openai_credentials()
    if provider == "openrouter":
        key = _env("OPENROUTER_API_KEY") or _env("OPENAI_API_KEY")
        if not key:
            raise RuntimeError("OPENROUTER_API_KEY is not set")
        return key, OPENROUTER_BASE
    if provider == "local":
        base = {
            "embed": _env("LOCAL_EMBED_BASE_URL"),
            "rerank": _env("LOCAL_RERANK_BASE_URL"),
            "stt": _env("LOCAL_STT_BASE_URL"),
        }.get(kind) or _env("LOCAL_LLM_BASE_URL")
        if not base:
            raise RuntimeError("LOCAL_LLM_BASE_URL is not set for the local provider")
        return _env("LOCAL_LLM_API_KEY") or "local", base.rstrip("/")
    raise RuntimeError(f"Provider {provider!r} has no credentials")


def provider_configured(provider: str, kind: str = "chat") -> bool:
    if provider == "stub":
        return True
    try:
        credentials(provider, kind)
        return True
    except Exception:
        return False


_clients: Dict[tuple, Any] = {}
_clients_lock = threading.Lock()


def _client(provider: str, kind: str):
    """One pooled client per credential set, so calls reuse warm HTTPS connections."""
    from openai import OpenAI

    api_key, base_url = credentials(provider, kind)
    timeout = float(os.getenv("AI_REQUEST_TIMEOUT_SECONDS", "120"))
    key = (api_key, base_url, timeout)
    with _clients_lock:
        client = _clients.get(key)
        if client is None:
            client = OpenAI(api_key=api_key, base_url=base_url, timeout=timeout) if base_url else OpenAI(api_key=api_key, timeout=timeout)
            _clients[key] = client
    return client


class OpenAICompatibleChat:
    def __init__(self, provider: str):
        self.name = provider

    def complete(self, *, model, messages, temperature, max_tokens, json_mode):
        client = _client(self.name, "chat")
        kwargs: Dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if json_mode and os.getenv("AI_JSON_MODE", "1") == "1":
            kwargs["response_format"] = {"type": "json_object"}
        try:
            response = client.chat.completions.create(**kwargs)
        except Exception as exc:
            # Some local servers reject response_format; retry once without it.
            if "response_format" in kwargs and "response_format" in str(exc).lower():
                kwargs.pop("response_format")
                response = client.chat.completions.create(**kwargs)
            else:
                raise
        return response.choices[0].message.content or ""


class OpenAICompatibleEmbedding:
    def __init__(self, provider: str):
        self.name = provider

    def embed(self, *, model, inputs):
        client = _client(self.name, "embed")
        response = client.embeddings.create(model=model, input=inputs)
        return [item.embedding for item in response.data]


class HttpRerank:
    """TEI ``POST /rerank`` or Cohere/Jina-style ``POST /v1/rerank``."""

    def __init__(self, provider: str):
        self.name = provider

    def rerank(self, *, model, query, documents):
        import httpx

        api_key, base_url = credentials(self.name, "rerank")
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        timeout = float(os.getenv("AI_RERANK_TIMEOUT_SECONDS", "10"))
        if base_url.rstrip("/").endswith("/v1"):
            resp = httpx.post(
                f"{base_url}/rerank",
                json={"model": model, "query": query, "documents": documents},
                headers=headers,
                timeout=timeout,
            )
            resp.raise_for_status()
            scores = [0.0] * len(documents)
            for row in resp.json().get("results", []):
                scores[int(row["index"])] = float(row.get("relevance_score", row.get("score", 0.0)))
            return scores
        resp = httpx.post(
            f"{base_url}/rerank",
            json={"query": query, "texts": documents, "raw_scores": False},
            headers=headers,
            timeout=timeout,
        )
        resp.raise_for_status()
        scores = [0.0] * len(documents)
        for row in resp.json():
            scores[int(row["index"])] = float(row["score"])
        return scores


class OpenAIWhisperStt:
    def __init__(self, provider: str):
        self.name = provider

    def transcribe(self, *, model, path, language):
        client = _client(self.name, "stt")
        with open(path, "rb") as fh:
            result = client.audio.transcriptions.create(
                model=model, file=fh, language=language or None, response_format="verbose_json"
            )
        segments = getattr(result, "segments", None) or []
        out = []
        for idx, seg in enumerate(segments, start=1):
            get = seg.get if isinstance(seg, dict) else lambda k, d=None: getattr(seg, k, d)
            out.append({"id": idx, "speaker": "Speaker", "text": (get("text") or "").strip(), "t0": get("start"), "t1": get("end")})
        if not out and getattr(result, "text", ""):
            out.append({"id": 1, "speaker": "Speaker", "text": result.text.strip()})
        return out


class LocalWhisperStt:
    name = "local_whisper"

    def transcribe(self, *, model, path, language):
        from pathlib import Path

        from ..asr import transcribe_local

        return transcribe_local(Path(path), language=language, model=model)


# ``\w`` alone splits Bangla words at vowel signs and the virama.
_WORD = re.compile(r"[\w\u0980-\u09FF\u200c\u200d]+", re.UNICODE)


def _tokens(text: str) -> List[str]:
    return [t.casefold() for t in _WORD.findall(text or "")]


class StubChat:
    """Echoes a deterministic JSON or Markdown answer; never leaves the process."""

    name = "stub"

    def complete(self, *, model, messages, temperature, max_tokens, json_mode):
        user = messages[-1]["content"] if messages else ""
        digest = hashlib.sha256(user.encode()).hexdigest()[:8]
        if json_mode:
            return '{"refused": true, "refusal_reason": "stub provider", "answer": null, "citations": []}'
        return f"_Stub draft {digest}. Configure AI_CHAT_PROVIDER for real output._"


class StubEmbedding:
    """Hashed bag-of-words vectors: same words, same direction. Good enough for CI retrieval tests."""

    name = "stub"

    def embed(self, *, model, inputs):
        dim = int(os.getenv("AI_EMBED_DIM") or 0) or EMBED_DIMENSIONS.get(model) or 256
        vectors = []
        for text in inputs:
            vec = [0.0] * dim
            for token in _tokens(text):
                h = int(hashlib.md5(token.encode()).hexdigest(), 16)
                vec[h % dim] += 1.0 if (h >> 8) & 1 else -1.0
            norm = math.sqrt(sum(v * v for v in vec)) or 1.0
            vectors.append([v / norm for v in vec])
        return vectors


class LexicalRerank:
    """Token-overlap reranker used by ``stub`` and as the no-network fallback."""

    name = "stub"

    def rerank(self, *, model, query, documents):
        q = set(_tokens(query))
        if not q:
            return [0.0] * len(documents)
        scores = []
        for doc in documents:
            d = _tokens(doc)
            overlap = sum(1 for t in d if t in q)
            scores.append(overlap / (len(q) + 0.5 * math.sqrt(len(d) or 1)))
        return scores


def chat_provider(name: str) -> ChatProvider:
    if name == "stub":
        return StubChat()
    if name in ALL_PROVIDERS:
        return OpenAICompatibleChat(name)
    raise RuntimeError(f"Unknown chat provider {name!r}")


def embedding_provider(name: str) -> EmbeddingProvider:
    if name == "stub":
        return StubEmbedding()
    if name in ALL_PROVIDERS:
        return OpenAICompatibleEmbedding(name)
    raise RuntimeError(f"Unknown embedding provider {name!r}")


def rerank_provider(name: str) -> Optional[RerankProvider]:
    if name in {"", "none"}:
        return None
    if name == "stub":
        return LexicalRerank()
    if name in ALL_PROVIDERS:
        return HttpRerank(name)
    raise RuntimeError(f"Unknown rerank provider {name!r}")


def stt_provider(name: str) -> Optional[SttProvider]:
    if name in {"", "none"}:
        return None
    if name == "local_whisper":
        return LocalWhisperStt()
    if name in ALL_PROVIDERS - {"stub"}:
        return OpenAIWhisperStt(name)
    raise RuntimeError(f"Unknown STT provider {name!r}")
