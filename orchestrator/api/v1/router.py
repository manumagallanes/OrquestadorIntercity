from fastapi import APIRouter
from .endpoints import customer, analytics, ops

api_router = APIRouter()
api_router.include_router(customer.router, tags=["Customer"])
api_router.include_router(analytics.router, tags=["Analytics"])
api_router.include_router(ops.router, tags=["Operations"])
