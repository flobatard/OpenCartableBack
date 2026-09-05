"""Routes d'export/import de cours (.zip) — aucun réseau, aucun S3 réel.

Même motif que tests/test_resources_api.py : fausse session FIFO (résultats
des SELECT servis dans l'ordre des ``execute`` du service) + faux client S3
injecté via ``get_storage``. Le premier ``[user]`` de la file est consommé
par ``get_or_create_by_sub`` (upsert auth, 1 commit). Les archives des tests
d'import sont construites en mémoire (io.BytesIO + zipfile).
"""

import io
import json
import uuid
import zipfile
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.sql.dml import Insert

from app.core.config import settings
from app.course_transfer.archive import rewrite_refs
from app.main import create_app
from tests.fakes import FakeSession, FakeStorage, inserts, make_client

_NOW = datetime(2026, 7, 7, 12, 0, tzinfo=UTC)


def _user_row():
    return SimpleNamespace(id=uuid.uuid4(), sub="prof-123", email=None)


def _course_row(**overrides):
    defaults = dict(
        id=uuid.uuid4(),
        owner_id=None,
        title="Mon cours",
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
        course_id=None,
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


def _resource_row(**overrides):
    defaults = dict(
        id=uuid.uuid4(),
        course_id=None,
        type="document",
        s3_key="courses/c/resources/r/schema.pdf",
        original_name="schema.pdf",
        size=1024,
        mime="application/pdf",
        status="available",
        created_at=_NOW,
        updated_at=_NOW,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _module_row(**overrides):
    defaults = dict(
        id=uuid.uuid4(),
        course_id=None,
        title="Grapheur",
        html="<canvas></canvas>",
        css="canvas{width:100%}",
        js="console.log('ok')",
        created_at=_NOW,
        updated_at=_NOW,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


class _FakeStorage(FakeStorage):
    """Faux S3 : lit/écrit des contenus déterministes, trace tout.

    ``session`` optionnelle : ``put_object`` mémorise le compteur de commits
    au moment de l'appel (contrat « put S3 AVANT le commit » de l'import).
    """

    def __init__(self, session=None, put_raises_from=None):
        self._session = session
        self._put_raises_from = put_raises_from
        # (s3_key, content_type, contenu, commits au moment de l'appel)
        self.put_objects: list[tuple[str, str, bytes, int | None]] = []
        self.read_keys: list[str] = []
        self.deleted: list[str] = []

    def read_object_into(self, s3_key, fileobj):
        self.read_keys.append(s3_key)
        fileobj.write(b"data:" + s3_key.encode())

    async def put_object(self, s3_key, fileobj, content_type):
        if self._put_raises_from is not None and len(self.put_objects) >= self._put_raises_from:
            raise RuntimeError("S3 en panne")
        commits = self._session.commits if self._session is not None else None
        self.put_objects.append((s3_key, content_type, fileobj.read(), commits))


def _manifest(**overrides):
    """Manifest minimal valide ; surcharges par clé de premier niveau."""
    manifest = {
        "format": "opencartable-course",
        "format_version": 2,
        "exported_at": "2026-07-07T12:00:00Z",
        "course": {
            "title": "Cours importé",
            "description": None,
            "preview_settings": {},
            "subject_codes": [],
            "education_level_codes": [],
        },
        "blocks": [],
        "resources": [],
        "modules": [],
    }
    manifest.update(overrides)
    return manifest


def _zip_bytes(manifest, binaries=()):
    """Archive en mémoire : manifest.json + entrées ``resources/<id>``."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("manifest.json", json.dumps(manifest))
        for entry_id, content in binaries:
            with zf.open(f"resources/{entry_id}", mode="w") as stream:
                stream.write(content)
    return buf.getvalue()


def _post_import(client, content: bytes):
    return client.post(
        "/api/v1/courses/import",
        files={"file": ("course.zip", content, "application/zip")},
    )


_COURSE_ID = uuid.uuid4()


# --- Auth requise -------------------------------------------------------------


def test_auth_required_export():
    response = TestClient(create_app()).get(f"/api/v1/courses/{_COURSE_ID}/export")
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


def test_auth_required_import():
    response = TestClient(create_app()).post(
        "/api/v1/courses/import",
        files={"file": ("course.zip", b"x", "application/zip")},
    )
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


# --- Export -------------------------------------------------------------------


def test_export_nominal():
    user = _user_row()
    course = _course_row(
        title="Fractions", description="Cours de 6e", preview_settings={"font": "serif"}
    )
    resource = _resource_row(s3_key="courses/c/resources/r/img.png",
                             original_name="img.png", type="image", mime="image/png")
    module = _module_row()
    question_id = str(uuid.uuid4())
    blocks = [
        _block_row(
            position=0,
            content={"markdown": f"![i](oc-resource:{resource.id})"},
        ),
        _block_row(
            position=1,
            type="exercise",
            content={
                "statement": "Sujet",
                "questions": [
                    {
                        "id": question_id,
                        "statement": "Q1",
                        "type": "free_text",
                        "expected_answer": "42",
                    }
                ],
            },
        ),
        _block_row(
            position=2,
            type="document",
            title="Schéma",
            content={"caption": None, "display": "inline"},
            resource_id=resource.id,
        ),
        _block_row(position=3, type="module", content={}, module_id=module.id),
    ]
    session = FakeSession(
        [
            [user],
            [course],
            ["mathematiques.fractions"],  # codes matières
            ["fr.college.6e"],  # codes niveaux
            blocks,
            [resource],
            [module],
        ]
    )
    storage = _FakeStorage()
    response = make_client(session, storage).get(f"/api/v1/courses/{course.id}/export")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    disposition = response.headers["content-disposition"]
    assert disposition.startswith('attachment; filename="course-Fractions-')
    assert disposition.endswith('.zip"')
    assert response.headers["content-length"] == str(len(response.content))

    with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
        infos = {i.filename: i for i in zf.infolist()}
        assert set(infos) == {"manifest.json", f"resources/{resource.id}"}
        # Binaires en STORE (déjà compressés), manifest en DEFLATE.
        assert infos[f"resources/{resource.id}"].compress_type == zipfile.ZIP_STORED
        assert infos["manifest.json"].compress_type == zipfile.ZIP_DEFLATED
        assert zf.read(f"resources/{resource.id}") == b"data:" + resource.s3_key.encode()
        manifest = json.loads(zf.read("manifest.json"))

    assert manifest["format"] == "opencartable-course"
    assert manifest["format_version"] == 2
    assert manifest["course"] == {
        "title": "Fractions",
        "description": "Cours de 6e",
        "preview_settings": {"font": "serif"},
        "subject_codes": ["mathematiques.fractions"],
        "education_level_codes": ["fr.college.6e"],
    }
    assert [b["type"] for b in manifest["blocks"]] == [
        "text", "exercise", "document", "module",
    ]
    # Contenus verbatim : refs oc-* et ids de questions inchangés à l'export.
    assert manifest["blocks"][0]["content"]["markdown"] == (
        f"![i](oc-resource:{resource.id})"
    )
    assert manifest["blocks"][1]["content"]["questions"][0]["id"] == question_id
    assert manifest["blocks"][1]["content"]["questions"][0]["expected_answer"] == "42"
    assert manifest["blocks"][2]["resource_ref"] == str(resource.id)
    assert manifest["blocks"][3]["module_ref"] == str(module.id)
    assert manifest["resources"] == [
        {
            "id": str(resource.id),
            "type": "image",
            "original_name": "img.png",
            "size": 1024,
            "mime": "image/png",
        }
    ]
    assert manifest["modules"][0]["title"] == "Grapheur"
    assert manifest["modules"][0]["html"] == "<canvas></canvas>"

    assert storage.read_keys == [resource.s3_key]
    # Lecture seule : seul le commit de get_or_create_by_sub.
    assert session.commits == 1
    # Le select des ressources exclut les « pending » en SQL.
    selects = [str(stmt) for stmt, _ in session.executed if not isinstance(stmt, Insert)]
    assert any("resources.status" in s for s in selects)


def test_export_foreign_course_404():
    user = _user_row()
    session = FakeSession([[user], []])  # select cours scopé owner → vide
    response = make_client(session).get(f"/api/v1/courses/{uuid.uuid4()}/export")
    assert response.status_code == 404


def test_export_empty_course():
    user = _user_row()
    course = _course_row()
    session = FakeSession([[user], [course], [], [], [], [], []])
    response = make_client(session).get(f"/api/v1/courses/{course.id}/export")

    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
        assert zf.namelist() == ["manifest.json"]
        manifest = json.loads(zf.read("manifest.json"))
    assert manifest["blocks"] == []
    assert manifest["resources"] == []
    assert manifest["modules"] == []


def test_export_detaches_document_block_of_unexported_resource():
    # Ne peut pas arriver par l'API (un bloc ne pointe qu'une ressource
    # disponible) ; la garde défensive détache plutôt que d'échouer.
    user = _user_row()
    course = _course_row()
    block = _block_row(type="document", content={"caption": None, "display": "inline"},
                       resource_id=uuid.uuid4())
    session = FakeSession([[user], [course], [], [], [block], [], []])
    response = make_client(session).get(f"/api/v1/courses/{course.id}/export")

    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
        manifest = json.loads(zf.read("manifest.json"))
    assert manifest["blocks"][0]["resource_ref"] is None


# --- Import -------------------------------------------------------------------


def test_import_nominal():
    old_resource_id = str(uuid.uuid4())
    old_module_id = str(uuid.uuid4())
    question_id = str(uuid.uuid4())
    manifest = _manifest(
        course={
            "title": "Cours importé",
            "description": "desc",
            "preview_settings": {"font": "serif"},
            "subject_codes": ["mathematiques.fractions"],
            "education_level_codes": ["fr.college.6e"],
        },
        blocks=[
            {
                "position": 0,
                "type": "text",
                "content": {"markdown": f"![i](oc-resource:{old_resource_id})"},
            },
            {
                "position": 1,
                "type": "exercise",
                "content": {
                    "statement": f"Sujet oc-module:{old_module_id}",
                    "questions": [
                        {
                            "id": question_id,
                            "statement": f"Q oc-resource:{old_resource_id}",
                            "type": "free_text",
                            "expected_answer": f"oc-resource:{old_resource_id}",
                        }
                    ],
                },
            },
            {
                "position": 2,
                "type": "document",
                "title": "Schéma",
                "content": {"caption": "l", "display": "download"},
                "resource_ref": old_resource_id,
            },
            {"position": 3, "type": "module", "content": {}, "module_ref": old_module_id},
        ],
        resources=[
            {
                "id": old_resource_id,
                "type": "image",
                "original_name": "img.png",
                "size": 5,
                "mime": "image/png",
            }
        ],
        modules=[
            {"id": old_module_id, "title": "Grapheur", "html": "<b>x</b>", "css": "", "js": ""}
        ],
    )
    content = _zip_bytes(manifest, binaries=[(old_resource_id, b"12345")])

    user = _user_row()
    subject_id = uuid.uuid4()
    level_id = uuid.uuid4()
    session = FakeSession([[user], [subject_id], [level_id], [(_NOW, _NOW)]])
    storage = _FakeStorage(session=session)
    response = _post_import(make_client(session, storage), content)

    assert response.status_code == 201
    body = response.json()
    assert body["title"] == "Cours importé"
    assert body["block_count"] == 4
    assert body["visibility"] == "draft"
    assert body["preview_settings"] == {"font": "serif"}
    assert body["subject_ids"] == [str(subject_id)]
    assert body["education_level_ids"] == [str(level_id)]

    # Classement remappé par code.
    [(_, subject_m2m)] = inserts(session, "course_subjects")
    assert subject_m2m == [{"course_id": uuid.UUID(body["id"]), "subject_id": subject_id}]
    [(_, level_m2m)] = inserts(session, "course_education_levels")
    assert level_m2m[0]["education_level_id"] == level_id

    # Modules et ressources : nouveaux uuid, statut available, clé S3 neuve.
    [(_, modules)] = inserts(session, "modules")
    assert modules[0]["title"] == "Grapheur"
    new_module_id = modules[0]["id"]
    assert str(new_module_id) != old_module_id
    [(_, resources)] = inserts(session, "resources")
    new_resource_id = resources[0]["id"]
    assert str(new_resource_id) != old_resource_id
    assert resources[0]["status"] == "available"
    assert resources[0]["s3_key"] == (
        f"courses/{body['id']}/resources/{new_resource_id}/img.png"
    )

    # Blocs : positions réécrites, colonnes remappées, refs oc-* réécrites
    # dans markdown/statement/questions[].statement — expected_answer intacte,
    # questions[].id verbatim.
    [(_, blocks)] = inserts(session, "blocks")
    assert [b["position"] for b in blocks] == [0, 1, 2, 3]
    assert blocks[0]["content"]["markdown"] == f"![i](oc-resource:{new_resource_id})"
    exercise = blocks[1]["content"]
    assert exercise["statement"] == f"Sujet oc-module:{new_module_id}"
    assert exercise["questions"][0]["id"] == question_id
    assert exercise["questions"][0]["statement"] == f"Q oc-resource:{new_resource_id}"
    assert exercise["questions"][0]["expected_answer"] == f"oc-resource:{old_resource_id}"
    assert blocks[2]["resource_id"] == new_resource_id
    assert blocks[2]["title"] == "Schéma"
    assert blocks[3]["module_id"] == new_module_id

    # Binaire poussé sur la nouvelle clé, AVANT le commit final.
    [(s3_key, content_type, pushed, commits_at_put)] = storage.put_objects
    assert s3_key == resources[0]["s3_key"]
    assert content_type == "image/png"
    assert pushed == b"12345"
    assert commits_at_put == 1  # seul le commit de get_or_create_by_sub
    assert session.commits == 2
    assert session.rollbacks == 0


def test_import_unknown_codes_ignored():
    manifest = _manifest(
        course={
            "title": "T",
            "description": None,
            "preview_settings": {},
            "subject_codes": ["inconnu.code"],
            "education_level_codes": ["xx.inconnu"],
        },
    )
    user = _user_row()
    session = FakeSession([[user], [], [], [(_NOW, _NOW)]])
    response = _post_import(make_client(session), _zip_bytes(manifest))

    assert response.status_code == 201
    assert response.json()["subject_ids"] == []
    assert inserts(session, "course_subjects") == []
    assert inserts(session, "course_education_levels") == []


def test_import_unknown_ref_left_verbatim():
    ref = f"oc-resource:{uuid.uuid4()}"
    manifest = _manifest(
        blocks=[{"position": 0, "type": "text", "content": {"markdown": ref}}]
    )
    user = _user_row()
    session = FakeSession([[user], [], [], [(_NOW, _NOW)]])
    response = _post_import(make_client(session), _zip_bytes(manifest))

    assert response.status_code == 201
    [(_, blocks)] = inserts(session, "blocks")
    assert blocks[0]["content"]["markdown"] == ref


def test_import_question_without_id_receives_one():
    manifest = _manifest(
        blocks=[
            {
                "position": 0,
                "type": "exercise",
                "content": {"statement": "S", "questions": [{"statement": "Q"}]},
            }
        ]
    )
    user = _user_row()
    session = FakeSession([[user], [], [], [(_NOW, _NOW)]])
    response = _post_import(make_client(session), _zip_bytes(manifest))

    assert response.status_code == 201
    [(_, blocks)] = inserts(session, "blocks")
    question = blocks[0]["content"]["questions"][0]
    assert uuid.UUID(question["id"])  # id frais, jamais None en base
    assert question["type"] == "free_text"
    assert question["expected_answer"] == ""


def test_import_v1_archive_normalized_to_english():
    """Compat descendante : une archive v1 (clés/valeurs françaises) reste
    importable — normalisée en v2 anglais avant validation, contenu identique."""
    old_resource_id = str(uuid.uuid4())
    old_module_id = str(uuid.uuid4())
    question_id = str(uuid.uuid4())
    manifest_v1 = {
        "format": "opencartable-course",
        "format_version": 1,
        "exported_at": "2026-07-07T12:00:00Z",
        "course": {
            "titre": "Cours hérité",
            "description": "desc",
            "preview_settings": {},
            "subject_codes": [],
            "education_level_codes": [],
        },
        "blocks": [
            {"position": 0, "type": "texte", "content": {"markdown": "# Ancien"}},
            {
                "position": 1,
                "type": "exercice",
                "titre": "Exo",
                "content": {
                    "enonce": "Sujet global",
                    "questions": [
                        {
                            "id": question_id,
                            "enonce": "Montrer que $u_n$ converge.",
                            "type": "texte_libre",
                            "reponse_attendue": "Par récurrence.",
                        }
                    ],
                },
            },
            {
                "position": 2,
                "type": "document",
                "content": {"legende": "Schéma", "affichage": "telechargement"},
                "resource_ref": old_resource_id,
            },
            {"position": 3, "type": "module", "content": {}, "module_ref": old_module_id},
        ],
        "resources": [
            {
                "id": old_resource_id,
                "type": "image",
                "nom_original": "img.png",
                "taille": 5,
                "mime": "image/png",
            }
        ],
        "modules": [
            {"id": old_module_id, "titre": "Grapheur", "html": "<b>x</b>", "css": "", "js": ""}
        ],
    }
    content = _zip_bytes(manifest_v1, binaries=[(old_resource_id, b"12345")])

    user = _user_row()
    session = FakeSession([[user], [], [], [(_NOW, _NOW)]])
    storage = _FakeStorage(session=session)
    response = _post_import(make_client(session, storage), content)

    assert response.status_code == 201
    body = response.json()
    assert body["title"] == "Cours hérité"
    assert body["block_count"] == 4

    [(_, blocks)] = inserts(session, "blocks")
    assert [b["type"] for b in blocks] == ["text", "exercise", "document", "module"]
    assert blocks[1]["title"] == "Exo"
    exercise = blocks[1]["content"]
    assert set(exercise) == {"statement", "questions"}
    assert exercise["statement"] == "Sujet global"
    question = exercise["questions"][0]
    assert question["id"] == question_id
    assert question["statement"] == "Montrer que $u_n$ converge."
    assert question["type"] == "free_text"
    assert question["expected_answer"] == "Par récurrence."
    assert blocks[2]["content"] == {"caption": "Schéma", "display": "download"}

    [(_, resources)] = inserts(session, "resources")
    assert resources[0]["original_name"] == "img.png"
    assert resources[0]["size"] == 5
    [(_, modules)] = inserts(session, "modules")
    assert modules[0]["title"] == "Grapheur"


@pytest.mark.parametrize(
    ("name", "content"),
    [
        ("not-a-zip", b"definitivement pas un zip"),
        ("manifest-missing", None),  # zip valide sans manifest.json
        ("unknown-version", _zip_bytes(_manifest(format_version=3))),
        ("unknown-field", _zip_bytes(_manifest(champ_pirate=True))),
        (
            "binary-entry-missing",
            _zip_bytes(
                _manifest(
                    resources=[
                        {
                            "id": str(uuid.uuid4()),
                            "type": "image",
                            "original_name": "i.png",
                            "size": 5,
                            "mime": "image/png",
                        }
                    ]
                )
            ),
        ),
        (
            "inconsistent-size",
            (lambda rid: _zip_bytes(
                _manifest(
                    resources=[
                        {
                            "id": rid,
                            "type": "image",
                            "original_name": "i.png",
                            "size": 10,  # l'entrée fait 5 octets
                            "mime": "image/png",
                        }
                    ]
                ),
                binaries=[(rid, b"12345")],
            ))(str(uuid.uuid4())),
        ),
        (
            "too-many-blocks",
            _zip_bytes(
                _manifest(
                    blocks=[
                        {"position": i, "type": "text", "content": {"markdown": ""}}
                        for i in range(501)
                    ]
                )
            ),
        ),
        (
            "ref-outside-manifest",
            _zip_bytes(
                _manifest(
                    blocks=[
                        {
                            "position": 0,
                            "type": "document",
                            "content": {},
                            "resource_ref": str(uuid.uuid4()),
                        }
                    ]
                )
            ),
        ),
        (
            "ref-on-wrong-type",
            _zip_bytes(
                _manifest(
                    blocks=[
                        {
                            "position": 0,
                            "type": "text",
                            "content": {"markdown": ""},
                            "module_ref": str(uuid.uuid4()),
                        }
                    ]
                )
            ),
        ),
        (
            "non-empty-module-content",
            _zip_bytes(
                _manifest(
                    blocks=[{"position": 0, "type": "module", "content": {"x": 1}}]
                )
            ),
        ),
    ],
)
def test_import_invalid_archive_422(name, content):
    if content is None:  # zip valide sans manifest.json
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("autre.json", b"{}")
        content = buf.getvalue()
    user = _user_row()
    session = FakeSession([[user]])
    response = _post_import(make_client(session), content)

    assert response.status_code == 422
    # Rien d'écrit : la validation précède tout execute du service.
    assert inserts(session, "courses") == []
    assert session.commits == 1  # get_or_create_by_sub uniquement


def test_import_archive_too_large_413(monkeypatch):
    monkeypatch.setattr(settings, "TRANSFER_MAX_ZIP_BYTES", 10)
    user = _user_row()
    session = FakeSession([[user]])
    response = _post_import(make_client(session), _zip_bytes(_manifest()))

    assert response.status_code == 413
    assert inserts(session, "courses") == []


def test_import_s3_failure_503():
    rid_a, rid_b = str(uuid.uuid4()), str(uuid.uuid4())
    manifest = _manifest(
        resources=[
            {"id": rid_a, "type": "image", "original_name": "a.png",
             "size": 3, "mime": "image/png"},
            {"id": rid_b, "type": "image", "original_name": "b.png",
             "size": 3, "mime": "image/png"},
        ]
    )
    content = _zip_bytes(manifest, binaries=[(rid_a, b"aaa"), (rid_b, b"bbb")])
    user = _user_row()
    session = FakeSession([[user], [], [], [(_NOW, _NOW)]])
    # Le premier put passe, le second lève.
    storage = _FakeStorage(session=session, put_raises_from=1)
    response = _post_import(make_client(session, storage), content)

    assert response.status_code == 503
    assert session.rollbacks == 1
    assert session.commits == 1  # jamais le commit final
    # La clé déjà poussée est purgée (best effort).
    assert len(storage.put_objects) == 1
    assert storage.deleted == [storage.put_objects[0][0]]


# --- Helpers purs (archive.py) ------------------------------------------------


def test_rewrite_refs_replaces_and_normalizes_case():
    old = str(uuid.uuid4())
    text = f"a oc-resource:{old.upper()} b oc-module:{old} c"
    out = rewrite_refs(text, {old: "NEW-R"}, {old: "NEW-M"})
    assert out == "a oc-resource:NEW-R b oc-module:NEW-M c"


def test_rewrite_refs_unknown_ref_verbatim():
    old = str(uuid.uuid4())
    text = f"voir oc-resource:{old}"
    assert rewrite_refs(text, {}, {}) == text


def test_rewrite_refs_respects_word_boundaries():
    old = str(uuid.uuid4())
    # Préfixe collé : « xoc-resource: » n'est pas une référence.
    text = f"xoc-resource:{old}"
    assert rewrite_refs(text, {old: "NEW"}, {}) == text


def test_rewrite_refs_invalid_uuid_ignored():
    text = "oc-resource:pas-un-uuid"
    assert rewrite_refs(text, {"pas-un-uuid": "NEW"}, {}) == text
