"""Schémas des liens de partage élèves d'un cours.

``ShareLinkRead`` expose le ``token`` en clair (capability URL, cf.
:mod:`app.models.share_link`) : c'est le front qui construit
l'URL complète (``{siteUrl}/{lang}/shared/{token}``) — le back ne connaît
pas l'origine de la SPA.
"""

import uuid
from datetime import datetime

from pydantic import BaseModel, Field, field_validator


class ShareLinkCreate(BaseModel):
    # Aide-mémoire facultatif pour distinguer ses liens (« 6eB 2026 »).
    label: str | None = Field(default=None, max_length=200)

    @field_validator("label")
    @classmethod
    def _blank_label_becomes_none(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        return v or None


class ShareLinkRead(BaseModel):
    id: uuid.UUID
    token: str
    label: str | None
    expires_at: datetime
    revoked: bool
    created_at: datetime
