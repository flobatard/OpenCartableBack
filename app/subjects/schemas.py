import uuid

from pydantic import BaseModel


class SubjectRead(BaseModel):
    id: uuid.UUID
    parent_id: uuid.UUID | None
    name: str
    code: str
    depth: int
    position: int
    children: list["SubjectRead"] = []


SubjectRead.model_rebuild()
