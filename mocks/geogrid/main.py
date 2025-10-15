import logging
import os
from typing import Dict, List, Optional

from fastapi import Depends, FastAPI, HTTPException, Path, Query, status
from fastapi.responses import Response
from pydantic import BaseModel, Field


logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "DEBUG"),
    format="%(asctime)s %(levelname)s geogrid :: %(message)s",
)
logger = logging.getLogger("geogrid_mock")


class Location(BaseModel):
    lat: float = Field(..., ge=-90.0, le=90.0)
    lon: float = Field(..., ge=-180.0, le=180.0)


class FeaturePayload(BaseModel):
    name: str = Field(..., min_length=1)
    location: Location
    attrs: Dict[str, object] = Field(default_factory=dict)


class Feature(BaseModel):
    id: str
    name: str
    location: Location
    attrs: Dict[str, object]


class FeatureStore:
    def __init__(self) -> None:
        self._features: Dict[str, Feature] = {}
        self._index_by_onu_sn: Dict[str, str] = {}
        self._index_by_customer_id: Dict[int, str] = {}
        self._counter = 1

    def _next_id(self) -> str:
        feature_id = f"feature_{self._counter:05d}"
        self._counter += 1
        return feature_id

    def list_features(self) -> List[Feature]:
        return list(self._features.values())

    def create_feature(self, payload: FeaturePayload) -> Feature:
        onu_sn = str(payload.attrs.get("onu_sn", "")).strip().lower()
        if onu_sn:
            existing_id = self._index_by_onu_sn.get(onu_sn)
            if existing_id:
                logger.info(
                    "Conflict creating feature for onu_sn=%s existing_id=%s",
                    onu_sn,
                    existing_id,
                )
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={
                        "message": "Feature already exists for this ONU",
                        "id": existing_id,
                    },
                )

        feature_id = self._next_id()
        feature = Feature(id=feature_id, **payload.model_dump())
        self._features[feature_id] = feature
        if onu_sn:
            self._index_by_onu_sn[onu_sn] = feature_id
        
        logger.info("Attempting to process customer_id for feature %s", feature_id)
        customer_id_raw = payload.attrs.get("customer_id")
        logger.debug("create_feature: customer_id_raw=%s (type: %s)", customer_id_raw, type(customer_id_raw))

        if customer_id_raw is not None:
            try:
                customer_id_int = int(customer_id_raw)
                self._index_by_customer_id[customer_id_int] = feature_id
                logger.debug("create_feature: Successfully indexed customer_id=%s", customer_id_int)
            except (ValueError, TypeError) as e:
                logger.error("create_feature: Failed to convert customer_id %s to int: %s", customer_id_raw, e)
        else:
            logger.debug("create_feature: customer_id is None, skipping indexing")

        logger.info("Created feature %s", feature_id)
        return feature

    def get_feature(self, feature_id: str) -> Feature:
        feature = self._features.get(feature_id)
        if not feature:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"message": f"Feature {feature_id} not found"},
            )
        return feature

    def update_feature(self, feature_id: str, payload: FeaturePayload) -> Feature:
        feature = self.get_feature(feature_id)

        onu_sn_old = str(feature.attrs.get("onu_sn", "")).strip().lower()
        onu_sn_new = str(payload.attrs.get("onu_sn", "")).strip().lower()

        if onu_sn_new and onu_sn_new != onu_sn_old:
            existing_id = self._index_by_onu_sn.get(onu_sn_new)
            if existing_id and existing_id != feature_id:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={
                        "message": "Another feature already uses this ONU",
                        "id": existing_id,
                    },
                )

        if onu_sn_old and onu_sn_old != onu_sn_new:
            self._index_by_onu_sn.pop(onu_sn_old, None)

        if onu_sn_new:
            self._index_by_onu_sn[onu_sn_new] = feature_id

        old_customer_id_raw = feature.attrs.get("customer_id")
        logger.debug("update_feature: old_customer_id_raw=%s (type: %s)", old_customer_id_raw, type(old_customer_id_raw))
        if old_customer_id_raw is not None:
            try:
                old_customer_id_int = int(old_customer_id_raw)
                self._index_by_customer_id.pop(old_customer_id_int, None)
                logger.debug("update_feature: Successfully de-indexed old_customer_id=%s", old_customer_id_int)
            except (ValueError, TypeError) as e:
                logger.error("update_feature: Failed to convert old_customer_id %s to int: %s", old_customer_id_raw, e)
        else:
            logger.debug("update_feature: old_customer_id is None, skipping de-indexing")

        new_customer_id_raw = payload.attrs.get("customer_id")
        logger.debug("update_feature: new_customer_id_raw=%s (type: %s)", new_customer_id_raw, type(new_customer_id_raw))
        if new_customer_id_raw is not None:
            try:
                new_customer_id_int = int(new_customer_id_raw)
                self._index_by_customer_id[new_customer_id_int] = feature_id
                logger.debug("update_feature: Successfully indexed new_customer_id=%s", new_customer_id_int)
            except (ValueError, TypeError) as e:
                logger.error("update_feature: Failed to convert new_customer_id %s to int: %s", new_customer_id_raw, e)
        else:
            logger.debug("update_feature: new_customer_id is None, skipping indexing")

        updated_feature = Feature(id=feature_id, **payload.model_dump())
        self._features[feature_id] = updated_feature
        logger.info("Updated feature %s", feature_id)
        return updated_feature

    def delete_feature(self, feature_id: str) -> None:
        feature = self._features.pop(feature_id, None)
        if not feature:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"message": f"Feature {feature_id} not found"},
            )
        onu_sn = str(feature.attrs.get("onu_sn", "")).strip().lower()
        if onu_sn:
            self._index_by_onu_sn.pop(onu_sn, None)
        customer_id = feature.attrs.get("customer_id")
        if isinstance(customer_id, int):
            self._index_by_customer_id.pop(customer_id, None)
        logger.info("Deleted feature %s", feature_id)

    def find_by_onu(self, onu_sn: str) -> Feature | None:
        feature_id = self._index_by_onu_sn.get(onu_sn.strip().lower())
        if not feature_id:
            return None
        return self._features.get(feature_id)

    def reset(self) -> None:
        self._features.clear()
        self._index_by_onu_sn.clear()
        self._index_by_customer_id.clear()
        self._counter = 1
        logger.info("Reset feature store to empty state")

    def find_by_customer_id(self, customer_id: int) -> Feature | None:
        feature_id = self._index_by_customer_id.get(customer_id)
        if not feature_id:
            return None
        return self._features.get(feature_id)


