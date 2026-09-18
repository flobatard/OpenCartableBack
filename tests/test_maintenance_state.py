"""Tests de l'état des jobs en base (:mod:`app.maintenance.state`).

Sur le **SQL compilé**, comme le reste de la maintenance et la recherche FTS :
ce qui se joue ici, ce sont les colonnes présentes ou absentes du ``SET`` d'un
``ON CONFLICT``. Trois absences valent des présences :

- ``consecutive_failures`` **absent** sur un ``skipped`` — le compteur doit
  survivre à une semaine de schéma désynchronisé ;
- ``last_detail`` **absent** quand la passe n'a rien produit — sinon un
  ``skipped`` effacerait le curseur de rotation de ``missing_s3_objects``, qui
  ne repartirait jamais du bon endroit ;
- le mot de passe Postgres **absent** de ``last_error``.
"""

from datetime import UTC, datetime

import pytest

from app.core.config import settings
from app.maintenance import state
from app.maintenance.results import STATUS_FAILED, STATUS_OK, STATUS_SKIPPED
from tests.maintenance_fakes import FakeSession, compiled_sql

NOW = datetime.now(UTC)


def upsert(status=STATUS_OK, *, count=0, error=None, detail=None):
    return state.build_state_upsert(
        "s3_orphans",
        started_at=NOW,
        finished_at=NOW,
        status=status,
        count=count,
        duration_ms=42,
        error=error,
        detail=detail,
    )


def set_clause(statement) -> str:
    """La partie ``DO UPDATE SET …`` du SQL compilé."""
    sql = compiled_sql(statement)
    return sql[sql.index("DO UPDATE SET") :]


def test_upsert_targets_the_primary_key():
    sql = compiled_sql(upsert())
    assert "INSERT INTO maintenance_job_state" in sql
    assert "ON CONFLICT (job_name) DO UPDATE" in sql


def test_total_runs_always_increments():
    assert "total_runs = (maintenance_job_state.total_runs + " in set_clause(upsert())


def test_failure_increments_consecutive_failures():
    clause = set_clause(upsert(STATUS_FAILED, error="boom"))
    assert "consecutive_failures = (maintenance_job_state.consecutive_failures + " in clause


def test_success_resets_consecutive_failures():
    assert "consecutive_failures = %(param_" in set_clause(upsert(STATUS_OK))


def test_skip_leaves_consecutive_failures_alone():
    """Un job sauté n'est ni un succès ni un échec : le compteur n'est pas touché."""
    assert "consecutive_failures" not in set_clause(upsert(STATUS_SKIPPED))


def test_detail_is_written_when_the_pass_produced_one():
    assert "last_detail = excluded.last_detail" in set_clause(
        upsert(detail={"cursor": "courses/a"})
    )


def test_detail_is_not_overwritten_when_absent():
    """C'est ce qui protège le curseur de ``missing_s3_objects`` d'un ``skipped``."""
    assert "last_detail" not in set_clause(upsert(STATUS_SKIPPED))
    assert "last_detail" not in set_clause(upsert(STATUS_OK, count=3))


def test_state_select_reads_only_the_detail_of_that_job():
    sql = compiled_sql(state.build_state_select("missing_s3_objects"))
    assert "SELECT maintenance_job_state.last_detail" in sql
    assert "WHERE maintenance_job_state.job_name = " in sql


# ─────────────────────────────────────────────
# Erreur : typée, tronquée, sans secret
# ─────────────────────────────────────────────


def test_error_keeps_the_exception_type():
    assert state.format_error(ValueError("cassé")) == "ValueError: cassé"


def test_error_is_truncated(monkeypatch):
    monkeypatch.setattr(settings, "MAINTENANCE_ERROR_MAX_CHARS", 40)
    message = state.format_error(RuntimeError("x" * 500))

    assert len(message) == 40
    assert message.endswith(state.TRUNCATION_MARKER)


def test_error_carries_no_password(monkeypatch):
    """Certains messages de driver citent le DSN complet ; un secret ne passe
    jamais dans une trace persistée."""
    monkeypatch.setattr(settings, "POSTGRES_PASSWORD", "hunter2")
    message = state.format_error(RuntimeError("connexion refusée pour hunter2@db"))

    assert "hunter2" not in message
    assert state.REDACTED in message


def test_empty_password_is_not_redacted_everywhere(monkeypatch):
    """Un mot de passe vide ne doit pas faire remplacer chaque caractère."""
    monkeypatch.setattr(settings, "POSTGRES_PASSWORD", "")
    assert state.format_error(RuntimeError("boom")) == "RuntimeError: boom"


# ─────────────────────────────────────────────
# Détail borné
# ─────────────────────────────────────────────


def test_detail_lists_are_bounded(monkeypatch):
    monkeypatch.setattr(settings, "MAINTENANCE_DETAIL_MAX_ITEMS", 3)
    bounded = state.bounded({"missing_keys": list(range(50)), "checked": 50})

    assert bounded["missing_keys"] == [0, 1, 2]
    assert bounded["checked"] == 50  # un scalaire n'est pas touché


def test_detail_bounding_is_recursive(monkeypatch):
    monkeypatch.setattr(settings, "MAINTENANCE_DETAIL_MAX_ITEMS", 2)
    bounded = state.bounded({"s3": {"courses/": {"keys": [1, 2, 3, 4]}}})

    assert bounded["s3"]["courses/"]["keys"] == [1, 2]


# ─────────────────────────────────────────────
# La session dédiée
# ─────────────────────────────────────────────


@pytest.mark.anyio
async def test_record_state_uses_its_own_session(monkeypatch):
    """La session du job peut être transactionnellement morte au moment où l'on
    veut justement enregistrer son échec : l'écriture d'état n'y touche pas."""
    dedicated = FakeSession()
    monkeypatch.setattr(state, "AsyncSessionLocal", lambda: dedicated)

    await state.record_state(
        "s3_orphans", started_at=NOW, finished_at=NOW, status=STATUS_FAILED, error="boom"
    )

    assert len(dedicated.statements) == 1
    assert dedicated.commits == 1
    assert "INSERT INTO maintenance_job_state" in compiled_sql(dedicated.statements[0])


@pytest.mark.anyio
async def test_record_state_bounds_the_detail(monkeypatch):
    monkeypatch.setattr(settings, "MAINTENANCE_DETAIL_MAX_ITEMS", 2)
    dedicated = FakeSession()
    monkeypatch.setattr(state, "AsyncSessionLocal", lambda: dedicated)

    await state.record_state(
        "missing_s3_objects",
        started_at=NOW,
        finished_at=NOW,
        status=STATUS_OK,
        detail={"missing_keys": ["a", "b", "c", "d"]},
    )

    written = dedicated.statements[0].compile().params
    assert written["last_detail"]["missing_keys"] == ["a", "b"]


def test_model_is_registered_for_alembic():
    """Un modèle absent d'``app.models`` est invisible d'autogenerate : la table
    n'existerait jamais, et le job tomberait à sa première passe."""
    import app.models as models
    from app.core.database import Base

    assert "MaintenanceJobState" in models.__all__
    assert "maintenance_job_state" in Base.metadata.tables
