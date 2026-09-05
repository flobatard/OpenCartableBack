"""Routes /courses — aucun Postgres requis.

La fausse session sert les résultats des SELECT dans l'ordre des ``execute``
du service (FIFO, ordre documenté dans app/courses/service.py) ; les
INSERT/UPDATE/DELETE sont tracés dans ``executed`` sans consommer la file,
à une exception près : un INSERT porteur de RETURNING (celui de ``courses``)
consomme aussi la file pour servir les timestamps ``server_default``.
"""

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from tests.fakes import FakeSession, FakeStorage, deletes, inserts, make_client, updates

_NOW = datetime(2026, 7, 7, 12, 0, tzinfo=UTC)


def _user_row():
    return SimpleNamespace(id=uuid.uuid4(), sub="prof-123", email=None)


def _course_row(**overrides):
    defaults = dict(
        id=uuid.uuid4(),
        owner_id=None,
        title="Suites numériques",
        description=None,
        preview_settings={},
        visibility="draft",
        created_at=_NOW,
        updated_at=_NOW,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _block_row(**overrides):
    defaults = dict(
        id=uuid.uuid4(),
        position=0,
        type="text",
        title=None,
        description=None,
        content={"markdown": ""},
        resource_id=None,
        module_id=None,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


_COURSE_ID = uuid.uuid4()
_BLOCK_ID = uuid.uuid4()
# Réglages de preview « historiques » (facteurs neutres), clés camelCase du
# contrat CourseStyleSettings du front.
_PREVIEW = {
    "fontSizePx": 16,
    "headingScale": 1.0,
    "lineHeight": 1.7,
    "widthCh": 70,
    "paragraphGapEm": 1.5,
    "font": "sans",
}


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", "/api/v1/courses", None),
        ("POST", "/api/v1/courses", {"title": "x"}),
        ("GET", f"/api/v1/courses/{_COURSE_ID}", None),
        ("PATCH", f"/api/v1/courses/{_COURSE_ID}", {"title": "x"}),
        ("PUT", f"/api/v1/courses/{_COURSE_ID}/preview", _PREVIEW),
        ("POST", f"/api/v1/courses/{_COURSE_ID}/blocks", {"type": "text"}),
        ("PUT", f"/api/v1/courses/{_COURSE_ID}/blocks/order", {"block_ids": []}),
        (
            "PATCH",
            f"/api/v1/courses/{_COURSE_ID}/blocks/{_BLOCK_ID}",
            {"content": {"markdown": "x"}},
        ),
        ("DELETE", f"/api/v1/courses/{_COURSE_ID}/blocks/{_BLOCK_ID}", None),
        ("DELETE", f"/api/v1/courses/{_COURSE_ID}", None),
    ],
)
def test_routes_require_auth(client: TestClient, method, path, body):
    response = client.request(method, path, json=body)
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


def test_empty_list_short_circuits():
    user = _user_row()
    session = FakeSession([[user], []])
    response = make_client(session).get("/api/v1/courses")

    assert response.status_code == 200
    assert response.json() == []
    # 3 executes : upsert users, select user, select cours — pas de M2M/blocs.
    assert len(session.executed) == 3


def test_list_dispatches_classification_and_counts():
    user = _user_row()
    c1, c2 = _course_row(), _course_row(description="Avec description")
    s1, s2, l1 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    session = FakeSession(
        [
            [user],
            [c1, c2],
            [(c1.id, s1), (c2.id, s2)],
            [(c1.id, l1)],
            [(c1.id, 3)],
        ]
    )
    response = make_client(session).get("/api/v1/courses")

    assert response.status_code == 200
    body = response.json()
    assert [c["id"] for c in body] == [str(c1.id), str(c2.id)]  # ordre servi respecté
    assert body[0]["subject_ids"] == [str(s1)]
    assert body[1]["subject_ids"] == [str(s2)]
    assert body[0]["education_level_ids"] == [str(l1)]
    assert body[1]["education_level_ids"] == []
    assert body[0]["block_count"] == 3
    assert body[1]["block_count"] == 0  # absent du GROUP BY → 0


def test_create_happy_path():
    user = _user_row()
    s1, s2, l1 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    session = FakeSession([[user], [s1, s2], [l1], [(_NOW, _NOW)]])
    payload = {
        "title": "  Suites numériques  ",
        "description": "Premier chapitre",
        "subject_ids": [str(s1), str(s2)],
        "education_level_ids": [str(l1)],
    }
    response = make_client(session).post("/api/v1/courses", json=payload)

    assert response.status_code == 201
    body = response.json()
    assert body["id"]
    assert body["title"] == "Suites numériques"  # trimé par le schéma
    assert body["description"] == "Premier chapitre"
    assert body["subject_ids"] == [str(s1), str(s2)]
    assert body["education_level_ids"] == [str(l1)]
    assert body["block_count"] == 0
    assert body["created_at"] and body["updated_at"]

    [(stmt_course, _)] = inserts(session, "courses")
    assert stmt_course._returning  # timestamps server_default relus en RETURNING
    [(_, subject_params)] = inserts(session, "course_subjects")
    assert [p["subject_id"] for p in subject_params] == [s1, s2]
    [(_, level_params)] = inserts(session, "course_education_levels")
    assert [p["education_level_id"] for p in level_params] == [l1]
    assert session.commits >= 1


def test_create_without_classification():
    user = _user_row()
    session = FakeSession([[user], [], [], [(_NOW, _NOW)]])
    response = make_client(session).post("/api/v1/courses", json={"title": "Sans classement"})

    assert response.status_code == 201
    body = response.json()
    assert body["subject_ids"] == []
    assert body["education_level_ids"] == []
    # Aucun executemany sur liste vide (erreur SQLAlchemy sinon).
    assert inserts(session, "course_subjects") == []
    assert inserts(session, "course_education_levels") == []


