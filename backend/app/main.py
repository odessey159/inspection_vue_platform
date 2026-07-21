from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .db import create_db_and_tables
from .routers.findings import router as findings_router
from .routers.projects import router as projects_router
from .services.rtsp_recorder import start_rtsp_recording_cleanup_worker, stop_rtsp_recording_cleanup_worker
from .services.rtsp_vehicles import ensure_robot_runtime_dirs
from .services.rtsp_watchdog import start_rtsp_watchdog, stop_rtsp_watchdog
from .settings import CORS_ORIGINS, PROJECTS_DIR


@asynccontextmanager
async def lifespan(_app: FastAPI):
    create_db_and_tables()
    ensure_robot_runtime_dirs()
    start_rtsp_recording_cleanup_worker()
    start_rtsp_watchdog()
    try:
        yield
    finally:
        stop_rtsp_watchdog()
        stop_rtsp_recording_cleanup_worker()


app = FastAPI(
    title="Inspection Vue Platform API",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(projects_router)
app.include_router(findings_router)
app.mount("/artifacts", StaticFiles(directory=PROJECTS_DIR), name="artifacts")


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/")
def root() -> dict[str, str]:
    return {
        "service": "inspection-vue-platform",
        "status": "ready",
    }
