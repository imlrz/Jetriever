from __future__ import annotations

import asyncio
import logging
import math
import random
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import ClassVar, Protocol

import httpx

from .audit import audit_event, elapsed_ms, hash_identifier
from .store import MemoryChunk

LOGGER = logging.getLogger("jetriever.jev")

JEV_INSTRUCTION = (
    "Does this memory chunk contain personal information needed to give a correct, "
    "personalized response to the user query, including indirectly relevant information? "
    "Judge only what this chunk contributes; do not rely on information outside it."
)


class JevScorer(Protocol):
    async def score(
        self, query: str, options: list[str] | None, chunk: MemoryChunk
    ) -> float: ...


class JevRateLimitError(RuntimeError):
    """Jev remained rate limited after the bounded internal retry budget."""

    def __init__(self, retry_after_seconds: int) -> None:
        self.retry_after_seconds = max(1, retry_after_seconds)
        super().__init__(
            f"Jev rate limit persisted; retry after {self.retry_after_seconds} seconds"
        )


def parse_retry_after(value: str | None, default_seconds: float) -> int:
    """Parse Retry-After seconds or an HTTP date into a positive whole second."""

    if value:
        try:
            return max(1, math.ceil(float(value)))
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(value)
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=timezone.utc)
                remaining = (retry_at - datetime.now(timezone.utc)).total_seconds()
                return max(1, math.ceil(remaining))
            except (TypeError, ValueError, OverflowError):
                pass
    return max(1, math.ceil(default_seconds))


class OpenRouterJevScorer:
    """Score one query-memory pair with OpenRouter's Jev Decisions API."""

    RETRYABLE_STATUS: ClassVar[set[int]] = {
        408,
        409,
        425,
        429,
        500,
        502,
        503,
        504,
        524,
    }

    def __init__(
        self,
        api_key: str,
        url: str,
        model: str,
        timeout_seconds: float,
        max_retries: int,
        concurrency: int,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.api_key = api_key
        self.url = url
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self._semaphore = asyncio.Semaphore(concurrency)
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=timeout_seconds,
            limits=httpx.Limits(
                max_connections=concurrency,
                max_keepalive_connections=concurrency,
            ),
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def score(
        self, query: str, options: list[str] | None, chunk: MemoryChunk
    ) -> float:
        if not self.api_key:
            raise RuntimeError("OPENROUTER_API_KEY is not configured")
        query_text = query
        if options:
            query_text += "\n\nOPTIONS:\n" + "\n".join(options)
        payload = {
            "model": self.model,
            "state": f"USER QUERY:\n{query_text}\n\nMEMORY CHUNK:\n{chunk.content}",
            "questions": {"useful": {"type": "noul", "instructions": JEV_INSTRUCTION}},
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/imlrz/Jetriever",
            "X-Title": "Jetriever",
        }
        last_error: Exception | None = None
        last_status: int | None = None
        last_retry_after = 1
        score_started = time.perf_counter()
        chunk_id_hash = hash_identifier(chunk.id)
        queue_started = time.perf_counter()
        async with self._semaphore:
            queue_ms = elapsed_ms(queue_started)
            audit_event(
                "jev_score_started",
                chunk_id_hash=chunk_id_hash,
                queue_ms=queue_ms,
                max_attempts=self.max_retries,
            )
            for attempt in range(self.max_retries):
                retry_delay = min(0.5 * (2**attempt), 8.0)
                attempt_started = time.perf_counter()
                audit_event(
                    "jev_attempt_started",
                    chunk_id_hash=chunk_id_hash,
                    attempt=attempt + 1,
                )
                try:
                    response = await self._client.post(
                        self.url, headers=headers, json=payload
                    )
                    last_status = response.status_code
                    if (
                        response.status_code not in self.RETRYABLE_STATUS
                        and not response.is_success
                    ):
                        response.raise_for_status()
                    if response.is_success:
                        body = response.json()
                        answer = (body.get("answers") or {}).get("useful") or {}
                        value = answer.get("noul")
                        if answer.get("type") != "noul" or not isinstance(
                            value, (int, float)
                        ):
                            raise ValueError(
                                "Jev returned a missing or invalid Noul score"
                            )
                        audit_event(
                            "jev_attempt_finished",
                            chunk_id_hash=chunk_id_hash,
                            attempt=attempt + 1,
                            status="success",
                            status_code=response.status_code,
                            duration_ms=elapsed_ms(attempt_started),
                            provider_request_id=(
                                response.headers.get("x-request-id")
                                or response.headers.get("x-openrouter-request-id")
                            ),
                        )
                        audit_event(
                            "jev_score_finished",
                            chunk_id_hash=chunk_id_hash,
                            status="success",
                            attempts=attempt + 1,
                            duration_ms=elapsed_ms(score_started),
                        )
                        return float(value)
                    last_error = RuntimeError(
                        f"Jev returned retryable HTTP {response.status_code}"
                    )
                    if response.status_code == 429:
                        last_retry_after = parse_retry_after(
                            response.headers.get("Retry-After"), retry_delay
                        )
                        retry_delay = last_retry_after
                    LOGGER.warning(
                        "Jev retryable response status=%s attempt=%s retry_after=%s",
                        response.status_code,
                        attempt + 1,
                        response.headers.get("Retry-After", ""),
                    )
                    audit_event(
                        "jev_attempt_finished",
                        chunk_id_hash=chunk_id_hash,
                        attempt=attempt + 1,
                        status="retryable_response",
                        status_code=response.status_code,
                        duration_ms=elapsed_ms(attempt_started),
                        retry_after=response.headers.get("Retry-After"),
                        provider_request_id=(
                            response.headers.get("x-request-id")
                            or response.headers.get("x-openrouter-request-id")
                        ),
                    )
                except asyncio.CancelledError:
                    audit_event(
                        "jev_attempt_finished",
                        chunk_id_hash=chunk_id_hash,
                        attempt=attempt + 1,
                        status="cancelled",
                        duration_ms=elapsed_ms(attempt_started),
                    )
                    audit_event(
                        "jev_score_finished",
                        chunk_id_hash=chunk_id_hash,
                        status="cancelled",
                        attempts=attempt + 1,
                        duration_ms=elapsed_ms(score_started),
                    )
                    raise
                except (httpx.HTTPError, TypeError, ValueError) as error:
                    last_error = error
                    LOGGER.warning(
                        "Jev retryable exception type=%s attempt=%s detail=%s",
                        type(error).__name__,
                        attempt + 1,
                        str(error)[:500],
                    )
                    audit_event(
                        "jev_attempt_finished",
                        chunk_id_hash=chunk_id_hash,
                        attempt=attempt + 1,
                        status="exception",
                        duration_ms=elapsed_ms(attempt_started),
                        error_type=type(error).__name__,
                    )
                if attempt + 1 < self.max_retries:
                    jitter = random.uniform(0, min(1.0, retry_delay * 0.25))
                    await asyncio.sleep(retry_delay + jitter)
        audit_event(
            "jev_score_finished",
            chunk_id_hash=chunk_id_hash,
            status="failed",
            attempts=self.max_retries,
            duration_ms=elapsed_ms(score_started),
            last_status=last_status,
            error_type=type(last_error).__name__ if last_error else None,
        )
        if last_status == 429:
            raise JevRateLimitError(last_retry_after) from last_error
        raise RuntimeError(
            f"Jev request failed after {self.max_retries} attempts: {last_error}"
        ) from last_error
