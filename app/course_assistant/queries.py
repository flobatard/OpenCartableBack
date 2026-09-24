"""Lectures des pièces jointes partagées avec les autres paquets.

**Pourquoi ce module existe** — une conversation de l'assistant est rattachée
à sa cible d'édition (``ai_conversations.block_id`` / ``module_id``, FK
``CASCADE``). Supprimer un bloc, un module ou une ressource emporte donc, en
cascade et sans que personne ne le voie, les conversations qui les visaient,
leurs messages **et leurs pièces jointes** — mais les objets S3, eux, sont
hors cascade. Sans cette collecte, chaque suppression de ce genre laisserait
des orphelins **systématiques** dans le bucket (pas un échec réseau
occasionnel, que la réconciliation est là pour rattraper : un comportement
garanti, qui s'accumulerait d'autant plus que la réconciliation tourne en
``dry_run`` par défaut).

Ce module n'importe que des modèles : `app/courses/` et `app/resources/`
peuvent s'en servir sans créer de cycle avec le reste de
:mod:`app.course_assistant`, qui lit `app/courses/queries.py`.
"""

import uuid
from collections.abc import Sequence

from sqlalchemy import ColumnElement, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ai_attachment import AIAttachment
from app.models.ai_conversation import AIConversation
from app.models.block import Block


async def attachment_keys_of_conversations(
    db: AsyncSession, *conditions: ColumnElement[bool]
) -> list[str]:
    """Clés S3 des pièces jointes des conversations qui satisfont ``conditions``.

    **Un seul execute**, à appeler AVANT le delete qui déclenche la cascade —
    ensuite les lignes n'existent plus et les clés sont perdues. L'appelant
    purge le bucket APRÈS son commit (motif ``delete_course``).
    """
    return list(
        (
            await db.execute(
                select(AIAttachment.s3_key).where(
                    AIAttachment.conversation_id.in_(
                        select(AIConversation.id).where(*conditions)
                    )
                )
            )
        )
        .scalars()
        .all()
    )


async def attachment_keys_for_block(db: AsyncSession, block_id: uuid.UUID) -> list[str]:
    """Pièces jointes des conversations d'édition de CE bloc (un execute)."""
    return await attachment_keys_of_conversations(db, AIConversation.block_id == block_id)


async def attachment_keys_for_module(db: AsyncSession, module_id: uuid.UUID) -> list[str]:
    """Pièces jointes des conversations d'édition de CE module (un execute)."""
    return await attachment_keys_of_conversations(db, AIConversation.module_id == module_id)


async def attachment_keys_for_resource(
    db: AsyncSession, resource_id: uuid.UUID
) -> list[str]:
    """Pièces jointes des conversations des blocs qui pointent CETTE ressource.

    Supprimer une ressource supprime ses blocs ``document`` (FK ``CASCADE``),
    donc les conversations de ces blocs, donc leurs pièces jointes : deux
    cascades de profondeur, un seul execute.
    """
    return await attachment_keys_of_conversations(
        db,
        AIConversation.block_id.in_(
            select(Block.id).where(Block.resource_id == resource_id)
        ),
    )


def available_attachment_keys_statement(cursor: str | None, limit: int, cutoff):
    """Tranche de clés de pièces jointes ``available`` à vérifier côté S3.

    Réservé au contrôle ``missing_s3_objects`` : même forme que sa tranche de
    ressources (curseur sur la clé, tri sur l'index unique, grâce sur la date
    de création).
    """
    from app.models.ai_attachment import STATUS_AVAILABLE

    condition = (AIAttachment.status == STATUS_AVAILABLE) & (
        AIAttachment.created_at < cutoff
    )
    if cursor:
        condition &= AIAttachment.s3_key > cursor
    return (
        select(AIAttachment.s3_key)
        .where(condition)
        .order_by(AIAttachment.s3_key)
        .limit(limit)
    )


__all__: Sequence[str] = [
    "attachment_keys_for_block",
    "attachment_keys_for_module",
    "attachment_keys_for_resource",
    "attachment_keys_of_conversations",
    "available_attachment_keys_statement",
]