store = FeatureStore()

app = FastAPI(
    title="GeoGrid Mock",
    description="Synthetic GeoGrid API for integration testing",
    version="0.1.0",
)


def check_simulation(simulate: str | None) -> None:
    if simulate == "429":
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"message": "GeoGrid mock rate limit exceeded"},
        )


@app.post(
    "/features",
    status_code=status.HTTP_201_CREATED,
    response_model=Feature,
)
async def create_feature(
    payload: FeaturePayload,
    simulate: str | None = Query(default=None),
    feature_store: FeatureStore = Depends(lambda: store),
):
    logger.info("POST /features simulate=%s", simulate)
    check_simulation(simulate)
    feature = feature_store.create_feature(payload)
    return feature


@app.put(
    "/features/{feature_id}",
    response_model=Feature,
)
async def update_feature(
    feature_id: str = Path(..., min_length=3),
    payload: FeaturePayload | None = None,
    simulate: str | None = Query(default=None),
    feature_store: FeatureStore = Depends(lambda: store),
):
    logger.info("PUT /features/%s simulate=%s", feature_id, simulate)
    check_simulation(simulate)
    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"message": "Request body is required"},
        )
    feature = feature_store.update_feature(feature_id, payload)
    return feature


@app.get("/features", response_model=List[Feature])
async def list_features(
    feature_store: FeatureStore = Depends(lambda: store),
):
    logger.info("GET /features")
    return feature_store.list_features()


@app.get("/features/{feature_id}", response_model=Feature)
async def get_feature(
    feature_id: str,
    feature_store: FeatureStore = Depends(lambda: store),
):
    logger.info("GET /features/%s", feature_id)
    return feature_store.get_feature(feature_id)


@app.get("/features/search", response_model=Feature)
async def search_feature(
    customer_id: Optional[int] = Query(default=None, ge=1),
    onu_sn: Optional[str] = Query(default=None, min_length=3),
    simulate: str | None = Query(default=None),
    feature_store: FeatureStore = Depends(lambda: store),
):
    logger.info(
        "GET /features/search customer_id=%s onu_sn=%s simulate=%s",
        customer_id,
        onu_sn,
        simulate,
    )
    check_simulation(simulate)
    feature = None
    if customer_id is not None:
        feature = feature_store.find_by_customer_id(customer_id)
    if feature is None and onu_sn:
        feature = feature_store.find_by_onu(onu_sn)
    if feature is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"message": "Feature not found"},
        )
    return feature


@app.delete("/features/{feature_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_feature(
    feature_id: str,
    simulate: str | None = Query(default=None),
    feature_store: FeatureStore = Depends(lambda: store),
):
    logger.info("DELETE /features/%s simulate=%s", feature_id, simulate)
    check_simulation(simulate)
    feature_store.delete_feature(feature_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.post("/reset")
async def reset(feature_store: FeatureStore = Depends(lambda: store)):
    feature_store.reset()
    return {"status": "reset"}


@app.get("/health")
async def health():
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "mocks.geogrid.main:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8002")),
        reload=bool(int(os.getenv("UVICORN_RELOAD", "0"))),
    )
