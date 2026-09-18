"""Canal de contrôle backoffice ↔ scheduler (:mod:`app.maintenance.control`).

Sur un ``FakeKV`` (l'interface de ``app.core.kv``). Ce qu'on protège :

- une demande par job au plus (``SET NX``), qui **expire** si personne ne la
  prend ;
- la prise est une suppression (``GETDEL``) : au plus une exécution par
  demande, la plus ancienne d'abord, et une demande illisible ne bloque pas
  son job ;
- le statut publié est du JSON lisible par l'API, plan en UTC quel que soit le
  fuseau des crons.
"""

import json
import uuid
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.core.config import settings
from app.maintenance import control
from tests.fakes import FakeKV

NOW = datetime.now(UTC)


def _payload(moment, requested_by="u-1"):
    return json.dumps({"requested_at": moment.isoformat(), "requested_by": requested_by})


# ─────────────────────────────────────────────
# Côté API
# ─────────────────────────────────────────────


@pytest.mark.anyio
async def test_a_request_is_filed_with_an_expiry():
    kv = FakeKV()
    requester = uuid.uuid4()

    requested_at = await control.request_run(kv, "share_links", requester)

    key = control.request_key("share_links")
    assert json.loads(kv.data[key]) == {
        "requested_at": requested_at.isoformat(),
        "requested_by": str(requester),
    }
    assert kv.ttls[key] == settings.MAINTENANCE_REQUEST_TTL_SECONDS


@pytest.mark.anyio
async def test_a_request_is_never_doubled():
    kv = FakeKV()
    await control.request_run(kv, "share_links", uuid.uuid4())
    first = kv.data[control.request_key("share_links")]

    assert await control.request_run(kv, "share_links", uuid.uuid4()) is None
    assert kv.data[control.request_key("share_links")] == first


@pytest.mark.anyio
async def test_pending_requests_are_read_in_one_go_and_skip_garbage():
    kv = FakeKV(
        {
            control.request_key("share_links"): _payload(NOW),
            control.request_key("s3_orphans"): "pas du json",
        }
    )

    pending = await control.pending_requests(kv, ["share_links", "s3_orphans", "storage_inventory"])

    assert pending == {"share_links": NOW}


@pytest.mark.anyio
async def test_status_is_none_until_published():
    assert await control.read_status(FakeKV()) is None


# ─────────────────────────────────────────────
# Côté scheduler
# ─────────────────────────────────────────────


@pytest.mark.anyio
async def test_the_oldest_request_is_claimed_and_removed():
    kv = FakeKV(
        {
            control.request_key("share_links"): _payload(NOW),
            control.request_key("s3_orphans"): _payload(NOW - timedelta(minutes=1), "u-2"),
        }
    )

    claimed = await control.claim_request(kv, ["share_links", "s3_orphans"])

    assert claimed == control.ClaimedRequest("s3_orphans", "u-2", NOW - timedelta(minutes=1))
    assert control.request_key("s3_orphans") not in kv.data
    assert control.request_key("share_links") in kv.data  # la suivante attend


@pytest.mark.anyio
async def test_nothing_to_claim():
    assert await control.claim_request(FakeKV(), ["share_links"]) is None


@pytest.mark.anyio
async def test_a_request_taken_meanwhile_is_skipped(monkeypatch):
    """Lue au MGET, disparue au GETDEL (expirée, ou prise ailleurs) : on passe
    à la suivante au lieu de lancer une passe que personne n'a plus demandée."""
    kv = FakeKV(
        {
            control.request_key("s3_orphans"): _payload(NOW - timedelta(minutes=1)),
            control.request_key("share_links"): _payload(NOW),
        }
    )
    take = kv.take

    async def racing_take(key):
        if key == control.request_key("s3_orphans"):
            kv.data.pop(key)  # expirée entre la lecture et la prise
        return await take(key)

    monkeypatch.setattr(kv, "take", racing_take)

    claimed = await control.claim_request(kv, ["share_links", "s3_orphans"])

    assert claimed.job_name == "share_links"


@pytest.mark.anyio
async def test_an_unreadable_request_is_removed_rather_than_blocking_its_job():
    kv = FakeKV({control.request_key("share_links"): "pas du json"})

    assert await control.claim_request(kv, ["share_links"]) is None
    assert control.request_key("share_links") not in kv.data


@pytest.mark.anyio
async def test_the_published_status_round_trips():
    kv = FakeKV()
    since = NOW - timedelta(seconds=10)

    await control.publish_status(
        kv,
        started_at=NOW,
        timezone="Europe/Paris",
        running=("s3_orphans", since),
        next_runs={"share_links": "2026-09-19T01:20:00+00:00"},
    )

    status = await control.read_status(kv)
    assert status == {
        "started_at": NOW.isoformat(),
        "timezone": "Europe/Paris",
        "running_job": "s3_orphans",
        "running_since": since.isoformat(),
        "next_runs": {"share_links": "2026-09-19T01:20:00+00:00"},
    }


@pytest.mark.anyio
async def test_clearing_the_status_leaves_nothing_stale():
    kv = FakeKV({control.STATUS_KEY: "{}"})

    await control.clear_status(kv)

    assert control.STATUS_KEY not in kv.data


def test_the_plan_is_published_in_utc_and_skips_unscheduled_jobs():
    paris = datetime(2026, 9, 19, 3, 20, tzinfo=ZoneInfo("Europe/Paris"))

    plan = control.serialize_plan({"share_links": paris, "s3_orphans": None})

    assert plan == {"share_links": "2026-09-19T01:20:00+00:00"}
    json.dumps(plan)  # le statut passe par json.dumps : aucun datetime ne doit rester


def test_the_platform_role_check_is_declared():
    """Autogenerate ne voit pas un CHECK ajouté à une table existante : la
    migration le pose à la main, le modèle doit le déclarer pour rester fidèle."""
    from app.core.database import Base

    checks = {constraint.name for constraint in Base.metadata.tables["users"].constraints}
    assert "ck_users_platform_role" in checks
