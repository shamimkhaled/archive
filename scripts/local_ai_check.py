#!/usr/bin/env python3
"""Readiness check for the bank-hosted AI services before switching the archive to them.

Reads the same LOCAL_* / AI_* settings the archive uses and checks each service directly:

  chat     model is served, JSON mode works, a Bangla prompt gets a Bangla answer, tokens/s
  embed    dimension matches AI_EMBED_DIM, Bangla and English versions of one sentence land
           closer to each other than to an unrelated sentence
  rerank   the relevant passage is ranked first
  stt      the transcription endpoint accepts audio (a real file with --audio, else 1 s of silence)
  policy   AI_EGRESS and cloud keys are consistent with an on-premise deployment

  PYTHONPATH=src python scripts/local_ai_check.py --env deploy/onprem/archive-ai.env --json eval/reports/local-ai-check.json
  PYTHONPATH=src python scripts/local_ai_check.py --load 16      # 16 concurrent chat requests
"""

from __future__ import annotations

import argparse
import concurrent.futures
import io
import json
import math
import os
import sys
import time
import wave
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

ROOT = Path(__file__).resolve().parents[1]

BN_SENTENCE = "পরিচালনা পর্ষদ বার্ষিক বাজেট অনুমোদন করেছে।"
EN_SENTENCE = "The Board of Directors approved the annual budget."
UNRELATED = "Heavy rain is expected in Chattogram this weekend."


def env(name: str, default: Optional[str] = None) -> Optional[str]:
    value = os.getenv(name)
    return value.strip() if value and value.strip() else default


def headers() -> Dict[str, str]:
    key = env("LOCAL_LLM_API_KEY")
    return {"Authorization": f"Bearer {key}"} if key else {}


def cosine(a: List[float], b: List[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    return dot / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)) or 1.0)


def check(name: str, fn) -> Dict[str, Any]:
    started = time.perf_counter()
    try:
        detail = fn()
        ok = bool(detail.pop("ok", True))
    except Exception as exc:  # noqa: BLE001 - every failure is reported, not raised
        ok, detail = False, {"error": f"{type(exc).__name__}: {str(exc)[:300]}"}
    result = {"check": name, "ok": ok, "seconds": round(time.perf_counter() - started, 2), **detail}
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {json.dumps(detail, ensure_ascii=False)[:300]}")
    return result


def chat_checks(client: httpx.Client, base: str, model: str) -> Dict[str, Any]:
    served = [m["id"] for m in client.get(f"{base}/models", headers=headers()).raise_for_status().json().get("data", [])]
    started = time.perf_counter()
    r = client.post(
        f"{base}/chat/completions",
        headers=headers(),
        json={
            "model": model,
            "temperature": 0,
            "max_tokens": 200,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": 'Reply with JSON only: {"summary_bn": "...", "summary_en": "..."}'},
                {"role": "user", "content": f"Summarise in one sentence each, Bangla and English: {BN_SENTENCE}"},
            ],
        },
    ).raise_for_status().json()
    seconds = time.perf_counter() - started
    content = r["choices"][0]["message"]["content"]
    parsed = json.loads(content)
    bangla = any("\u0980" <= ch <= "\u09ff" for ch in str(parsed.get("summary_bn", "")))
    tokens = (r.get("usage") or {}).get("completion_tokens") or 0
    return {
        "ok": model in served and bangla,
        "served_models": served,
        "json_mode": True,
        "bangla_answer": bangla,
        "latency_s": round(seconds, 2),
        "tokens_per_s": round(tokens / seconds, 1) if tokens and seconds else None,
    }


