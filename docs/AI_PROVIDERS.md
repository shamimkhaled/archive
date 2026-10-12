# AI providers: switching between cloud and bank-hosted models

Every model call in the archive goes through one port, `bcp_project.ai`:

| Function | Used by |
|---|---|
| `ai.chat(task, ...)` / `ai.chat_json(...)` | document summaries (`summary`), meeting minutes (`minutes`), Ask Sonali (`ask`), board briefs (`board_brief`), metadata extraction (`metadata`) |
| `ai.embed(texts, sensitivity=...)` | summary and chunk vectors, search queries |
| `ai.rerank(query, docs)` | hybrid search re-ordering (best effort, optional) |
| `ai.transcribe(path)` | meeting recordings |

Callers pass the **task** and the **sensitivity** of the content. The port picks the
provider, applies the egress policy, records latency (`ai_request_seconds` on
`/metrics`), and remembers which provider served the call so the UI can show it.

## Providers

All network providers speak the OpenAI-compatible API, so one adapter covers them.

| Provider | Where | Settings |
|---|---|---|
| `openai` | api.openai.com, or `OPENAI_BASE_URL` | `OPENAI_API_KEY` (OpenRouter keys are detected, as before) |
| `openrouter` | openrouter.ai | `OPENROUTER_API_KEY` |
| `local` | vLLM, Ollama, llama.cpp, TEI on bank hardware | `LOCAL_LLM_BASE_URL`, `LOCAL_LLM_MODEL`, `LOCAL_LLM_API_KEY`, `LOCAL_EMBED_BASE_URL`, `LOCAL_EMBED_MODEL`, `LOCAL_RERANK_BASE_URL` |
| `stub` | in-process, deterministic | none; tests, CI evaluation, and air-gapped demos |
| `local_whisper` (STT only) | faster-whisper in the `worker-meeting` image | `WHISPER_MODEL`, `WHISPER_DEVICE`, `WHISPER_COMPUTE_TYPE` |

## Choosing a provider

Highest precedence first:

1. **Admin switch** at `/admin/ai`, stored in `model_registry`. Every service picks it up
   within `AI_REGISTRY_REFRESH_SECONDS` (default 30). Each switch is a new row with who
   and when, and is written to the audit log; "Roll back to this" re-activates an older row.
2. **Per-task env**: `AI_TASK_<TASK>_PROVIDER`, `AI_TASK_<TASK>_MODEL`
   (`SUMMARY`, `MINUTES`, `ASK`, `BOARD_BRIEF`, `METADATA`).
3. **Global env**: `AI_CHAT_PROVIDER`, `AI_CHAT_MODEL`; `AI_EMBED_PROVIDER`, `AI_EMBED_MODEL`,
   `AI_EMBED_DIM`; `AI_RERANK_PROVIDER`, `AI_RERANK_MODEL`; `AI_STT_PROVIDER`, `AI_STT_MODEL`.
4. **Legacy**: `OPENAI_CHAT_MODEL`, `OPENAI_EMBEDDING_MODEL`. With no `AI_*` variables set,
   behaviour is identical to the pre-port code.

Embeddings are not switchable from the admin page, because the vectors must match
the collection they are searched against. See "Changing the embedding model" below.

### Today on Railway (cloud)

```
AI_CHAT_PROVIDER=openrouter
AI_CHAT_MODEL=openai/gpt-4.1-mini
AI_EMBED_PROVIDER=openrouter
AI_EMBED_MODEL=openai/text-embedding-3-small
AI_STT_PROVIDER=local_whisper
AI_EGRESS=cloud_allowed
```

### Later on bank hardware (local)

```
LOCAL_LLM_BASE_URL=http://vllm.internal:8000/v1
LOCAL_LLM_MODEL=Qwen/Qwen2.5-14B-Instruct
LOCAL_EMBED_BASE_URL=http://tei-embed.internal:8080/v1
LOCAL_EMBED_MODEL=bge-m3
LOCAL_RERANK_BASE_URL=http://tei-rerank.internal:8080
AI_CHAT_PROVIDER=local
AI_RERANK_PROVIDER=local
AI_EGRESS=internal_only
```

