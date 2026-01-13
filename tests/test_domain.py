import pytest
from fastapi import HTTPException
from orchestrator.logic.domain import (
    ensure_customer_ready,
    extract_geogrid_box_sigla,
    extract_geogrid_port_number,
    _customer_coordinates,
    CORDOBA_LAT_RANGE,
    CORDOBA_LON_RANGE,
)

# Coordenadas dummy validas para pasar validaciones geograficas
VALID_LAT = CORDOBA_LAT_RANGE[0] + 0.01
VALID_LON = CORDOBA_LON_RANGE[0] + 0.01

@pytest.mark.asyncio
async def test_ensure_customer_ready_success():
    customer = {
        "customer_id": 123,
        "status": "enabled",
        "name": "Cliente Test",
        "doc_number": "12345678",
        "phone": "3584444444",
        "email": "test@intercity.com",
        "address": "Calle Test 123",
        "lat": VALID_LAT,
        "lon": VALID_LON,
        "plan_name": "Plan 100M",
        # Campos de red obligatorios
        "olt_id": 1,
        "board": 1,
        "pon": 1,
        "onu_sn": "ALCL00000001",
        "plan_id": 100,
        "ftthbox_id": 50,
        "ftth_port_id": 1,
    }
    # Should not raise exception
    ensure_customer_ready(customer, "test")

@pytest.mark.asyncio
async def test_ensure_customer_ready_inactive():
    customer = {
        "customer_id": 123,
        "status": "disabled",
        "name": "Cliente Inactivo",
        "doc_number": "111", 
        "address": "Calle",
    }
    # Ahora ensure_customer_ready levanta 412 si el cliente no está en estado valido (active/enabled)
    with pytest.raises(HTTPException) as excinfo:
        ensure_customer_ready(customer, "test")
    assert excinfo.value.status_code == 412

def test_extract_geogrid_box_sigla():
    # Test 1: Campo directo
    c1 = {"ftthbox_name": "NAP-A1"}
    assert extract_geogrid_box_sigla(c1) == "NAP-A1"
    
    # Test 2: Anidado en connection (nuevo modelo)
    c2 = {"connection": {"ftthbox": {"name": "NAP-B2"}}}
    assert extract_geogrid_box_sigla(c2) == "NAP-B2"
    
    # Test 3: Fallback a ftth_box_name
    c3 = {"ftth_box_name": "NAP-C3"}
    assert extract_geogrid_box_sigla(c3) == "NAP-C3"

def test_customer_coordinates():
    c = {"lat": -33.123, "lon": -64.123}
    lat, lon = _customer_coordinates(c)
    assert lat == -33.123
    assert lon == -64.123
    
    # Test parsing strings and 'lng' alias
    c2 = {"lat": "-33.999", "lng": "-64.999"}
    lat, lon = _customer_coordinates(c2)
    assert lat == -33.999
    assert lon == -64.999