def test_create_unknown_subject():
    user = _user_row()
    session = FakeSession([[user], []])  # lookup matières vide
    payload = {"title": "x", "subject_ids": [str(uuid.uuid4())]}
    response = make_client(session).post("/api/v1/courses", json=payload)

    assert response.status_code == 422
    assert "Matières inconnues" in response.json()["detail"]
    assert inserts(session, "courses") == []


def test_create_unknown_education_level():
    user = _user_row()
    s1 = uuid.uuid4()
    session = FakeSession([[user], [s1], []])  # lookup niveaux vide
    payload = {
        "title": "x",
        "subject_ids": [str(s1)],
        "education_level_ids": [str(uuid.uuid4())],
    }
    response = make_client(session).post("/api/v1/courses", json=payload)

    assert response.status_code == 422
    assert "Niveaux d'étude inconnus" in response.json()["detail"]
    assert inserts(session, "courses") == []


@pytest.mark.parametrize(
    "payload",
    [
        {},  # title manquant
        {"title": ""},
        {"title": "   "},  # blanc : rejeté après trim
        {"title": "x" * 301},
        {"title": "ok", "description": "d" * 2001},
    ],
)
def test_create_invalid_payload_without_db_access(payload):
    session = FakeSession()
    response = make_client(session).post("/api/v1/courses", json=payload)
    assert response.status_code == 422
    assert session.executed == []


def test_create_deduplicates_ids():
    user = _user_row()
    s1 = uuid.uuid4()
    session = FakeSession([[user], [s1], [], [(_NOW, _NOW)]])
    payload = {"title": "x", "subject_ids": [str(s1), str(s1)]}
    response = make_client(session).post("/api/v1/courses", json=payload)

    assert response.status_code == 201
    assert response.json()["subject_ids"] == [str(s1)]
    [(_, params)] = inserts(session, "course_subjects")
    assert len(params) == 1


def test_detail_course_not_owned():
    user = _user_row()
    session = FakeSession([[user], []])  # select cours scopé owner → vide
    response = make_client(session).get(f"/api/v1/courses/{uuid.uuid4()}")

    assert response.status_code == 404
    assert response.json()["detail"] == "Cours introuvable"


def test_detail_with_ordered_blocks():
    user = _user_row()
    course = _course_row()
    s1, l1 = uuid.uuid4(), uuid.uuid4()
    b1 = _block_row()
    b2 = _block_row(
        position=1,
        type="document",
        content={"caption": "Schéma", "display": "inline"},
        resource_id=uuid.uuid4(),
    )
    session = FakeSession([[user], [course], [s1], [l1], [b1, b2]])
    response = make_client(session).get(f"/api/v1/courses/{course.id}")

    assert response.status_code == 200
    body = response.json()
    assert body["title"] == course.title
    assert body["subject_ids"] == [str(s1)]
    assert body["education_level_ids"] == [str(l1)]
    assert body["block_count"] == 2
    assert [b["id"] for b in body["blocks"]] == [str(b1.id), str(b2.id)]  # ordre servi
    assert body["blocks"][0] == {
        "id": str(b1.id),
        "position": 0,
        "type": "text",
        "title": None,
        "description": None,
        "content": {"markdown": ""},
        "resource_id": None,
        "module_id": None,
    }


@pytest.mark.parametrize(
    ("block_type", "content"),
    [
        ("text", {"markdown": ""}),
        ("exercise", {"statement": "", "questions": []}),
        ("document", {"caption": None, "display": "inline"}),
        ("module", {}),
    ],
)
def test_add_block_default_content(block_type, content):
    user = _user_row()
    course = _course_row()
    session = FakeSession([[user], [course], [3]])  # position suivante servie : 3
    response = make_client(session).post(
        f"/api/v1/courses/{course.id}/blocks", json={"type": block_type}
    )

    assert response.status_code == 201
    body = response.json()
    assert body["id"]
    assert body["type"] == block_type
    assert body["title"] is None
    assert body["description"] is None
    assert body["content"] == content
    assert body["position"] == 3
    assert body["resource_id"] is None
    assert body["module_id"] is None
    assert len(inserts(session, "blocks")) == 1
    assert course.updated_at != _NOW  # le cours remonte dans la liste
    assert session.commits >= 1


def test_add_block_with_title_and_description():
    user = _user_row()
    course = _course_row()
    session = FakeSession([[user], [course], [0]])
    payload = {"type": "text", "title": "Introduction", "description": "Bref rappel"}
    response = make_client(session).post(f"/api/v1/courses/{course.id}/blocks", json=payload)

    assert response.status_code == 201
    body = response.json()
    assert body["title"] == "Introduction"
    assert body["description"] == "Bref rappel"
    [(stmt, _)] = inserts(session, "blocks")
    values = stmt.compile().params
    assert values["title"] == "Introduction"
    assert values["description"] == "Bref rappel"


def test_add_first_block_position_zero():
    user = _user_row()
    course = _course_row()
    session = FakeSession([[user], [course], [0]])  # coalesce(max+1, 0) sur cours vide
    response = make_client(session).post(
        f"/api/v1/courses/{course.id}/blocks", json={"type": "text"}
    )

    assert response.status_code == 201
    assert response.json()["position"] == 0


