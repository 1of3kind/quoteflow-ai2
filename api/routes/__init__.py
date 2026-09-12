from api.routes.auth_routes import router as auth_router
from api.routes.org_routes import router as org_router
from api.routes.quotes_routes import router as quotes_router
from api.routes.jobs_routes import router as jobs_router
from api.routes.billing_routes import router as billing_router
from api.routes.dashboard_routes import router as dashboard_router
from api.routes.materials_routes import router as materials_router
from api.routes.assistant_routes import router as assistant_router

__all__ = [
    "auth_router", "org_router", "quotes_router",
    "jobs_router", "billing_router", "dashboard_router",
    "materials_router", "assistant_router",
]
