import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from orchestrator.api.v1.endpoints.customer import decommission_customer
from orchestrator.schemas.requests import DecommissionRequest
from fastapi import Request

@pytest.mark.asyncio
async def test_decommission_adds_baja_marker():
    # 1. Setup Mocks
    mock_settings = MagicMock()
    mock_state = MagicMock()
    mock_state.dry_run = False
    
    mock_request = MagicMock(spec=Request)
    mock_request.headers.get.return_value = "tester"

    # Datos del cliente simulado
    fake_customer = {
        "customer_id": 100,
        "name": "Juan Perez",
        "status": "inactive", # Importante: debe estar inactivo para permitir la baja
        "olt_id": 1, "board": 1, "pon": 1,
        "connection": {"ftthbox": {"name": "NAP-1"}, "ftth_port": {"nro": 1}}
    }

    # 2. Patch de dependencias externas
    with patch("orchestrator.api.v1.endpoints.customer.fetch_customer_record", return_value=fake_customer) as mock_fetch, \
         patch("orchestrator.api.v1.endpoints.customer.geogrid_service") as mock_geo:

        # Simulamos que GeoGrid encuentra al cliente
        mock_geo.get_cliente_by_codigo.return_value = {"id": 500, "nome": "Juan Perez"}
        mock_geo.remove_assignment = AsyncMock()
        mock_geo.resolve_port_id_by_sigla_and_number.return_value = 777
        mock_geo.comment_port = AsyncMock()
        mock_geo.upsert_cliente = AsyncMock(return_value=(500, "updated"))

        # 3. Ejecutar la acción
        payload = DecommissionRequest(customer_id=100, connection_code="TEST")
        await decommission_customer(payload, mock_request, mock_settings, mock_state)

        # 4. Verificaciones
        # Verificar que se llamó a upsert_cliente (actualizar nombre)
        assert mock_geo.upsert_cliente.called
        
        # Verificar el payload enviado: ¿Tiene [BAJA]?
        call_args = mock_geo.upsert_cliente.call_args
        sent_payload = call_args[0][1] # El segundo argumento posicional es el payload
        
        print(f"\nNombre enviado a GeoGrid: {sent_payload.get('nome')}")
        assert "[BAJA] Juan Perez" in sent_payload.get("nome")

