#!/usr/bin/env python3
"""Write FastAPI's OpenAPI schema to frontend/openapi.json for the generated TypeScript client.

  PYTHONPATH=src python scripts/export_openapi.py && (cd frontend && npm run gen:api)
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
os.environ.setdefault("AUTO_MIGRATE", "0")

from bcp_project.main_api import app


def main() -> int:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "frontend" / "openapi.json"
    schema = app.openapi()
    out.write_text(json.dumps(schema, indent=1, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {out} ({len(schema.get('paths', {}))} paths)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
