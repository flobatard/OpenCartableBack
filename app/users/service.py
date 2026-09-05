"""Profil utilisateur : auto-provisioning par ``sub`` et mise à jour du profil.

L'ordre des ``execute`` de chaque fonction est stable et documenté : les
tests le rejouent avec une fausse session FIFO (voir tests/test_users_api.py).
"""

import uuid
from collections.abc import Iterable
from datetime import UTC, datetime

from sqlalchemy import distinct, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import AuthenticatedUser
from app.core.config import settings
from app.core.http import conflict, invalid
from app.core.storage import Storage
from app.models.education_level import EducationLevel
from app.models.resource import STATUS_AVAILABLE, STATUS_PENDING
from app.models.subject import Subject
from app.models.user import (
    CONTEXT_LEARNING,
    CONTEXT_TEACHING,
    User,
    user_education_levels,
    user_subjects,
)
from app.users.schemas import (
    AVATAR_EXTENSIONS,
    AvatarCreate,
    AvatarPresign,
    ProfileContext,
    ProfileUpdate,
    UserProfileRead,
)


def _dedupe(ids: Iterable[uuid.UUID]) -> list[uuid.UUID]:
    """Dédoublonne en préservant l'ordre de première apparition."""
    return list(dict.fromkeys(ids))


def avatar_url_for(s3_key: str | None, avatar_status: str | None, storage: Storage) -> str | None:
    """URL présignée inline de l'avatar, ``None`` si absent ou non disponible.

    Calcul local (aucune I/O) : appelable depuis les routes anonymes
    (:mod:`app.public`, :mod:`app.search`) sans coût réseau par item.
    """
    if s3_key is None or avatar_status != STATUS_AVAILABLE:
        return None
    return storage.presign_get(s3_key, s3_key.rsplit("/", 1)[-1], inline=True)


async def get_or_create_by_sub(db: AsyncSession, auth: AuthenticatedUser) -> User:
    """Retourne l'utilisateur du ``sub``, en le créant au premier appel.

    Race-safe : deux requêtes concurrentes d'un même nouvel utilisateur
    passent toutes deux par l'INSERT ``ON CONFLICT DO NOTHING`` puis
    relisent la même ligne. Ordre des execute : 1) insert, 2) select.
    """
    await db.execute(
        pg_insert(User)
        .values(sub=auth.sub, email=auth.email)
        .on_conflict_do_nothing(index_elements=["sub"])
    )
    user = (await db.execute(select(User).where(User.sub == auth.sub))).scalars().one()
    if user.email != auth.email:
        user.email = auth.email
    await db.commit()
    return user


def _blocks_from_rows(
    user: User,
    level_rows: Iterable[tuple[uuid.UUID, str]],
    subject_rows: Iterable[tuple[uuid.UUID, str]],
) -> tuple[ProfileContext | None, ProfileContext | None]:
    levels: dict[str, list[uuid.UUID]] = {CONTEXT_TEACHING: [], CONTEXT_LEARNING: []}
    subjects: dict[str, list[uuid.UUID]] = {CONTEXT_TEACHING: [], CONTEXT_LEARNING: []}
    for level_id, context in level_rows:
        levels[context].append(level_id)
    for subject_id, context in subject_rows:
        subjects[context].append(subject_id)

    def block(role: bool, context: str) -> ProfileContext | None:
        if not role:
            return None
        return ProfileContext(
            education_level_ids=levels[context], subject_ids=subjects[context]
        )

    return block(user.is_teacher, CONTEXT_TEACHING), block(user.is_student, CONTEXT_LEARNING)


def _profile(
    user: User,
    teaching: ProfileContext | None,
    learning: ProfileContext | None,
    storage: Storage,
) -> UserProfileRead:
    return UserProfileRead(
        id=user.id,
        sub=user.sub,
        email=user.email,
        is_teacher=user.is_teacher,
        is_student=user.is_student,
        school_system=user.school_system,
        public_name=user.public_name,
        searchable=user.searchable,
        avatar_url=avatar_url_for(user.avatar_s3_key, user.avatar_status, storage),
        onboarding_complete=user.onboarded_at is not None,
        teaching=teaching,
        learning=learning,
    )


