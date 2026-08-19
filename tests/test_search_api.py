"""Routes /public/search/* — recherche publique (J3), sans JWT ni identité.

Deux étages de tests, car la fausse session FIFO ne valide pas du SQL :
- contrat HTTP + ordre des ``execute`` + assemblage (motif test_public_api) ;
- assertions sur le **SQL compilé** des builders purs de ``app/search/service``
  (websearch_to_tsquery/french_unaccent/ts_rank/visibilite… — c'est le seul
  moyen de valider la FTS sans Postgres), plus un garde-fou textuel sur la
  migration : ``blocks_tsvector`` ne doit jamais indexer ``reponse_attendue``.
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
        titre="Théorème de Pythagore",
        description="Triangle rectangle",
        preview_settings={},
        visibilite="public",
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

    def presign_get(self, s3_key, nom_original, inline=False):
        return f"https://s3.test/get/{s3_key}"


def _client(session) -> TestClient:
    # PAS d'override de get_current_user : ces routes vivent sans lui.
    app = create_app()
    app.dependency_overrides[get_db] = lambda: session
    app.dependency_overrides[get_storage] = lambda: _FakeStorage()
    return TestClient(app)


# --- Contrat HTTP : régime public, pagination, assemblage ----------------------


def test_recherche_cours_repond_sans_authorization():
    session = _FakeSession([[0], []])
    response = _client(session).get("/api/v1/public/search/courses")
    assert response.status_code == 200
    assert "WWW-Authenticate" not in response.headers
    assert session.commits == 0  # lecture seule


def test_recherche_cours_page_assemblee():
    c1 = _course_row()
    c2 = _course_row(titre="Racines carrées", description=None)
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
    assert [c["titre"] for c in body["items"]] == [
        "Théorème de Pythagore",
        "Racines carrées",
    ]
    assert body["items"][0]["subjects"] == ["Mathématiques"]
    assert body["items"][0]["block_count"] == 5
    assert body["items"][1]["education_levels"] == ["3e"]
    assert body["items"][1]["block_count"] == 0


def test_recherche_cours_sans_resultat_deux_executes():
    session = _FakeSession([[0], []])
    response = _client(session).get(
        "/api/v1/public/search/courses", params={"q": "introuvable"}
    )
    assert response.status_code == 200
    assert response.json() == {"items": [], "total": 0, "limit": 20, "offset": 0}
    # count + page, puis court-circuit : pas d'execute d'assemblage.
    assert len(session.executed) == 2


def test_facette_matiere_inconnue_page_vide_sans_oracle():
    # Id de matière inconnu : 200 + page vide immédiate (pas de 422 — une URL
    # partagée avec une facette périmée doit rester servable), un seul execute.
    session = _FakeSession([[]])
    response = _client(session).get(
        "/api/v1/public/search/courses", params={"subject_id": str(uuid.uuid4())}
    )
    assert response.status_code == 200
    assert response.json() == {"items": [], "total": 0, "limit": 20, "offset": 0}
    assert len(session.executed) == 1


def test_facette_matiere_resout_le_sous_arbre_par_code():
    sid = uuid.uuid4()
    enfant_id = uuid.uuid4()
    # FIFO : code de la matière, ids du sous-arbre, count, page (vide).
    session = _FakeSession([["mathematiques.algebre"], [sid, enfant_id], [0], []])
    response = _client(session).get(
        "/api/v1/public/search/courses", params={"subject_id": str(sid)}
    )
    assert response.status_code == 200
    assert len(session.executed) == 4
    # Le select du sous-arbre matche le code exact OU le préfixe descendant.
    sql_sous_arbre = str(
        session.executed[1][0].compile(dialect=postgresql.dialect())
    )
    assert "LIKE" in sql_sous_arbre


def test_facette_niveau_inconnu_page_vide_sans_oracle():
    session = _FakeSession([[]])
    response = _client(session).get(
        "/api/v1/public/search/courses",
        params={"education_level_id": str(uuid.uuid4())},
    )
    assert response.status_code == 200
    assert response.json()["total"] == 0
    assert len(session.executed) == 1


def test_recherche_sans_q_est_un_catalogue():
    # Facettes seules (ou rien) : autorisé — tri par updated_at, pas de FTS.
    session = _FakeSession([[0], []])
    response = _client(session).get("/api/v1/public/search/courses")
    assert response.status_code == 200
    sql_page = str(session.executed[1][0].compile(dialect=postgresql.dialect()))
    assert "websearch_to_tsquery" not in sql_page
    assert "updated_at DESC" in sql_page


def test_q_blanc_traite_comme_absent():
    # websearch_to_tsquery('') ne matcherait rien : un q blanc est neutralisé.
    session = _FakeSession([[0], []])
    response = _client(session).get(
        "/api/v1/public/search/courses", params={"q": "   "}
    )
    assert response.status_code == 200
    sql_page = str(session.executed[1][0].compile(dialect=postgresql.dialect()))
    assert "websearch_to_tsquery" not in sql_page


def test_pagination_bornes_validees():
    response = _client(_FakeSession()).get(
        "/api/v1/public/search/courses", params={"limit": 51}
    )
    assert response.status_code == 422
    response = _client(_FakeSession()).get(
        "/api/v1/public/search/courses", params={"offset": -1}
    )
    assert response.status_code == 422


def test_recherche_teachers_page_assemblee():
    uid1, uid2 = uuid.uuid4(), uuid.uuid4()
    # FIFO : count, page (id, nom_public, avatar_s3_key, avatar_statut),
    # matières enseignées, comptes de cours publics.
    session = _FakeSession(
        [
            [2],
            [
                (uid1, "Mme Ada", "users/u1/avatar/x/avatar.jpg", "disponible"),
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
    assert [t["nom_public"] for t in body["items"]] == ["Mme Ada", "M. Turing"]
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


def test_recherche_teachers_sans_resultat():
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


def test_sql_cours_fts_et_visibilite():
    tsq = _tsquery("pythagore")
    compiled = _compiled(_courses_page_stmt(tsq, None, None, 20, 0))
    sql = str(compiled)
    assert "websearch_to_tsquery" in sql
    # Le nom de la config voyage en bind param (type REGCONFIG).
    assert "french_unaccent" in compiled.params.values()
    assert "ts_rank" in sql
    assert "search_vector @@" in sql
    assert "visibilite" in sql  # seuls les cours publics
    assert "blocks" in sql  # le vecteur des blocs participe (EXISTS + rank)
    assert "reponse_attendue" not in sql  # garde-fou du corrigé
    assert "LIMIT" in sql and "OFFSET" in sql


def test_sql_cours_count_filtre_comme_la_page():
    tsq = _tsquery("pythagore")
    sql = _sql(_courses_count_stmt(tsq, [uuid.uuid4()], [uuid.uuid4()]))
    assert "count(" in sql
    assert "course_subjects" in sql
    assert "course_education_levels" in sql
    assert "visibilite" in sql


def test_sql_teachers_criteres_de_visibilite():
    tsq = _tsquery("ada")
    compiled = _compiled(_teachers_page_stmt(tsq, None, None, 20, 0))
    sql = str(compiled)
    assert "cherchable" in sql  # opt-in explicite
    assert "nom_public IS NOT NULL" in sql
    assert "visibilite" in sql  # au moins un cours public (EXISTS)
    assert "to_tsvector" in sql  # vecteur à la volée
    assert "french_unaccent" in compiled.params.values()
    assert "contexte" in sql  # matières « enseigne » uniquement
    assert "email" not in sql  # jamais de donnée privée
    assert "users.sub" not in sql  # ni l'identifiant OIDC
    # Colonnes avatar sélectionnées pour minter avatar_url (jamais le mime).
    assert "avatar_s3_key" in sql and "avatar_statut" in sql
    assert "avatar_mime" not in sql


def test_sql_teachers_count_sans_q():
    sql = _sql(_teachers_count_stmt(None, None, None))
    assert "websearch_to_tsquery" not in sql
    assert "cherchable" in sql
    assert "avatar_s3_key" not in sql  # le count ne sélectionne pas les colonnes


# --- Garde-fou migration : le corrigé n'entre jamais dans l'index --------------


def test_migration_blocks_tsvector_nindexe_pas_le_corrige():
    """Scanne les corps SQL ``$$…$$`` des fonctions ``blocks_tsvector`` de
    toutes les migrations : ``reponse_attendue`` ne doit jamais y figurer
    (il deviendrait cherchable depuis le régime public). Vacuité assumée
    tant que la migration J3 n'existe pas encore."""
    versions = Path(__file__).resolve().parent.parent / "alembic" / "versions"
    pattern = re.compile(
        r"CREATE\s+FUNCTION\s+blocks_tsvector.*?\$\$(.*?)\$\$",
        re.IGNORECASE | re.DOTALL,
    )
    for path in sorted(versions.glob("*.py")):
        for body in pattern.findall(path.read_text(encoding="utf-8")):
            assert "reponse_attendue" not in body, path.name
