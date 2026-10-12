// Railway Infrastructure as Code for the Sonali Bank Archive System.
//
//   npm i -D railway@^3.12   (in .railway/, or globally with the CLI)
//   railway config plan      # preview
//   railway config apply     # apply to the linked environment
//
// Every service builds the root Dockerfile. SERVICE_ROLE is passed to the
// Dockerfile as a build arg: "worker" adds ffmpeg and faster-whisper.
// Secrets are shared variables on the environment (ctx.shared.*) so they are
// set once in the Railway dashboard and never committed. See docs/RAILWAY.md.
import { defineRailway, group, project, redis, service } from "railway/iac";

export default defineRailway((ctx) => {
  const prod = ctx.environment === "production";

  // Redis Streams with consumer groups needs a real Redis with no per-command
  // billing; Railway Redis is preferred over Upstash for the job queue.
  const cache = redis("redis");

  const shared = {
    APP_ENV: prod ? "production" : "staging",
    DATABASE_URL: ctx.shared.DATABASE_URL,
    REDIS_URL: cache.env.REDIS_URL,
    JWT_SECRET_KEY: ctx.shared.JWT_SECRET_KEY,
    QDRANT_URL: ctx.shared.QDRANT_URL,
    QDRANT_API_KEY: ctx.shared.QDRANT_API_KEY,
    STORAGE_BACKEND: "s3",
    AWS_REGION: ctx.shared.AWS_REGION,
    AWS_ACCESS_KEY_ID: ctx.shared.AWS_ACCESS_KEY_ID,
    AWS_SECRET_ACCESS_KEY: ctx.shared.AWS_SECRET_ACCESS_KEY,
    AWS_BUCKET_NAME: ctx.shared.AWS_BUCKET_NAME,
    AWS_S3_ENDPOINT_URL: ctx.shared.AWS_S3_ENDPOINT_URL,
    AWS_PREFIX: "pdfs",
    // AI providers are switchable at runtime (docs/AI_PROVIDERS.md).
    AI_CHAT_PROVIDER: ctx.shared.AI_CHAT_PROVIDER,
    AI_CHAT_MODEL: ctx.shared.AI_CHAT_MODEL,
    AI_EMBED_PROVIDER: ctx.shared.AI_EMBED_PROVIDER,
    AI_EMBED_MODEL: ctx.shared.AI_EMBED_MODEL,
    AI_EMBED_DIM: ctx.shared.AI_EMBED_DIM,
    AI_RERANK_PROVIDER: ctx.shared.AI_RERANK_PROVIDER,
    AI_EGRESS: ctx.shared.AI_EGRESS,
    OPENROUTER_API_KEY: ctx.shared.OPENROUTER_API_KEY,
    LOCAL_LLM_BASE_URL: ctx.shared.LOCAL_LLM_BASE_URL,
    LOCAL_LLM_MODEL: ctx.shared.LOCAL_LLM_MODEL,
    LOCAL_LLM_API_KEY: ctx.shared.LOCAL_LLM_API_KEY,
    LOCAL_EMBED_BASE_URL: ctx.shared.LOCAL_EMBED_BASE_URL,
    LOCAL_RERANK_BASE_URL: ctx.shared.LOCAL_RERANK_BASE_URL,
    RESEND_API_KEY: ctx.shared.RESEND_API_KEY,
    ENABLE_JOB_QUEUE: "1",
    LOG_FORMAT: "json",
  };

  const web = service("web", {
    start:
      "uvicorn bcp_project.main_api:app --host 0.0.0.0 --port $PORT --proxy-headers --forwarded-allow-ips='*' --workers ${WEB_CONCURRENCY:-2}",
    preDeploy: "python -m bcp_project.migrate",
    healthcheck: "/readyz",
    healthcheckTimeout: 120,
    replicas: prod ? 2 : 1,
    domains: prod ? ["archive.sonalibank.com.bd"] : [],
    env: {
      ...shared,
      SERVICE_ROLE: "web",
      SERVICE_NAME: "web",
      AUTO_MIGRATE: "0",
      WEB_CONCURRENCY: "2",
      METRICS_TOKEN: ctx.shared.METRICS_TOKEN,
      // The outbox-relay service publishes; uploads return 202 and poll status.
      OUTBOX_RELAY_MODE: "process",
      INGEST_MODE: "async",
      MFA_ENFORCE: "1",
      MFA_ENCRYPTION_KEY: ctx.shared.MFA_ENCRYPTION_KEY,
    },
  });

  // Scale ingest horizontally: consumers in one group share the stream, and
  // XAUTOCLAIM hands a crashed replica's entries to a live one.
  const worker = (
    name: string,
    queues: string,
    replicas: number,
    role: "pipeline" | "worker",
    extra: Record<string, string> = {},
  ) =>
    service(name, {
      start: "python -m bcp_project.jobs.worker",
      replicas,
      env: {
        ...shared,
        SERVICE_ROLE: role,
        SERVICE_NAME: name,
        JOB_WORKER_QUEUES: queues,
        AUTO_MIGRATE: "0",
        ...extra,
      },
    });

  const ingest = worker("worker-ingest", "ingest", prod ? 2 : 1, "pipeline");
  const meeting = worker("worker-meeting", "meeting", 1, "worker", {
    AI_STT_PROVIDER: "local_whisper",
    WHISPER_MODEL: "small",
    WHISPER_DEVICE: "cpu",
    WHISPER_COMPUTE_TYPE: "int8",
  });
  const notify = worker("worker-notify", "notify", 1, "pipeline");

  const relay = service("outbox-relay", {
    start: "python -m bcp_project.jobs.relay",
    replicas: 1,
    env: {
      ...shared,
      SERVICE_ROLE: "pipeline",
      SERVICE_NAME: "outbox-relay",
      AUTO_MIGRATE: "0",
    },
  });

  return project("sonali-bank-archive", {
    resources: [
      group("Data", [cache]),
      group("API", [web]),
      group("Pipeline", [relay, ingest, meeting, notify]),
    ],
  });
});
