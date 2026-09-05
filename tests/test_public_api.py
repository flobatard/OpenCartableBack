"""Routes /public/* — régime d'accès élève (J2), sans JWT ni identité.

L'inverse du ``test_auth_required`` systématique des autres suites : ici le
client n'envoie JAMAIS d'en-tête Authorization et les routes doivent
répondre quand même. Sémantique d'erreur : 404 uniforme (token inconnu/
révoqué/expiré, cours ``draft`` ou privé sans token) — jamais 401/403/410.

Fausse session FIFO habituelle (ordre des ``execute`` documenté dans
app/public/service.py) ; l'expiration des liens est comparée en Python,
donc pilotable par les lignes fabriquées.
"""

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from tests.fakes import FakeSession, FakeStorage, make_client

_NOW = datetime(2026, 7, 7, 12, 0, tzinfo=UTC)
_NOW_JSON = "2026-07-07T12:00:00Z"
_TOKEN = "tok-" + "a" * 39


def _course_row(**overrides):
    defaults = dict(
        id=uuid.uuid4(),
        owner_id=uuid.uuid4(),
        title="Fractions",
        description="Les bases",
        preview_settings={},
        visibility="public",
        updated_at=_NOW,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _link_row(course_id, **overrides):
    defaults = dict(
        id=uuid.uuid4(),
        course_id=course_id,
        token=_TOKEN,
        label=None,
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
        type="text",
        title=None,
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
        original_name="photo.png",
        size=1234,
        mime="image/png",
        status="available",
        created_at=_NOW,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _module_row(course_id, **overrides):
    defaults = dict(
        id=uuid.uuid4(),
        course_id=course_id,
        title="Quiz",
        html="<p>Q</p>",
        css="",
        js="console.log('ok')",
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _user_row(**overrides):
    defaults = dict(
        id=uuid.uuid4(),
        public_name="M. Dupont",
        avatar_s3_key=None,
        avatar_mime=None,
        avatar_status=None,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


class _FakeStorage(FakeStorage):
    """Variante publique : trace ``(clé, nom, inline)`` et une URL propre."""

    def presign_get(self, s3_key, original_name, inline=False):
        self.inline_calls.append((s3_key, original_name, inline))
        return f"https://s3.test/{s3_key}?signed=1"


def _client(session, storage=None) -> TestClient:
    # PAS d'override de get_current_user : ces routes vivent sans lui.
    return make_client(session, storage or _FakeStorage(), authenticated=False)


# Sélections vides du détail (matières, niveaux, blocs, ressources, modules).
_EMPTY_DETAIL = [[], [], [], [], []]


# --- Aucune route publique n'exige de Bearer ----------------------------------


def test_public_routes_respond_without_authorization():
    course = _course_row()
    session = FakeSession([[course], *_EMPTY_DETAIL])
    response = _client(session).get(f"/api/v1/public/courses/{course.id}")
    assert response.status_code == 200
    assert "WWW-Authenticate" not in response.headers
    assert session.commits == 0  # lecture seule, aucun upsert auth


# --- Autorisation par visibilité ------------------------------------------------


def test_public_course_accessible_without_token():
    course = _course_row(visibility="public")
    session = FakeSession([[course], *_EMPTY_DETAIL])
    response = _client(session).get(f"/api/v1/public/courses/{course.id}")
    assert response.status_code == 200
    assert response.json()["title"] == "Fractions"


def test_private_course_without_token_not_found():
    course = _course_row(visibility="private")
    session = FakeSession([[course]])
    response = _client(session).get(f"/api/v1/public/courses/{course.id}")
    assert response.status_code == 404
    assert response.json()["detail"] == "Cours introuvable"


def test_private_course_valid_token_ok():
    course = _course_row(visibility="private")
    link = _link_row(course.id)
    # FIFO : cours, lien (scopé cours), puis détail.
    session = FakeSession([[course], [link], *_EMPTY_DETAIL])
    response = _client(session).get(
        f"/api/v1/public/courses/{course.id}", params={"token": _TOKEN}
    )
    assert response.status_code == 200


def test_draft_course_not_found_even_with_token():
    # Le lien est suspendu, pas supprimé : la visibilité prime, court-circuit
    # avant même le select du lien.
    course = _course_row(visibility="draft")
    session = FakeSession([[course]])
    response = _client(session).get(
        f"/api/v1/public/courses/{course.id}", params={"token": _TOKEN}
    )
    assert response.status_code == 404


def test_missing_course_not_found():
    session = FakeSession([[]])
    response = _client(session).get(f"/api/v1/public/courses/{uuid.uuid4()}")
    assert response.status_code == 404


@pytest.mark.parametrize(
    "link_overrides",
    [
        dict(revoked=True),
        dict(expires_at=datetime.now(UTC) - timedelta(seconds=1)),
    ],
)
def test_private_course_revoked_or_expired_link_not_found(link_overrides):
    course = _course_row(visibility="private")
    link = _link_row(course.id, **link_overrides)
    session = FakeSession([[course], [link]])
    response = _client(session).get(
        f"/api/v1/public/courses/{course.id}", params={"token": _TOKEN}
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "Cours introuvable"


def test_token_for_course_a_does_not_grant_course_b():
    # Le select du lien est scopé (course_id + token) : il ne retourne rien.
    course_b = _course_row(visibility="private")
    session = FakeSession([[course_b], []])
    response = _client(session).get(
        f"/api/v1/public/courses/{course_b.id}", params={"token": _TOKEN}
    )
    assert response.status_code == 404


# --- Entrée par lien : /public/shared/{token} -----------------------------------


def test_shared_valid_link_returns_detail():
    course = _course_row(visibility="private")
    link = _link_row(course.id)
    block = _block_row(course.id)
    # FIFO : lien (par token), cours, puis détail (matières, niveaux,
    # blocs, ressources, modules).
    session = FakeSession([[link], [course], [], [], [block], [], []])
    response = _client(session).get(f"/api/v1/public/shared/{_TOKEN}")

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == str(course.id)
    assert body["block_count"] == 1
    assert body["blocks"][0]["content"] == {"markdown": "Bonjour"}


def test_shared_unknown_token_not_found():
    session = FakeSession([[]])
    response = _client(session).get(f"/api/v1/public/shared/{_TOKEN}")
    assert response.status_code == 404
    assert response.json()["detail"] == "Cours introuvable"


def test_shared_course_back_to_draft_not_found():
    course = _course_row(visibility="draft")
    link = _link_row(course.id)
    session = FakeSession([[link], [course]])
    response = _client(session).get(f"/api/v1/public/shared/{_TOKEN}")
    assert response.status_code == 404


def test_shared_valid_link_on_course_turned_public_stays_valid():
    course = _course_row(visibility="public")
    link = _link_row(course.id)
    session = FakeSession([[link], [course], *_EMPTY_DETAIL])
    response = _client(session).get(f"/api/v1/public/shared/{_TOKEN}")
    assert response.status_code == 200


# --- Filtrage du corrigé des exercices ------------------------------------------


def test_exercise_content_without_expected_answer():
    course = _course_row()
    q1 = str(uuid.uuid4())
    q2 = str(uuid.uuid4())
    block = _block_row(
        course.id,
        type="exercise",
        content={
            "statement": "Calculer",
            "questions": [
                {"id": q1, "statement": "2+2 ?", "type": "free_text",
                 "expected_answer": "4"},
                {"id": q2, "statement": "3+3 ?", "type": "free_text",
                 "expected_answer": "6"},
            ],
        },
    )
    session = FakeSession([[course], [], [], [block], [], []])
    response = _client(session).get(f"/api/v1/public/courses/{course.id}")

    assert response.status_code == 200
    content = response.json()["blocks"][0]["content"]
    # Ids/énoncés/type préservés (les soumissions élèves référencent
    # (block_id, question_id)) ; le corrigé n'existe pas, par construction.
    assert content == {
        "statement": "Calculer",
        "questions": [
            {"id": q1, "statement": "2+2 ?", "type": "free_text"},
            {"id": q2, "statement": "3+3 ?", "type": "free_text"},
        ],
    }
    assert "expected_answer" not in str(response.json())
    # Le JSONB d'origine n'a pas été muté (nouveau dict côté service).
    assert block.content["questions"][0]["expected_answer"] == "4"


def test_full_detail_denormalized_names_and_available_resources():
    course = _course_row(preview_settings={"font": "serif"})
    block = _block_row(course.id)
    resource = _resource_row(course.id)
    session = FakeSession(
        [[course], ["Mathématiques"], ["6e"], [block], [resource], []]
    )
    response = _client(session).get(f"/api/v1/public/courses/{course.id}")

    assert response.status_code == 200
    body = response.json()
    assert body["subjects"] == ["Mathématiques"]
    assert body["education_levels"] == ["6e"]
    assert body["preview_settings"] == {"font": "serif"}
    assert body["resources"] == [
        {
            "id": str(resource.id),
            "type": "image",
            "original_name": "photo.png",
            "size": 1234,
            "mime": "image/png",
        }
    ]
    # Jamais de s3_key dans une réponse publique.
    assert "s3_key" not in str(body)


def test_full_detail_lists_modules_without_code():
    course = _course_row()
    block = _block_row(course.id)
    module = _module_row(course.id)
    session = FakeSession([[course], [], [], [block], [], [module]])
    response = _client(session).get(f"/api/v1/public/courses/{course.id}")

    assert response.status_code == 200
    body = response.json()
    # La bibliothèque de modules alimente l'onglet Modules de la vue élève…
    assert body["modules"] == [{"id": str(module.id), "title": "Quiz"}]
    # …mais le code n'y figure jamais : il passe par /modules/{id}.
    assert "console.log" not in str(body)


# --- Presign ressource -----------------------------------------------------------


def test_public_resource_presign():
    course = _course_row()
    resource = _resource_row(course.id)
    storage = _FakeStorage()
    session = FakeSession([[course], [resource]])
    response = _client(session, storage).get(
        f"/api/v1/public/courses/{course.id}/resources/{resource.id}/download",
        params={"disposition": "inline"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["download_url"] == "https://s3.test/abc/photo.png?signed=1"
    assert body["expires_in"] > 0
    assert storage.inline_calls == [("abc/photo.png", "photo.png", True)]


def test_pending_resource_presign_conflict():
    # Miroir exact du régime prof : on est déjà autorisé sur le cours, 409.
    course = _course_row()
    resource = _resource_row(course.id, status="pending")
    session = FakeSession([[course], [resource]])
    response = _client(session).get(
        f"/api/v1/public/courses/{course.id}/resources/{resource.id}/download"
    )
    assert response.status_code == 409


def test_resource_from_other_course_not_found():
    course = _course_row()
    session = FakeSession([[course], []])
    response = _client(session).get(
        f"/api/v1/public/courses/{course.id}/resources/{uuid.uuid4()}/download"
    )
    assert response.status_code == 404


# --- Module ----------------------------------------------------------------------


def test_public_module_code_served():
    course = _course_row()
    module = _module_row(course.id)
    session = FakeSession([[course], [module]])
    response = _client(session).get(
        f"/api/v1/public/courses/{course.id}/modules/{module.id}"
    )

    assert response.status_code == 200
    assert response.json() == {
        "id": str(module.id),
        "title": "Quiz",
        "html": "<p>Q</p>",
        "css": "",
        "js": "console.log('ok')",
    }


def test_module_from_other_course_not_found():
    course = _course_row()
    session = FakeSession([[course], []])
    response = _client(session).get(
        f"/api/v1/public/courses/{course.id}/modules/{uuid.uuid4()}"
    )
    assert response.status_code == 404


# --- Catalogue public d'un prof ---------------------------------------------------


def test_teacher_public_course_catalog():
    user = _user_row()
    c1 = _course_row(owner_id=user.id)
    c2 = _course_row(owner_id=user.id, title="Géométrie", description=None)
    # FIFO : user, cours publics, noms matières, noms niveaux, comptes blocs.
    session = FakeSession(
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
    assert body["public_name"] == "M. Dupont"
    assert body["avatar_url"] is None  # prof sans photo
    assert [c["title"] for c in body["courses"]] == ["Fractions", "Géométrie"]
    assert body["courses"][0]["subjects"] == ["Mathématiques"]
    assert body["courses"][0]["block_count"] == 3
    assert body["courses"][1]["subjects"] == []
    assert body["courses"][1]["education_levels"] == ["5e"]
    assert body["courses"][1]["block_count"] == 0


def test_catalog_teacher_with_avatar():
    user = _user_row(
        avatar_s3_key="users/u/avatar/x/avatar.jpg",
        avatar_mime="image/jpeg",
        avatar_status="available",
    )
    session = FakeSession([[user], []])
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


def test_catalog_unknown_user_empty_list_without_oracle():
    # Utilisateur inexistant : même forme de réponse (200, liste vide).
    session = FakeSession([[], []])
    response = _client(session).get(
        f"/api/v1/public/professors/{uuid.uuid4()}/courses"
    )
    assert response.status_code == 200
    assert response.json() == {"public_name": None, "avatar_url": None, "courses": []}


def test_catalog_teacher_without_public_name_anonymous():
    user = _user_row(public_name=None)
    session = FakeSession([[user], []])
    response = _client(session).get(f"/api/v1/public/professors/{user.id}/courses")
    assert response.status_code == 200
    assert response.json() == {"public_name": None, "avatar_url": None, "courses": []}


# --- Arbres de taxonomie publics (facettes de recherche, J3) -------------------


def _subject_tree_row(name, depth=0, parent_id=None, position=0):
    return SimpleNamespace(
        id=uuid.uuid4(),
        parent_id=parent_id,
        name=name,
        code=name.lower().replace(" ", "-"),
        depth=depth,
        position=position,
    )


def _level_tree_row(name, depth=0, parent_id=None, position=0):
    return SimpleNamespace(
        id=uuid.uuid4(),
        parent_id=parent_id,
        name=name,
        code=f"fr.{name.lower().replace(' ', '-')}",
        system="fr",
        cite=None,
        age_min=None,
        age_max=None,
        depth=depth,
        position=position,
    )


def test_public_subject_tree_without_authorization():
    # Délégation pure vers le service subjects : même forme de réponse que
    # la route JWT, mais accessible anonymement (facettes de la recherche).
    root = _subject_tree_row("Mathématiques")
    child = _subject_tree_row("Algèbre", depth=1, parent_id=root.id)
    session = FakeSession([[root, child]])
    response = _client(session).get("/api/v1/public/subjects/tree")

    assert response.status_code == 200
    assert "WWW-Authenticate" not in response.headers
    [node] = response.json()
    assert node["name"] == "Mathématiques"
    assert [c["name"] for c in node["children"]] == ["Algèbre"]


def test_public_level_tree_without_authorization():
    root = _level_tree_row("Collège")
    child = _level_tree_row("6e", depth=1, parent_id=root.id)
    session = FakeSession([[root, child]])
    response = _client(session).get("/api/v1/public/education-levels/tree")

    assert response.status_code == 200
    assert "WWW-Authenticate" not in response.headers
    [node] = response.json()
    assert node["name"] == "Collège"
    assert node["system"] == "fr"
    assert [c["name"] for c in node["children"]] == ["6e"]
