"""Assemble versioned HTTP routes without loading them from domain package imports."""

from fastapi import APIRouter

from src.api.v1.auth.routers import router as auth_router
from src.api.v1.finance.routers import router as finance_router
from src.api.v1.imports.routers import router as imports_router
from src.api.v1.quality.routers import router as quality_router

router = APIRouter(prefix="/api/v1")
router.include_router(auth_router)
router.include_router(finance_router)
router.include_router(quality_router)
router.include_router(imports_router)
