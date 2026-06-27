"""Central registry of SQLAlchemy models.

Import every feature's models here so they are registered on ``Base.metadata``
and picked up by Alembic autogenerate. Add a line for each new feature.
"""

from app.core.database import Base  # noqa: F401
from app.users.models import User  # noqa: F401

__all__ = ["Base", "User"]
