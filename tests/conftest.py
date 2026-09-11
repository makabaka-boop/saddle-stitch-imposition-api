"""Shared pytest fixtures."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture()
def client() -> TestClient:
    # raise_server_exceptions keeps 422 validation responses observable
    # while still surfacing real 500s.
    return TestClient(app, raise_server_exceptions=True)
