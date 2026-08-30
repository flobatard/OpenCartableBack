"""Utilisateurs authentifiés de l'application (profs et/ou élèves).

La ligne est créée par auto-provisioning au premier ``GET /users/me``
porteur d'un JWT valide : ``sub`` est l'identifiant OIDC opaque (Zitadel
aujourd'hui — aucune autre donnée IdP n'est persistée), ``id`` l'identifiant
interne, seul à référencer depuis les autres tables. Les rôles sont
cumulables (un enseignant peut aussi apprendre) ; le profil est complet
quand ``onboarded_at`` est posé. Matières et niveaux du profil vivent dans
les tables d'association, qualifiées par ``contexte`` (« enseigne » /
« apprend ») — c'est lui, pas le rôle, qui porte la sémantique d'une ligne.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Table,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

CONTEXTE_ENSEIGNE = "enseigne"
CONTEXTE_APPREND = "apprend"


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("sub", name="uq_users_sub"),
        CheckConstraint(
            "onboarded_at IS NULL OR est_prof OR est_eleve",
            name="ck_users_onboarde_implique_role",
        ),
        CheckConstraint(
            "(avatar_s3_key IS NULL AND avatar_mime IS NULL AND avatar_statut IS NULL) "
            "OR (avatar_s3_key IS NOT NULL AND avatar_mime IS NOT NULL "
            "AND avatar_statut IN ('en_attente', 'disponible'))",
            name="ck_users_avatar_coherence",
        ),
        # Cohérence structurelle du credential IA : tout-NULL (pas de config)
        # ou au moins provider+model. Les règles PAR provider (clé requise ou
        # non, base_url requise/interdite) sont métier → 422 en service,
        # jamais en CHECK (ajouter un provider ne doit pas exiger de migration).
        CheckConstraint(
            "(ai_provider IS NULL AND ai_model IS NULL AND ai_base_url IS NULL "
            "AND ai_api_key_chiffree IS NULL AND ai_chiffrement_sel IS NULL) "
            "OR (ai_provider IS NOT NULL AND ai_model IS NOT NULL)",
            name="ck_users_ai_coherence",
        ),
        CheckConstraint(
            "(ai_api_key_chiffree IS NULL) = (ai_chiffrement_sel IS NULL)",
            name="ck_users_ai_cle_sel",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    sub: Mapped[str] = mapped_column(String(255))
    # Snapshot du claim, rafraîchi à chaque lecture du profil si différent.
    email: Mapped[str | None] = mapped_column(String(320))
    # Nom d'affichage public choisi par l'utilisateur (jalon J2) : seule donnée
    # d'identité exposée sur les pages publiques (catalogue des cours d'un
    # prof). Jamais dérivé de l'IdP, jamais l'email. NULL = catalogue anonyme.
    nom_public: Mapped[str | None] = mapped_column(String(100))
    # Opt-in explicite à la recherche publique de professeurs (jalon J3).
    # Le flag seul ne suffit jamais : un prof ne remonte dans
    # /public/search/teachers que si cherchable AND nom_public non NULL
    # AND au moins un cours public (règle portée par app/search/service.py).
    cherchable: Mapped[bool] = mapped_column(default=False, server_default="false")
    # Photo de profil (avatar) : objet S3 privé, servi en URL présignée inline.
    # NULL sur les trois colonnes = pas d'avatar (CHECK de cohérence). Le statut
    # suit le flow presigned des ressources (en_attente → disponible) ; jamais
    # exposé tel quel : seule avatar_url (présignée, statut disponible) sort de
    # l'API — la clé ne figure dans aucun schéma de réponse.
    avatar_s3_key: Mapped[str | None] = mapped_column(String(1024))
    avatar_mime: Mapped[str | None] = mapped_column(String(255))
    avatar_statut: Mapped[str | None] = mapped_column(String(20))
    # Credential IA de l'utilisateur (une seule config, app/ai_credentials/) :
    # provider ∈ AIProvider (validé Pydantic), clé API chiffrée par
    # app/core/crypto.py (AES-256-GCM, blob versionné) avec un sel par
    # utilisateur régénéré à chaque écriture de clé. Comme avatar_s3_key,
    # la clé (chiffrée ou non) ne figure dans AUCUN schéma de réponse — seule
    # sort la projection api_key_definie: bool.
    ai_provider: Mapped[str | None] = mapped_column(String(50))
    ai_model: Mapped[str | None] = mapped_column(String(200))
    ai_base_url: Mapped[str | None] = mapped_column(String(2000))
    ai_api_key_chiffree: Mapped[bytes | None] = mapped_column(LargeBinary)
    ai_chiffrement_sel: Mapped[bytes | None] = mapped_column(LargeBinary)
    # Quota QUOTIDIEN d'appels à l'IA PAR DÉFAUT (le fallback serveur AI_*) :
    # NULL = quota standard (settings.AI_DEFAULT_DAILY_QUOTA), 0 = illimité,
    # sinon plafond individuel par jour. Aucune route ne l'écrit (l'utilisateur
    # pourrait se dé-limiter) : posé à la main par l'opérateur. Les appels BYO
    # token (config explicite ou credential ci-dessus) ne sont jamais comptés ;
    # le comptage par jour vit dans la table ai_daily_usage.
    ai_quota_appels: Mapped[int | None] = mapped_column(Integer)
    est_prof: Mapped[bool] = mapped_column(default=False, server_default="false")
    est_eleve: Mapped[bool] = mapped_column(default=False, server_default="false")
    # Même dimension que education_levels.systeme ; validé en service
    # (pas de FK possible : les systèmes ne sont pas une table).
    systeme_scolaire: Mapped[str | None] = mapped_column(String(20))
    onboarded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Pas de relations ORM vers subjects/education_levels : lazy-load async
    # interdit, le service fait des selects explicites sur les tables Core.


# ``user_id`` en tête des PK composites : l'index de PK couvre la lecture
# du profil (WHERE user_id = ...), pas d'index séparé nécessaire.
user_subjects = Table(
    "user_subjects",
    Base.metadata,
    Column(
        "user_id",
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "subject_id",
        UUID(as_uuid=True),
        ForeignKey("subjects.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("contexte", String(10), primary_key=True),
    CheckConstraint(
        f"contexte IN ('{CONTEXTE_ENSEIGNE}', '{CONTEXTE_APPREND}')",
        name="ck_user_subjects_contexte",
    ),
)

user_education_levels = Table(
    "user_education_levels",
    Base.metadata,
    Column(
        "user_id",
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "education_level_id",
        UUID(as_uuid=True),
        ForeignKey("education_levels.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("contexte", String(10), primary_key=True),
    CheckConstraint(
        f"contexte IN ('{CONTEXTE_ENSEIGNE}', '{CONTEXTE_APPREND}')",
        name="ck_user_education_levels_contexte",
    ),
)
