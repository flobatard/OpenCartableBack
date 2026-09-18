"""CLI du rôle de plateforme (:mod:`app.users.roles`) — sans Postgres.

Aucune route n'écrit ``platform_role`` : cette commande est le seul chemin vers
le backoffice, et elle doit refuser ce qui est ambigu plutôt que promouvoir le
mauvais compte. L'ordre des ``execute`` (sub, puis email) est rejoué par la
fausse session FIFO.
"""

import uuid
from types import SimpleNamespace

import pytest

from app.models.user import PLATFORM_ROLE_PUBLIC, PLATFORM_ROLE_SUPER_ADMIN
from app.users import roles
from tests.fakes import FakeSession
from tests.maintenance_fakes import FakeResult, compiled_params, compiled_sql
from tests.maintenance_fakes import FakeSession as ScriptedSession


def _account(**overrides):
    defaults = dict(
        id=uuid.uuid4(),
        sub="sub-1",
        email="prof@example.org",
        platform_role=PLATFORM_ROLE_PUBLIC,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


class FakeEngine:
    def __init__(self):
        self.disposed = False

    async def dispose(self):
        self.disposed = True


@pytest.fixture
def cli_session(monkeypatch):
    """Branche la CLI sur une session scriptée et un faux engine."""

    def install(*results):
        session = ScriptedSession([FakeResult(rows) for rows in results])
        engine = FakeEngine()
        monkeypatch.setattr(roles, "AsyncSessionLocal", lambda: session)
        monkeypatch.setattr(roles, "engine", engine)
        return session, engine

    return install


# ─────────────────────────────────────────────
# Recherche du compte
# ─────────────────────────────────────────────


@pytest.mark.anyio
async def test_the_sub_is_looked_up_first():
    account = _account()
    session = FakeSession([[account]])

    user, changed = await roles.set_platform_role(session, "sub-1", PLATFORM_ROLE_SUPER_ADMIN)

    assert user is account and changed
    assert account.platform_role == PLATFORM_ROLE_SUPER_ADMIN
    assert session.commits == 1
    assert len(session.executed) == 1  # trouvé par sub : pas de recherche par email


@pytest.mark.anyio
async def test_the_email_is_matched_case_insensitively():
    account = _account()
    session = FakeSession([[], [account]])

    user, _ = await roles.set_platform_role(session, "Prof@Example.org", PLATFORM_ROLE_SUPER_ADMIN)

    assert user is account
    email_lookup = session.executed[1][0]
    assert "lower(users.email) = " in compiled_sql(email_lookup)
    assert "prof@example.org" in compiled_params(email_lookup).values()


@pytest.mark.anyio
async def test_an_unknown_account_is_refused_without_writing():
    """Le compte naît à la première connexion : rien à promouvoir avant."""
    session = FakeSession([[], []])

    with pytest.raises(roles.AccountLookupError, match="connectée une première fois"):
        await roles.set_platform_role(session, "inconnu@example.org", PLATFORM_ROLE_SUPER_ADMIN)
    assert session.commits == 0


@pytest.mark.anyio
async def test_an_ambiguous_email_is_refused():
    """L'email n'est qu'un instantané du claim : deux comptes peuvent le porter."""
    first, second = _account(sub="a"), _account(sub="b")
    session = FakeSession([[], [first, second]])

    with pytest.raises(roles.AccountLookupError, match="par son sub"):
        await roles.set_platform_role(session, "prof@example.org", PLATFORM_ROLE_SUPER_ADMIN)
    assert first.platform_role == second.platform_role == PLATFORM_ROLE_PUBLIC
    assert session.commits == 0


@pytest.mark.anyio
async def test_granting_twice_is_idempotent():
    account = _account(platform_role=PLATFORM_ROLE_SUPER_ADMIN)
    session = FakeSession([[account]])

    _, changed = await roles.set_platform_role(session, "sub-1", PLATFORM_ROLE_SUPER_ADMIN)

    assert changed is False
    assert session.commits == 0


@pytest.mark.anyio
async def test_list_filters_on_the_super_admin_role():
    admin = _account(platform_role=PLATFORM_ROLE_SUPER_ADMIN)
    session = FakeSession([[admin]])

    assert await roles.list_super_admins(session) == [admin]
    sql = compiled_sql(session.executed[0][0])
    assert "WHERE users.platform_role = " in sql
    assert "ORDER BY users.created_at" in sql


# ─────────────────────────────────────────────
# Commande
# ─────────────────────────────────────────────


@pytest.mark.anyio
@pytest.mark.parametrize("argv", [[], ["promote", "x"], ["grant"]])
async def test_bad_usage_exits_2_without_touching_the_database(argv, monkeypatch):
    monkeypatch.setattr(roles, "AsyncSessionLocal", lambda: pytest.fail("session ouverte"))

    assert await roles.main(argv) == roles.EXIT_USAGE


@pytest.mark.anyio
async def test_grant_prints_the_account_and_exits_0(cli_session, capsys):
    account = _account()
    session, engine = cli_session([account])

    assert await roles.main(["grant", "sub-1"]) == roles.EXIT_OK

    assert account.platform_role == PLATFORM_ROLE_SUPER_ADMIN
    assert session.commits == 1
    assert engine.disposed
    out = capsys.readouterr().out
    assert "rôle posé : super_admin" in out
    assert "prof@example.org" in out


@pytest.mark.anyio
async def test_revoke_brings_the_account_back_to_public(cli_session):
    account = _account(platform_role=PLATFORM_ROLE_SUPER_ADMIN)
    cli_session([account])

    assert await roles.main(["revoke", "sub-1"]) == roles.EXIT_OK
    assert account.platform_role == PLATFORM_ROLE_PUBLIC


@pytest.mark.anyio
async def test_an_unknown_account_exits_1(cli_session, capsys):
    _, engine = cli_session([], [])

    assert await roles.main(["grant", "personne@example.org"]) == roles.EXIT_NOT_FOUND
    assert "aucun compte" in capsys.readouterr().err
    assert engine.disposed
