# Runbook: moving AI from cloud to bank GPU hardware

Covers the move from OpenRouter/OpenAI (Railway today) to vLLM, TEI and Whisper on Sonali Bank
hardware. The switch is per task and reversible: nothing is migrated except the embedding index,
which is rebuilt alongside the old one. Background on providers and egress:
[`docs/AI_PROVIDERS.md`](../AI_PROVIDERS.md). Hardware: [`sizing.md`](sizing.md) section 3.

## 0. Before you start

- GPU host with NVIDIA driver and container toolkit, Docker Compose v2, and the archive's Docker
  network (`ARCHIVE_NETWORK`) reachable.
- Weights copied from an approved mirror to `MODEL_DIR` (default `/srv/models`):
  `Qwen2.5-14B-Instruct`, `bge-m3`, `bge-reranker-v2-m3`, and `Systran/faster-whisper-large-v3`.
  Record each directory's SHA-256 manifest in the change ticket.
- Container images mirrored into the bank registry and pinned by digest in `ai.env`.
- A baseline: the cloud benchmark reports for the same manifests
  (`eval/reports/*-cloud.json`, see `benchmark-report.md` section 4).

## 1. Start the AI stack

```bash
cd deploy/onprem
cp ai.env.example ai.env          # set LOCAL_LLM_API_KEY (long random), digests, GPU ids
docker compose -f docker-compose.gpu.yml --env-file ai.env up -d
docker compose -f docker-compose.gpu.yml --env-file ai.env ps
curl -s http://127.0.0.1:8088/healthz
```

The model containers sit on an internal network with no route out. Only `ai-gateway` joins the
archive network, and it requires `Authorization: Bearer $LOCAL_LLM_API_KEY` on every path.

## 2. Point the archive at it (no traffic yet)

Add to every archive service (Railway variables or the bank's env files):

```bash
LOCAL_LLM_BASE_URL=http://ai-gateway:8088/llm/v1
LOCAL_LLM_API_KEY=<same as ai.env>
LOCAL_LLM_MODEL=qwen2.5-14b-instruct
LOCAL_EMBED_BASE_URL=http://ai-gateway:8088/embed/v1
LOCAL_EMBED_MODEL=bge-m3
LOCAL_RERANK_BASE_URL=http://ai-gateway:8088/rerank
LOCAL_STT_BASE_URL=http://ai-gateway:8088/stt/v1
```

Defining `LOCAL_*` only makes the provider available; routes still point at the cloud.

## 3. Check the servers

From a host on the archive network, with the step 2 values in a file (here `local.env`):

```bash
PYTHONPATH=src python scripts/local_ai_check.py --env local.env \
  --audio samples/bangla-30s.wav --load 16 --json eval/reports/local-ai-check.json
```

Checks: served model names, JSON mode, a Bangla answer, tokens per second (single and with 16
concurrent requests), embedding dimension and Bangla–English cross-lingual similarity, reranker
ordering, speech to text on a Bangla clip, and the egress policy. Every check must PASS.

## 4. Benchmark local against cloud

Run each benchmark with the local route (per-task env overrides, so production is untouched):

```bash
export AI_CHAT_PROVIDER=local AI_STT_PROVIDER=local AI_RERANK_PROVIDER=local
PYTHONPATH=src python scripts/bench_accuracy.py metadata --manifest $SAMPLES/metadata.jsonl --json eval/reports/metadata-local.json
PYTHONPATH=src python scripts/bench_accuracy.py summary  --manifest $SAMPLES/summary.jsonl  --json eval/reports/summary-local.json
PYTHONPATH=src python scripts/bench_accuracy.py stt      --manifest $SAMPLES/stt.jsonl      --json eval/reports/stt-local.json
PYTHONPATH=src python scripts/bench_accuracy.py compare eval/reports/metadata-cloud.json eval/reports/metadata-local.json \
  --baseline-label cloud --candidate-label "vLLM Qwen2.5-14B" --out docs/evidence/compare-metadata.md
```

Repeat `compare` for summary and STT. Ask is benchmarked on UAT after step 5, against the same
question set. Decision rule: switch a task when the local result is within the agreed tolerance of
cloud (proposed: no more than 2 points worse on the headline metric) and latency meets
`docs/CAPACITY.md`. Otherwise try the 32B model on two GPUs before switching.

## 5. Switch chat, rerank and STT per task

In `/admin/ai`, change one task at a time (suggested order: `chat.metadata`, `chat.summary`,
`rerank`, `stt`, `chat.minutes`, `chat.board_brief`, `chat.ask`). Each change applies to every
replica within 30 seconds, is audited (`ai_route_switch`), and can be reverted from the route
history (`ai_route_rollback`). Between switches, watch for a day: the `/metrics` counters (Ask
latency, Ask refusals, egress blocks), the dead-letter queue at `/admin/queues`, and the review
queue length.

## 6. Move the embedding index

The vector space changes (1536-d `text-embedding-3-small` to 1024-d `bge-m3`), so it is rebuilt beside
the old one without a search outage:

```bash
PYTHONPATH=src python scripts/reindex_embeddings.py --provider local --model bge-m3 --dim 1024 --dry-run
PYTHONPATH=src python scripts/reindex_embeddings.py --provider local --model bge-m3 --dim 1024
# deploy AI_EMBED_PROVIDER=local AI_EMBED_MODEL=bge-m3 AI_EMBED_DIM=1024 to every service
PYTHONPATH=src python scripts/reindex_embeddings.py --provider local --model bge-m3 --dim 1024 --catch-up
PYTHONPATH=src python scripts/eval_retrieval.py --live --json eval/reports/retrieval-local.json
PYTHONPATH=src python scripts/reindex_embeddings.py --provider local --model bge-m3 --dim 1024 --point-alias
```

Compare `retrieval-local.json` with the cloud run and run the Ask benchmark on UAT. Keep the old
collections for at least two weeks; only then run `--drop-source`.

## 7. Close the egress

When every task runs locally and the results are accepted, set `AI_EGRESS=internal_only` and remove
the cloud API keys from every service. `local_ai_check.py` (policy check) then confirms that no
cloud key remains and that the policy blocks cloud routes. From this point no document text leaves
the bank.

## 8. Rollback

| Situation | Action |
|---|---|
| One task misbehaves | `/admin/ai` → route history → activate the previous cloud route (needs `AI_EGRESS` other than `internal_only` and the cloud key present) |
| GPU host down | Tasks routed to local fail and jobs retry, then dead-letter; the `restricted` text never falls back to cloud. Restore the host, then retry from `/admin/queues`. If policy allows, switch non-restricted tasks back to cloud meanwhile. |
| Embedding index problem before `--drop-source` | Redeploy the previous `AI_EMBED_*` values; the old collections are intact |

## 9. Record

File the `local-ai-check.json`, all `compare-*.md` files and the switch dates in
`docs/evidence/` as the cutover evidence, and update `benchmark-report.md`.
