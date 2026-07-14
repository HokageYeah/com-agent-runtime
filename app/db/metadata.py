from __future__ import annotations

from sqlalchemy import MetaData

from app.db.sqlalchemy_db import Base


def get_target_metadata() -> MetaData:
    import app.models  # noqa: F401

    return Base.metadata
