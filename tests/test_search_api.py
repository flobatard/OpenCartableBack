"""Routes /public/search/* — recherche publique (J3), sans JWT ni identité.

Deux étages de tests, car la fausse session FIFO ne valide pas du SQL :
- contrat HTTP + ordre des ``execute`` + assemblage (motif test_public_api) ;
- assertions sur le **SQL compilé** des builders purs de ``app/search/service``
  (websearch_to_tsquery/french_unaccent/ts_rank/visibility… — c'est le seul
  moyen de valider la FTS sans Postgres), plus un garde-fou textuel sur les
  migrations : ``blocks_tsvector`` ne doit jamais indexer le corrigé
  (``reponse_attendue`` historique comme ``expected_answer``).
"""

import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql

from app.core.database import get_db
from app.core.storage import get_storage
from app.main import create_app
from app.search.service import (
    _courses_count_stmt,
    _courses_page_stmt,
    _teachers_count_stmt,
    _teachers_page_stmt,
    _tsquery,
)

_NOW = datetime(2026, 7, 7, 12, 0, tzinfo=UTC)


def _course_row(**overrides):
    defaults = dict(
        id=uuid.uuid4(),
        owner_id=uuid.uuid4(),
        title="Théorème de Pythagore",
        description="Triangle rectangle",
        preview_settings={},
        visibility="public",
        updated_at=_NOW,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows

    def one_or_none(self):
        if not self._rows:
            return None
        [row] = self._rows
        return row

    def scalar_one(self):
        [row] = self._rows
        return row


class _FakeSession:
    """FIFO des résultats de SELECT (lecture seule : rien d'autre à tracer)."""

    def __init__(self, select_results=()):
        self._select_results = list(select_results)
        self.executed = []
        self.commits = 0

    async def execute(self, stmt, params=None):
        self.executed.append((stmt, params))
        return _FakeResult(self._select_results.pop(0))

    async def commit(self):
        self.commits += 1


class _FakeStorage:
    """Faux client S3 : seul presign_get sert ici (avatar_url des profs)."""

    def presign_get(self, s3_key, original_name, inline=False):
        return f"https://s3.test/get/{s3_key}"


def _client(session) -> TestClient:
    # PAS d'override de get_current_user : ces routes vivent sans lui.
    app = create_app()
    app.dependency_overrides[get_db] = lambda: session
    app.dependency_overrides[get_storage] = lambda: _FakeStorage()
    return TestClient(app)


# --- Contrat HTTP : régime public, pagination, assemblage ----------------------


def test_search_courses_responds_without_authorization():
    session = _FakeSession([[0], []])
    response = _client(session).get("/api/v1/public/search/courses")
    assert response.status_code == 200
    assert "WWW-Authenticate" not in response.headers
    assert session.commits == 0  # lecture seule


def test_search_courses_page_assembled():
    c1 = _course_row()
    c2 = _course_row(title="Racines carrées", description=None)
    # FIFO : count, page, noms matières, noms niveaux, comptes de blocs.
    session = _FakeSession(
        [
            [12],
            [c1, c2],
            [(c1.id, "Mathématiques")],
            [(c1.id, "4e"), (c2.id, "3e")],
            [(c1.id, 5)],
        ]
    )
    response = _client(session).get(
        "/api/v1/public/search/courses", params={"q": "pythagore", "limit": 2}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 12
    assert body["limit"] == 2
    assert body["offset"] == 0
    assert [c["title"] for c in body["items"]] == [
        "Théorème de Pythagore",
        "Racines carrées",
    ]
    assert body["items"][0]["subjects"] == ["Mathématiques"]
    assert body["items"][0]["block_count"] == 5
    assert body["items"][1]["education_levels"] == ["3e"]
    assert body["items"][1]["block_count"] == 0


def test_search_courses_no_result_two_executes():
    session = _FakeSession([[0], []])
    response = _client(session).get(
        "/api/v1/public/search/courses", params={"q": "introuvable"}
    )
    assert response.status_code == 200
    assert response.json() == {"items": [], "total": 0, "limit": 20, "offset": 0}
    # count + page, puis court-circuit : pas d'execute d'assemblage.
    assert len(session.executed) == 2


def test_unknown_subject_facet_empty_page_without_oracle():
    # Id de matière inconnu : 200 + page vide immédiate (pas de 422 — une URL
    # partagée avec une facette périmée doit rester servable), un seul execute.
    session = _FakeSession([[]])
    response = _client(session).get(
        "/api/v1/public/search/courses", params={"subject_id": str(uuid.uuid4())}
    )
    assert response.status_code == 200
    assert response.json() == {"items": [], "total": 0, "limit": 20, "offset": 0}
    assert len(session.executed) == 1


def test_subject_facet_resolves_subtree_by_code():
    sid = uuid.uuid4()
    child_id = uuid.uuid4()
    # FIFO : code de la matière, ids du sous-arbre, count, page (vide).
    session = _FakeSession([["mathematiques.algebre"], [sid, child_id], [0], []])
    response = _client(session).get(
        "/api/v1/public/search/courses", params={"subject_id": str(sid)}
    )
    assert response.status_code == 200
    assert len(session.executed) == 4
    # Le select du sous-arbre matche le code exact OU le préfixe descendant.
    subtree_sql = str(
        session.executed[1][0].compile(dialect=postgresql.dialect())
    )
    assert "LIKE" in subtree_sql


def test_unknown_level_facet_empty_page_without_oracle():
    session = _FakeSession([[]])
    response = _client(session).get(
        "/api/v1/public/search/courses",
        params={"education_level_id": str(uuid.uuid4())},
    )
    assert response.status_code == 200
    assert response.json()["total"] == 0
    assert len(session.executed) == 1


def test_search_without_q_is_a_catalog():
    # Facettes seules (ou rien) : autorisé — tri par updated_at, pas de FTS.
    session = _FakeSession([[0], []])
    response = _client(session).get("/api/v1/public/search/courses")
    assert response.status_code == 200
    page_sql = str(session.executed[1][0].compile(dialect=postgresql.dialect()))
    assert "websearch_to_tsquery" not in page_sql
    assert "updated_at DESC" in page_sql


def test_blank_q_treated_as_absent():
    # websearch_to_tsquery('') ne matcherait rien : un q blanc est neutralisé.
    session = _FakeSession([[0], []])
    response = _client(session).get(
        "/api/v1/public/search/courses", params={"q": "   "}
    )
    assert response.status_code == 200
    page_sql = str(session.executed[1][0].compile(dialect=postgresql.dialect()))
    assert "websearch_to_tsquery" not in page_sql


def test_pagination_bounds_validated():
    response = _client(_FakeSession()).get(
        "/api/v1/public/search/courses", params={"limit": 51}
    )
    assert response.status_code == 422
    response = _client(_FakeSession()).get(
        "/api/v1/public/search/courses", params={"offset": -1}
    )
    assert response.status_code == 422


def test_search_teachers_page_assembled():
    uid1, uid2 = uuid.uuid4(), uuid.uuid4()
    # FIFO : count, page (id, public_name, avatar_s3_key, avatar_status),
    # matières enseignées, comptes de cours publics.
    session = _FakeSession(
        [
            [2],
            [
                (uid1, "Mme Ada", "users/u1/avatar/x/avatar.jpg", "available"),
                (uid2, "M. Turing", None, None),
            ],
            [(uid1, "Informatique"), (uid1, "Mathématiques")],
            [(uid1, 3)],
        ]
    )
    response = _client(session).get(
        "/api/v1/public/search/teachers", params={"q": "ada"}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    assert [t["public_name"] for t in body["items"]] == ["Mme Ada", "M. Turing"]
    assert body["items"][0]["subjects"] == ["Informatique", "Mathématiques"]
    assert body["items"][0]["public_course_count"] == 3
    assert body["items"][0]["avatar_url"] == (
        "https://s3.test/get/users/u1/avatar/x/avatar.jpg"
    )
    assert body["items"][1]["subjects"] == []
    assert body["items"][1]["public_course_count"] == 0
    assert body["items"][1]["avatar_url"] is None
    # La clé S3 ne sort jamais telle quelle dans le corps de la réponse.
    assert "avatar_s3_key" not in response.text


def test_search_teachers_no_result():
    session = _FakeSession([[0], []])
    response = _client(session).get("/api/v1/public/search/teachers")
    assert response.status_code == 200
    assert response.json() == {"items": [], "total": 0, "limit": 20, "offset": 0}
    assert len(session.executed) == 2


# --- SQL compilé des builders (seule validation FTS possible sans Postgres) ----


def _compiled(stmt):
    return stmt.compile(dialect=postgresql.dialect())


def _sql(stmt) -> str:
    return str(_compiled(stmt))


def test_sql_courses_fts_and_visibility():
    tsq = _tsquery("pythagore")
    compiled = _compiled(_courses_page_stmt(tsq, None, None, 20, 0))
    sql = str(compiled)
    assert "websearch_to_tsquery" in sql
    # Le nom de la config voyage en bind param (type REGCONFIG).
    assert "french_unaccent" in compiled.params.values()
    assert "ts_rank" in sql
    assert "search_vector @@" in sql
    assert "visibility" in sql  # seuls les cours publics
    assert "blocks" in sql  # le vecteur des blocs participe (EXISTS + rank)
    assert "expected_answer" not in sql  # garde-fou du corrigé
    assert "LIMIT" in sql and "OFFSET" in sql


def test_sql_courses_count_filters_like_page():
    tsq = _tsquery("pythagore")
    sql = _sql(_courses_count_stmt(tsq, [uuid.uuid4()], [uuid.uuid4()]))
    assert "count(" in sql
    assert "course_subjects" in sql
    assert "course_education_levels" in sql
    assert "visibility" in sql


def test_sql_teachers_visibility_criteria():
    tsq = _tsquery("ada")
    compiled = _compiled(_teachers_page_stmt(tsq, None, None, 20, 0))
    sql = str(compiled)
    assert "searchable" in sql  # opt-in explicite
    assert "public_name IS NOT NULL" in sql
    assert "visibility" in sql  # au moins un cours public (EXISTS)
    assert "to_tsvector" in sql  # vecteur à la volée
    assert "french_unaccent" in compiled.params.values()
    assert "context" in sql  # matières « teaching » uniquement
    assert "email" not in sql  # jamais de donnée privée
    assert "users.sub" not in sql  # ni l'identifiant OIDC
    # Colonnes avatar sélectionnées pour minter avatar_url (jamais le mime).
    assert "avatar_s3_key" in sql and "avatar_status" in sql
    assert "avatar_mime" not in sql


def test_sql_teachers_count_without_q():
    sql = _sql(_teachers_count_stmt(None, None, None))
    assert "websearch_to_tsquery" not in sql
    assert "searchable" in sql
    assert "avatar_s3_key" not in sql  # le count ne sélectionne pas les colonnes


# --- Garde-fou migration : le corrigé n'entre jamais dans l'index --------------


def test_migrations_blocks_tsvector_never_index_expected_answer():
    """Scanne les corps SQL ``$$…$$`` des fonctions ``blocks_tsvector`` de
    toutes les migrations — historiques (clé JSONB française
    ``reponse_attendue``) comme récentes (clé anglaise ``expected_answer``) :
    aucune des deux clés ne doit jamais y figurer (le corrigé deviendrait
    cherchable depuis le régime public)."""
    versions = Path(__file__).resolve().parent.parent / "alembic" / "versions"
    pattern = re.compile(
        r"CREATE\s+FUNCTION\s+blocks_tsvector.*?\$\$(.*?)\$\$",
        re.IGNORECASE | re.DOTALL,
    )
    for path in sorted(versions.glob("*.py")):
        for body in pattern.findall(path.read_text(encoding="utf-8")):
            assert "reponse_attendue" not in body, path.name
            assert "expected_answer" not in body, path.name