async def read_profile(db: AsyncSession, user: User, storage: Storage) -> UserProfileRead:
    """Profil complet. Ordre des execute : 1) niveaux, 2) matières."""
    level_rows = (
        await db.execute(
            select(
                user_education_levels.c.education_level_id,
                user_education_levels.c.context,
            )
            .where(user_education_levels.c.user_id == user.id)
            .order_by(
                user_education_levels.c.context,
                user_education_levels.c.education_level_id,
            )
        )
    ).all()
    subject_rows = (
        await db.execute(
            select(user_subjects.c.subject_id, user_subjects.c.context)
            .where(user_subjects.c.user_id == user.id)
            .order_by(user_subjects.c.context, user_subjects.c.subject_id)
        )
    ).all()
    teaching, learning = _blocks_from_rows(user, level_rows, subject_rows)
    return _profile(user, teaching, learning, storage)


async def update_profile(
    db: AsyncSession, user: User, payload: ProfileUpdate, storage: Storage
) -> UserProfileRead:
    """Valide puis enregistre le profil (PUT = remplacement, rejouable).

    La cohérence rôles/blocs est déjà garantie par ``ProfileUpdate``.
    Ordre des execute : 1) systèmes distincts, 2) lookup niveaux,
    3) lookup matières, 4) delete niveaux, 5) delete matières,
    6) insert niveaux, 7) insert matières.
    """
    blocks: list[tuple[str, ProfileContext]] = []
    if payload.teaching is not None:
        blocks.append((CONTEXT_TEACHING, payload.teaching))
    if payload.learning is not None:
        blocks.append((CONTEXT_LEARNING, payload.learning))

    # Un même id peut légitimement apparaître dans les deux contextes ;
    # le dédoublonnage est intra-bloc uniquement.
    levels_by_block = {c: _dedupe(b.education_level_ids) for c, b in blocks}
    subjects_by_block = {c: _dedupe(b.subject_ids) for c, b in blocks}
    all_levels = {i for ids in levels_by_block.values() for i in ids}
    all_subjects = {i for ids in subjects_by_block.values() for i in ids}

    systems = set(
        (await db.execute(select(distinct(EducationLevel.system)))).scalars().all()
    )
    if payload.school_system not in systems:
        raise invalid(f"Système scolaire inconnu : {payload.school_system}")

    rows = (
        await db.execute(
            select(EducationLevel.id, EducationLevel.system).where(
                EducationLevel.id.in_(all_levels)
            )
        )
    ).all()
    system_by_level = {level_id: system for level_id, system in rows}
    unknown = all_levels - system_by_level.keys()
    if unknown:
        raise invalid(f"Niveaux d'étude inconnus : {sorted(map(str, unknown))}")
    out_of_system = [
        level_id
        for level_id, system in system_by_level.items()
        if system != payload.school_system
    ]
    if out_of_system:
        raise invalid(
            f"Niveaux hors du système scolaire '{payload.school_system}' : "
            f"{sorted(map(str, out_of_system))}"
        )

    known_subjects = set(
        (await db.execute(select(Subject.id).where(Subject.id.in_(all_subjects))))
        .scalars()
        .all()
    )
    unknown_subjects = all_subjects - known_subjects
    if unknown_subjects:
        raise invalid(f"Matières inconnues : {sorted(map(str, unknown_subjects))}")

    await db.execute(
        user_education_levels.delete().where(user_education_levels.c.user_id == user.id)
    )
    await db.execute(user_subjects.delete().where(user_subjects.c.user_id == user.id))
    await db.execute(
        user_education_levels.insert(),
        [
            {"user_id": user.id, "education_level_id": level_id, "context": context}
            for context, ids in levels_by_block.items()
            for level_id in ids
        ],
    )
    await db.execute(
        user_subjects.insert(),
        [
            {"user_id": user.id, "subject_id": subject_id, "context": context}
            for context, ids in subjects_by_block.items()
            for subject_id in ids
        ],
    )

    user.is_teacher = payload.is_teacher
    user.is_student = payload.is_student
    user.school_system = payload.school_system
    user.public_name = payload.public_name
    user.searchable = payload.searchable
    # La date de première complétion est conservée à la re-soumission.
    user.onboarded_at = user.onboarded_at or datetime.now(UTC)
    await db.commit()

    def block(context: str) -> ProfileContext | None:
        if context not in levels_by_block:
            return None
        return ProfileContext(
            education_level_ids=levels_by_block[context],
            subject_ids=subjects_by_block[context],
        )

    return _profile(user, block(CONTEXT_TEACHING), block(CONTEXT_LEARNING), storage)


