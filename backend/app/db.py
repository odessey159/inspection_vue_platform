from __future__ import annotations

from sqlalchemy import text
from sqlmodel import Session, SQLModel, create_engine

from .settings import DATABASE_PATH


engine = create_engine(
    f"sqlite:///{DATABASE_PATH}",
    connect_args={"check_same_thread": False},
)


def create_db_and_tables() -> None:
    SQLModel.metadata.create_all(engine)
    _ensure_vehicle_id_column()


def _ensure_vehicle_id_column() -> None:
    """Add vehicle_id to existing SQLite files created before the workspace-per-vehicle change."""
    with engine.connect() as connection:
        rows = connection.execute(text("PRAGMA table_info(project)")).fetchall()
        columns = {row[1] for row in rows}
        if "vehicle_id" not in columns:
            connection.execute(text("ALTER TABLE project ADD COLUMN vehicle_id VARCHAR"))
            connection.commit()


def get_session():
    with Session(engine) as session:
        yield session
