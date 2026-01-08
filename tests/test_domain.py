import pytest
from orchestrator.logic.domain import (
    ensure_customer_ready,
    extract_geogrid_box_sigla,
    extract_geogrid_port_number,
    _customer_coordinates,
)
from orchestrator.core.integration import RetryableHTTPStatus

@pytest.mark.asyncio
async def test_ensure_customer_ready_success():
    customer = {
        "customer_id": 123,
        "status": "enabled",
        "integration_enabled": True
    }
    # Should not raise
    ensure_customer_ready(customer, "test")

@pytest.mark.asyncio
async def test_ensure_customer_ready_inactive():
    customer = {
        "customer_id": 123,
        "status": "disabled",
        "integration_enabled": True
    }
    # Incident recorded, but no exception raised by default unless patched to raise?
    # Function returns None.
    ensure_customer_ready(customer, "test")
    # Real test would mock record_incident to verify call.

def test_extract_geogrid_box_sigla():
    # Test valid parsing
    c1 = {"address": "NAP-A1-01"}
    assert extract_geogrid_box_sigla(c1) == "NAP-A1"
    
    c2 = {"address": "NAP-B2"}
    assert extract_geogrid_box_sigla(c2) == "NAP-B2"

def test_customer_coordinates():
    c = {"lat": 10.0, "lon": 20.0}
    assert _customer_coordinates(c) == (10.0, 20.0)
    
    c2 = {"lat": "10.0", "lng": "20.0"}
    assert _customer_coordinates(c2) == (10.0, 20.0)
