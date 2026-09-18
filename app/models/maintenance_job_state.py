"""État de la dernière passe de chaque job de maintenance.

Une ligne **par job du registre** (:mod:`app.maintenance.registry`), upsertée à
la fin de chaque passe : la table est donc **bornée par construction** (une
dizaine de lignes) et n'a jamais besoin d'être purgée elle-même — contrairement
à un historique une-ligne-par-exécution, qui aurait exigé son propre job.

C'est un **état de dernière passe, pas un état d'exécution** : rien n'est écrit
au démarrage d'un job, seulement à sa fin. « En cours » est une affaire de logs.

Écarts assumés aux conventions du projet, parce que c'est de la métadonnée
opérationnelle et non une entité du domaine :

- **aucun index hors la clé primaire** — un seq scan de dix lignes bat n'importe
  quel parcours d'index ;
- **pas de ``created_at``/``updated_at``** — ``last_finished_at`` *est*
  l'``updated_at`` ; **pas de FK** — un job n'appartient à personne ;
- ``job_name`` est l'identifiant **anglais** du registre, qui sert aussi d'id de
  job APScheduler.

``last_detail`` est une pièce **portante**, pas un confort :
:func:`app.maintenance.checks.missing_s3_objects` y range son curseur de
rotation (sans lui, il revérifierait éternellement les mêmes clés) et
``storage_inventory`` y dépose un rapport structuré comparable d'une semaine à
l'autre, qu'un simple compte ne saurait porter. **Toute liste qu'on y range est
tronquée** à ``MAINTENANCE_DETAIL_MAX_ITEMS`` (``_bounded`` de
:mod:`app.maintenance.state`) : un JSONB attire la donnée non bornée, la table
doit rester à quelques kilo-octets.
"""

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

STATUS_OK = "ok"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"


class MaintenanceJobState(Base):
    __tablename__ = "maintenance_job_state"
    __table_args__ = (
        CheckConstraint(
            f"last_status IN ('{STATUS_OK}', '{STATUS_FAILED}', '{STATUS_SKIPPED}')",
            name="ck_maintenance_job_state_status",
        ),
        CheckConstraint("last_count >= 0", name="ck_maintenance_job_state_count_positive"),
        CheckConstraint(
            "last_duration_ms >= 0", name="ck_maintenance_job_state_duration_positive"
        ),
        CheckConstraint(
            "consecutive_failures >= 0", name="ck_maintenance_job_state_failures_positive"
        ),
        CheckConstraint("total_runs >= 0", name="ck_maintenance_job_state_runs_positive"),
    )

    job_name: Mapped[str] = mapped_column(String(50), primary_key=True)
    last_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_finished_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_status: Mapped[str] = mapped_column(String(10))
    # Ce que la passe a touché : lignes purgées/allégées, orphelins trouvés,
    # objets inventoriés, clés absentes. 0 pour un `skipped`.
    last_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    last_duration_ms: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    # « Pourquoi cette passe n'est pas un succès franc » : `type: message`
    # tronqué sur un échec (JAMAIS la stacktrace, elle vit dans les logs), ou
    # la raison du `skipped` (`retention_disabled`, `schema_not_current`,
    # `busy`). NULL sur un `ok`. C'est ici et non dans `last_detail` que va la
    # raison d'un saut, pour ne pas écraser le curseur de rotation.
    last_error: Mapped[str | None] = mapped_column(Text)
    last_detail: Mapped[dict | None] = mapped_column(JSONB)
    # +1 sur un échec, remis à 0 sur un succès, INTOUCHÉ sur un `skipped` : le
    # compteur doit survivre à une semaine de schéma désynchronisé.
    consecutive_failures: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0")
    )
    total_runs: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
