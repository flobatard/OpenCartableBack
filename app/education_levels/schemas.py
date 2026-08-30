import uuid

from pydantic import BaseModel


class EducationLevelRead(BaseModel):
    id: uuid.UUID
    parent_id: uuid.UUID | None
    name: str
    code: str
    system: str
    cite: int | None
    age_min: int | None
    age_max: int | None
    depth: int
    position: int
    children: list["EducationLevelRead"] = []


EducationLevelRead.model_rebuild()
