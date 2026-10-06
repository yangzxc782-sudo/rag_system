from fastapi import APIRouter

from app.api.v1 import casting_design, conversations, documents, health, knowledge_items, rag, search

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(documents.router)
api_router.include_router(knowledge_items.router)
api_router.include_router(search.router)
api_router.include_router(rag.router)
api_router.include_router(conversations.router)
api_router.include_router(casting_design.router)
