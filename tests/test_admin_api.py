"""Backoffice ``/admin/*`` : garde du rôle de plateforme et vue des jobs.

Aucun Postgres ni Redis : la fausse session FIFO rejoue l'ordre des execute
documenté dans app/admin/service.py, précédé de ceux de la garde
(``get_or_create_by_sub`` — l'upsert ne consomme pas la file, la relecture du
compte si) ; le canal de contrôle est un ``FakeKV``.

Ce qui compte ici :

- un compte ``public`` reçoit un **403** sans qu'aucune donnée de maintenance
  ne soit lue, un appel sans token un **401** ;
- l'API ne fait que **déposer une demande** dans Redis (``SET NX`` avec
  expiration), jamais tourner un job, et n'écrit rien en base ;
- personne ne vérifie que le scheduler vit : sans statut publié, une demande
  est acceptée et attend ; seul un Redis injoignable refuse (503), et la vue
  reste servie (l'état des passes vient de Postgres).
"""

import json
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.sql.dml import Insert

from app.core.config import settings
from app.main import create_app
from app.maintenance.control import STATUS_KEY, request_key
from app.maintenance.registry import JOBS
from tests.fakes import FakeKV, FakeSession, make_client
from tests.maintenance_fakes import compiled_sql

OVERVIEW_URL = "/api/v1/admin/maintenance/jobs"


def run_url(job_name: str) -> str:
    return f"{OVERVIEW_URL}/{job_name}/run"


def _account(role="super_admin"):
    return SimpleNamespace(id=uuid.uuid4(), sub="prof-123", email=None, platform_role=role)


def _status(*, running_job=None, next_runs=None):
    now = datetime.now(UTC)
    return json.dumps(
        {
            "started_at": (now - timedelta(hours=2)).isoformat(),
            "timezone": "Europe/Paris",
            "running_job": running_job,
            "running_since": (now - timedelta(seconds=30)).isoformat() if running_job else None,
            "next_runs": next_runs or {},
        }
    )


def _request(job_name, requested_at=None):
    moment = requested_at or datetime.now(UTC)
    payload = {"requested_at": moment.isoformat(), "requested_by": str(uuid.uuid4())}
    return request_key(job_name), json.dumps(payload)