async def presign_avatar(
    db: AsyncSession, user: User, payload: AvatarCreate, storage: Storage
) -> AvatarPresign:
    """Déclare l'upload d'avatar et renvoie l'URL présignée PUT.

    Ordre des execute : aucun (mutation d'attributs de l'instance déjà
    chargée par ``get_or_create_by_sub``). Écrase l'éventuel avatar
    existant (nouvelle clé — l'uuid4 par upload invalide les caches
    navigateur —, statut ``pending``) ; l'ancienne clé S3 est purgée
    APRÈS le commit (motif ``delete_resource`` : un échec S3 laisse un
    orphelin bucket, jamais une réf DB pointant un objet absent). Fenêtre
    assumée : entre presign et confirm, ``avatar_url`` est ``None``.
    """
    previous = user.avatar_s3_key
    ext = AVATAR_EXTENSIONS[payload.mime]
    user.avatar_s3_key = f"users/{user.id}/avatar/{uuid.uuid4()}/avatar.{ext}"
    user.avatar_mime = payload.mime
    user.avatar_status = STATUS_PENDING
    await db.commit()
    if previous is not None:
        await storage.delete_many([previous])
    return AvatarPresign(
        upload_url=storage.presign_put(user.avatar_s3_key, payload.mime),
        expires_in=settings.S3_PRESIGN_PUT_TTL,
    )


async def confirm_avatar(
    db: AsyncSession, user: User, storage: Storage
) -> UserProfileRead:
    """Vérifie l'objet S3 et passe l'avatar à ``available``.

    Ordre des execute : ceux de ``read_profile`` (1 niveaux, 2 matières),
    après le commit. Avant eux, HEAD S3 : pas d'upload en attente ou déjà
    confirmé → 409 ; objet absent → 409 ; ``ContentLength`` au-dessus du
    plafond (une URL présignée PUT ne borne pas la taille) ou
    ``ContentType`` différent du mime déclaré → 409 avec purge best-effort
    de l'objet hors gabarit (la ligne reste ``pending``).
    """
    if user.avatar_s3_key is None or user.avatar_status != STATUS_PENDING:
        raise conflict("Aucun upload d'avatar en attente")

    metadata = await storage.head(user.avatar_s3_key)
    if metadata is None:
        raise conflict("Objet introuvable sur S3 : upload non abouti")
    size = metadata.get("ContentLength")
    content_type = metadata.get("ContentType")
    if (size is not None and size > settings.AVATAR_MAX_BYTES) or (
        content_type is not None and content_type != user.avatar_mime
    ):
        await storage.delete_many([user.avatar_s3_key])
        raise conflict("Objet hors gabarit (taille ou type inattendu)")

    user.avatar_status = STATUS_AVAILABLE
    await db.commit()
    return await read_profile(db, user, storage)


async def delete_avatar(
    db: AsyncSession, user: User, storage: Storage
) -> UserProfileRead:
    """Supprime l'avatar (colonnes à NULL) et purge l'objet S3.

    Ordre des execute : ceux de ``read_profile`` (1 niveaux, 2 matières),
    après le commit. La purge S3 a lieu APRÈS le commit (motif
    ``delete_resource``). Sans avatar, l'appel est un no-op idempotent
    (200, aucun appel S3).
    """
    s3_key = user.avatar_s3_key
    user.avatar_s3_key = None
    user.avatar_mime = None
    user.avatar_status = None
    await db.commit()
    if s3_key is not None:
        await storage.delete_many([s3_key])
    return await read_profile(db, user, storage)
