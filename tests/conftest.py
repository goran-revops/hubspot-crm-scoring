from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from hubspot_crm import db
from hubspot_crm.config import Settings


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    return Settings(
        database_url=f"sqlite:///{(tmp_path / 'scores.db').as_posix()}",
        hubspot_access_token="test-token",
        hubspot_recent_activity_days=730,
        hubspot_semantics_path=tmp_path / "semantics.json",
        hubspot_lists_path=tmp_path / "lists.json",
    )


@pytest.fixture()
def conn(settings: Settings) -> sqlite3.Connection:
    connection = db.connect(settings)
    db.init_db(connection)
    try:
        yield connection
        connection.commit()
    finally:
        connection.close()
