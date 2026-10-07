"""Shared FastAPI wiring: lifespan, error mapping, health endpoints."""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from fastapi.exceptions import RequestValidationError

from ..config import Settings, get_settings, integration_status
from ..container import Container
from ..db import migrate
from ..logging_setup import configure_logging
from .. import telemetry
from ..services.errors import ServiceError

log = logging.getLogger("salesagent.api")


def lifespan_factory(
    pool_size: int, on_start: Callable[[FastAPI, Container], Any] | None = None
) -> Callable[[FastAPI], Any]:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        settings: Settings = get_settings()
        configure_logging(settings.log_level)
        telemetry.configure(settings.otel_service_name, settings.otel_exporter_otlp_endpoint, settings.environment)
        settings.require_secrets()
        await migrate(settings.database_url.get_secret_value())
        container = await Container.create(settings, pool_size=pool_size)
        app.state.c = container
        extra = await on_start(app, container) if on_start else None
        try:
            yield
        finally:
            if extra is not None and hasattr(extra, "aclose"):
                await extra.aclose()
            await container.close()

    return lifespan


def install(app: FastAPI) -> None:
    @app.exception_handler(ServiceError)
    async def _service_error(_: Request, exc: ServiceError) -> JSONResponse:
        body: dict[str, Any] = {"error": {"code": exc.code, "message": exc.message}}
        if exc.say:
            body["error"]["say"] = exc.say
        return JSONResponse(body, status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        errs = [{"field": ".".join(str(p) for p in e["loc"][1:]), "message": e["msg"]} for e in exc.errors()]
        return JSONResponse({"error": {"code": "INVALID_REQUEST", "message": "invalid request", "details": errs}},
                            status_code=422)

    @app.exception_handler(ValidationError)
    async def _model_validation(_: Request, exc: ValidationError) -> JSONResponse:
        return JSONResponse({"error": {"code": "INVALID_REQUEST", "message": str(exc)[:500]}}, status_code=422)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        ref = uuid.uuid4().hex[:12]
        log.exception("unhandled error ref=%s path=%s", ref, request.url.path)
        return JSONResponse({"error": {"code": "INTERNAL", "message": f"internal error (ref {ref})"}}, status_code=500)

    @app.middleware("http")
    async def _timing(request: Request, call_next: Callable[..., Any]) -> Any:
        start = time.perf_counter()
        response = await call_next(request)
        ms = (time.perf_counter() - start) * 1000
        response.headers["Server-Timing"] = f"app;dur={ms:.1f}"
        response.headers["X-Content-Type-Options"] = "nosniff"
        if request.url.path not in ("/healthz",):
            log.info("%s %s %s %.1fms", request.method, request.url.path, response.status_code, ms)
        return response

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz", include_in_schema=False)
    async def readyz(request: Request) -> JSONResponse:
        c: Container = request.app.state.c
        try:
            async with c.pool.connection() as conn:
                await conn.execute("SELECT 1")
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"status": "db_unavailable", "error": str(exc)[:200]}, status_code=503)
        return JSONResponse({"status": "ready", "integrations": integration_status(c.settings)})
