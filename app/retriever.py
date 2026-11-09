from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from .audit import audit_event, elapsed_ms, hash_identifier
from .config import Settings
from .jev import JevScorer, OpenRouterJevScorer
from .models import MemoryResult
from .store import MemoryChunk, MemoryStore


@dataclass(frozen=True)
class RetrievalStats:
    candidates: int
    jev_requests: int
    jev_failures: int
    returned: int


class JetrieverRetriever:
    """Exhaustively rerank raw Add chunks with Jev and return only top-5 evidence."""

    def __init__(
        self,
        store: MemoryStore,
        settings: Settings,
        scorer: JevScorer | None = None,
    ) -> None:
        self.store = store
        self.settings = settings
        self.scorer = scorer or OpenRouterJevScorer(
            api_key=settings.openrouter_api_key,
            url=settings.jev_url,
            model=settings.jev_model,
            timeout_seconds=settings.jev_timeout_seconds,
            max_retries=settings.jev_max_retries,
            concurrency=settings.jev_concurrency,
        )

    async def aclose(self) -> None:
        close = getattr(self.scorer, "aclose", None)
        if close is not None:
            await close()

    async def search(
        self,
        query: str,
        options: list[str] | None,
        user_id: str,
        top_k: int,
    ) -> tuple[list[MemoryResult], RetrievalStats]:
        chunks = self.store.list_ingested_chunks(user_id)
        started = time.perf_counter()
        audit_event(
            "rerank_started",
            user_id_hash=hash_identifier(user_id),
            candidates=len(chunks),
        )
        if not chunks:
            audit_event(
                "rerank_finished",
                user_id_hash=hash_identifier(user_id),
                candidates=0,
                jev_requests=0,
                jev_failures=0,
                returned=0,
                duration_ms=elapsed_ms(started),
            )
            return [], RetrievalStats(0, 0, 0, 0)

        tasks = [
            asyncio.create_task(self.scorer.score(query, options, chunk))
            for chunk in chunks
        ]
        try:
            outcomes = await asyncio.gather(*tasks, return_exceptions=True)
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        scored: list[tuple[MemoryChunk, float]] = []
        failed_outcomes: list[BaseException] = []
        for chunk, outcome in zip(chunks, outcomes):
            if isinstance(outcome, BaseException):
                failed_outcomes.append(outcome)
                continue
            scored.append((chunk, outcome))
        if failed_outcomes:
            audit_event(
                "rerank_finished",
                user_id_hash=hash_identifier(user_id),
                status="failed",
                candidates=len(chunks),
                jev_requests=len(chunks),
                jev_failures=len(failed_outcomes),
                returned=0,
                duration_ms=elapsed_ms(started),
            )
            raise failed_outcomes[0]
        ranked: list[tuple[MemoryChunk, float]] = sorted(
            scored,
            key=lambda item: (-item[1], item[0].sequence),
        )
        limit = min(top_k, self.settings.result_limit)
        results = [
            MemoryResult(
                id=chunk.id,
                content=chunk.content,
                score=score,
                created_at=chunk.created_at,
            )
            for chunk, score in ranked[:limit]
        ]
        audit_event(
            "rerank_finished",
            user_id_hash=hash_identifier(user_id),
            candidates=len(chunks),
            jev_requests=len(chunks),
            jev_failures=0,
            returned=len(results),
            duration_ms=elapsed_ms(started),
        )
        return results, RetrievalStats(len(chunks), len(chunks), 0, len(results))
