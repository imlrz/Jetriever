from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, Mock, patch

import httpx
from fastapi.testclient import TestClient

from app.audit import configure_audit_logging
from app.config import Settings
from app.jev import JEV_INSTRUCTION, JevRateLimitError, OpenRouterJevScorer
from app.main import create_app
from app.retriever import JetrieverRetriever
from app.store import MemoryChunk, MemoryStore


def settings(tmp_path, service_api_key: str = "") -> Settings:
    return Settings(
        database_path=tmp_path / "jetriever.sqlite3",
        openrouter_api_key="test-openrouter-key",
        service_api_key=service_api_key,
        jev_url="https://example.invalid/decisions",
        jev_model="~typesafe/jev-latest",
        jev_timeout_seconds=1,
        jev_concurrency=50,
        jev_max_retries=2,
        backend_retry_after_seconds=15,
        result_limit=5,
        retention_days=30,
        audit_log_path=tmp_path / "audit.jsonl",
        audit_log_max_bytes=1024 * 1024,
        audit_log_backups=2,
    )


def add_payload(
    request_id: str = "run:chunk-0",
    user_id: str = "run:user-0",
    session_id: str = "run:session-0",
    content: str = "I adopted a cat named Luna.",
    role: str = "user",
    timestamp: int | None = 1704067200000,
) -> dict:
    return {
        "request_id": request_id,
        "messages": [{"role": role, "timestamp": timestamp, "content": content}],
        "user_id": user_id,
        "session_id": session_id,
    }


class RecordingScorer:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[str] | None, MemoryChunk]] = []

    async def score(
        self, query: str, options: list[str] | None, chunk: MemoryChunk
    ) -> float:
        self.calls.append((query, options, chunk))
        marker = chunk.content.rsplit("score=", maxsplit=1)
        return float(marker[1]) if len(marker) == 2 else 0.5


def client_with_scorer(tmp_path, service_api_key: str = ""):
    configured = settings(tmp_path, service_api_key)
    store = MemoryStore(configured.database_path)
    scorer = RecordingScorer()
    retriever = JetrieverRetriever(store, configured, scorer=scorer)
    return TestClient(create_app(configured, store, retriever)), store, scorer


def test_add_is_synchronous_idempotent_and_immediately_searchable(tmp_path) -> None:
    client, _, scorer = client_with_scorer(tmp_path)
    payload = add_payload()

    first = client.post("/v1/memories/add", json=payload)
    retry = client.post("/v1/memories/add", json=payload)
    search = client.post(
        "/v1/memories/search",
        json={
            "query": "What is the cat's name?",
            "user_id": payload["user_id"],
            "top_k": 100,
        },
    )

    expected = {
        "success": True,
        "request_id": payload["request_id"],
        "user_id": payload["user_id"],
        "session_id": payload["session_id"],
    }
    assert first.status_code == 200 and first.json() == expected
    assert retry.status_code == 200 and retry.json() == expected
    assert search.status_code == 200
    assert len(search.json()["data"]) == 1
    assert "Luna" in search.json()["data"][0]["content"]
    assert len(scorer.calls) == 1


