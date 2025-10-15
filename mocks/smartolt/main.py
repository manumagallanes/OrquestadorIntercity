import asyncio
import logging
import os
from typing import Dict, List, Optional, Tuple

from fastapi import FastAPI, HTTPException, Query, status
from pydantic import BaseModel, Field


logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s smartolt :: %(message)s",
)
logger = logging.getLogger("smartolt_mock")


class AuthorizeRequest(BaseModel):
    olt_id: int = Field(..., ge=1)
    board: int = Field(..., ge=0)
    pon_port: int = Field(..., ge=0)
    onu_sn: str = Field(..., min_length=6, max_length=32)
    profile: str = Field(default="Internet_100M")


class ONU(BaseModel):
    onu_id: int
    status: str
    olt_id: int
    board: int
    pon_port: int
    onu_sn: str
    profile: str


class SmartOLTStore:
    def __init__(self) -> None:
        self._onus: Dict[Tuple[int, int, int, str], ONU] = {}
        self._counter = 1000

    def authorize(self, payload: AuthorizeRequest) -> ONU:
        key = self._make_key(payload)
        if key in self._onus:
            existing = self._onus[key]
            logger.info("ONU already authorized: %s", existing.onu_id)
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "message": "ONU already authorized",
                    "onu_id": existing.onu_id,
                },
            )

        self._counter += 1
        onu = ONU(
            onu_id=self._counter,
            status="authorized",
            **payload.model_dump(),
        )
        self._onus[key] = onu
        logger.info("Authorized ONU %s", onu.onu_id)
        return onu

    def reset(self) -> None:
        self._onus.clear()
        self._counter = 1000
        logger.info("SmartOLT store reset to empty state")

    def list_onus(
        self,
        olt_id: Optional[int] = None,
        onu_sn: Optional[str] = None,
    ) -> List[ONU]:
        items = list(self._onus.values())
        if olt_id is not None:
            items = [onu for onu in items if onu.olt_id == olt_id]
        if onu_sn is not None:
            items = [onu for onu in items if onu.onu_sn.lower() == onu_sn.lower()]
        return items

    @staticmethod
    def _make_key(payload: AuthorizeRequest) -> Tuple[int, int, int, str]:
        return (
            payload.olt_id,
            payload.board,
            payload.pon_port,
            payload.onu_sn.lower(),
        )

    def remove_onu(
        self,
        olt_id: int,
        board: int,
        pon_port: int,
        onu_sn: str,
    ) -> ONU:
        key = (olt_id, board, pon_port, onu_sn.lower())
        onu = self._onus.pop(key, None)
        if not onu:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"message": "ONU not found"},
            )
        logger.info("Removed ONU %s", onu.onu_id)
        return onu


store = SmartOLTStore()

app = FastAPI(
    title="SmartOLT Mock",
    description="Synthetic SmartOLT API for ONU provisioning tests",
    version="0.1.0",
)


def maybe_raise_simulated_error(simulate: Optional[str]) -> None:
    if simulate == "429":
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"message": "SmartOLT mock rate limit exceeded"},
        )
    if simulate == "400":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"message": "Malformed request simulated"},
        )


@app.post("/onu/authorize")
async def authorize_onu(
    payload: AuthorizeRequest,
    simulate: Optional[str] = Query(default=None),
):
    logger.info(
        "POST /onu/authorize simulate=%s onu_sn=%s",
        simulate,
        payload.onu_sn,
    )
    maybe_raise_simulated_error(simulate)
    if simulate == "timeout":
        await asyncio.sleep(12)
    authorized = store.authorize(payload)
    return {"status": "ok", "onu_id": authorized.onu_id, "message": "authorized"}


@app.get("/onus")
async def list_onus(
    olt_id: Optional[int] = Query(default=None, ge=1),
    onu_sn: Optional[str] = Query(default=None),
    simulate: Optional[str] = Query(default=None),
):
    logger.info("GET /onus simulate=%s olt_id=%s onu_sn=%s", simulate, olt_id, onu_sn)
    maybe_raise_simulated_error(simulate)
    if simulate == "timeout":
        await asyncio.sleep(12)
    onus = store.list_onus(olt_id=olt_id, onu_sn=onu_sn)
    return {"onus": [onu.model_dump() for onu in onus]}


@app.delete("/onus")
async def delete_onu(
    olt_id: int = Query(..., ge=1),
    board: int = Query(..., ge=0),
    pon_port: int = Query(..., ge=0),
    onu_sn: str = Query(..., min_length=6),
    simulate: Optional[str] = Query(default=None),
):
    logger.info(
        "DELETE /onus simulate=%s olt_id=%s board=%s pon=%s onu_sn=%s",
        simulate,
        olt_id,
        board,
        pon_port,
        onu_sn,
    )
    maybe_raise_simulated_error(simulate)
    if simulate == "timeout":
        await asyncio.sleep(12)
    removed = store.remove_onu(olt_id, board, pon_port, onu_sn)
    return {"status": "deleted", "onu_id": removed.onu_id}


@app.post("/reset")
async def reset():
    store.reset()
    return {"status": "reset"}


@app.get("/health")
async def health():
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "mocks.smartolt.main:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8003")),
        reload=bool(int(os.getenv("UVICORN_RELOAD", "0"))),
    )
