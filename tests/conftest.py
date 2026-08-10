"""Shared pytest fixtures.

Database tests run against a dedicated PostgreSQL database (the engine fixture
recreates the schema, including the probe-version immutability triggers, for
every test). Pure unit tests never touch the database.
"""

from __future__ import annotations

import os

os.environ.setdefault(
    "ENDPOINT_LEDGER_DATABASE_URL",
    "postgresql+psycopg://localhost:5432/endpoint_ledger_test",
)

from collections.abc import Iterator  # noqa: E402

import pytest  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402

import endpoint_ledger.db.models  # noqa: F401, E402
from endpoint_ledger.config import Settings  # noqa: E402
from endpoint_ledger.db.base import Base  # noqa: E402
from endpoint_ledger.db.sealed_evidence import ALL_SQL as SEALED_EVIDENCE_SQL  # noqa: E402
from endpoint_ledger.db.triggers import TRIGGERS_SQL  # noqa: E402

TEST_DATABASE_URL = os.environ["ENDPOINT_LEDGER_DATABASE_URL"]


@pytest.fixture()
def settings() -> Settings:
    return Settings(database_url=TEST_DATABASE_URL, show_raw_responses=False)


@pytest.fixture()
def engine():
    engine = create_engine(TEST_DATABASE_URL)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(text(TRIGGERS_SQL))
        conn.execute(text(SEALED_EVIDENCE_SQL))
    yield engine
    # Fail fast on lock contention instead of hanging pytest teardown.
    with engine.begin() as conn:
        conn.execute(text("SET LOCAL lock_timeout = '5s'"))
        Base.metadata.drop_all(bind=conn)
    engine.dispose()


@pytest.fixture()
def db(engine) -> Iterator[Session]:
    factory = sessionmaker(bind=engine)
    session = factory()
    try:
        yield session
    finally:
        session.close()
