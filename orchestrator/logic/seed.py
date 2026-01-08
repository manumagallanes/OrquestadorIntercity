import logging
from ..core.config import EnvConfig

logger = logging.getLogger("orchestrator.logic.seed")

async def _seed_isp_customers(settings: EnvConfig) -> None:
    # Logic from main.py
    # Los clientes demo ya están embebidos en el mock de ISP-Cube.
    # return {"seeded": 0, "skipped": 0}
    pass

async def ensure_customer_seed(settings: EnvConfig) -> None:
    try:
        await _seed_isp_customers(settings)
    except Exception as exc:
        logger.warning("Unable to ensure customer seed: %s", exc)
