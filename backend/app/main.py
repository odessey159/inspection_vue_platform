from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .db import create_db_and_tables
from .routers.findings import router as findings_router
from .routers.projects import router as projects_router
from .settings import CORS_ORIGINS, PROJECTS_DIR


@asynccontextmanager
async def lifespan(_app: FastAPI):
    create_db_and_tables()
    yield


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
