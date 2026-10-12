#!/usr/bin/env python3
"""Speech-to-text real-time factor (processing seconds / audio seconds) on this machine.

Runs the same ``ai.transcribe`` the meeting worker uses, so the active STT route (local
faster-whisper or a hosted provider) is what gets measured. An RTF below 1.0 means a meeting
transcribes faster than it was recorded; the report's target is RTF <= 0.5 on worker hardware.

  PYTHONPATH=src python scripts/bench_stt.py samples/board-meeting-10min.wav --language bn --rounds 2
"""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from bcp_project.config import load_environment

load_environment()

from bcp_project import ai


def audio_seconds(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True,
        text=True,
        check=True,
    )
    return float(out.stdout.strip())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("audio", nargs="+", type=Path)
    parser.add_argument("--language", default=None, help="bn, en, or blank for auto-detect")
    parser.add_argument("--rounds", type=int, default=1)
    parser.add_argument("--sensitivity", default="confidential")
    parser.add_argument("--json", type=Path, help="Write results here")
    args = parser.parse_args()

    rows = []
    for path in args.audio:
        duration = audio_seconds(path)
        for round_no in range(1, args.rounds + 1):
            started = time.perf_counter()
            segments = ai.transcribe(str(path), language=args.language, sensitivity=args.sensitivity)
            elapsed = time.perf_counter() - started
            call = ai.last_call() or {}
            rows.append(
                {
                    "file": path.name,
                    "round": round_no,
                    "audio_s": round(duration, 1),
                    "processing_s": round(elapsed, 1),
                    "rtf": round(elapsed / duration, 3) if duration else None,
                    "segments": len(segments),
                    "provider": call.get("provider"),
                    "model": call.get("model"),
                }
            )
            print(f"{path.name} round {round_no}: {elapsed:.1f}s for {duration:.1f}s audio, RTF {elapsed / duration:.3f}")

    rtfs = [r["rtf"] for r in rows if r["rtf"] is not None]
    summary = {
        "runs": rows,
        "rtf_mean": round(statistics.mean(rtfs), 3) if rtfs else None,
        "rtf_max": max(rtfs) if rtfs else None,
    }
    print(json.dumps({k: v for k, v in summary.items() if k != "runs"}))
    if args.json:
        args.json.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
