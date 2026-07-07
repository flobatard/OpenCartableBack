"""Central registry of SQLAlchemy models.

One module per model in this package; import everything here so the tables
are registered on ``Base.metadata`` and picked up by Alembic autogenerate:

    from app.models.course import Course
    from app.models.subject import Subject

    __all__ = ["Base", "Course", "Subject"]
"""

from app.core.database import Base  # noqa: F401
from app.models.education_level import EducationLevel
from app.models.subject import Subject
from app.models.user import User, user_education_levels, user_subjects

__all__ = [
    "Base",
    "EducationLevel",
    "Subject",
    "User",
    "user_education_levels",
    "user_subjects",
]
