<div align="center">

# Jetriever

### Similarity ✗ &nbsp;·&nbsp; Utility ✓

[![Code](https://img.shields.io/badge/Code-Open_Source-2563eb?style=flat-square)](./app)
[![Tests](https://img.shields.io/github/actions/workflow/status/imlrz/Jetriever/tests.yml?branch=main&style=flat-square&label=Tests)](https://github.com/imlrz/Jetriever/actions)
[![License](https://img.shields.io/badge/License-MIT-059669?style=flat-square)](./LICENSE)
[![Technical Report](https://img.shields.io/badge/Technical_Report-Coming_Soon-7c3aed?style=flat-square)](#citation)

[English](./README.md) · [简体中文](./README.zh-CN.md)

</div>

Jetriever is an agent-memory retrieval service that asks a utility-oriented
reranker which stored interactions are useful for the current query. It stores
the raw chunks supplied through `Add`, exhaustively scores every chunk scoped
to the exact `user_id`, and returns the five highest-scoring evidence chunks.
It does not generate the final answer or expand results with neighboring turns.

The repository contains the complete Add/Search API, persistent SQLite store,
bounded provider concurrency, retry and rate-limit handling, privacy-preserving
audit logs, Docker packaging, and contract tests.

## Method

For each query-memory pair, Jetriever calls OpenRouter's Decisions API with
`~typesafe/jev-latest` and requests a Noul utility score. Search then:

1. loads only chunks belonging to the exact `user_id`;
2. scores every candidate with a global concurrency bound;
3. requires every candidate score to succeed, so incomplete reranking is never
   returned as a valid top-5;
4. sorts by utility and returns at most `min(top_k, 5)` raw evidence chunks.

See the [method card](./docs/METHOD_CARD.md) for the evidence boundary and
competition-specific adaptations.

## API

The service exposes:

- `POST /v1/memories/add`
- `POST /v1/memories/search`
- `GET /health`

`/add` and `/search` are compatibility aliases. Add is synchronous and
idempotent by `request_id`; HTTP 200 means every submitted message is stored
and immediately searchable. Retrieval isolation always uses the exact
`user_id`. `session_id` is retained as source metadata, not as an isolation
scope.

## Quick start

Requirements: Docker Compose and an OpenRouter key with access to the Decisions
API.

```bash
cp .env.example .env
# Fill OPENROUTER_API_KEY and choose a private JETRIEVER_API_KEY in .env.
docker compose up --build -d
curl http://127.0.0.1:8000/health
```

Run the live smoke test after startup:

```bash
export JETRIEVER_API_KEY="the same local service key"
python scripts/smoke_test.py http://127.0.0.1:8000
```

Never commit `.env`, API keys, databases, audit logs, or evaluation payloads.

## Configuration

| Variable | Default | Purpose |
|---|---:|---|
| `OPENROUTER_API_KEY` | required | Server-side provider credential |
| `JETRIEVER_API_KEY` | optional outside Compose | Credential protecting Add/Search |
| `JEV_MODEL` | `~typesafe/jev-latest` | Decisions model |
| `JEV_CONCURRENCY` | `32` | Global concurrent candidate scores |
| `JEV_TIMEOUT_SECONDS` | `20` | Timeout for one provider attempt |
| `JEV_MAX_RETRIES` | `5` | Maximum attempts per candidate |
| `BACKEND_RETRY_AFTER_SECONDS` | `15` | Retry delay returned for backend failures |
| `JETRIEVER_RESULT_LIMIT` | `5` | Fixed response cap |
| `RETENTION_DAYS` | `30` | Automatic data-retention window |

The service maps transient provider or storage failures to HTTP 429 with a
`Retry-After` header. Provider-native 429 responses retain their supplied
retry delay. Audit logs contain event metadata and hashed identifiers, never
memory text, queries, provider responses, or credentials.

## Development

The project supports Python 3.9 or newer. This repository's experiments use
the `longmemeval` Conda environment:

```bash
conda run -n longmemeval python -m pip install -r requirements-dev.txt
conda run -n longmemeval python -m ruff check app tests
conda run -n longmemeval python -m pytest
```

Tests mock the provider API and cover synchronous visibility, idempotency,
user isolation, authentication, raw-chunk granularity, option forwarding,
top-5 ordering, retry behavior, all-or-nothing reranking, and log redaction.

## Previous work

Jetriever continues our work on agent memory:

- **ReFind** — [paper](https://arxiv.org/abs/2608.12888) ·
  [code](https://github.com/imlrz/ReFind). ReFind ranked second on the first
  Agent Memory Leaderboard Academic Textual track and studies agent-controlled
  search over raw chat logs.
- **InMind** — [paper](https://arxiv.org/abs/2607.24368) ·
  [code](https://github.com/imlrz/InMind) ·
  [project page](https://keep-it-inmind.github.io/). InMind evaluates memories
  whose relevance depends on implicit world-knowledge associations.

## Citation

The technical report is being prepared. For now, cite the software metadata in
[`CITATION.cff`](./CITATION.cff).

## License

Jetriever is released under the [MIT License](./LICENSE).
