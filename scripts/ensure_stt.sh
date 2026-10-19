#!/usr/bin/env bash
# Install local speech-to-text deps into a writable venv and print stt_status.
# Prefer the Docker worker image in production; this script is for laptop / CI smoke.
#
# Some IDE sandboxes rewrite `python3` to a non-venv interpreter. We install with
# the venv pip, then verify with a real /usr/bin/python* plus the venv site-packages.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
VENV="${STT_VENV:-$ROOT/.venv-worker}"

if [[ ! -x "$VENV/bin/pip" ]]; then
  /usr/bin/python3 -m venv "$VENV" 2>/dev/null || python3 -m venv "$VENV"
fi
"$VENV/bin/pip" install -q -U pip
"$VENV/bin/pip" install -q -r requirements-worker.txt

SITE="$(/usr/bin/ls -d "$VENV"/lib/python*/site-packages | /usr/bin/head -n1)"
# Prefer a system python that can load the venv packages (same major version).
for PY in /usr/bin/python3.14 /usr/bin/python3.12 /usr/bin/python3.11 /usr/bin/python3; do
  [[ -x "$PY" ]] || continue
  if "$PY" -c "import sys; sys.path.insert(0, r'$SITE'); import faster_whisper" 2>/dev/null; then
    break
  fi
  PY=""
done
if [[ -z "${PY:-}" ]]; then
  echo "ERROR: could not import faster_whisper with any system python" >&2
  exit 1
fi

export PYTHONPATH="$SITE:$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
"$PY" - <<'PY'
from bcp_project.asr import stt_status, whisper_available
print("python_check_ok")
print("whisper_available=", whisper_available())
print("stt_status=", stt_status())
if not whisper_available():
    raise SystemExit("faster-whisper still not importable")
print("OK — meeting jobs need this PYTHONPATH (or the Docker worker image).")
print("  export PYTHONPATH=%r" % __import__("os").environ["PYTHONPATH"])
PY
command -v ffmpeg >/dev/null || echo "WARNING: ffmpeg not on PATH (needed to merge/normalise recordings)"