def test_audit_log_covers_requests_without_recording_payload_text(tmp_path) -> None:
    client, _, _ = client_with_scorer(tmp_path)
    payload = add_payload(content="PRIVATE MEMORY SENTINEL")
    assert client.post("/add", json=payload).status_code == 200
    assert (
        client.post(
            "/search",
            json={
                "query": "PRIVATE QUERY SENTINEL",
                "user_id": payload["user_id"],
                "top_k": 5,
            },
        ).status_code
        == 200
    )

    raw_log = (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
    records = [json.loads(line) for line in raw_log.splitlines()]
    events = [record["event"] for record in records]
    assert events.count("http_request_started") == 2
    assert events.count("http_request_finished") == 2
    assert "add_finished" in events
    assert "search_started" in events
    assert "search_finished" in events
    assert "rerank_started" in events
    assert "rerank_finished" in events
    assert "PRIVATE MEMORY SENTINEL" not in raw_log
    assert "PRIVATE QUERY SENTINEL" not in raw_log
    assert payload["user_id"] not in raw_log


def test_conflicting_request_id_is_rejected(tmp_path) -> None:
    client, _, _ = client_with_scorer(tmp_path)
    assert client.post("/add", json=add_payload()).status_code == 200
    conflict = client.post(
        "/add", json=add_payload(content="This is a different payload.")
    )
    assert conflict.status_code == 409


def test_add_backend_failure_returns_429(tmp_path) -> None:
    configured = settings(tmp_path)
    store = MemoryStore(configured.database_path)
    store.add = Mock(side_effect=RuntimeError("database temporarily unavailable"))
    retriever = JetrieverRetriever(store, configured, scorer=RecordingScorer())
    client = TestClient(create_app(configured, store, retriever))

    response = client.post("/add", json=add_payload())
    assert response.status_code == 429
    assert response.headers["Retry-After"] == "15"


def test_one_add_request_is_one_candidate_and_message_order_is_preserved(
    tmp_path,
) -> None:
    client, store, _ = client_with_scorer(tmp_path)
    payload = add_payload()
    payload["messages"] = [
        {
            "role": "user",
            "timestamp": 1704067201000,
            "content": "First in payload.",
        },
        {
            "role": "assistant",
            "timestamp": 1704067200000,
            "content": "Second in payload.",
        },
    ]
    assert client.post("/add", json=payload).status_code == 200

    chunks = store.list_ingested_chunks(payload["user_id"])
    assert len(chunks) == 1
    assert [message["content"] for message in chunks[0].messages] == [
        "First in payload.",
        "Second in payload.",
    ]
    assert chunks[0].content == (
        "USER: First in payload.\n\nASSISTANT: Second in payload."
    )


def test_search_isolated_by_exact_user_and_fixed_to_top_five(tmp_path) -> None:
    client, _, scorer = client_with_scorer(tmp_path)
    for index in range(7):
        response = client.post(
            "/add",
            json=add_payload(
                request_id=f"run:chunk-{index}",
                content=f"candidate {index} score={index / 10}",
            ),
        )
        assert response.status_code == 200
    client.post(
        "/add",
        json=add_payload(
            request_id="other:chunk-0",
            user_id="other:user",
            content="secret score=1.0",
        ),
    )

    response = client.post(
        "/search",
        json={
            "query": "personal query",
            "options": ["A. one", "B. two"],
            "user_id": "run:user-0",
            "top_k": 100,
        },
    )
    data = response.json()["data"]
    assert response.status_code == 200
    assert len(data) == 5
    assert [item["score"] for item in data] == [0.6, 0.5, 0.4, 0.3, 0.2]
    assert all("secret" not in item["content"] for item in data)
    assert len(scorer.calls) == 7
    assert all(call[1] == ["A. one", "B. two"] for call in scorer.calls)


def test_requested_top_k_is_a_hard_lower_cap(tmp_path) -> None:
    client, _, _ = client_with_scorer(tmp_path)
    for index in range(3):
        client.post(
            "/add",
            json=add_payload(request_id=f"run:chunk-{index}", content=f"score={index}"),
        )
    response = client.post(
        "/search",
        json={"query": "q", "user_id": "run:user-0", "top_k": 2},
    )
    assert response.status_code == 200
    assert len(response.json()["data"]) == 2


def test_authentication_and_unauthenticated_health(tmp_path) -> None:
    client, _, _ = client_with_scorer(tmp_path, service_api_key="leaderboard-key")
    assert client.get("/health").status_code == 200
    assert client.get("/health").json()["auth_required"] is True
    assert client.post("/add", json=add_payload()).status_code == 401
    assert (
        client.post(
            "/add",
            json=add_payload(),
            headers={"X-Api-Key": "leaderboard-key"},
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/search",
            json={"query": "q", "user_id": "run:user-0", "top_k": 100},
            headers={"Authorization": "Bearer leaderboard-key"},
        ).status_code
        == 200
    )


def test_schema_rejects_extra_fields_and_top_k_over_100(tmp_path) -> None:
    client, _, _ = client_with_scorer(tmp_path)
    extra = client.post("/add", json={**add_payload(), "metadata": {}})
    too_large = client.post(
        "/search",
        json={"query": "q", "user_id": "run:user-0", "top_k": 101},
    )
    assert extra.status_code == 422
    assert too_large.status_code == 422


def test_openrouter_jev_request_matches_evaluated_prompt_shape(tmp_path) -> None:
    captured: dict = {}
    audit_path = tmp_path / "jev-audit.jsonl"
    configure_audit_logging(audit_path, 1024 * 1024, 2)

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        assert request.headers["Authorization"] == "Bearer private-key"
        return httpx.Response(
            200,
            json={"answers": {"useful": {"type": "noul", "noul": 0.87}}},
        )

    async def run() -> float:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            scorer = OpenRouterJevScorer(
                api_key="private-key",
                url="https://example.invalid/decisions",
                model="~typesafe/jev-latest",
                timeout_seconds=1,
                max_retries=1,
                concurrency=50,
                client=client,
            )
            chunk = MemoryChunk(
                id="mem-1",
                user_id="user-1",
                session_id="session-1",
                request_id="request-1",
                sequence=1,
                messages=(
                    {
                        "role": "user",
                        "content": "My cat is Luna.",
                        "timestamp": 1704067200000,
                    },
                ),
                timestamp_ms=1704067200000,
                created_at="2026-10-26T00:00:00Z",
            )
            return await scorer.score("Which pet?", ["A. cat", "B. dog"], chunk)

    assert asyncio.run(run()) == 0.87
    assert captured == {
        "model": "~typesafe/jev-latest",
        "state": (
            "USER QUERY:\nWhich pet?\n\nOPTIONS:\nA. cat\nB. dog\n\n"
            "MEMORY CHUNK:\nUSER: My cat is Luna."
        ),
        "questions": {"useful": {"type": "noul", "instructions": JEV_INSTRUCTION}},
    }
    raw_log = audit_path.read_text(encoding="utf-8")
    records = [json.loads(line) for line in raw_log.splitlines()]
    assert [record["event"] for record in records] == [
        "jev_score_started",
        "jev_attempt_started",
        "jev_attempt_finished",
        "jev_score_finished",
    ]
    assert "My cat is Luna" not in raw_log
    assert "Which pet?" not in raw_log
    assert "private-key" not in raw_log


def test_jev_429_honors_retry_after_then_succeeds() -> None:
    responses = iter(
        [
            httpx.Response(429, headers={"Retry-After": "3"}),
            httpx.Response(
                200,
                json={"answers": {"useful": {"type": "noul", "noul": 0.75}}},
            ),
        ]
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return next(responses)

    async def run() -> float:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            scorer = OpenRouterJevScorer(
                api_key="private-key",
                url="https://example.invalid/decisions",
                model="~typesafe/jev-latest",
                timeout_seconds=1,
                max_retries=2,
                concurrency=1,
                client=client,
            )
            chunk = MemoryChunk(
                id="mem-1",
                user_id="user-1",
                session_id="session-1",
                request_id="request-1",
                sequence=1,
                messages=({"role": "user", "content": "memory", "timestamp": None},),
                timestamp_ms=None,
                created_at="2026-10-29T00:00:00Z",
            )
            return await scorer.score("query", None, chunk)

    with (
        patch("app.jev.asyncio.sleep", new_callable=AsyncMock) as sleep,
        patch("app.jev.random.uniform", return_value=0),
    ):
        assert asyncio.run(run()) == 0.75
        sleep.assert_awaited_once_with(3)


def test_persistent_jev_429_is_returned_with_retry_after(tmp_path) -> None:
    class RateLimitedScorer:
        async def score(self, query, options, chunk):
            raise JevRateLimitError(11)

    configured = settings(tmp_path)
    store = MemoryStore(configured.database_path)
    retriever = JetrieverRetriever(store, configured, scorer=RateLimitedScorer())
    client = TestClient(create_app(configured, store, retriever))
    assert client.post("/add", json=add_payload()).status_code == 200

    response = client.post(
        "/search", json={"query": "q", "user_id": "run:user-0", "top_k": 5}
    )
    assert response.status_code == 429
    assert response.headers["Retry-After"] == "11"


def test_jev_network_failure_retries_then_succeeds() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise httpx.ConnectTimeout("temporary timeout", request=request)
        return httpx.Response(
            200,
            json={"answers": {"useful": {"type": "noul", "noul": 0.61}}},
        )

    async def run() -> float:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            scorer = OpenRouterJevScorer(
                api_key="private-key",
                url="https://example.invalid/decisions",
                model="~typesafe/jev-latest",
                timeout_seconds=1,
                max_retries=3,
                concurrency=1,
                client=client,
            )
            chunk = MemoryChunk(
                id="mem-1",
                user_id="user-1",
                session_id="session-1",
                request_id="request-1",
                sequence=1,
                messages=({"role": "user", "content": "memory", "timestamp": None},),
                timestamp_ms=None,
                created_at="2026-10-29T00:00:00Z",
            )
            return await scorer.score("query", None, chunk)

    with (
        patch("app.jev.asyncio.sleep", new_callable=AsyncMock) as sleep,
        patch("app.jev.random.uniform", return_value=0),
    ):
        assert asyncio.run(run()) == 0.61
        assert [call.args[0] for call in sleep.await_args_list] == [0.5, 1.0]
    assert calls == 3


def test_non_rate_limit_backend_failure_returns_429(tmp_path) -> None:
    class FailedScorer:
        async def score(self, query, options, chunk):
            raise RuntimeError("backend unavailable")

    configured = settings(tmp_path)
    store = MemoryStore(configured.database_path)
    retriever = JetrieverRetriever(store, configured, scorer=FailedScorer())
    client = TestClient(create_app(configured, store, retriever))
    assert client.post("/add", json=add_payload()).status_code == 200

    response = client.post(
        "/search", json={"query": "q", "user_id": "run:user-0", "top_k": 5}
    )
    assert response.status_code == 429
    assert response.headers["Retry-After"] == "15"


def test_any_jev_failure_rejects_the_entire_search(tmp_path) -> None:
    class PartialScorer:
        async def score(self, query, options, chunk):
            if "failed" in chunk.content:
                raise RuntimeError("one candidate failed")
            return float(chunk.content.rsplit("score=", maxsplit=1)[1])

    configured = settings(tmp_path)
    store = MemoryStore(configured.database_path)
    retriever = JetrieverRetriever(store, configured, scorer=PartialScorer())
    client = TestClient(create_app(configured, store, retriever))
    for index, content in enumerate(["score=0.9", "failed score=1.0", "score=0.7"]):
        assert (
            client.post(
                "/add",
                json=add_payload(request_id=f"partial:{index}", content=content),
            ).status_code
            == 200
        )

    response = client.post(
        "/search", json={"query": "q", "user_id": "run:user-0", "top_k": 5}
    )
    assert response.status_code == 429
    assert response.headers["Retry-After"] == "15"
