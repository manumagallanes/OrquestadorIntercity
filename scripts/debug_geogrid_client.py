import sys
sys.path.append("/app")

import asyncio
import logging
import httpx
# Ajuste de imports para correr dentro del contenedor
try:
    from orchestrator.core.config import get_settings
    from orchestrator.core.integration import fetch_json
except ImportError:
    # Si corre desde root sin estar instalado como paquete
    sys.path.append("/app")
    from orchestrator.core.config import get_settings
    from orchestrator.core.integration import fetch_json

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("debug")

async def main():
    settings = get_settings()
    client_kwargs, region = settings.http_client_kwargs("geogrid")
    
    # 1. Probar búsqueda por codigoIntegracao
    print("\n--- Buscando por codigoIntegracao=11122 ---")
    async with httpx.AsyncClient(**client_kwargs) as client:
        try:
            resp = await fetch_json(
                client, "GET", "/clientes",
                params={"codigoIntegracao": "11122"},
                service="geogrid", settings=settings, region_name=region
            )
            print(resp)
        except Exception as e:
            print(f"Error: {e}")

    # 2. Probar búsqueda por q=PLANTA EXTERNA
    print("\n--- Buscando por q=PLANTA EXTERNA ---")
    async with httpx.AsyncClient(**client_kwargs) as client:
        try:
            resp = await fetch_json(
                client, "GET", "/clientes",
                params={"q": "PLANTA EXTERNA"},
                service="geogrid", settings=settings, region_name=region
            )
            print(resp)
        except Exception as e:
            print(f"Error: {e}")

    # 3. Probar búsqueda por q=11122
    print("\n--- Buscando por q=11122 ---")
    async with httpx.AsyncClient(**client_kwargs) as client:
        try:
            resp = await fetch_json(
                client, "GET", "/clientes",
                params={"q": "11122"},
                service="geogrid", settings=settings, region_name=region
            )
            print(resp)
        except Exception as e:
            print(f"Error: {e}")

if __name__ == "__main__":
    asyncio.run(main())
