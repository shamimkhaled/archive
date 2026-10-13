"""Phase 3: AI provider port, per-task routing, egress policy, registry, versioned collections."""
from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from bcp_project import ai
from bcp_project.ai import egress, providers, routing
from bcp_project.ai.egress import EgressBlocked

AI_ENV = (
    "AI_EGRESS",
    "AI_CHAT_PROVIDER",
    "AI_CHAT_MODEL",
    "AI_EMBED_PROVIDER",
    "AI_EMBED_MODEL",
    "AI_EMBED_DIM",
    "AI_RERANK_PROVIDER",
    "AI_STT_PROVIDER",
    "AI_TASK_SUMMARY_PROVIDER",
    "AI_TASK_SUMMARY_MODEL",
    "LOCAL_LLM_BASE_URL",
    "LOCAL_LLM_MODEL",
    "LOCAL_EMBED_BASE_URL",
)


@pytest.fixture(autouse=True)
def clean_ai_env(monkeypatch):
    for name in AI_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real-0000000000000000000000")
    routing.clear_overrides()
    yield
    routing.clear_overrides()


@pytest.fixture
def cloud_calls(monkeypatch):
    """Record every request that would reach an OpenAI-compatible server."""
    calls = []

    def fake_complete(self, *, model, messages, temperature, max_tokens, json_mode):
        calls.append(("chat", self.name, model))
        return '{"searchable_keywords": ["board"], "core_info": {}}'

    def fake_embed(self, *, model, inputs):
        calls.append(("embed", self.name, model))
        return [[0.0] * 4 for _ in inputs]

    monkeypatch.setattr(providers.OpenAICompatibleChat, "complete", fake_complete)
    monkeypatch.setattr(providers.OpenAICompatibleEmbedding, "embed", fake_embed)
    return calls


def test_one_setting_moves_chat_to_local_server(monkeypatch, cloud_calls):
    assert routing.chat_route("summary").provider == "openai"
    monkeypatch.setenv("AI_CHAT_PROVIDER", "local")
    monkeypatch.setenv("LOCAL_LLM_BASE_URL", "http://ollama:11434/v1")
    monkeypatch.setenv("LOCAL_LLM_MODEL", "qwen2.5:14b-instruct")
    route = routing.chat_route("ask")
    assert (route.provider, route.model) == ("local", "qwen2.5:14b-instruct")
    assert providers.credentials("local") == ("local", "http://ollama:11434/v1")
    ai.chat("ask", [{"role": "user", "content": "hi"}])
    assert cloud_calls == [("chat", "local", "qwen2.5:14b-instruct")]


def test_per_task_override_beats_global(monkeypatch):
    monkeypatch.setenv("AI_CHAT_PROVIDER", "openrouter")
    monkeypatch.setenv("AI_CHAT_MODEL", "anthropic/claude-sonnet")
    monkeypatch.setenv("AI_TASK_SUMMARY_PROVIDER", "stub")
    assert routing.chat_route("summary").provider == "stub"
    assert routing.chat_route("summary").model == "stub-chat"
    assert routing.chat_route("minutes").model == "anthropic/claude-sonnet"


def test_registry_override_beats_env():
    routing.set_overrides([SimpleNamespace(task="chat.minutes", provider="stub", model="m1", params=None)])
    assert routing.chat_route("minutes").source == "registry"
    assert routing.chat_route("minutes").provider == "stub"
    assert routing.chat_route("summary").source == "env"


@pytest.mark.parametrize(
    "policy,level,cloud_ok",
    [
        ("cloud_allowed", "confidential", True),
        ("cloud_allowed", "restricted", False),
        ("restricted_local_only", "internal", True),
        ("restricted_local_only", "confidential", False),
        ("internal_only", "public", False),
        ("bogus", "confidential", False),
    ],
)
def test_egress_policy_table(monkeypatch, policy, level, cloud_ok):
    monkeypatch.setenv("AI_EGRESS", policy)
    assert egress.allows("openai", level) is cloud_ok
    assert egress.allows("local", level) is True


def test_restricted_document_makes_no_cloud_request(cloud_calls):
    with pytest.raises(EgressBlocked):
        ai.chat("summary", [{"role": "user", "content": "secret"}], sensitivity="restricted")
    with pytest.raises(EgressBlocked):
        ai.embed(["secret"], sensitivity="restricted")
    assert cloud_calls == []


def test_restricted_document_reroutes_to_local_when_available(monkeypatch, cloud_calls):
    monkeypatch.setenv("LOCAL_LLM_BASE_URL", "http://vllm:8000/v1")
    ai.chat("summary", [{"role": "user", "content": "secret"}], sensitivity="restricted")
    assert cloud_calls == [("chat", "local", "qwen2.5:7b-instruct")]
    assert ai.last_call()["source"] == "egress_fallback"


def test_summary_records_provider_and_skips_when_blocked(monkeypatch, cloud_calls):
    from bcp_project.services.ingest import ai_provenance_label, summarize_with_provenance

    summary, prov = summarize_with_provenance("Board meeting text", "internal")
    assert prov["provider"] == "openai" and prov["summary"] == "ai"
    assert summary.searchable_keywords == ["board"]
    assert ai_provenance_label({"_ai": prov}).startswith("AI summary: openai")

    summary, prov = summarize_with_provenance("Restricted text", "restricted")
    assert prov["summary"] == "skipped_egress_policy"
    assert summary.searchable_keywords == []
    assert len(cloud_calls) == 1


