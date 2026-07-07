import uuid

from pydantic import BaseModel


class EducationLevelRead(BaseModel):
    id: uuid.UUID
    parent_id: uuid.UUID | None
    nom: str
    code: str
    systeme: str
    cite: int | None
    age_min: int | None
    age_max: int | None
    profondeur: int
    position: int
    children: list["EducationLevelRead"] = []


EducationLevelRead.model_rebuild()
