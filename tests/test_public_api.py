"""Routes /public/* — régime d'accès élève (J2), sans JWT ni identité.

L'inverse du ``test_auth_requise`` systématique des autres suites : ici le
client n'envoie JAMAIS d'en-tête Authorization et les routes doivent
répondre quand même. Sémantique d'erreur : 404 uniforme (token inconnu/
révoqué/expiré, cours en_cours ou privé sans token) — jamais 401/403/410.

Fausse session FIFO habituelle (ordre des ``execute`` documenté dans
app/public/service.py) ; l'expiration des liens est comparée en Python,
donc pilotable par les lignes fabriquées.
"""

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.sql.dml import Delete, Insert, Update

from app.core.database import get_db
from app.core.storage import get_storage
from app.main import create_app

_NOW = datetime(2026, 7, 7, 12, 0, tzinfo=UTC)
_NOW_JSON = "2026-07-07T12:00:00Z"
_TOKEN = "tok-" + "a" * 39


def _course_row(**overrides):
    defaults = dict(
        id=uuid.uuid4(),
        owner_id=uuid.uuid4(),
        titre="Fractions",
        description="Les bases",
        preview_settings={},
        visibilite="public",
        updated_at=_NOW,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _link_row(course_id, **overrides):
    defaults = dict(
        id=uuid.uuid4(),
        course_id=course_id,
        token=_TOKEN,
        libelle=None,
        expires_at=datetime.now(UTC) + timedelta(days=100),
        revoked=False,
        created_at=_NOW,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _block_row(course_id, **overrides):
    defaults = dict(
        id=uuid.uuid4(),
        course_id=course_id,
        position=0,
        type="texte",
        titre=None,
        description=None,
        content={"markdown": "Bonjour"},
        resource_id=None,
        module_id=None,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _resource_row(course_id, **overrides):
    defaults = dict(
        id=uuid.uuid4(),
        course_id=course_id,
        type="image",
        s3_key="abc/photo.png",
        nom_original="photo.png",
        taille=1234,
        mime="image/png",
        statut="disponible",
        created_at=_NOW,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _module_row(course_id, **overrides):
    defaults = dict(
        id=uuid.uuid4(),
        course_id=course_id,
        titre="Quiz",
        html="<p>Q</p>",
        css="",
        js="console.log('ok')",
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _user_row(**overrides):
    defaults = dict(
        id=uuid.uuid4(),
        nom_public="M. Dupont",
        avatar_s3_key=None,
        avatar_mime=None,
        avatar_statut=None,
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

    def one(self):
        [row] = self._rows
        return row

    def one_or_none(self):
        if not self._rows:
            return None
        [row] = self._rows
        return row


class _FakeSession:
    """FIFO des résultats de SELECT ; écritures tracées sans consommer."""

    def __init__(self, select_results=()):
        self._select_results = list(select_results)
        self.executed = []
        self.commits = 0

    async def execute(self, stmt, params=None):
        self.executed.append((stmt, params))
        if isinstance(stmt, Insert) and stmt._returning:
            return _FakeResult(self._select_results.pop(0))
        if isinstance(stmt, (Insert, Update, Delete)):
            return _FakeResult([])
        return _FakeResult(self._select_results.pop(0))

    async def commit(self):
        self.commits += 1


class _FakeStorage:
    def __init__(self):
        self.inline_calls = []

    def presign_get(self, s3_key, nom_original, inline=False):
        self.inline_calls.append((s3_key, nom_original, inline))
        return f"https://s3.test/{s3_key}?signed=1"


def _client(session, storage=None) -> TestClient:
    # PAS d'override de get_current_user : ces routes vivent sans lui.
    app = create_app()
    app.dependency_overrides[get_db] = lambda: session
    app.dependency_overrides[get_storage] = lambda: storage or _FakeStorage()
    return TestClient(app)


# Sélections vides du détail (matières, niveaux, blocs, ressources).
_DETAIL_VIDE = [[], [], [], []]


# --- Aucune route publique n'exige de Bearer ----------------------------------


def test_routes_publiques_repondent_sans_authorization():
    course = _course_row()
    session = _FakeSession([[course], *_DETAIL_VIDE])
    response = _client(session).get(f"/api/v1/public/courses/{course.id}")
    assert response.status_code == 200
    assert "WWW-Authenticate" not in response.headers
    assert session.commits == 0  # lecture seule, aucun upsert auth


# --- Autorisation par visibilité ------------------------------------------------


def test_cours_public_accessible_sans_token():
    course = _course_row(visibilite="public")
    session = _FakeSession([[course], *_DETAIL_VIDE])
    response = _client(session).get(f"/api/v1/public/courses/{course.id}")
    assert response.status_code == 200
    assert response.json()["titre"] == "Fractions"


def test_cours_prive_sans_token_introuvable():
    course = _course_row(visibilite="prive")
    session = _FakeSession([[course]])
    response = _client(session).get(f"/api/v1/public/courses/{course.id}")
    assert response.status_code == 404
    assert response.json()["detail"] == "Cours introuvable"


def test_cours_prive_token_valide_ok():
    course = _course_row(visibilite="prive")
    link = _link_row(course.id)
    # FIFO : cours, lien (scopé cours), puis détail.
    session = _FakeSession([[course], [link], *_DETAIL_VIDE])
    response = _client(session).get(
        f"/api/v1/public/courses/{course.id}", params={"token": _TOKEN}
    )
    assert response.status_code == 200


def test_cours_en_cours_introuvable_meme_avec_token():
    # Le lien est suspendu, pas supprimé : la visibilité prime, court-circuit
    # avant même le select du lien.
    course = _course_row(visibilite="en_cours")
    session = _FakeSession([[course]])
    response = _client(session).get(
        f"/api/v1/public/courses/{course.id}", params={"token": _TOKEN}
    )
    assert response.status_code == 404


def test_cours_inexistant_introuvable():
    session = _FakeSession([[]])
    response = _client(session).get(f"/api/v1/public/courses/{uuid.uuid4()}")
    assert response.status_code == 404


@pytest.mark.parametrize(
    "link_overrides",
    [
        dict(revoked=True),
        dict(expires_at=datetime.now(UTC) - timedelta(seconds=1)),
    ],
)
def test_cours_prive_lien_revoque_ou_expire_introuvable(link_overrides):
    course = _course_row(visibilite="prive")
    link = _link_row(course.id, **link_overrides)
    session = _FakeSession([[course], [link]])
    response = _client(session).get(
        f"/api/v1/public/courses/{course.id}", params={"token": _TOKEN}
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "Cours introuvable"


def test_token_du_cours_a_ne_donne_pas_le_cours_b():
    # Le select du lien est scopé (course_id + token) : il ne retourne rien.
    course_b = _course_row(visibilite="prive")
    session = _FakeSession([[course_b], []])
    response = _client(session).get(
        f"/api/v1/public/courses/{course_b.id}", params={"token": _TOKEN}
    )
    assert response.status_code == 404


# --- Entrée par lien : /public/shared/{token} -----------------------------------


def test_shared_lien_valide_renvoie_le_detail():
    course = _course_row(visibilite="prive")
    link = _link_row(course.id)
    bloc = _block_row(course.id)
    # FIFO : lien (par token), cours, puis détail (matières, niveaux,
    # blocs, ressources).
    session = _FakeSession([[link], [course], [], [], [bloc], []])
    response = _client(session).get(f"/api/v1/public/shared/{_TOKEN}")

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == str(course.id)
    assert body["block_count"] == 1
    assert body["blocks"][0]["content"] == {"markdown": "Bonjour"}


def test_shared_token_inconnu_introuvable():
    session = _FakeSession([[]])
    response = _client(session).get(f"/api/v1/public/shared/{_TOKEN}")
    assert response.status_code == 404
    assert response.json()["detail"] == "Cours introuvable"


def test_shared_cours_repasse_en_cours_introuvable():
    course = _course_row(visibilite="en_cours")
    link = _link_row(course.id)
    session = _FakeSession([[link], [course]])
    response = _client(session).get(f"/api/v1/public/shared/{_TOKEN}")
    assert response.status_code == 404


def test_shared_lien_valide_sur_cours_devenu_public_reste_valide():
    course = _course_row(visibilite="public")
    link = _link_row(course.id)
    session = _FakeSession([[link], [course], *_DETAIL_VIDE])
    response = _client(session).get(f"/api/v1/public/shared/{_TOKEN}")
    assert response.status_code == 200


# --- Filtrage du corrigé des exercices ------------------------------------------


def test_content_exercice_sans_reponse_attendue():
    course = _course_row()
    q1 = str(uuid.uuid4())
    q2 = str(uuid.uuid4())
    bloc = _block_row(
        course.id,
        type="exercice",
        content={
            "enonce": "Calculer",
            "questions": [
                {"id": q1, "enonce": "2+2 ?", "type": "texte_libre",
                 "reponse_attendue": "4"},
                {"id": q2, "enonce": "3+3 ?", "type": "texte_libre",
                 "reponse_attendue": "6"},
            ],
        },
    )
    session = _FakeSession([[course], [], [], [bloc], []])
    response = _client(session).get(f"/api/v1/public/courses/{course.id}")

    assert response.status_code == 200
    content = response.json()["blocks"][0]["content"]
    # Ids/énoncés/type préservés (les soumissions élèves référencent
    # (block_id, question_id)) ; le corrigé n'existe pas, par construction.
    assert content == {
        "enonce": "Calculer",
        "questions": [
            {"id": q1, "enonce": "2+2 ?", "type": "texte_libre"},
            {"id": q2, "enonce": "3+3 ?", "type": "texte_libre"},
        ],
    }
    assert "reponse_attendue" not in str(response.json())
    # Le JSONB d'origine n'a pas été muté (nouveau dict côté service).
    assert bloc.content["questions"][0]["reponse_attendue"] == "4"


def test_detail_complet_noms_denormalises_et_ressources_disponibles():
    course = _course_row(preview_settings={"font": "serif"})
    bloc = _block_row(course.id)
    ressource = _resource_row(course.id)
    session = _FakeSession(
        [[course], ["Mathématiques"], ["6e"], [bloc], [ressource]]
    )
    response = _client(session).get(f"/api/v1/public/courses/{course.id}")

    assert response.status_code == 200
    body = response.json()
    assert body["subjects"] == ["Mathématiques"]
    assert body["education_levels"] == ["6e"]
    assert body["preview_settings"] == {"font": "serif"}
    assert body["resources"] == [
        {
            "id": str(ressource.id),
            "type": "image",
            "nom_original": "photo.png",
            "taille": 1234,
            "mime": "image/png",
        }
    ]
    # Jamais de s3_key dans une réponse publique.
    assert "s3_key" not in str(body)


# --- Presign ressource -----------------------------------------------------------


def test_presign_ressource_publique():
    course = _course_row()
    ressource = _resource_row(course.id)
    storage = _FakeStorage()
    session = _FakeSession([[course], [ressource]])
    response = _client(session, storage).get(
        f"/api/v1/public/courses/{course.id}/resources/{ressource.id}/download",
        params={"disposition": "inline"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["download_url"] == "https://s3.test/abc/photo.png?signed=1"
    assert body["expires_in"] > 0
    assert storage.inline_calls == [("abc/photo.png", "photo.png", True)]


def test_presign_ressource_en_attente_conflit():
    # Miroir exact du régime prof : on est déjà autorisé sur le cours, 409.
    course = _course_row()
    ressource = _resource_row(course.id, statut="en_attente")
    session = _FakeSession([[course], [ressource]])
    response = _client(session).get(
        f"/api/v1/public/courses/{course.id}/resources/{ressource.id}/download"
    )
    assert response.status_code == 409


def test_presign_ressource_autre_cours_introuvable():
    course = _course_row()
    session = _FakeSession([[course], []])
    response = _client(session).get(
        f"/api/v1/public/courses/{course.id}/resources/{uuid.uuid4()}/download"
    )
    assert response.status_code == 404


# --- Module ----------------------------------------------------------------------


def test_module_public_code_servi():
    course = _course_row()
    module = _module_row(course.id)
    session = _FakeSession([[course], [module]])
    response = _client(session).get(
        f"/api/v1/public/courses/{course.id}/modules/{module.id}"
    )

    assert response.status_code == 200
    assert response.json() == {
        "id": str(module.id),
        "titre": "Quiz",
        "html": "<p>Q</p>",
        "css": "",
        "js": "console.log('ok')",
    }


def test_module_autre_cours_introuvable():
    course = _course_row()
    session = _FakeSession([[course], []])
    response = _client(session).get(
        f"/api/v1/public/courses/{course.id}/modules/{uuid.uuid4()}"
    )
    assert response.status_code == 404


# --- Catalogue public d'un prof ---------------------------------------------------


def test_catalogue_cours_publics_du_prof():
    user = _user_row()
    c1 = _course_row(owner_id=user.id)
    c2 = _course_row(owner_id=user.id, titre="Géométrie", description=None)
    # FIFO : user, cours publics, noms matières, noms niveaux, comptes blocs.
    session = _FakeSession(
        [
            [user],
            [c1, c2],
            [(c1.id, "Mathématiques")],
            [(c1.id, "6e"), (c2.id, "5e")],
            [(c1.id, 3)],
        ]
    )
    response = _client(session).get(f"/api/v1/public/professors/{user.id}/courses")

    assert response.status_code == 200
    body = response.json()
    assert body["nom_public"] == "M. Dupont"
    assert body["avatar_url"] is None  # prof sans photo
    assert [c["titre"] for c in body["courses"]] == ["Fractions", "Géométrie"]
    assert body["courses"][0]["subjects"] == ["Mathématiques"]
    assert body["courses"][0]["block_count"] == 3
    assert body["courses"][1]["subjects"] == []
    assert body["courses"][1]["education_levels"] == ["5e"]
    assert body["courses"][1]["block_count"] == 0


def test_catalogue_prof_avec_avatar():
    user = _user_row(
        avatar_s3_key="users/u/avatar/x/avatar.jpg",
        avatar_mime="image/jpeg",
        avatar_statut="disponible",
    )
    session = _FakeSession([[user], []])
    storage = _FakeStorage()
    response = _client(session, storage=storage).get(
        f"/api/v1/public/professors/{user.id}/courses"
    )
    assert response.status_code == 200
    body = response.json()
    assert body["avatar_url"] == "https://s3.test/users/u/avatar/x/avatar.jpg?signed=1"
    # Présignée inline : la photo s'affiche, elle ne se télécharge pas.
    assert storage.inline_calls == [
        ("users/u/avatar/x/avatar.jpg", "avatar.jpg", True)
    ]


def test_catalogue_user_inconnu_liste_vide_sans_oracle():
    # Utilisateur inexistant : même forme de réponse (200, liste vide).
    session = _FakeSession([[], []])
    response = _client(session).get(
        f"/api/v1/public/professors/{uuid.uuid4()}/courses"
    )
    assert response.status_code == 200
    assert response.json() == {"nom_public": None, "avatar_url": None, "courses": []}


def test_catalogue_prof_sans_nom_public_anonyme():
    user = _user_row(nom_public=None)
    session = _FakeSession([[user], []])
    response = _client(session).get(f"/api/v1/public/professors/{user.id}/courses")
    assert response.status_code == 200
    assert response.json() == {"nom_public": None, "avatar_url": None, "courses": []}


# --- Arbres de taxonomie publics (facettes de recherche, J3) -------------------


def _subject_tree_row(nom, profondeur=0, parent_id=None, position=0):
    return SimpleNamespace(
        id=uuid.uuid4(),
        parent_id=parent_id,
        nom=nom,
        code=nom.lower().replace(" ", "-"),
        profondeur=profondeur,
        position=position,
    )


def _level_tree_row(nom, profondeur=0, parent_id=None, position=0):
    return SimpleNamespace(
        id=uuid.uuid4(),
        parent_id=parent_id,
        nom=nom,
        code=f"fr.{nom.lower().replace(' ', '-')}",
        systeme="fr",
        cite=None,
        age_min=None,
        age_max=None,
        profondeur=profondeur,
        position=position,
    )


def test_arbre_matieres_public_sans_authorization():
    # Délégation pure vers le service subjects : même forme de réponse que
    # la route JWT, mais accessible anonymement (facettes de la recherche).
    racine = _subject_tree_row("Mathématiques")
    enfant = _subject_tree_row("Algèbre", profondeur=1, parent_id=racine.id)
    session = _FakeSession([[racine, enfant]])
    response = _client(session).get("/api/v1/public/subjects/tree")

    assert response.status_code == 200
    assert "WWW-Authenticate" not in response.headers
    [noeud] = response.json()
    assert noeud["nom"] == "Mathématiques"
    assert [c["nom"] for c in noeud["children"]] == ["Algèbre"]


def test_arbre_niveaux_public_sans_authorization():
    racine = _level_tree_row("Collège")
    enfant = _level_tree_row("6e", profondeur=1, parent_id=racine.id)
    session = _FakeSession([[racine, enfant]])
    response = _client(session).get("/api/v1/public/education-levels/tree")

    assert response.status_code == 200
    assert "WWW-Authenticate" not in response.headers
    [noeud] = response.json()
    assert noeud["nom"] == "Collège"
    assert noeud["systeme"] == "fr"
    assert [c["nom"] for c in noeud["children"]] == ["6e"]
