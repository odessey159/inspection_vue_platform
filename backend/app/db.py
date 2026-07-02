from __future__ import annotations

from sqlmodel import Session, SQLModel, create_engine

from .settings import DATABASE_PATH


engine = create_engine(
    f"sqlite:///{DATABASE_PATH}",
    connect_args={"check_same_thread": False},
)


def create_db_and_tables() -> None:
    SQLModel.metadata.create_all(engine)


def get_session():
    with Session(engine) as session:
        yield session