A mixed setup is also valid. For example, keep Ask Sonali on a cloud model while
summaries run locally (`AI_TASK_SUMMARY_PROVIDER=local`), and set
`AI_EGRESS=restricted_local_only` so confidential material never leaves the bank.

## Egress policy

`AI_EGRESS` decides which document classifications may be sent to a cloud provider
(`openai`, `openrouter`). Local providers are always allowed.

| Policy | Cloud allowed for | Local only |
|---|---|---|
| `cloud_allowed` (default) | public, internal, confidential | restricted |
| `restricted_local_only` | public, internal | confidential, restricted |
| `internal_only` | nothing | everything |

**Restricted documents never go to a cloud provider under any policy.** An
unrecognised value is treated as `restricted_local_only`.

When a call is blocked:

- **Chat** (summary, minutes, Ask): rerouted to `local` if `LOCAL_LLM_BASE_URL` is set.
  Otherwise the step is skipped. The summary is left empty and the viewer shows "AI summary
  withheld by egress policy". Minutes fall back to the template. Ask Sonali drops the
  withheld evidence and refuses if nothing is left.
- **Embeddings**: never rerouted, because a different model would produce vectors in a
  different space. The document is not vector-indexed. It stays findable through
  Postgres keyword and metadata search.
- **Reranking**: skipped; fusion order is kept.
- **STT**: falls back to `local_whisper`.

Meeting transcripts and minutes use `MEETING_SENSITIVITY` (default `confidential`).
Documents pulled by the inbox/SFTP connector use `CONNECTOR_SENSITIVITY` (default `internal`).

The enforcement is in code (`ai/egress.py`, `ai/__init__.py`) and covered by
`tests/test_phase3_ai.py::test_restricted_document_makes_no_cloud_request`.

## Provenance

Each summary stores `summary_json._ai`: `{summary, provider, model, route, at, vectors}`.
The viewer subtitle shows it (for example "AI summary: local / Qwen2.5-14B-Instruct"),
and `/api/v1/documents/{id}/metadata` returns it as `ai_provenance`.

## Changing the embedding model (no search outage)

Collections are named by model and dimension: `document_chunks__bge-m3__1024`.
Each process searches the collection that matches **its own** `AI_EMBED_*` settings,
so old and new replicas can run side by side during a rolling deploy. The logical names
(`document_summaries`, `document_chunks`) are Qdrant aliases on fresh installs. Collections
created before versioning keep their plain names and keep working until they are retired.

```
# 1. Build the new collections from the current ones (payloads and ids are kept; text is re-embedded)
python scripts/reindex_embeddings.py --provider local --model bge-m3 --dim 1024

# 2. Deploy AI_EMBED_PROVIDER / AI_EMBED_MODEL / AI_EMBED_DIM to every service

# 3. Copy anything uploaded to old replicas during the rollout
python scripts/reindex_embeddings.py --provider local --model bge-m3 --dim 1024 --catch-up \
    --source-suffix ""            # or __text-embedding-3-small__1536 for a versioned source

# 4. Retire the old collections, then point the logical aliases at the new ones
python scripts/reindex_embeddings.py --provider local --model bge-m3 --dim 1024 --drop-source --source-suffix ""
python scripts/reindex_embeddings.py --provider local --model bge-m3 --dim 1024 --point-alias
```

Rolling back before step 4 only needs the old env restored. If a service starts with
an embedding model whose dimension does not match an unversioned collection, indexing
fails with a clear "run scripts/reindex_embeddings.py" error instead of writing
mismatched vectors.

## Evaluating a switch

Run the retrieval evaluation set (Phase 5) against both routes before switching in
production. Use `/admin/ai` to switch one task at a time, and roll back from the history
table if quality drops.
