"""FastAPI application factory for the private dashboard."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from endpoint_ledger.config import Settings, get_settings
from endpoint_ledger.db.session import make_engine, make_sessionmaker
from endpoint_ledger.web.ops_routes import router as ops_router
from endpoint_ledger.web.routes import router
from endpoint_ledger.web.security import CSRFMiddleware

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
STATIC_DIR = Path(__file__).resolve().parent / "static"


def create_app(settings: Settings | None = None, start_scheduler: bool = False) -> FastAPI:
    settings = settings or get_settings()
    engine = make_engine(settings.database_url)
    session_factory = make_sessionmaker(engine)
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

    app = FastAPI(title="Endpoint Ledger", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.session_factory = session_factory
    app.state.templates = templates
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    app.add_middleware(CSRFMiddleware)
    app.include_router(router)
    app.include_router(ops_router)

    if start_scheduler and settings.scheduler_enabled:
        from endpoint_ledger.services.scheduler import start_scheduler as launch_scheduler

        launch_scheduler(session_factory, settings)

    return app