def _state(job_name, **overrides):
    now = datetime.now(UTC)
    defaults = dict(
        job_name=job_name,
        last_started_at=now - timedelta(minutes=1),
        last_finished_at=now,
        last_status="ok",
        last_count=3,
        last_duration_ms=12,
        last_error=None,
        last_detail=None,
        consecutive_failures=0,
        total_runs=5,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _jobs_by_name(body):
    return {job["name"]: job for job in body["jobs"]}


def _users_upserts(session):
    return [
        stmt
        for stmt, _ in session.executed
        if isinstance(stmt, Insert) and stmt.table.name == "users"
    ]


# ─────────────────────────────────────────────
# Garde
# ─────────────────────────────────────────────


@pytest.mark.parametrize("method, url", [("get", OVERVIEW_URL), ("post", run_url("share_links"))])
def test_a_missing_token_is_a_401(method, url):
    response = getattr(TestClient(create_app()), method)(url)

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


@pytest.mark.parametrize("method, url", [("get", OVERVIEW_URL), ("post", run_url("share_links"))])
def test_a_public_account_is_forbidden_before_any_maintenance_read(method, url):
    session = FakeSession([[_account(role="public")]])
    kv = FakeKV(down=True)  # la moindre lecture du canal lèverait

    response = getattr(make_client(session, kv=kv), method)(url)

    assert response.status_code == 403
    assert response.json()["detail"] == "Accès réservé aux super administrateurs"
    # Les deux execute de la garde (upsert, relecture du compte), rien d'autre.
    assert len(session.executed) == 2


# ─────────────────────────────────────────────
# Vue d'ensemble
# ─────────────────────────────────────────────


def test_overview_lists_every_registry_job_in_order():
    next_run = "2026-09-19T01:20:00+00:00"
    kv = FakeKV(
        dict([(STATUS_KEY, _status(next_runs={"share_links": next_run})), _request("s3_orphans")])
    )
    session = FakeSession(
        [[_account()], [_state("share_links", last_detail={"cursor": "courses/a"})]]
    )

    response = make_client(session, kv=kv).get(OVERVIEW_URL)

    assert response.status_code == 200
    body = response.json()
    assert [job["name"] for job in body["jobs"]] == [job.name for job in JOBS]
    assert body["control_available"] is True
    assert body["scheduler"]["timezone"] == "Europe/Paris"
    assert body["scheduler"]["running_job"] is None

    jobs = _jobs_by_name(body)
    share_links = jobs["share_links"]
    assert share_links["cron"] == settings.MAINTENANCE_CRON_SHARE_LINKS
    assert share_links["retention_days"] == settings.PURGE_SHARE_LINKS_DAYS
    assert datetime.fromisoformat(share_links["next_run_at"]) == datetime.fromisoformat(next_run)
    assert share_links["last_run"]["status"] == "ok"
    assert share_links["last_run"]["detail"] == {"cursor": "courses/a"}
    assert share_links["requested_at"] is None

    assert jobs["s3_orphans"]["requested_at"] is not None
    # Un job jamais passé n'a pas de ligne d'état ; un contrôle n'a pas de rétention.
    assert jobs["storage_inventory"]["last_run"] is None
    assert jobs["storage_inventory"]["retention_days"] is None


def test_overview_reads_only_the_states_in_postgres():
    """Un seul execute après la garde : le reste vient de Redis — c'est ce qui
    laisse une base managée se mettre en veille entre deux passes."""
    session = FakeSession([[_account()], []])

    make_client(session).get(OVERVIEW_URL)

    [(states, _)] = session.executed[2:]
    assert "FROM maintenance_job_state" in compiled_sql(states)


def test_without_a_published_status_the_scheduler_is_unknown():
    """Rien de publié (scheduler arrêté proprement, pas encore démarré) : pas
    de plan, pas de passe en cours — et aucune supposition sur sa santé."""
    session = FakeSession([[_account()], []])

    body = make_client(session, kv=FakeKV()).get(OVERVIEW_URL).json()

    assert body["control_available"] is True
    assert body["scheduler"] is None
    assert all(job["next_run_at"] is None for job in body["jobs"])


def test_an_unreachable_redis_still_serves_the_pass_states():
    session = FakeSession([[_account()], [_state("share_links")]])

    response = make_client(session, kv=FakeKV(down=True)).get(OVERVIEW_URL)

    assert response.status_code == 200
    body = response.json()
    assert body["control_available"] is False
    assert body["scheduler"] is None
    assert _jobs_by_name(body)["share_links"]["last_run"]["status"] == "ok"


def test_a_garbled_status_is_ignored_rather_than_a_500():
    kv = FakeKV({STATUS_KEY: '{"started_at": "hier"}'})
    session = FakeSession([[_account()], []])

    response = make_client(session, kv=kv).get(OVERVIEW_URL)

    assert response.status_code == 200
    assert response.json()["scheduler"] is None


# ─────────────────────────────────────────────
# Demande de passe manuelle
# ─────────────────────────────────────────────


def test_a_run_request_is_filed_in_redis_and_the_overview_returned():
    admin = _account()
    kv = FakeKV()
    session = FakeSession([[admin], []])

    response = make_client(session, kv=kv).post(run_url("storage_inventory"))

    assert response.status_code == 202
    assert _jobs_by_name(response.json())["storage_inventory"]["requested_at"] is not None
    payload = json.loads(kv.data[request_key("storage_inventory")])
    assert payload["requested_by"] == str(admin.id)
    # Une demande que personne ne prend expire d'elle-même.
    assert kv.ttls[request_key("storage_inventory")] == settings.MAINTENANCE_REQUEST_TTL_SECONDS
    # Rien en base au-delà de la garde et de la lecture des états.
    assert len(session.executed) == 3
    assert session.commits == 1  # celui de la garde


def test_a_request_is_accepted_even_without_a_published_status():
    """Pas de preuve de vie exigée : la demande attendra le scheduler."""
    kv = FakeKV()
    session = FakeSession([[_account()], []])

    assert make_client(session, kv=kv).post(run_url("share_links")).status_code == 202
    assert request_key("share_links") in kv.data


def test_the_guard_runs_once_even_when_the_route_asks_for_the_account():
    """Garde du router + paramètre de la route : une seule résolution du compte
    (sinon la FIFO se décalerait et les états seraient lus comme un compte)."""
    session = FakeSession([[_account()], []])

    assert make_client(session).post(run_url("share_links")).status_code == 202
    assert len(_users_upserts(session)) == 1


def test_an_unknown_job_is_a_404_before_any_maintenance_read():
    session = FakeSession([[_account()]])

    response = make_client(session, kv=FakeKV(down=True)).post(run_url("everything"))

    assert response.status_code == 404
    assert len(session.executed) == 2


def test_an_unreachable_redis_refuses_with_a_503():
    session = FakeSession([[_account()], []])

    response = make_client(session, kv=FakeKV(down=True)).post(run_url("share_links"))

    assert response.status_code == 503


def test_a_running_job_cannot_be_requested_again():
    kv = FakeKV({STATUS_KEY: _status(running_job="share_links")})
    session = FakeSession([[_account()], []])

    response = make_client(session, kv=kv).post(run_url("share_links"))

    assert response.status_code == 409
    assert response.json()["detail"] == "Ce job est déjà en cours"
    assert request_key("share_links") not in kv.data


def test_another_running_job_does_not_block_a_request():
    """La demande attendra son tour, derrière la passe en cours."""
    kv = FakeKV({STATUS_KEY: _status(running_job="s3_orphans")})
    session = FakeSession([[_account()], []])

    assert make_client(session, kv=kv).post(run_url("share_links")).status_code == 202


def test_a_pending_request_cannot_be_doubled():
    key, payload = _request("share_links")
    kv = FakeKV({key: payload})
    session = FakeSession([[_account()], []])

    response = make_client(session, kv=kv).post(run_url("share_links"))

    assert response.status_code == 409
    assert response.json()["detail"] == "Une passe de ce job est déjà demandée"
    assert kv.data[key] == payload  # la demande d'origine est intacte
