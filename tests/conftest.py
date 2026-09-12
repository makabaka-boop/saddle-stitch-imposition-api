"""Shared pytest fixtures."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture()
def client(tmp_path, monkeypatch) -> TestClient:
    # Each test gets an isolated SQLite database; entering the TestClient
    # runs the app lifespan, which applies the migrations against it —
    # exactly what happens when the real process starts.
    monkeypatch.setenv("QUOTE_DB_PATH", str(tmp_path / "quotes.sqlite3"))
    # raise_server_exceptions keeps 422 validation responses observable
    # while still surfacing real 500s.
    with TestClient(app, raise_server_exceptions=True) as test_client:
        yield test_client
