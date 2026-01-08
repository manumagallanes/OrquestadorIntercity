import os
import pytest
from typing import AsyncGenerator
from httpx import AsyncClient, ASGITransport
from fastapi import FastAPI

# Mock env vars before importing app
os.environ["ISP_BASE_URL"] = "http://mock-isp"
os.environ["GEOGRID_BASE_URL"] = "http://mock-geogrid"
os.environ["LOG_LEVEL"] = "CRITICAL" # Reduce noise

from orchestrator.main import app

@pytest.fixture
async def client() -> AsyncGenerator[AsyncClient, None]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
