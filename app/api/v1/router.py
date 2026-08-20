from fastapi import APIRouter

from app.api.v1.endpoints import batches, nsdl_speede, post_trade, settlements, system

api_router = APIRouter()
api_router.include_router(batches.router)
api_router.include_router(settlements.router)
api_router.include_router(post_trade.router)
api_router.include_router(nsdl_speede.router)
api_router.include_router(system.router)