def test_summary_provider_error_indexes_without_summary(monkeypatch, cloud_calls):
    from bcp_project.ai.providers import OpenAICompatibleChat
    from bcp_project.services.ingest import ai_provenance_label, summarize_with_provenance

    def out_of_credits(self, **_):
        raise RuntimeError("Error code: 402 - requires more credits")

    monkeypatch.setattr(OpenAICompatibleChat, "complete", out_of_credits)
    summary, prov = summarize_with_provenance("Board meeting text", "internal")
    assert prov["summary"] == "failed" and "402" in prov["error"]
    assert summary.searchable_keywords == []
    assert ai_provenance_label({"_ai": prov}) == "AI summary unavailable (model provider error)"


def test_stub_embeddings_are_deterministic_and_sized(monkeypatch):
    monkeypatch.setenv("AI_EMBED_PROVIDER", "stub")
    assert routing.embed_dimension() == 256
    a, b, c = ai.embed(["board meeting", "board meeting", "loan recovery"])
    assert a == b and a != c and len(a) == 256


def test_embed_dimension_known_models(monkeypatch):
    monkeypatch.setenv("AI_EMBED_PROVIDER", "local")
    monkeypatch.setenv("AI_EMBED_MODEL", "BAAI/bge-m3")
    assert routing.embed_dimension() == 1024
    monkeypatch.setenv("AI_EMBED_DIM", "768")
    assert routing.embed_dimension() == 768


def test_lexical_rerank_orders_by_overlap(monkeypatch):
    assert ai.rerank("board loan", ["x"]) is None
    monkeypatch.setenv("AI_RERANK_PROVIDER", "stub")
    scores = ai.rerank("board loan approval", ["weather report", "board approved the loan"])
    assert scores[1] > scores[0]


def test_validate_route_rejects_unconfigured_local():
    assert routing.validate_route("chat.ask", "local", "qwen") is not None
    assert routing.validate_route("embed", "openai", "x") is not None
    assert routing.validate_route("chat.ask", "stub", "stub-chat") is None


@pytest.mark.asyncio
async def test_registry_activation_history_and_rollback(db_session):
    @asynccontextmanager
    async def factory():
        yield db_session

    first = await routing.activate_route(db_session, task="chat.ask", provider="stub", model="a", actor="admin")
    await db_session.commit()
    await routing.activate_route(db_session, task="chat.ask", provider="stub", model="b", actor="admin")
    await db_session.commit()
    await routing.refresh_registry(factory)
    assert routing.chat_route("ask").model == "b"

    await routing.activate_route(db_session, task="chat.ask", provider=first.provider, model=first.model, actor="admin")
    await db_session.commit()
    await routing.refresh_registry(factory)
    assert routing.chat_route("ask").model == "a"

    assert await routing.reset_to_env(db_session, task="chat.ask") == 1
    await db_session.commit()
    await routing.refresh_registry(factory)
    assert routing.chat_route("ask").source == "env"


@pytest.mark.asyncio
async def test_ask_withholds_restricted_evidence_from_cloud_model():
    from unittest.mock import AsyncMock, MagicMock, patch

    from bcp_project.models import Role, User
    from bcp_project.policy import principal_from_user
    from bcp_project.services import ask_sonali

    principal = principal_from_user(User(id=1, username="admin1", hashed_password="x", role=Role.admin, is_active=True))
    search = {"results": [{"doc_id": "SB-R", "can_view": True, "snippet": "secret", "page": 1}]}
    fake_llm = MagicMock()
    with patch.object(
        ask_sonali.document_query, "search_documents_for_principal", new_callable=AsyncMock, return_value=search
    ), patch.object(
        ask_sonali, "get_sensitivity_for_docs", new_callable=AsyncMock, return_value={"SB-R": "restricted"}
    ), patch.object(ask_sonali, "get_llm_client", return_value=fake_llm), patch.object(
        ask_sonali, "write_audit", new_callable=AsyncMock
    ):
        result = await ask_sonali.ask_sonali_for_principal(AsyncMock(), principal, "secret?")
    assert result["refused"] is True
    assert "on-premise" in result["refusal_reason"]
    fake_llm.chat_json.assert_not_called()


def test_physical_collection_names():
    from bcp_project.qdrant_store import physical_collection_name

    assert physical_collection_name("document_chunks", "BAAI/bge-m3", 1024) == "document_chunks__bge-m3__1024"
    assert (
        physical_collection_name("document_summaries", "text-embedding-3-small", 1536)
        == "document_summaries__text-embedding-3-small__1536"
    )


def test_collection_resolution_follows_this_process_model(monkeypatch):
    from bcp_project import qdrant_store

    indexer = qdrant_store.QdrantIndexer.__new__(qdrant_store.QdrantIndexer)
    indexer._known = {"document_chunks", "document_chunks__bge-m3__1024"}
    indexer._known_at = float("inf")
    monkeypatch.setattr(qdrant_store.time, "monotonic", lambda: 0.0)

    monkeypatch.setenv("AI_EMBED_PROVIDER", "openai")
    assert indexer.resolve_collection("document_chunks") == "document_chunks"
    monkeypatch.setenv("AI_EMBED_PROVIDER", "local")
    monkeypatch.setenv("AI_EMBED_MODEL", "bge-m3")
    assert indexer.resolve_collection("document_chunks") == "document_chunks__bge-m3__1024"
    assert indexer.resolve_collection("custom") == "custom"


def test_routed_client_rewrites_collection_name():
    from bcp_project.qdrant_store import _RoutedClient

    seen = {}

    class Raw:
        def query_points(self, **kwargs):
            seen.update(kwargs)

        def get_collections(self):
            return "untouched"

    client = _RoutedClient(Raw(), lambda name: f"{name}__v2")
    client.query_points(collection_name="document_chunks", limit=3)
    assert seen["collection_name"] == "document_chunks__v2"
    assert client.get_collections() == "untouched"
