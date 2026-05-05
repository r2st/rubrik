"""FastAPI application entry point.

Exposes the analysis pipeline as a REST API and serves the static frontend
from `web/`. Pipeline state is built once on startup and cached in memory.

Run dev:  uvicorn api.main:app --reload
Run prod: uvicorn api.main:app --host 0.0.0.0 --port 8000 --workers 4
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from src.logging_config import configure_logging, get_logger

from . import state
from .routes import router

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI):
    configure_logging()
    log.info("Starting up — building pipeline state…")
    state.get_state()  # warm cache
    log.info("Ready to serve")
    yield
    log.info("Shutting down")


app = FastAPI(
    title="Transcript Intelligence API",
    version="0.1.0",
    description="Topic categorization, sentiment analysis, and strategic "
                "insights for B2B meeting transcripts.",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighten in prod
    allow_methods=["GET"],
    allow_headers=["*"],
)

app.include_router(router)


# ---------------------------------------------------------------------------
# Static frontend
# ---------------------------------------------------------------------------
if (WEB_DIR / "static").exists():
    app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    fpath = WEB_DIR / "static" / "favicon.ico"
    if fpath.exists():
        return FileResponse(fpath)
    return FileResponse(WEB_DIR / "index.html", status_code=404)
