"""Types de valeur partagés par le registre, les contrôles et le runner.

Un module à part, et pas ces dataclasses posées dans ``runner.py`` : le registre
a besoin de ``JobContext`` pour typer ses ``bind`` et les contrôles de
``JobOutcome`` pour leur retour ; le runner, lui, importe le registre. Les
loger ici casse le cycle et garde la lecture dans le bon sens — un contrôle
n'a pas à importer l'exécuteur.
"""

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover — annotations seules, rien au runtime
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.core.storage import Storage

# Statuts d'une passe, tels qu'ils atterrissent dans `maintenance_job_state`.
STATUS_OK = "ok"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"

# Raisons d'un `skipped`. Les deux premières sont les **deux leviers
# d'inactivité** à ne jamais confondre : une rétention à 0 désactive la TÂCHE
# (le job reste planifié), un cron vide ne planifie pas le JOB (il n'atteint
# même pas le runner et n'écrit aucune ligne d'état).
SKIP_RETENTION = "retention_disabled"
SKIP_SCHEMA = "schema_not_current"
SKIP_BUSY = "busy"


@dataclass(frozen=True)
class JobContext:
    """Ce qu'une passe reçoit : sa session, son S3 éventuel, son détail passé."""

    db: "AsyncSession"
    storage: "Storage | None" = None
    # `last_detail` de la passe précédente, chargé seulement si le job l'a
    # demandé (`needs_previous_detail`) : c'est par là que passe le curseur de
    # rotation de `missing_s3_objects`.
    previous_detail: dict | None = None


@dataclass(frozen=True)
class JobOutcome:
    """Retour riche d'une passe : un compte, et un rapport structuré.

    Les purges rendent un simple ``int`` (leur compte de lignes) ; les
    contrôles rendent ceci, parce qu'un inventaire n'a pas de « compte » unique
    et qu'un curseur doit survivre à la passe.
    """

    count: int
    detail: dict | None = None


@dataclass(frozen=True)
class JobResult:
    """Issue d'une passe, telle qu'elle est journalisée et enregistrée."""

    name: str
    status: str
    count: int = 0
    duration_ms: int = 0

    @property
    def failed(self) -> bool:
        return self.status == STATUS_FAILED


@dataclass(frozen=True)
class MaintenanceReport:
    """Synthèse d'un lot de passes (le one-shot en produit une)."""

    results: list[JobResult] = field(default_factory=list)

    @property
    def failed(self) -> bool:
        """Un ``skipped`` n'est **pas** un échec : ne rien faire reste sûr."""
        return any(result.failed for result in self.results)

    def summary(self) -> str:
        return ", ".join(
            f"{result.name}="
            + ("échec" if result.failed else f"{result.status}:{result.count}")
            for result in self.results
        )
