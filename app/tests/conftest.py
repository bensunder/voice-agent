"""Integration fixtures: real Postgres (TEST_DATABASE_URL), apps run in-process."""

from __future__ import annotations

import base64
import os

TEST_DSN = os.environ.get("TEST_DATABASE_URL", "postgresql://postgres@localhost:55432/salesagent_test?host=/tmp")
os.environ.update(
    {
        "DATABASE_URL": TEST_DSN,
        "TOOL_API_KEY": "test-tool-api-key-0123456789abcdef",
        "CALL_TOKEN_SECRET": "test-call-token-secret-0123456789abcdef0123",
        "COCKPIT_USERNAME": "demo",
        "COCKPIT_PASSWORD": "test-cockpit-pass",
        "DEMO_MODE": "true",
        "DEMO_ALLOWLIST": "+18015550123",
        "CALLING_WINDOW_START_HOUR": "0",
        "CALLING_WINDOW_END_HOUR": "24",
        "SALES_REPS": "ana@contoso.com|Ana Lopez;ben@contoso.com|Ben Ortiz",
        "SECRETS_DIR": "/nonexistent",
        "LOG_LEVEL": "WARNING",
    }
)

import psycopg
import pytest

AUTH = {"Authorization": "Basic " + base64.b64encode(b"demo:test-cockpit-pass").decode()}
KEY = {"X-API-Key": "test-tool-api-key-0123456789abcdef"}


@pytest.fixture(scope="session", autouse=True)
def clean_db():
    with psycopg.connect(TEST_DSN, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    yield
