# Jetriever v1.0.0 method card

## Declared method

Jetriever is the fixed Jev-only, top-5, no-expansion configuration selected for
the Agent Memory Challenge commercial leaderboard. It is derived from an
offline comparison against the prior ReFind implementation, but it does not run
ReFind's BM25 planner at evaluation time.

For a query `q` and every Add chunk `m` belonging to the exact `user_id`, the
service asks Jev a binary Noul question:

> Does this memory chunk contain personal information needed to give a correct,
> personalized response to the user query, including indirectly relevant
> information? Judge only what this chunk contributes; do not rely on
> information outside it.

The query, optional answer choices, and raw chunk are supplied to OpenRouter's
Decisions API with model `~typesafe/jev-latest`. Jetriever sorts candidates by
the returned numeric Noul score and emits at most five. Equal scores preserve
SQLite ingestion order for deterministic output.

## Competition adaptations

- Add is synchronous, transactional, and idempotent by `request_id`.
- Retrieval isolation is exact `user_id`; `session_id` is not an access scope.
- One Add request is one candidate. Ordered messages within it remain ordered.
- The request's `top_k` is a hard cap, while this method's own cap is five.
- Search returns evidence only; answer generation is outside the memory API.
- Memory/question text and provider responses are not written to logs.
- Stored evaluation data is swept once per minute and expires after at most 30 days.
- Endpoint authentication is independent from the server-side OpenRouter key.

## Evidence boundary

The local MAB probe that motivated this choice is not an official Cycle 2
leaderboard score. It tested Jev-only top-5 retrieval on a fixed 60-question MAB
subset, followed by the matched answer/judge pipeline. That result supports the
configuration choice but does not establish its Full benchmark score, latency,
or production cost. The live leaderboard run remains necessary.
