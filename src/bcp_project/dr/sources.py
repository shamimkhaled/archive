"""Connection and object helpers shared by the backup, restore, and failover drill scripts."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Dict, Optional, Tuple
from urllib.parse import parse_qsl, unquote, urlencode, urlparse, urlunparse

from ..config import normalize_database_url

LIBPQ_PARAMS = {"sslmode", "sslrootcert", "sslcert", "sslkey", "connect_timeout", "application_name", "options", "target_session_attrs"}


def libpq_parts(url: Optional[str] = None) -> Tuple[str, Dict[str, str]]:
    """A libpq URL without the password, plus the environment that carries it.

    pg_dump and pg_restore read PGPASSWORD from the environment, so the password never
    appears in the process list or in drill records.
    """
    parsed = urlparse(normalize_database_url(url))
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    if "ssl" in query:
        query.setdefault("sslmode", query.pop("ssl"))
    query = {k: v for k, v in query.items() if k in LIBPQ_PARAMS}
    host = parsed.hostname or "localhost"
    netloc = (unquote(parsed.username) + "@" if parsed.username else "") + host + (f":{parsed.port}" if parsed.port else "")
    dsn = urlunparse(("postgresql", netloc, parsed.path, "", urlencode(query), ""))
    env = {"PGPASSWORD": unquote(parsed.password)} if parsed.password else {}
    return dsn, env


def redact(url: Optional[str]) -> str:
    """Host class and database name for records: the endpoint id of a managed host is dropped."""
    parsed = urlparse(normalize_database_url(url))
    host = parsed.hostname or "localhost"
    labels = host.split(".")
    if len(labels) > 3 and not host.replace(".", "").isdigit():
        host = "*." + ".".join(labels[-3:])
    return f"{host}{':' + str(parsed.port) if parsed.port else ''}{parsed.path}"


def same_database(a: Optional[str], b: Optional[str]) -> bool:
    pa, pb = urlparse(normalize_database_url(a)), urlparse(normalize_database_url(b))
    return (pa.hostname or "localhost", pa.port or 5432, pa.path.strip("/")) == (pb.hostname or "localhost", pb.port or 5432, pb.path.strip("/"))


def object_location(file_location: Optional[str], original_location: Optional[str]) -> Optional[str]:
    """The stored bytes whose SHA-256 is on the version row: the original upload."""
    return (original_location or file_location or "").strip() or None


def read_object(location: str) -> bytes:
    if location.startswith("s3://"):
        from ..aws_utils import get_s3_client

        bucket, _, key = location[len("s3://") :].partition("/")
        return get_s3_client().get_object(Bucket=bucket, Key=key)["Body"].read()
    from ..intake import local_path_for

    path = local_path_for(location)
    assert path is not None
    return Path(path).read_bytes()


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
