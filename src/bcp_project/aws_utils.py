import os
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from .config import _is_placeholder, load_environment

load_environment()
S3_CONFIG = Config(signature_version="s3v4")


def use_local_storage() -> bool:
    """
    Demo/default: store PDFs on local disk under UPLOAD_DIR.
    Set STORAGE_BACKEND=s3 (and real AWS_* / bucket) for object storage.
    STORAGE_BACKEND=auto uses S3 only when AWS_BUCKET_NAME is a real value.

    On Railway/production, local disk is ephemeral — prefer S3 unless
    ALLOW_EPHEMERAL_UPLOADS=1 is set intentionally.
    """
    mode = (os.getenv("STORAGE_BACKEND") or "auto").strip().lower()
    if mode in {"local", "disk", "filesystem"}:
        return True
    if mode in {"s3", "bucket", "object"}:
        return False
    return _is_placeholder(os.getenv("AWS_BUCKET_NAME"))


def storage_backend_name() -> str:
    return "local" if use_local_storage() else "s3"


def assert_production_storage_ready() -> None:
    """Fail loudly in production when uploads would vanish on container restart."""
    from .config import is_production

    if not is_production():
        return
    allow = (os.getenv("ALLOW_EPHEMERAL_UPLOADS") or "").strip().lower() in {"1", "true", "yes", "on"}
    if use_local_storage() and not allow:
        raise RuntimeError(
            "Production must use object storage. Set STORAGE_BACKEND=s3 and AWS_BUCKET_NAME, "
            "AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_REGION on Railway. "
            "(Local UPLOAD_DIR is wiped on redeploy.) Override with ALLOW_EPHEMERAL_UPLOADS=1 only for demos."
        )


def get_s3_client():
    region = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION")
    if region is None:
        raise RuntimeError("AWS_REGION or AWS_DEFAULT_REGION must be set for S3 access.")

    return boto3.client(
        "s3",
        aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
        region_name=region,
        endpoint_url=os.getenv("AWS_S3_ENDPOINT_URL") or None,
        config=S3_CONFIG,
    )


def probe_s3() -> dict:
    """Lightweight S3 connectivity check for /readyz."""
    bucket = os.getenv("AWS_BUCKET_NAME")
    if _is_placeholder(bucket):
        return {"ok": False, "error": "AWS_BUCKET_NAME not configured"}
    try:
        client = get_s3_client()
        client.head_bucket(Bucket=bucket)
        return {"ok": True, "bucket": bucket, "region": os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION")}
    except (BotoCoreError, ClientError, Exception) as exc:
        return {"ok": False, "bucket": bucket, "error": str(exc)[:200]}


def upload_pdf_to_s3(local_path: str, key: str) -> str:
    bucket = os.getenv("AWS_BUCKET_NAME")
    if _is_placeholder(bucket):
        raise RuntimeError("AWS_BUCKET_NAME must be set to upload files to S3.")
    prefix = os.getenv("AWS_PREFIX", "pdfs")
    remote_key = f"{prefix.rstrip('/')}/{key}"
    s3_client = get_s3_client()
    try:
        s3_client.upload_file(local_path, bucket, remote_key)
    except (BotoCoreError, ClientError) as exc:
        raise RuntimeError(f"Failed to upload PDF to S3: {exc}") from exc
    return f"s3://{bucket}/{remote_key}"


def store_pdf(local_path: str, key: str) -> str:
    """Persist an uploaded PDF; returns file_location for DocumentRecord."""
    assert_production_storage_ready()
    if use_local_storage():
        # Caller already wrote bytes under UPLOAD_DIR / key
        path = Path(local_path)
        if not path.exists():
            raise RuntimeError(f"Local PDF missing at {local_path}")
        return key
    return upload_pdf_to_s3(local_path, key)


def get_presigned_pdf_url(s3_uri: str, expires_in: int = 3600) -> str:
    if not s3_uri.startswith("s3://"):
        raise ValueError("Expected S3 URI for presigned URL generation.")
    _, path = s3_uri.split("s3://", 1)
    bucket, key = path.split("/", 1)
    s3_client = get_s3_client()
    try:
        return s3_client.generate_presigned_url(
            ClientMethod="get_object",
            Params={"Bucket": bucket, "Key": key},
            ExpiresIn=expires_in,
        )
    except (BotoCoreError, ClientError) as exc:
        raise RuntimeError(f"Failed to generate presigned URL: {exc}") from exc
