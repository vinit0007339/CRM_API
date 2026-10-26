"""`get_crud` — the dependency every CRM route uses to get a CRUD object.

Copied from the monolith's `api/dependencies/crud.py`, with one change: it
resolves the database from `app.db` instead of `app.db.connections.main_db`.

Twelve routers and 83 endpoints declare their CRUDs this way
(`crud: DealCrud = Depends(get_crud(DealCrud))`), which is also why the
integration tests can call the handlers as plain functions: the dependency is
just a default value, so a test passes `crud=crud(DealCrud)` and never touches
FastAPI's injection at all. 916 tests run with no database because of that
shape — worth not breaking.
"""
from __future__ import annotations

from typing import AsyncGenerator, Callable, Type

from fastapi import Depends
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.crud._platform_base import CrudBase
from app.db import get_db


def get_crud(crud_type: Type[CrudBase]) -> Callable:  # type: ignore
    """Build the FastAPI dependency that yields one CRUD instance."""

    async def _get_crud(
        db_session: AsyncIOMotorDatabase = Depends(get_db),
    ) -> AsyncGenerator[CrudBase, None]:
        yield crud_type(db_session=db_session)

    return _get_crud