@pytest.mark.parametrize("block_type", ["ressource", "lien", "inconnu"])
def test_add_block_rejected_type_without_db_access(block_type):
    # « ressource » et « lien » sont des types supprimés (les ressources sont
    # une bibliothèque indépendante, les liens vivent dans le markdown) : le
    # schéma BlockCreate ne les accepte pas.
    session = FakeSession()
    response = make_client(session).post(
        f"/api/v1/courses/{uuid.uuid4()}/blocks", json={"type": block_type}
    )
    assert response.status_code == 422
    assert session.executed == []


def test_edit_text_block_content():
    user = _user_row()
    course = _course_row()
    block = _block_row()
    session = FakeSession([[user], [course], [block]])
    payload = {"content": {"markdown": "## Suites\nDéfinition d'une suite."}}
    response = make_client(session).patch(
        f"/api/v1/courses/{course.id}/blocks/{block.id}", json=payload
    )

    assert response.status_code == 200
    assert response.json() == {
        "id": str(block.id),
        "position": 0,
        "type": "text",
        "title": None,
        "description": None,
        "content": {"markdown": "## Suites\nDéfinition d'une suite."},
        "resource_id": None,
        "module_id": None,
    }
    # Écriture via l'unité de travail ORM (mutation d'attribut), pas d'Update Core.
    assert block.content == {"markdown": "## Suites\nDéfinition d'une suite."}
    assert updates(session) == []
    assert course.updated_at != _NOW
    assert session.commits >= 1


def test_edit_title_and_description_on_non_text_block():
    # Métadonnées éditables sur tous les types, indépendamment du contenu.
    user = _user_row()
    course = _course_row()
    block = _block_row(type="module", content={})
    session = FakeSession([[user], [course], [block]])
    payload = {"title": "Vidéo complémentaire", "description": "Pour aller plus loin"}
    response = make_client(session).patch(
        f"/api/v1/courses/{course.id}/blocks/{block.id}", json=payload
    )

    assert response.status_code == 200
    body = response.json()
    assert body["title"] == "Vidéo complémentaire"
    assert body["description"] == "Pour aller plus loin"
    assert block.title == "Vidéo complémentaire"
    assert block.description == "Pour aller plus loin"
    assert course.updated_at != _NOW
    assert session.commits >= 1


