# Deployment guide

Jetriever is designed to run behind HTTPS as a single-worker FastAPI service.
The application stores state in SQLite, so mount `/data` on durable storage and
run only one writer process for a given database file.

## Docker Compose

```bash
cp .env.example .env
# Fill OPENROUTER_API_KEY and JETRIEVER_API_KEY locally.
docker compose up --build -d
docker compose ps
curl http://127.0.0.1:8000/health
```

Terminate TLS at a reverse proxy or managed load balancer. Keep `/health`
public and protect `/v1/memories/add` and `/v1/memories/search` with the
dedicated service credential. Never reuse the provider credential as the
client-facing service credential.

## Persistence and retention

The container writes SQLite data under `/data/jetriever.sqlite3`. Evaluation
records are removed by a startup cleanup and a once-per-minute retention sweep.
The maximum supported `RETENTION_DAYS` value is 30.

Compose writes audit logs to the persistent `/logs` volume. For a custom
deployment, set `AUDIT_LOG_PATH` to a writable mounted volume or forward process
logs to an external collector. Audit records omit request content, memory text,
provider responses, and credentials.

## Capacity and failure behavior

`JEV_CONCURRENCY` is global to the process, not per Search request. A Search
returns successfully only after every candidate has a valid score. If a
candidate exhausts its retry budget, the entire Search returns HTTP 429 with a
`Retry-After` header so the caller can safely retry without accepting an
incomplete ranking.

Monitor provider balance, HTTP failures, latency, disk capacity, and the audit
log. Do not set a reverse-proxy timeout shorter than the longest expected Search.
