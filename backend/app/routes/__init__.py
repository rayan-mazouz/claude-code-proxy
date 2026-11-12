# Path: app/routes/__init__.py
# Description: This file contains the main router for the application.

from fastapi import APIRouter

from .accounts import router as accounts_router
from .archive import router as archive_router
from .auth import router as auth_router
from .clients import router as clients_router
from .events import router as events_router
from .fallbacks import router as fallbacks_router
from .lookups import router as lookups_router
from .me import router as me_router
from .notifications import router as notifications_router
from .presets import router as presets_router
from .proxy import router as proxy_router
from .settings import router as settings_router
from .stats import router as stats_router
from .team import router as team_router
from .users import router as users_router

# Public API (exposed under /api via traefik)
main_router = APIRouter(prefix="/v1")

main_router.include_router(auth_router)
main_router.include_router(accounts_router)
main_router.include_router(archive_router)
main_router.include_router(users_router)
main_router.include_router(presets_router)
main_router.include_router(stats_router)
main_router.include_router(me_router)
main_router.include_router(notifications_router)
main_router.include_router(clients_router)
main_router.include_router(fallbacks_router)
main_router.include_router(lookups_router)
main_router.include_router(events_router)
main_router.include_router(proxy_router)
main_router.include_router(team_router)
main_router.include_router(settings_router)

__all__ = ["main_router"]
