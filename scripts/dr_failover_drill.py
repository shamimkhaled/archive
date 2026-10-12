#!/usr/bin/env python3
"""Failover drill: take the primary site down, bring the standby up, and record how long it took.

The script does not decide how the bank fails over (DNS, load balancer, Neon branch promotion,
Railway region, or the DR data centre); the commands that do it are passed in, or the operator
performs them and presses Enter. What it does is time every step against the real endpoints and
write a record that the evidence pack can cite.

  1. Baseline: primary /healthz and /readyz, and (with --token) the audit-chain position
  2. Fail:     run --fail-cmd (or wait for the operator) and confirm the primary stops answering
  3. Switch:   run --switch-cmd (or wait for the operator)
  4. Recover:  poll the standby (or --public URL) until /readyz reports ready
  5. Smoke:    health, login page, PWA shell, and with --token: search plus audit-chain verify,
               comparing the sealed audit position with the baseline to measure lost writes

  PYTHONPATH=src python scripts/dr_failover_drill.py \\
      --primary https://archive.sbl.local --standby https://archive-dr.sbl.local \\
      --fail-cmd "ssh lb01 sudo /opt/lb/drain primary" --switch-cmd "ssh lb01 sudo /opt/lb/promote dr" \\
      --token "$DRILL_TOKEN" --operator "Infra on duty"
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import shlex
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import httpx

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SMOKE = ("/healthz", "/readyz", "/login", "/app/")


class Timeline:
    def __init__(self) -> None:
        self.t0 = time.perf_counter()
        self.started_at = datetime.now(timezone.utc)
        self.events: List[Dict[str, Any]] = []

    def mark(self, name: str, ok: bool = True, **detail: Any) -> float:
        at = round(time.perf_counter() - self.t0, 2)
        self.events.append({"t_seconds": at, "event": name, "ok": ok, **detail})
        print(f"[{at:8.1f}s] {'ok ' if ok else 'ERR'} {name} {json.dumps(detail, default=str)[:240]}")
        return at


def probe(client: httpx.Client, base: str, path: str, headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    started = time.perf_counter()
    try:
        r = client.get(base.rstrip("/") + path, headers=headers or {}, follow_redirects=False)
        body: Any = None
        if r.headers.get("content-type", "").startswith("application/json"):
            body = r.json()
        return {"status": r.status_code, "ms": round((time.perf_counter() - started) * 1000), "body": body}
    except httpx.HTTPError as exc:
        return {"status": None, "ms": round((time.perf_counter() - started) * 1000), "error": type(exc).__name__}


def ready(result: Dict[str, Any]) -> bool:
    body = result.get("body") or {}
    return result.get("status") == 200 and body.get("status") in ("ready", "degraded", "ok", None)


def wait_until(check: Callable[[], bool], timeout: float, interval: float) -> bool:
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        if check():
            return True
        time.sleep(interval)
    return False


def run_or_prompt(cmd: Optional[str], prompt: str, timeline: Timeline, name: str) -> None:
    if cmd:
        proc = subprocess.run(shlex.split(cmd), capture_output=True, text=True)
        timeline.mark(name, proc.returncode == 0, command=cmd, returncode=proc.returncode, stderr=proc.stderr.strip()[-200:])
    else:
        input(f"\n>>> {prompt} Press Enter when done. ")
        timeline.mark(name, True, command="manual")


def chain_position(client: httpx.Client, base: str, token: Optional[str]) -> Optional[Dict[str, Any]]:
    if not token:
        return None
    result = probe(client, base, "/api/admin/security/status", {"Authorization": f"Bearer {token}"})
    body = result.get("body") or {}
    return {"status": result["status"], "audit_rows": body.get("audit_rows"), "sealed_up_to": (body.get("chain") or {}).get("last_id")}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0], formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    parser.add_argument("--primary", required=True)
    parser.add_argument("--standby", required=True)
    parser.add_argument("--public", help="The URL users hit (DNS/LB name); polled after the switch if given")
    parser.add_argument("--fail-cmd", help="Command that takes the primary down (omit for a manual step)")
    parser.add_argument("--switch-cmd", help="Command that promotes the standby (omit for a manual step)")
    parser.add_argument("--token", default=os.getenv("DRILL_TOKEN"), help="Admin bearer token for search and audit checks")
    parser.add_argument("--smoke", action="append", help="Extra GET paths to smoke-test after failover")
    parser.add_argument("--timeout", type=float, default=1800, help="Seconds to wait for the standby")
    parser.add_argument("--interval", type=float, default=5)
    parser.add_argument("--rto-target", type=float, default=240, help="Minutes (the bank's RTO objective)")
    parser.add_argument("--rpo-target", type=float, default=15, help="Minutes (the bank's RPO objective)")
    parser.add_argument("--scenario", default="Primary site unavailable; promote DR site")
    parser.add_argument("--operator", default=os.getenv("DRILL_OPERATOR") or getpass.getuser())
    parser.add_argument("--record", type=Path, default=ROOT / "docs" / "evidence" / "drills")
    args = parser.parse_args()

    timeline = Timeline()
    target = args.public or args.standby
    auth = {"Authorization": f"Bearer {args.token}"} if args.token else {}
    with httpx.Client(timeout=15, verify=os.getenv("DRILL_TLS_VERIFY", "1") != "0") as client:
        base_health = probe(client, args.primary, "/readyz")
        timeline.mark("baseline primary /readyz", ready(base_health), status=base_health.get("status"))
        baseline_chain = chain_position(client, args.primary, args.token)
        if baseline_chain:
            timeline.mark("baseline audit chain position", baseline_chain["status"] == 200, **baseline_chain)
        standby_before = probe(client, args.standby, "/healthz")
        timeline.mark("standby before failover", True, status=standby_before.get("status"), note="cold standby may be down")

        run_or_prompt(args.fail_cmd, f"Take the primary down ({args.scenario}).", timeline, "failure injected")
        t_fail = timeline.events[-1]["t_seconds"]
        down = wait_until(lambda: not ready(probe(client, args.primary, "/readyz")), 300, args.interval)
        timeline.mark("primary confirmed unavailable", down)

        run_or_prompt(args.switch_cmd, "Promote the standby / switch traffic.", timeline, "switch executed")
        up = wait_until(lambda: ready(probe(client, target, "/readyz")), args.timeout, args.interval)
        t_ready = timeline.mark("service ready on standby", up, url=target)

        smoke_paths = list(DEFAULT_SMOKE) + (args.smoke or [])
        smoke = {path: probe(client, target, path)["status"] for path in smoke_paths}
        smoke_ok = all(status is not None and status < 500 for status in smoke.values())
        timeline.mark("smoke checks", smoke_ok, results=smoke)

        lost_rows = None
        if args.token:
            # Read the audit position before this drill adds its own search and verify rows.
            after = chain_position(client, target, args.token)
            if baseline_chain and after and isinstance(baseline_chain.get("audit_rows"), int) and isinstance(after.get("audit_rows"), int):
                lost_rows = max(0, baseline_chain["audit_rows"] - after["audit_rows"])
                timeline.mark("audit rows lost in failover", lost_rows == 0, baseline=baseline_chain["audit_rows"], after=after["audit_rows"])
            search = probe(client, target, "/api/search?q=board&limit=3", auth)
            timeline.mark("authenticated search", search["status"] == 200, status=search["status"], ms=search["ms"])
            r = client.post(target.rstrip("/") + "/api/admin/audit/verify", headers=auth)
            body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            timeline.mark("audit chain verifies after failover", r.status_code == 200 and bool(body.get("ok")), status=r.status_code, reason=body.get("reason"))

    rto_minutes = round((t_ready - t_fail) / 60, 2) if up else None
    record = {
        "kind": "failover_drill",
        "scenario": args.scenario,
        "operator": args.operator,
        "drill_started_at": timeline.started_at.isoformat(timespec="seconds"),
        "primary": args.primary,
        "standby": args.standby,
        "public": args.public,
        "rto_minutes": rto_minutes,
        "rto_target_minutes": args.rto_target,
        "rpo_target_minutes": args.rpo_target,
        "audit_rows_lost": lost_rows,
        "ok": all(e["ok"] for e in timeline.events) and rto_minutes is not None and rto_minutes <= args.rto_target,
        "events": timeline.events,
    }
    path = write_record(record, args.record)
    print(f"\n{'PASS' if record['ok'] else 'FAIL'} — RTO {rto_minutes} min (target {args.rto_target}). Record: {path}")
    return 0 if record["ok"] else 1


def write_record(record: Dict[str, Any], directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    stem = f"failover-{record['drill_started_at'][:19].replace(':', '').replace('-', '')}"
    (directory / f"{stem}.json").write_text(json.dumps(record, indent=2, default=str) + "\n", encoding="utf-8")
    lines = [
        f"# Failover drill — {record['drill_started_at'][:16].replace('T', ' ')} UTC",
        "",
        f"**Result: {'PASS' if record['ok'] else 'FAIL'}** — {record['scenario']}",
        "",
        "| Item | Value |",
        "|---|---|",
        f"| Operator | {record['operator']} |",
        f"| Primary → standby | {record['primary']} → {record['standby']} |",
        f"| RTO measured (failure to ready) | {record['rto_minutes']} minutes (target {record['rto_target_minutes']}) |",
        f"| Audit rows lost | {record['audit_rows_lost'] if record['audit_rows_lost'] is not None else 'not measured (no token)'} (RPO target {record['rpo_target_minutes']} minutes) |",
        "",
        "| Time (s) | Event | Result | Detail |",
        "|---|---|---|---|",
    ]
    for e in record["events"]:
        detail = {k: v for k, v in e.items() if k not in ("t_seconds", "event", "ok")}
        lines.append(f"| {e['t_seconds']} | {e['event']} | {'ok' if e['ok'] else 'FAILED'} | `{json.dumps(detail, default=str)[:200]}` |")
    lines += ["", "Generated by `scripts/dr_failover_drill.py`.", ""]
    path = directory / f"{stem}.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


if __name__ == "__main__":
    sys.exit(main())
