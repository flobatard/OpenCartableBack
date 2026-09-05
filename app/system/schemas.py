from typing import Any

from pydantic import BaseModel


class MeRead(BaseModel):
    """Identité portée par le JWT, telle que validée — aucune donnée en base."""

    sub: str
    email: str | None
    roles: list[str]
    claims: dict[str, Any]