def chat_load(base: str, model: str, concurrency: int) -> Dict[str, Any]:
    def one(i: int) -> Dict[str, float]:
        with httpx.Client(timeout=300) as c:
            t = time.perf_counter()
            r = c.post(
                f"{base}/chat/completions",
                headers=headers(),
                json={"model": model, "max_tokens": 256, "messages": [{"role": "user", "content": f"({i}) Write 150 words on records retention for a bank board."}]},
            ).raise_for_status().json()
            return {"s": time.perf_counter() - t, "tokens": (r.get("usage") or {}).get("completion_tokens") or 0}

    started = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(concurrency) as pool:
        results = list(pool.map(one, range(concurrency)))
    wall = time.perf_counter() - started
    latencies = sorted(r["s"] for r in results)
    return {
        "concurrency": concurrency,
        "p50_s": round(latencies[len(latencies) // 2], 2),
        "max_s": round(latencies[-1], 2),
        "aggregate_tokens_per_s": round(sum(r["tokens"] for r in results) / wall, 1),
    }


def embed_checks(client: httpx.Client, base: str, model: str, dim: int) -> Dict[str, Any]:
    r = client.post(f"{base}/embeddings", headers=headers(), json={"model": model, "input": [EN_SENTENCE, BN_SENTENCE, UNRELATED]}).raise_for_status().json()
    vectors = [d["embedding"] for d in sorted(r["data"], key=lambda d: d["index"])]
    same, other = cosine(vectors[0], vectors[1]), cosine(vectors[0], vectors[2])
    return {
        "ok": len(vectors[0]) == dim and same > other,
        "dimension": len(vectors[0]),
        "expected_dimension": dim,
        "cosine_en_bn_same_meaning": round(same, 3),
        "cosine_en_unrelated": round(other, 3),
    }


def rerank_checks(client: httpx.Client, base: str) -> Dict[str, Any]:
    texts = [UNRELATED, BN_SENTENCE, "Branch opening hours change during Ramadan."]
    r = client.post(f"{base}/rerank", headers=headers(), json={"query": "বাজেট অনুমোদন", "texts": texts}).raise_for_status().json()
    ranked = r if isinstance(r, list) else r.get("results", [])
    top = max(ranked, key=lambda x: x.get("score", x.get("relevance_score", 0)))
    return {"ok": top.get("index") == 1, "top_index": top.get("index"), "scores": [round(x.get("score", x.get("relevance_score", 0)), 3) for x in ranked]}


def silence_wav(seconds: float = 1.0, rate: int = 16000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(seconds * rate))
    return buf.getvalue()


def stt_checks(client: httpx.Client, base: str, model: str, audio: Optional[Path]) -> Dict[str, Any]:
    data = audio.read_bytes() if audio else silence_wav()
    name = audio.name if audio else "silence.wav"
    started = time.perf_counter()
    r = client.post(
        f"{base}/audio/transcriptions",
        headers=headers(),
        data={"model": model, "language": "bn", "response_format": "json"},
        files={"file": (name, data)},
    ).raise_for_status().json()
    return {"ok": True, "latency_s": round(time.perf_counter() - started, 2), "text_sample": (r.get("text") or "")[:120]}


def policy_checks() -> Dict[str, Any]:
    egress = env("AI_EGRESS", "cloud_allowed")
    cloud_keys = [k for k in ("OPENAI_API_KEY", "OPENROUTER_API_KEY") if env(k) and "your_" not in (env(k) or "")]
    providers = {k: env(k) for k in ("AI_CHAT_PROVIDER", "AI_EMBED_PROVIDER", "AI_RERANK_PROVIDER", "AI_STT_PROVIDER") if env(k)}
    ok = egress == "internal_only" and not cloud_keys
    return {"ok": ok, "AI_EGRESS": egress, "cloud_keys_present": cloud_keys, "providers": providers, "note": None if ok else "on-premise target is AI_EGRESS=internal_only with no cloud keys"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0], formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    parser.add_argument("--env", type=Path, help="Env file with LOCAL_* settings (overrides the process environment)")
    parser.add_argument("--audio", type=Path, help="A short Bangla recording for the STT check")
    parser.add_argument("--load", type=int, default=0, help="Concurrent chat requests for a throughput probe")
    parser.add_argument("--skip", action="append", default=[], choices=("chat", "embed", "rerank", "stt", "policy"))
    parser.add_argument("--json", type=Path)
    args = parser.parse_args()

    if args.env:
        from dotenv import load_dotenv

        load_dotenv(args.env, override=True)

    results = []
    with httpx.Client(timeout=300) as client:
        llm = (env("LOCAL_LLM_BASE_URL") or "").rstrip("/")
        if "chat" not in args.skip:
            results.append(check("chat", lambda: chat_checks(client, llm, env("LOCAL_LLM_MODEL", "qwen2.5-14b-instruct"))))
            if args.load:
                results.append(check(f"chat load x{args.load}", lambda: chat_load(llm, env("LOCAL_LLM_MODEL", "qwen2.5-14b-instruct"), args.load)))
        if "embed" not in args.skip:
            base = (env("LOCAL_EMBED_BASE_URL") or llm).rstrip("/")
            results.append(check("embed", lambda: embed_checks(client, base, env("LOCAL_EMBED_MODEL", "bge-m3"), int(env("AI_EMBED_DIM", "1024")))))
        if "rerank" not in args.skip:
            base = (env("LOCAL_RERANK_BASE_URL") or "").rstrip("/")
            results.append(check("rerank", lambda: rerank_checks(client, base)))
        if "stt" not in args.skip:
            base = (env("LOCAL_STT_BASE_URL") or llm).rstrip("/")
            results.append(check("stt", lambda: stt_checks(client, base, env("AI_STT_MODEL", "Systran/faster-whisper-large-v3"), args.audio)))
    if "policy" not in args.skip:
        results.append(check("policy", policy_checks))

    ok = all(r["ok"] for r in results)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps({"ok": ok, "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "results": results}, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print("\nREADY for cutover" if ok else "\nNOT READY — fix the failed checks above")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