def test_edit_clears_title_and_description_with_null():
    user = _user_row()
    course = _course_row()
    block = _block_row(title="Ancien titre", description="Ancienne description")
    session = FakeSession([[user], [course], [block]])
    response = make_client(session).patch(
        f"/api/v1/courses/{course.id}/blocks/{block.id}",
        json={"title": None, "description": None},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["title"] is None
    assert body["description"] is None


def test_edit_empty_payload_rejected():
    session = FakeSession()
    response = make_client(session).patch(
        f"/api/v1/courses/{uuid.uuid4()}/blocks/{uuid.uuid4()}", json={}
    )

    assert response.status_code == 422
    assert session.executed == []


def test_edit_course_not_owned():
    user = _user_row()
    session = FakeSession([[user], []])
    response = make_client(session).patch(
        f"/api/v1/courses/{uuid.uuid4()}/blocks/{uuid.uuid4()}",
        json={"content": {"markdown": "x"}},
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Cours introuvable"


def test_edit_content_block_not_found():
    user = _user_row()
    course = _course_row()
    session = FakeSession([[user], [course], []])
    response = make_client(session).patch(
        f"/api/v1/courses/{course.id}/blocks/{uuid.uuid4()}",
        json={"content": {"markdown": "x"}},
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Bloc introuvable"


def test_edit_text_content_on_module_block_rejected():
    # « module » n'a aucune forme de content éditable avant le J4 : toute
    # forme fournie est d'un autre type → 422.
    user = _user_row()
    course = _course_row()
    initial_content = {}
    block = _block_row(type="module", content=initial_content)
    session = FakeSession([[user], [course], [block]])
    response = make_client(session).patch(
        f"/api/v1/courses/{course.id}/blocks/{block.id}",
        json={"content": {"markdown": "x"}},
    )

    assert response.status_code == 422
    assert "correspond à un bloc" in response.json()["detail"]
    assert block.content == initial_content
    assert course.updated_at == _NOW
    # Seul commit : celui de get_or_create_by_sub (upsert auth) — pas d'écriture cours.
    assert session.commits == 1


def test_edit_exercise_content_on_text_block_rejected():
    # Garde-fou symétrique : une forme exercice sur un bloc texte est refusée.
    user = _user_row()
    course = _course_row()
    block = _block_row()
    session = FakeSession([[user], [course], [block]])
    response = make_client(session).patch(
        f"/api/v1/courses/{course.id}/blocks/{block.id}",
        json={"content": {"statement": "", "questions": []}},
    )

    assert response.status_code == 422
    assert "correspond à un bloc" in response.json()["detail"]
    assert block.content == {"markdown": ""}
    assert course.updated_at == _NOW
    assert session.commits == 1


_QUESTION_ID = str(uuid.uuid4())


@pytest.mark.parametrize(
    "payload",
    [
        {},  # aucun champ fourni
        {"content": {"markdown": None}},
        {"content": {"markdown": "x" * 100_001}},  # trop long
        {"content": {"markdown": "x", "html": "<b>"}},  # clé en trop (extra=forbid)
        {"content": {"statement": "x"}},  # questions manquantes (requis sans défaut)
        {"content": {"statement": "x" * 100_001, "questions": []}},  # sujet trop long
        {"content": {"statement": "", "questions": [], "extra": 1}},  # clé en trop
        {"content": {"statement": "", "questions": [{"expected_answer": "r"}]}},  # sans énoncé
        {"content": {"statement": "", "questions": [{"statement": "q", "note": 1}]}},  # clé en trop
        {"content": {"statement": "", "questions": [{"statement": "q", "type": "qcm"}]}},
        {"content": {"statement": "", "questions": [{"statement": "q"}] * 51}},  # > 50 questions
        {
            "content": {
                "statement": "",
                "questions": [{"statement": "q", "expected_answer": "r" * 20_001}],
            }
        },
        {
            "content": {
                "statement": "",
                "questions": [
                    {"id": _QUESTION_ID, "statement": "a"},
                    {"id": _QUESTION_ID, "statement": "b"},
                ],
            }
        },  # ids dupliqués
        {"content": {"caption": "x" * 501}},  # légende trop longue
        {"content": {"display": "popup"}},  # display hors littéraux
        {"content": {"caption": None, "resource_id": "x"}},  # clé en trop (extra=forbid)
        {"resource_id": "pas-un-uuid"},
    ],
)
def test_edit_content_invalid_payload_without_db_access(payload):
    session = FakeSession()
    response = make_client(session).patch(
        f"/api/v1/courses/{uuid.uuid4()}/blocks/{uuid.uuid4()}", json=payload
    )
    assert response.status_code == 422
    assert session.executed == []


def _exercise_row(**overrides):
    overrides.setdefault("type", "exercise")
    overrides.setdefault("content", {"statement": "", "questions": []})
    return _block_row(**overrides)


def test_edit_empty_exercise_block_content():
    # Prouve la non-ambiguïté de l'union : le payload exercice minimal ne
    # matche pas TextContent et atteint bien la branche exercice.
    user = _user_row()
    course = _course_row()
    block = _exercise_row()
    session = FakeSession([[user], [course], [block]])
    response = make_client(session).patch(
        f"/api/v1/courses/{course.id}/blocks/{block.id}",
        json={"content": {"statement": "", "questions": []}},
    )

    assert response.status_code == 200
    assert response.json()["content"] == {"statement": "", "questions": []}
    assert block.content == {"statement": "", "questions": []}
    assert course.updated_at != _NOW
    assert session.commits >= 1


def test_edit_exercise_block_new_questions_receive_an_id():
    user = _user_row()
    course = _course_row()
    block = _exercise_row()
    session = FakeSession([[user], [course], [block]])
    payload = {
        "content": {
            "statement": "## Sujet\nSoit $u_n$ une suite.",
            "questions": [
                {"statement": "Montrer que $u_n$ converge.", "expected_answer": "Par encadrement."},
                {"statement": "Donner sa limite."},
            ],
        }
    }
    response = make_client(session).patch(
        f"/api/v1/courses/{course.id}/blocks/{block.id}", json=payload
    )

    assert response.status_code == 200
    questions = block.content["questions"]
    assert len(questions) == 2
    for question in questions:
        # Le fake ne passe pas par asyncpg : cet isinstance est la seule
        # garde contre un uuid.UUID non JSON-sérialisable dans le JSONB.
        assert isinstance(question["id"], str)
        assert uuid.UUID(question["id"]).version == 4
        assert question["type"] == "free_text"
    assert questions[0]["id"] != questions[1]["id"]
    assert questions[0]["expected_answer"] == "Par encadrement."
    assert questions[1]["expected_answer"] == ""  # défaut si absente du payload
    assert block.content["statement"] == "## Sujet\nSoit $u_n$ une suite."
    assert response.json()["content"] == block.content
    assert course.updated_at != _NOW


def test_edit_exercise_block_preserves_provided_ids():
    user = _user_row()
    course = _course_row()
    existing_id = str(uuid.uuid4())
    block = _exercise_row(
        content={
            "statement": "Sujet",
            "questions": [
                {"id": existing_id, "statement": "Q1", "type": "free_text", "expected_answer": "R1"}
            ],
        }
    )
    session = FakeSession([[user], [course], [block]])
    payload = {
        "content": {
            "statement": "Sujet",
            "questions": [
                {"id": existing_id, "statement": "Q1 modifiée", "expected_answer": "R1"},
                {"statement": "Q2 nouvelle"},
            ],
        }
    }
    response = make_client(session).patch(
        f"/api/v1/courses/{course.id}/blocks/{block.id}", json=payload
    )

    assert response.status_code == 200
    questions = block.content["questions"]
    assert questions[0]["id"] == existing_id  # jamais régénéré (stable à vie)
    assert questions[0]["statement"] == "Q1 modifiée"
    assert questions[1]["id"] != existing_id
    assert uuid.UUID(questions[1]["id"]).version == 4


def test_edit_exercise_block_deletes_absent_questions():
    # Sémantique remplacement : une question absente du payload est supprimée.
    user = _user_row()
    course = _course_row()
    kept_id = str(uuid.uuid4())
    deleted_id = str(uuid.uuid4())
    block = _exercise_row(
        content={
            "statement": "Sujet",
            "questions": [
                {"id": kept_id, "statement": "Q1", "type": "free_text", "expected_answer": ""},
                {"id": deleted_id, "statement": "Q2", "type": "free_text", "expected_answer": ""},
            ],
        }
    )
    session = FakeSession([[user], [course], [block]])
    payload = {
        "content": {
            "statement": "Sujet",
            "questions": [{"id": kept_id, "statement": "Q1"}],
        }
    }
    response = make_client(session).patch(
        f"/api/v1/courses/{course.id}/blocks/{block.id}", json=payload
    )

    assert response.status_code == 200
    questions = block.content["questions"]
    assert [q["id"] for q in questions] == [kept_id]


def test_edit_exercise_block_unknown_id_rejected():
    # Un id jamais vu dans ce bloc = client bugué (ou ids d'un autre bloc) :
    # 422 strict, avant toute écriture.
    user = _user_row()
    course = _course_row()
    initial_content = {"statement": "", "questions": []}
    block = _exercise_row(content=dict(initial_content))
    session = FakeSession([[user], [course], [block]])
    payload = {
        "content": {
            "statement": "x",
            "questions": [{"id": str(uuid.uuid4()), "statement": "Q forgée"}],
        }
    }
    response = make_client(session).patch(
        f"/api/v1/courses/{course.id}/blocks/{block.id}", json=payload
    )

    assert response.status_code == 422
    assert "Questions inconnues" in response.json()["detail"]
    assert block.content == initial_content
    assert course.updated_at == _NOW
    assert session.commits == 1  # upsert auth seulement


def _document_row(**overrides):
    overrides.setdefault("type", "document")
    overrides.setdefault("content", {"caption": None, "display": "inline"})
    return _block_row(**overrides)


def _resource_row(**overrides):
    defaults = dict(
        id=uuid.uuid4(),
        course_id=None,
        type="document",
        s3_key="uuid/schema.pdf",
        original_name="schema.pdf",
        size=2048,
        mime="application/pdf",
        status="available",
        created_at=_NOW,
        updated_at=_NOW,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def test_edit_document_block_attaches_resource():
    user = _user_row()
    course = _course_row()
    block = _document_row()
    resource = _resource_row(course_id=course.id)
    # FIFO : cours, bloc, puis ressource (select déclenché par resource_id non nul).
    session = FakeSession([[user], [course], [block], [resource]])
    response = make_client(session).patch(
        f"/api/v1/courses/{course.id}/blocks/{block.id}",
        json={"resource_id": str(resource.id)},
    )

    assert response.status_code == 200
    assert response.json()["resource_id"] == str(resource.id)
    assert block.resource_id == resource.id
    assert block.content == {"caption": None, "display": "inline"}  # intact
    assert course.updated_at != _NOW
    assert session.commits >= 1


def test_edit_document_block_detaches_with_null():
    # resource_id: null explicite = détacher — pas de select ressource (FIFO
    # plus courte).
    user = _user_row()
    course = _course_row()
    block = _document_row(resource_id=uuid.uuid4())
    session = FakeSession([[user], [course], [block]])
    response = make_client(session).patch(
        f"/api/v1/courses/{course.id}/blocks/{block.id}", json={"resource_id": None}
    )

    assert response.status_code == 200
    assert response.json()["resource_id"] is None
    assert block.resource_id is None
    assert course.updated_at != _NOW


def test_edit_document_block_unknown_or_foreign_resource():
    # Le select scopé course_id ne retourne rien : 422 (le bloc, lui, existe).
    user = _user_row()
    course = _course_row()
    block = _document_row()
    session = FakeSession([[user], [course], [block], []])
    response = make_client(session).patch(
        f"/api/v1/courses/{course.id}/blocks/{block.id}",
        json={"resource_id": str(uuid.uuid4())},
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "Ressource inconnue"
    assert block.resource_id is None
    assert course.updated_at == _NOW
    assert session.commits == 1  # upsert auth seulement


def test_edit_document_block_pending_resource_rejected():
    user = _user_row()
    course = _course_row()
    block = _document_row()
    resource = _resource_row(course_id=course.id, status="pending")
    session = FakeSession([[user], [course], [block], [resource]])
    response = make_client(session).patch(
        f"/api/v1/courses/{course.id}/blocks/{block.id}",
        json={"resource_id": str(resource.id)},
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "Ressource non disponible"
    assert block.resource_id is None
    assert course.updated_at == _NOW


def test_edit_resource_id_on_non_document_block_rejected():
    # 422 levée avant tout select ressource (FIFO sans résultat supplémentaire).
    user = _user_row()
    course = _course_row()
    block = _block_row()  # type text
    session = FakeSession([[user], [course], [block]])
    response = make_client(session).patch(
        f"/api/v1/courses/{course.id}/blocks/{block.id}",
        json={"resource_id": str(uuid.uuid4())},
    )

    assert response.status_code == 422
    assert "blocs « document »" in response.json()["detail"]
    assert block.resource_id is None
    assert course.updated_at == _NOW
    assert session.commits == 1


def _module_block_row(**overrides):
    overrides.setdefault("type", "module")
    overrides.setdefault("content", {})
    return _block_row(**overrides)


def _module_row(**overrides):
    defaults = dict(
        id=uuid.uuid4(),
        course_id=None,
        title="Quiz interactif",
        html="<p>Salut</p>",
        css="p { color: red; }",
        js="console.log('ok')",
        created_at=_NOW,
        updated_at=_NOW,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def test_edit_module_block_attaches_module():
    user = _user_row()
    course = _course_row()
    block = _module_block_row()
    module = _module_row(course_id=course.id)
    # FIFO : cours, bloc, puis module (select déclenché par module_id non nul).
    session = FakeSession([[user], [course], [block], [module]])
    response = make_client(session).patch(
        f"/api/v1/courses/{course.id}/blocks/{block.id}",
        json={"module_id": str(module.id)},
    )

    assert response.status_code == 200
    assert response.json()["module_id"] == str(module.id)
    assert block.module_id == module.id
    assert block.content == {}  # intact
    assert course.updated_at != _NOW
    assert session.commits >= 1


def test_edit_module_block_detaches_with_null():
    # module_id: null explicite = détacher — pas de select module (FIFO
    # plus courte).
    user = _user_row()
    course = _course_row()
    block = _module_block_row(module_id=uuid.uuid4())
    session = FakeSession([[user], [course], [block]])
    response = make_client(session).patch(
        f"/api/v1/courses/{course.id}/blocks/{block.id}", json={"module_id": None}
    )

    assert response.status_code == 200
    assert response.json()["module_id"] is None
    assert block.module_id is None
    assert course.updated_at != _NOW


def test_edit_module_block_unknown_or_foreign_module():
    # Le select scopé course_id ne retourne rien : 422 (le bloc, lui, existe).
    user = _user_row()
    course = _course_row()
    block = _module_block_row()
    session = FakeSession([[user], [course], [block], []])
    response = make_client(session).patch(
        f"/api/v1/courses/{course.id}/blocks/{block.id}",
        json={"module_id": str(uuid.uuid4())},
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "Module inconnu"
    assert block.module_id is None
    assert course.updated_at == _NOW
    assert session.commits == 1  # upsert auth seulement


def test_edit_module_id_on_non_module_block_rejected():
    # 422 levée avant tout select module (FIFO sans résultat supplémentaire).
    user = _user_row()
    course = _course_row()
    block = _block_row()  # type text
    session = FakeSession([[user], [course], [block]])
    response = make_client(session).patch(
        f"/api/v1/courses/{course.id}/blocks/{block.id}",
        json={"module_id": str(uuid.uuid4())},
    )

    assert response.status_code == 422
    assert "blocs « module »" in response.json()["detail"]
    assert block.module_id is None
    assert course.updated_at == _NOW
    assert session.commits == 1


def test_edit_document_block_content():
    user = _user_row()
    course = _course_row()
    block = _document_row()
    session = FakeSession([[user], [course], [block]])
    response = make_client(session).patch(
        f"/api/v1/courses/{course.id}/blocks/{block.id}",
        json={"content": {"caption": "Figure 1", "display": "download"}},
    )

    assert response.status_code == 200
    assert response.json()["content"] == {
        "caption": "Figure 1",
        "display": "download",
    }
    assert block.content == {"caption": "Figure 1", "display": "download"}
    assert course.updated_at != _NOW


def test_edit_document_content_on_text_block_rejected():
    # Le content vide {} valide en DocumentContent : c'est le garde-fou
    # forme↔type qui le rejette sur un bloc d'un autre type.
    user = _user_row()
    course = _course_row()
    block = _block_row()  # type text
    session = FakeSession([[user], [course], [block]])
    response = make_client(session).patch(
        f"/api/v1/courses/{course.id}/blocks/{block.id}", json={"content": {}}
    )

    assert response.status_code == 422
    assert "correspond à un bloc « document »" in response.json()["detail"]
    assert block.content == {"markdown": ""}
    assert course.updated_at == _NOW


def test_delete_block():
    user = _user_row()
    course = _course_row()
    block = _block_row()  # type text : delete direct du bloc
    session = FakeSession([[user], [course], [block]])
    response = make_client(session).delete(f"/api/v1/courses/{course.id}/blocks/{block.id}")

    assert response.status_code == 204
    [(stmt, _)] = deletes(session)
    assert stmt.table.name == "blocks"
    assert course.updated_at != _NOW


def test_delete_document_block_touches_neither_resources_nor_s3():
    # Supprimer un bloc document laisse la ressource pointée dans la
    # bibliothèque du cours (et son objet S3 dans le bucket).
    user = _user_row()
    course = _course_row()
    block = _block_row(
        type="document",
        content={"caption": None, "display": "inline"},
        resource_id=uuid.uuid4(),
    )
    session = FakeSession([[user], [course], [block]])
    storage = FakeStorage()
    response = make_client(session, storage).delete(
        f"/api/v1/courses/{course.id}/blocks/{block.id}"
    )

    assert response.status_code == 204
    [(stmt, _)] = deletes(session)
    assert stmt.table.name == "blocks"  # jamais de delete resources ici
    assert storage.deleted == []
    assert course.updated_at != _NOW


def test_delete_missing_block():
    user = _user_row()
    course = _course_row()
    session = FakeSession([[user], [course], []])  # bloc absent du cours
    response = make_client(session).delete(f"/api/v1/courses/{course.id}/blocks/{uuid.uuid4()}")

    assert response.status_code == 404
    assert response.json()["detail"] == "Bloc introuvable"
    assert deletes(session) == []


def test_delete_block_course_not_owned():
    user = _user_row()
    session = FakeSession([[user], []])
    response = make_client(session).delete(f"/api/v1/courses/{uuid.uuid4()}/blocks/{uuid.uuid4()}")

    assert response.status_code == 404
    assert deletes(session) == []


def test_delete_course():
    user = _user_row()
    course = _course_row()
    # 3e résultat FIFO : les clés S3 des ressources du cours (à purger du bucket).
    session = FakeSession([[user], [course], ["abc/doc.pdf", "def/img.png"]])
    storage = FakeStorage()
    response = make_client(session, storage).delete(f"/api/v1/courses/{course.id}")

    assert response.status_code == 204
    [(stmt, _)] = deletes(session)
    assert stmt.table.name == "courses"  # cascade FK : blocs/ressources/classement
    assert session.commits >= 1
    # Les objets S3 sont supprimés après le commit (hors cascade DB).
    assert storage.deleted == ["abc/doc.pdf", "def/img.png"]


def test_delete_course_not_owned_404():
    user = _user_row()
    session = FakeSession([[user], []])  # select cours scopé owner → vide
    response = make_client(session).delete(f"/api/v1/courses/{uuid.uuid4()}")

    assert response.status_code == 404
    assert response.json()["detail"] == "Cours introuvable"
    assert deletes(session) == []


def test_reorder_rewrites_positions():
    user = _user_row()
    course = _course_row()
    b1, b2, b3 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    session = FakeSession([[user], [course], [b1, b2, b3]])
    payload = {"block_ids": [str(b3), str(b1), str(b2)]}
    response = make_client(session).put(f"/api/v1/courses/{course.id}/blocks/order", json=payload)

    assert response.status_code == 204
    [(stmt, params)] = updates(session)  # un seul executemany
    assert stmt.table.name == "blocks"
    assert params == [
        {"b_id": b3, "b_position": 0},
        {"b_id": b1, "b_position": 1},
        {"b_id": b2, "b_position": 2},
    ]
    assert course.updated_at != _NOW


def test_reorder_incomplete_or_foreign_list():
    user = _user_row()
    course = _course_row()
    b1, b2 = uuid.uuid4(), uuid.uuid4()
    session = FakeSession([[user], [course], [b1, b2]])
    payload = {"block_ids": [str(b1), str(uuid.uuid4())]}  # b2 manquant + id étranger
    response = make_client(session).put(f"/api/v1/courses/{course.id}/blocks/order", json=payload)

    assert response.status_code == 422
    assert "exactement les blocs" in response.json()["detail"]
    assert updates(session) == []


def test_reorder_duplicates_without_db_access():
    b1 = uuid.uuid4()
    session = FakeSession()
    payload = {"block_ids": [str(b1), str(b1)]}
    response = make_client(session).put(
        f"/api/v1/courses/{uuid.uuid4()}/blocks/order", json=payload
    )

    assert response.status_code == 422
    assert session.executed == []


def test_reorder_empty_course():
    user = _user_row()
    course = _course_row()
    session = FakeSession([[user], [course], []])
    response = make_client(session).put(
        f"/api/v1/courses/{course.id}/blocks/order", json={"block_ids": []}
    )

    assert response.status_code == 204
    assert updates(session) == []


def test_update_preview_settings():
    user = _user_row()
    course = _course_row()
    session = FakeSession([[user], [course]])  # auth, puis _get_owned_course
    response = make_client(session).put(f"/api/v1/courses/{course.id}/preview", json=_PREVIEW)

    assert response.status_code == 200
    assert response.json() == _PREVIEW  # écho camelCase des réglages enregistrés
    assert course.preview_settings == _PREVIEW  # mutation d'attribut ORM
    assert updates(session) == []  # pas d'Update Core
    assert course.updated_at != _NOW  # le cours remonte dans la liste
    assert session.commits >= 1


def test_update_course_title():
    user = _user_row()
    course = _course_row(description="Inchangée")
    session = FakeSession([[user], [course]])  # auth, puis get_owned_course
    response = make_client(session).patch(
        f"/api/v1/courses/{course.id}", json={"title": "  Suites et limites  "}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == str(course.id)
    assert body["title"] == "Suites et limites"  # titre trimé
    assert body["description"] == "Inchangée"  # champ absent du payload : intact
    assert course.title == "Suites et limites"  # mutation d'attribut ORM
    assert course.description == "Inchangée"
    assert updates(session) == []  # pas d'Update Core
    assert course.updated_at != _NOW  # le cours remonte dans la liste
    assert session.commits >= 1


def test_update_course_replaces_the_classification():
    user = _user_row()
    course = _course_row()
    s1, s2, l1 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    # auth, cours, lookup matières, lookup niveaux (les delete/insert ne consomment pas).
    session = FakeSession([[user], [course], [s1, s2], [l1]])
    response = make_client(session).patch(
        f"/api/v1/courses/{course.id}",
        json={
            "subject_ids": [str(s1), str(s2), str(s1)],  # doublon dédoublonné
            "education_level_ids": [str(l1)],
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["subject_ids"] == [str(s1), str(s2)]
    assert body["education_level_ids"] == [str(l1)]
    assert body["title"] == "Suites numériques"  # champs éditoriaux intacts
    # Remplacement : un delete par table de liaison, puis les insert.
    assert [stmt.table.name for stmt, _ in deletes(session)] == [
        "course_subjects",
        "course_education_levels",
    ]
    [(_, subject_params)] = inserts(session, "course_subjects")
    assert [p["subject_id"] for p in subject_params] == [s1, s2]
    [(_, level_params)] = inserts(session, "course_education_levels")
    assert [p["education_level_id"] for p in level_params] == [l1]
    assert course.updated_at != _NOW
    assert session.commits >= 1


def test_update_course_empty_lists_clear_the_classification():
    user = _user_row()
    course = _course_row()
    session = FakeSession([[user], [course], [], []])  # lookups sur listes vides
    response = make_client(session).patch(
        f"/api/v1/courses/{course.id}",
        json={"subject_ids": [], "education_level_ids": []},
    )

    assert response.status_code == 200
    assert response.json()["subject_ids"] == []
    assert len(deletes(session)) == 2  # les deux tables de liaison sont vidées
    # Aucun executemany sur liste vide (erreur SQLAlchemy sinon).
    assert inserts(session, "course_subjects") == []
    assert inserts(session, "course_education_levels") == []


def test_update_course_without_classification_keys_leaves_it_untouched():
    user = _user_row()
    course = _course_row()
    session = FakeSession([[user], [course]])  # aucun lookup de taxonomie
    response = make_client(session).patch(
        f"/api/v1/courses/{course.id}", json={"title": "Renommé"}
    )

    assert response.status_code == 200
    body = response.json()
    # null = « pas touché » (jamais [], qui voudrait dire « vidé »).
    assert body["subject_ids"] is None
    assert body["education_level_ids"] is None
    assert deletes(session) == []
    assert inserts(session, "course_subjects") == []


def test_update_course_unknown_subject_writes_nothing():
    user = _user_row()
    course = _course_row()
    session = FakeSession([[user], [course], []])  # lookup matières vide
    response = make_client(session).patch(
        f"/api/v1/courses/{course.id}",
        json={"title": "Renommé", "subject_ids": [str(uuid.uuid4())]},
    )

    assert response.status_code == 422
    assert "Matières inconnues" in response.json()["detail"]
    # Les lookups passent avant toute écriture : le cours est intact.
    assert deletes(session) == []
    assert course.title == "Suites numériques"
    assert course.updated_at == _NOW  # pas même le bump de updated_at


def test_update_course_unknown_level_writes_nothing():
    user = _user_row()
    course = _course_row()
    session = FakeSession([[user], [course], []])  # lookup niveaux vide
    response = make_client(session).patch(
        f"/api/v1/courses/{course.id}", json={"education_level_ids": [str(uuid.uuid4())]}
    )

    assert response.status_code == 422
    assert "Niveaux d'étude inconnus" in response.json()["detail"]
    assert deletes(session) == []
    assert course.updated_at == _NOW


def test_update_course_description_only_keeps_title():
    user = _user_row()
    course = _course_row(description="Ancienne")
    session = FakeSession([[user], [course]])
    response = make_client(session).patch(
        f"/api/v1/courses/{course.id}", json={"description": "Nouvelle"}
    )

    assert response.status_code == 200
    assert response.json()["title"] == "Suites numériques"  # titre non touché
    assert course.title == "Suites numériques"
    assert course.description == "Nouvelle"


def test_update_course_clears_description_with_explicit_null():
    user = _user_row()
    course = _course_row(description="À effacer")
    session = FakeSession([[user], [course]])
    response = make_client(session).patch(
        f"/api/v1/courses/{course.id}", json={"description": None}
    )

    assert response.status_code == 200
    assert response.json()["description"] is None
    assert course.description is None


@pytest.mark.parametrize(
    "payload",
    [
        {},  # aucun champ à modifier
        {"title": "   "},  # titre blanc
        {"title": None},  # titre non effaçable (colonne NOT NULL)
        {"title": "x" * 301},  # trop long
        {"description": "d" * 2001},  # trop longue
        {"title": "x", "visibility": "public"},  # clé inconnue (extra=forbid)
        {"subject_ids": None},  # null refusé : [] est la façon de tout retirer
        {"education_level_ids": None},
        {"subject_ids": ["pas-un-uuid"]},
    ],
)
def test_update_course_invalid_payload_without_db_access(payload):
    session = FakeSession()
    response = make_client(session).patch(f"/api/v1/courses/{uuid.uuid4()}", json=payload)

    assert response.status_code == 422
    assert session.executed == []


def test_update_course_not_owned():
    user = _user_row()
    session = FakeSession([[user], []])  # select cours scopé owner → vide
    response = make_client(session).patch(
        f"/api/v1/courses/{uuid.uuid4()}", json={"title": "Renommé"}
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Cours introuvable"


def test_update_visibility():
    user = _user_row()
    course = _course_row()
    session = FakeSession([[user], [course]])  # auth, puis _get_owned_course
    response = make_client(session).put(
        f"/api/v1/courses/{course.id}/visibility", json={"visibility": "private"}
    )

    assert response.status_code == 200
    assert response.json() == {"visibility": "private"}
    assert course.visibility == "private"  # mutation d'attribut ORM
    assert updates(session) == []  # pas d'Update Core
    assert course.updated_at != _NOW  # le cours remonte dans la liste
    assert session.commits >= 1


@pytest.mark.parametrize("visibility", ["publique", "en_cours", "", None])
def test_update_visibility_unknown_value_without_db_access(visibility):
    session = FakeSession()
    response = make_client(session).put(
        f"/api/v1/courses/{uuid.uuid4()}/visibility", json={"visibility": visibility}
    )
    assert response.status_code == 422
    assert session.executed == []


def test_update_visibility_course_not_owned():
    user = _user_row()
    session = FakeSession([[user], []])  # select cours scopé owner → vide
    response = make_client(session).put(
        f"/api/v1/courses/{uuid.uuid4()}/visibility", json={"visibility": "public"}
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Cours introuvable"


def test_read_exposes_visibility():
    user = _user_row()
    course = _course_row(visibility="public")
    session = FakeSession([[user], [course], [], [], []])
    response = make_client(session).get(f"/api/v1/courses/{course.id}")

    assert response.status_code == 200
    assert response.json()["visibility"] == "public"


def test_update_preview_settings_course_not_owned():
    user = _user_row()
    session = FakeSession([[user], []])  # select cours scopé owner → vide
    response = make_client(session).put(
        f"/api/v1/courses/{uuid.uuid4()}/preview", json=_PREVIEW
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Cours introuvable"


def test_read_exposes_preview_settings():
    # Le détail d'un cours remonte ses réglages de preview (le front les recharge).
    user = _user_row()
    course = _course_row(preview_settings=_PREVIEW)
    session = FakeSession([[user], [course], [], [], []])  # cours, matières, niveaux, blocs
    response = make_client(session).get(f"/api/v1/courses/{course.id}")

    assert response.status_code == 200
    assert response.json()["preview_settings"] == _PREVIEW


@pytest.mark.parametrize(
    "payload",
    [
        {"font": "sans"},  # champs requis manquants
        {**_PREVIEW, "fontSizePx": 4},  # hors bornes (ge=8)
        {**_PREVIEW, "font": "gothique"},  # littéral invalide
        {**_PREVIEW, "extra": 1},  # clé inconnue (extra=forbid)
    ],
)
def test_update_preview_settings_invalid_payload_without_db_access(payload):
    session = FakeSession()
    response = make_client(session).put(
        f"/api/v1/courses/{uuid.uuid4()}/preview", json=payload
    )
    assert response.status_code == 422
    assert session.executed == []
