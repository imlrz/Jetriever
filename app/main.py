from __future__ import annotations

import asyncio
import hmac
import logging
import time
from contextlib import asynccontextmanager, suppress
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Request

from . import __version__
from .audit import (
    TRACE_ID,
    audit_event,
    configure_audit_logging,
    elapsed_ms,
    hash_identifier,
    new_trace_id,
)
from .config import Settings
from .jev import JevRateLimitError
from .models import (
    AddRequest,
    AddResponse,
    HealthResponse,
    SearchRequest,
    SearchResponse,
)
from .retriever import JetrieverRetriever
from .store import ConflictingRequestError, MemoryStore

LOGGER = logging.getLogger("jetriever")


def create_app(
    settings: Settings | None = None,
    store: MemoryStore | None = None,
    retriever: JetrieverRetriever | None = None,
) -> FastAPI:
    resolved_settings = settings or Settings.from_env()
    resolved_store = store or MemoryStore(
        resolved_settings.database_path,
        retention_days=resolved_settings.retention_days,
    )
    resolved_retriever = retriever or JetrieverRetriever(
        resolved_store, resolved_settings
    )
    configure_audit_logging(
        resolved_settings.audit_log_path,
        resolved_settings.audit_log_max_bytes,
        resolved_settings.audit_log_backups,
    )

    async def retention_worker() -> None:
        while True:
            await asyncio.sleep(60)
            await asyncio.to_thread(resolved_store.delete_expired)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        resolved_store.delete_expired()
        cleanup_task = asyncio.create_task(retention_worker())
        try:
            yield
        finally:
            cleanup_task.cancel()
            with suppress(asyncio.CancelledError):
                await cleanup_task
            await resolved_retriever.aclose()

    application = FastAPI(
        title="Jetriever Agent Memory API",
        version=__version__,
        description="Utility-oriented top-5 evidence retrieval for agent memory.",
        lifespan=lifespan,
    )
    application.state.settings = resolved_settings
    application.state.store = resolved_store
    application.state.retriever = resolved_retriever

    @application.middleware("http")
    async def audit_external_request(request: Request, call_next):
        trace_id = new_trace_id()
        client_request_id_hash = hash_identifier(request.headers.get("X-Request-ID"))
        token = TRACE_ID.set(trace_id)
        started = time.perf_counter()
        audit_event(
            "http_request_started",
            method=request.method,
            path=request.url.path,
            content_length=request.headers.get("content-length"),
            client_request_id_hash=client_request_id_hash,
        )
        try:
            response = await call_next(request)
        except BaseException as error:
            audit_event(
                "http_request_finished",
                method=request.method,
                path=request.url.path,
                status_code=500,
                duration_ms=elapsed_ms(started),
                error_type=type(error).__name__,
                client_request_id_hash=client_request_id_hash,
            )
            raise
        else:
            audit_event(
                "http_request_finished",
                method=request.method,
                path=request.url.path,
                status_code=response.status_code,
                duration_ms=elapsed_ms(started),
                retry_after=response.headers.get("Retry-After"),
                client_request_id_hash=client_request_id_hash,
            )
            response.headers["X-Trace-ID"] = trace_id
            return response
        finally:
            TRACE_ID.reset(token)

    def require_api_key(
        authorization: Annotated[str | None, Header()] = None,
        x_api_key: Annotated[str | None, Header(alias="X-Api-Key")] = None,
    ) -> None:
        expected = resolved_settings.service_api_key
        if not expected:
            return
        candidates = [x_api_key or ""]
        if authorization:
            scheme, _, credential = authorization.partition(" ")
            if scheme.lower() in {"bearer", "token"}:
                candidates.append(credential)
        if not any(
            hmac.compare_digest(candidate, expected) for candidate in candidates
        ):
            raise HTTPException(status_code=401, detail="invalid API credential")

    protected = [Depends(require_api_key)]

    @application.get("/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        return HealthResponse(
            version=__version__,
            auth_required=bool(resolved_settings.service_api_key),
        )

    def add_memory(payload: AddRequest) -> AddResponse:
        started = time.perf_counter()
        fields = {
            "request_id_hash": hash_identifier(payload.request_id),
            "user_id_hash": hash_identifier(payload.user_id),
            "session_id_hash": hash_identifier(payload.session_id),
            "message_count": len(payload.messages),
            "content_chars": sum(len(message.content) for message in payload.messages),
        }
        try:
            inserted = resolved_store.add(payload)
        except ConflictingRequestError as error:
            audit_event(
                "add_finished",
                **fields,
                status_code=409,
                duration_ms=elapsed_ms(started),
                error_type=type(error).__name__,
            )
            raise HTTPException(status_code=409, detail=str(error)) from error
        except Exception as error:
            LOGGER.exception("Add backend failed; asking the evaluator to retry")
            audit_event(
                "add_finished",
                **fields,
                status_code=429,
                duration_ms=elapsed_ms(started),
                error_type=type(error).__name__,
            )
            raise HTTPException(
                status_code=429,
                detail="memory service temporarily unavailable",
                headers={
                    "Retry-After": str(resolved_settings.backend_retry_after_seconds)
                },
            ) from error
        audit_event(
            "add_finished",
            **fields,
            status_code=200,
            inserted=inserted,
            duration_ms=elapsed_ms(started),
        )
        return AddResponse(
            request_id=payload.request_id,
            user_id=payload.user_id,
            session_id=payload.session_id,
        )

    async def search_memory(payload: SearchRequest) -> SearchResponse:
        started = time.perf_counter()
        fields = {
            "user_id_hash": hash_identifier(payload.user_id),
            "top_k": payload.top_k,
            "options_count": len(payload.options or []),
            "query_chars": len(payload.query),
        }
        audit_event("search_started", **fields)
        try:
            results, stats = await resolved_retriever.search(
                query=payload.query,
                options=payload.options,
                user_id=payload.user_id,
                top_k=payload.top_k,
            )
        except JevRateLimitError as error:
            LOGGER.warning("Search rate limited: %s", error)
            audit_event(
                "search_finished",
                **fields,
                status_code=429,
                duration_ms=elapsed_ms(started),
                error_type=type(error).__name__,
                retry_after=error.retry_after_seconds,
            )
            raise HTTPException(
                status_code=429,
                detail="search temporarily rate limited",
                headers={"Retry-After": str(error.retry_after_seconds)},
            ) from error
        except Exception as error:
            LOGGER.exception("Search backend failed; asking the evaluator to retry")
            audit_event(
                "search_finished",
                **fields,
                status_code=429,
                duration_ms=elapsed_ms(started),
                error_type=type(error).__name__,
                retry_after=resolved_settings.backend_retry_after_seconds,
            )
            raise HTTPException(
                status_code=429,
                detail="search temporarily unavailable",
                headers={
                    "Retry-After": str(resolved_settings.backend_retry_after_seconds)
                },
            ) from error
        LOGGER.info(
            "Search completed: candidates=%d jev_requests=%d jev_failures=%d "
            "returned=%d",
            stats.candidates,
            stats.jev_requests,
            stats.jev_failures,
            stats.returned,
        )
        audit_event(
            "search_finished",
            **fields,
            status_code=200,
            duration_ms=elapsed_ms(started),
            candidates=stats.candidates,
            jev_requests=stats.jev_requests,
            jev_failures=stats.jev_failures,
            returned=stats.returned,
        )
        return SearchResponse(data=results)

    application.add_api_route(
        "/v1/memories/add",
        add_memory,
        methods=["POST"],
        response_model=AddResponse,
        dependencies=protected,
    )
    application.add_api_route(
        "/v1/memories/search",
        search_memory,
        methods=["POST"],
        response_model=SearchResponse,
        dependencies=protected,
    )
    application.add_api_route(
        "/add",
        add_memory,
        methods=["POST"],
        response_model=AddResponse,
        dependencies=protected,
        include_in_schema=False,
    )
    application.add_api_route(
        "/search",
        search_memory,
        methods=["POST"],
        response_model=SearchResponse,
        dependencies=protected,
        include_in_schema=False,
    )
    return application


app = create_app()
