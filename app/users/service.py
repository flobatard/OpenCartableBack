"""Profil utilisateur : auto-provisioning par ``sub`` et mise à jour du profil.

L'ordre des ``execute`` de chaque fonction est stable et documenté : les
tests le rejouent avec une fausse session FIFO (voir tests/test_users_api.py).
"""

import uuid
from collections.abc import Iterable
from datetime import UTC, datetime

from fastapi import HTTPException, status
from sqlalchemy import distinct, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import AuthenticatedUser
from app.models.education_level import EducationLevel
from app.models.subject import Subject
from app.models.user import (
    CONTEXTE_APPREND,
    CONTEXTE_ENSEIGNE,
    User,
    user_education_levels,
    user_subjects,
)
from app.users.schemas import ProfilContexte, ProfileUpdate, UserProfileRead


def _dedupe(ids: Iterable[uuid.UUID]) -> list[uuid.UUID]:
    """Dédoublonne en préservant l'ordre de première apparition."""
    return list(dict.fromkeys(ids))


def _invalide(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=detail)


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


def _blocs_depuis_lignes(
    user: User,
    lignes_niveaux: Iterable[tuple[uuid.UUID, str]],
    lignes_matieres: Iterable[tuple[uuid.UUID, str]],
) -> tuple[ProfilContexte | None, ProfilContexte | None]:
    niveaux: dict[str, list[uuid.UUID]] = {CONTEXTE_ENSEIGNE: [], CONTEXTE_APPREND: []}
    matieres: dict[str, list[uuid.UUID]] = {CONTEXTE_ENSEIGNE: [], CONTEXTE_APPREND: []}
    for level_id, contexte in lignes_niveaux:
        niveaux[contexte].append(level_id)
    for subject_id, contexte in lignes_matieres:
        matieres[contexte].append(subject_id)

    def bloc(role: bool, contexte: str) -> ProfilContexte | None:
        if not role:
            return None
        return ProfilContexte(
            education_level_ids=niveaux[contexte], subject_ids=matieres[contexte]
        )

    return bloc(user.est_prof, CONTEXTE_ENSEIGNE), bloc(user.est_eleve, CONTEXTE_APPREND)


def _profil(
    user: User,
    enseignement: ProfilContexte | None,
    apprentissage: ProfilContexte | None,
) -> UserProfileRead:
    return UserProfileRead(
        id=user.id,
        sub=user.sub,
        email=user.email,
        est_prof=user.est_prof,
        est_eleve=user.est_eleve,
        systeme_scolaire=user.systeme_scolaire,
        onboarding_complete=user.onboarded_at is not None,
        enseignement=enseignement,
        apprentissage=apprentissage,
    )


async def read_profile(db: AsyncSession, user: User) -> UserProfileRead:
    """Profil complet. Ordre des execute : 1) niveaux, 2) matières."""
    lignes_niveaux = (
        await db.execute(
            select(
                user_education_levels.c.education_level_id,
                user_education_levels.c.contexte,
            )
            .where(user_education_levels.c.user_id == user.id)
            .order_by(
                user_education_levels.c.contexte,
                user_education_levels.c.education_level_id,
            )
        )
    ).all()
    lignes_matieres = (
        await db.execute(
            select(user_subjects.c.subject_id, user_subjects.c.contexte)
            .where(user_subjects.c.user_id == user.id)
            .order_by(user_subjects.c.contexte, user_subjects.c.subject_id)
        )
    ).all()
    enseignement, apprentissage = _blocs_depuis_lignes(user, lignes_niveaux, lignes_matieres)
    return _profil(user, enseignement, apprentissage)


async def update_profile(
    db: AsyncSession, user: User, payload: ProfileUpdate
) -> UserProfileRead:
    """Valide puis enregistre le profil (PUT = remplacement, rejouable).

    La cohérence rôles/blocs est déjà garantie par ``ProfileUpdate``.
    Ordre des execute : 1) systèmes distincts, 2) lookup niveaux,
    3) lookup matières, 4) delete niveaux, 5) delete matières,
    6) insert niveaux, 7) insert matières.
    """
    blocs: list[tuple[str, ProfilContexte]] = []
    if payload.enseignement is not None:
        blocs.append((CONTEXTE_ENSEIGNE, payload.enseignement))
    if payload.apprentissage is not None:
        blocs.append((CONTEXTE_APPREND, payload.apprentissage))

    # Un même id peut légitimement apparaître dans les deux contextes ;
    # le dédoublonnage est intra-bloc uniquement.
    niveaux_par_bloc = {c: _dedupe(b.education_level_ids) for c, b in blocs}
    matieres_par_bloc = {c: _dedupe(b.subject_ids) for c, b in blocs}
    tous_niveaux = {i for ids in niveaux_par_bloc.values() for i in ids}
    toutes_matieres = {i for ids in matieres_par_bloc.values() for i in ids}

    systemes = set(
        (await db.execute(select(distinct(EducationLevel.systeme)))).scalars().all()
    )
    if payload.systeme_scolaire not in systemes:
        raise _invalide(f"Système scolaire inconnu : {payload.systeme_scolaire}")

    lignes = (
        await db.execute(
            select(EducationLevel.id, EducationLevel.systeme).where(
                EducationLevel.id.in_(tous_niveaux)
            )
        )
    ).all()
    systeme_par_niveau = {level_id: systeme for level_id, systeme in lignes}
    inconnus = tous_niveaux - systeme_par_niveau.keys()
    if inconnus:
        raise _invalide(f"Niveaux d'étude inconnus : {sorted(map(str, inconnus))}")
    hors_systeme = [
        level_id
        for level_id, systeme in systeme_par_niveau.items()
        if systeme != payload.systeme_scolaire
    ]
    if hors_systeme:
        raise _invalide(
            f"Niveaux hors du système scolaire '{payload.systeme_scolaire}' : "
            f"{sorted(map(str, hors_systeme))}"
        )

    matieres_connues = set(
        (await db.execute(select(Subject.id).where(Subject.id.in_(toutes_matieres))))
        .scalars()
        .all()
    )
    inconnues = toutes_matieres - matieres_connues
    if inconnues:
        raise _invalide(f"Matières inconnues : {sorted(map(str, inconnues))}")

    await db.execute(
        user_education_levels.delete().where(user_education_levels.c.user_id == user.id)
    )
    await db.execute(user_subjects.delete().where(user_subjects.c.user_id == user.id))
    await db.execute(
        user_education_levels.insert(),
        [
            {"user_id": user.id, "education_level_id": level_id, "contexte": contexte}
            for contexte, ids in niveaux_par_bloc.items()
            for level_id in ids
        ],
    )
    await db.execute(
        user_subjects.insert(),
        [
            {"user_id": user.id, "subject_id": subject_id, "contexte": contexte}
            for contexte, ids in matieres_par_bloc.items()
            for subject_id in ids
        ],
    )

    user.est_prof = payload.est_prof
    user.est_eleve = payload.est_eleve
    user.systeme_scolaire = payload.systeme_scolaire
    # La date de première complétion est conservée à la re-soumission.
    user.onboarded_at = user.onboarded_at or datetime.now(UTC)
    await db.commit()

    def bloc(contexte: str) -> ProfilContexte | None:
        if contexte not in niveaux_par_bloc:
            return None
        return ProfilContexte(
            education_level_ids=niveaux_par_bloc[contexte],
            subject_ids=matieres_par_bloc[contexte],
        )

    return _profil(user, bloc(CONTEXTE_ENSEIGNE), bloc(CONTEXTE_APPREND))
