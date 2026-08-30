"""Schémas de la bibliothèque de modules interactifs d'un cours (CRUD).

Le code (``html``/``css``/``js``) vit en base (app/models/module.py) — pas de
flow presigned ici, contrairement aux ressources. La liste sert l'onglet
« Modules » du front et les pickers : elle expose ``ModuleSummary`` **sans le
code** (payload léger) ; le détail ``ModuleRead`` le porte pour l'éditeur et
l'exécution sandbox. Les modules sont indépendants des blocs : un bloc
``module`` peut les pointer (``BlockUpdate.module_id``,
:mod:`app.courses.schemas`), jamais l'inverse.
"""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# Plafond par champ de code (caractères) : très au-delà d'un module
# pédagogique raisonnable, sous le confort d'un payload JSON sur Pi.
MAX_CODE_LENGTH = 200_000


class ModuleCreate(BaseModel):
    """Création d'un module ; le code peut naître vide (rempli dans l'éditeur)."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=255)
    html: str = Field(default="", max_length=MAX_CODE_LENGTH)
    css: str = Field(default="", max_length=MAX_CODE_LENGTH)
    js: str = Field(default="", max_length=MAX_CODE_LENGTH)

    @field_validator("title")
    @classmethod
    def _title_not_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Le titre ne peut pas être vide")
        return v


class ModuleSummary(BaseModel):
    """Module sans son code — la liste de l'onglet Modules reste légère."""

    id: uuid.UUID
    title: str
    created_at: datetime
    updated_at: datetime


class ModuleRead(ModuleSummary):
    """Module complet, code inclus (éditeur + exécution sandbox)."""

    html: str
    css: str
    js: str


class ModuleUpdate(BaseModel):
    """Édition partielle : seuls les champs fournis sont modifiés
    (``model_fields_set``) — renommage et sauvegarde de code passent par le
    même PATCH. ``null`` est rejeté sur tous les champs (422) : vider un champ
    de code = envoyer ``""`` — pas de sémantique « null efface » ici,
    contrairement au méta des blocs."""

    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=255)
    html: str | None = Field(default=None, max_length=MAX_CODE_LENGTH)
    css: str | None = Field(default=None, max_length=MAX_CODE_LENGTH)
    js: str | None = Field(default=None, max_length=MAX_CODE_LENGTH)

    @field_validator("title")
    @classmethod
    def _title_not_blank(cls, v: str | None) -> str | None:
        if v is None:
            # ``title: null`` n'a pas de sens (un module a toujours un titre) ;
            # rejeté ici plutôt qu'en service pour une 422 de validation.
            raise ValueError("Le titre ne peut pas être null")
        v = v.strip()
        if not v:
            raise ValueError("Le titre ne peut pas être vide")
        return v

    @field_validator("html", "css", "js")
    @classmethod
    def _code_not_null(cls, v: str | None) -> str | None:
        if v is None:
            # Sans ce rejet, ``{"js": null}`` passerait la validation puis
            # serait ignoré par le service : un 200 fantôme qui bump quand
            # même ``course.updated_at``.
            raise ValueError("Un champ de code ne peut pas être null")
        return v

    @model_validator(mode="after")
    def _at_least_one_field(self) -> "ModuleUpdate":
        if not self.model_fields_set:
            raise ValueError("Fournir au moins un champ à modifier")
        return self
