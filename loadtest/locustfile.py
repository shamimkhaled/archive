"""Capacity test for the archive: concurrent search, Ask, watermarked viewing, and upload throughput.

Run against staging (uploads create real documents), from a machine in the same region as the app:

  pip install -r requirements-optional.txt
  LOADTEST_USERS="lt_member1:pass,lt_member2:pass" LOADTEST_DOC_IDS="BM-2024-001,CIR-2023-17" \
    locust -f loadtest/locustfile.py --host https://archive-staging.example \
           --headless -u 100 -r 10 -t 10m --csv loadtest/results/run1

Use accounts whose role does not require MFA (board_member / uploader), or pass LOADTEST_OTP_<USER>.
On stop, writes loadtest/results/capacity-<timestamp>.json with p50/p95/p99 per operation and
exits non-zero if any p95 target is missed. Those numbers fill section 7.2 of the report.
"""

from __future__ import annotations

import itertools
import json
import os
import random
import threading
import time
import uuid
from pathlib import Path

from locust import HttpUser, between, events, task

ROOT = Path(__file__).resolve().parents[1]
RESULTS = Path(os.getenv("LOADTEST_RESULTS_DIR", ROOT / "loadtest" / "results"))

TARGETS_MS = {
    "search": float(os.getenv("LOADTEST_TARGET_SEARCH_MS", "1500")),
    "ask": float(os.getenv("LOADTEST_TARGET_ASK_MS", "8000")),
    "upload": float(os.getenv("LOADTEST_TARGET_UPLOAD_MS", "3000")),
    "view first page": float(os.getenv("LOADTEST_TARGET_VIEW_MS", "2000")),
}

DEFAULT_QUERIES = [
    "board meeting minutes ICT procurement",
    "loan classification circular",
    "পরিচালনা পর্ষদের সভার কার্যবিবরণী",
    "ঋণ শ্রেণীকরণ নীতিমালা",
    "audit committee recommendations",
    "বার্ষিক বাজেট অনুমোদন",
    "anti money laundering policy",
    "branch expansion proposal 2024",
]
DEFAULT_QUESTIONS = [
    "What did the board decide about ICT procurement?",
    "পরিচালনা পর্ষদ ঋণ পুনঃতফসিল সম্পর্কে কী সিদ্ধান্ত নিয়েছে?",
    "Summarise the latest loan classification circular.",
]


def _load_queries() -> list:
    path = ROOT / "eval" / "retrieval_bilingual.jsonl"
    queries = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                q = json.loads(line).get("q")
            except ValueError:
                continue
            if q:
                queries.append(q)
    return queries or DEFAULT_QUERIES


QUERIES = _load_queries()
DOC_IDS = [d.strip() for d in os.getenv("LOADTEST_DOC_IDS", "").split(",") if d.strip()]
UPLOAD_PDF = os.getenv("LOADTEST_UPLOAD_PDF", "")
_accounts = [
    tuple(pair.split(":", 1))
    for pair in os.getenv("LOADTEST_USERS", os.getenv("LOADTEST_USER", "")).split(",")
    if ":" in pair
]
_account_cycle = itertools.cycle(_accounts) if _accounts else None
_account_lock = threading.Lock()


class ArchiveUser(HttpUser):
    abstract = True
    wait_time = between(1, 4)

    def on_start(self) -> None:
        if _account_cycle is None:
            raise RuntimeError("Set LOADTEST_USERS=user:password[,user:password...]")
        with _account_lock:
            username, password = next(_account_cycle)
        headers = {}
        otp = os.getenv(f"LOADTEST_OTP_{username.upper()}")
        if otp:
            headers["X-OTP"] = otp
        resp = self.client.post(
            "/token", data={"username": username, "password": password}, headers=headers, name="login"
        )
        resp.raise_for_status()
        self.client.headers["Authorization"] = f"Bearer {resp.json()['access_token']}"


class SearchUser(ArchiveUser):
    weight = 6

    @task(5)
    def search(self) -> None:
        self.client.get("/api/search", params={"q": random.choice(QUERIES)}, name="search")

    @task(1)
    def view_first_page(self) -> None:
        if not DOC_IDS:
            return
        doc_id = random.choice(DOC_IDS)
        self.client.get(
            f"/documents/{doc_id}/file",
            headers={"Range": "bytes=0-262143"},
            name="view first page",
        )


class AskUser(ArchiveUser):
    weight = 2
    wait_time = between(5, 15)

    @task
    def ask(self) -> None:
        with self.client.post(
            "/api/v1/ask", json={"question": random.choice(DEFAULT_QUESTIONS)}, name="ask", catch_response=True
        ) as resp:
            if resp.status_code == 429:
                resp.success()  # rate limits are a correct response at overload, counted separately
                events.request.fire(
                    request_type="RATE", name="ask 429", response_time=0, response_length=0, exception=None, context={}
                )


class UploadUser(ArchiveUser):
    weight = 1 if UPLOAD_PDF else 0
    wait_time = between(10, 30)

    @task
    def upload(self) -> None:
        if not UPLOAD_PDF:
            return
        with open(UPLOAD_PDF, "rb") as fh:
            payload = fh.read()
        doc_id = f"LT-{time.strftime('%Y%m%d')}-{uuid.uuid4().hex[:10]}"
        self.client.post(
            "/api/documents/ingest",
            data={"doc_id": doc_id, "doc_date": time.strftime("%Y-%m-%d"), "doc_type": "loadtest", "keywords": "loadtest"},
            files={"file": (f"{doc_id}.pdf", payload, "application/pdf")},
            name="upload",
        )


@events.quitting.add_listener
def write_capacity_report(environment, **_kwargs) -> None:
    stats = environment.stats
    rows = {}
    failed_targets = []
    for entry in stats.entries.values():
        p95 = entry.get_response_time_percentile(0.95)
        rows[entry.name] = {
            "requests": entry.num_requests,
            "failures": entry.num_failures,
            "rps": round(entry.total_rps, 2),
            "p50_ms": entry.get_response_time_percentile(0.50),
            "p95_ms": p95,
            "p99_ms": entry.get_response_time_percentile(0.99),
            "max_ms": entry.max_response_time,
            "target_p95_ms": TARGETS_MS.get(entry.name),
        }
        target = TARGETS_MS.get(entry.name)
        if target and entry.num_requests and p95 > target:
            failed_targets.append(entry.name)
    report = {
        "host": environment.host,
        "users": environment.runner.user_count if environment.runner else None,
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "total_failure_ratio": round(stats.total.fail_ratio, 4),
        "operations": rows,
        "p95_targets_missed": failed_targets,
    }
    RESULTS.mkdir(parents=True, exist_ok=True)
    out = RESULTS / f"capacity-{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"capacity report: {out}")
    if failed_targets or stats.total.fail_ratio > float(os.getenv("LOADTEST_MAX_FAILURE_RATIO", "0.01")):
        environment.process_exit_code = 1
