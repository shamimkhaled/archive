# Deploy checklist for Fly.io
#
# 1) Install flyctl: https://fly.io/docs/hands-on/install-flyctl/
# 2) Log in: fly auth login
# 3) From this directory:
#      fly apps create bcp-document-hub   # or keep the name in fly.toml
#      fly volumes create bcp_uploads --region iad --size 3
# 4) Set secrets (never commit these):
#      fly secrets set \
#        JWT_SECRET_KEY="$(openssl rand -hex 32)" \
#        DATABASE_URL="postgresql://USER:PASS@HOST/db?sslmode=require" \
#        QDRANT_URL="https://YOUR-CLUSTER.cloud.qdrant.io" \
#        QDRANT_API_KEY="..." \
#        REDIS_URL="rediss://default:TOKEN@HOST:6379" \
#        OPENROUTER_API_KEY="..." \
#        RESEND_API_KEY="..." \
#        RESEND_FROM="onboarding@resend.dev" \
#        AWS_ACCESS_KEY_ID="..." \
#        AWS_SECRET_ACCESS_KEY="..." \
#        AWS_BUCKET_NAME="..." \
#        AWS_REGION="auto" \
#        AWS_S3_ENDPOINT_URL="https://xxxxx.r2.cloudflarestorage.com"
#
# Notes:
# - DATABASE_URL may be Neon `postgresql://...`; the app converts it to asyncpg.
# - Upstash: set REDIS_URL="rediss://default:TOKEN@HOST:6379"
#   OR set UPSTASH_REDIS_REST_URL + UPSTASH_REDIS_REST_TOKEN (app builds rediss://).
#   Do NOT leave REDIS_URL=redis://127.0.0.1:6379 on Fly — that only works locally.
# - OpenRouter is used automatically when OPENAI_API_KEY is unset/placeholder.
# - Demo storage: STORAGE_BACKEND=local (Fly volume at /data/uploads).
# - Production object storage: set STORAGE_BACKEND=s3 and AWS_* / R2 secrets.
#
# 5) Deploy:
#      fly deploy
#
# 6) Bootstrap admin (one-time):
#      fly ssh console -C "python scripts/create_admin_user.py --username admin"
#
# 7) Open:
#      fly apps open
