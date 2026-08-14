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
from sqlalchemy.sql.dml import Delete, Insert, Update

from app.core.auth import AuthenticatedUser, get_current_user
from app.core.config import settings
from app.core.database import get_db
from app.core.storage import get_storage
from app.course_transfer.archive import rewrite_refs
from app.main import create_app

_NOW = datetime(2026, 7, 7, 12, 0, tzinfo=UTC)


def _user_row():
    return SimpleNamespace(id=uuid.uuid4(), sub="prof-123", email=None)


def _course_row(**overrides):
    defaults = dict(
        id=uuid.uuid4(),
        owner_id=None,
        titre="Mon cours",
        description=None,
        preview_settings={},
        visibilite="en_cours",
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
        type="texte",
        titre=None,
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
        nom_original="schema.pdf",
        taille=1024,
        mime="application/pdf",
        statut="disponible",
        created_at=_NOW,
        updated_at=_NOW,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _module_row(**overrides):
    defaults = dict(
        id=uuid.uuid4(),
        course_id=None,
        titre="Grapheur",
        html="<canvas></canvas>",
        css="canvas{width:100%}",
        js="console.log('ok')",
        created_at=_NOW,
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
        self.rollbacks = 0

    async def execute(self, stmt, params=None):
        self.executed.append((stmt, params))
        if isinstance(stmt, Insert) and stmt._returning:
            return _FakeResult(self._select_results.pop(0))
        if isinstance(stmt, (Insert, Update, Delete)):
            return _FakeResult([])
        return _FakeResult(self._select_results.pop(0))

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


class _FakeStorage:
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

    async def delete_many(self, s3_keys):
        self.deleted.extend(s3_keys)


def _client(session, storage=None) -> TestClient:
    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedUser(
        sub="prof-123", email=None, roles=frozenset(), claims={}
    )
    app.dependency_overrides[get_db] = lambda: session
    app.dependency_overrides[get_storage] = lambda: storage or _FakeStorage()
    return TestClient(app)


def _inserts(session, table_name):
    return [
        (stmt, params)
        for stmt, params in session.executed
        if isinstance(stmt, Insert) and stmt.table.name == table_name
    ]


def _manifest(**overrides):
    """Manifest minimal valide ; surcharges par clé de premier niveau."""
    manifest = {
        "format": "opencartable-course",
        "format_version": 1,
        "exported_at": "2026-07-07T12:00:00Z",
        "course": {
            "titre": "Cours importé",
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
        for entry_id, contenu in binaries:
            with zf.open(f"resources/{entry_id}", mode="w") as stream:
                stream.write(contenu)
    return buf.getvalue()


def _post_import(client, contenu: bytes):
    return client.post(
        "/api/v1/courses/import",
        files={"file": ("cours.zip", contenu, "application/zip")},
    )


_COURSE_ID = uuid.uuid4()


# --- Auth requise -------------------------------------------------------------


def test_auth_requise_export():
    response = TestClient(create_app()).get(f"/api/v1/courses/{_COURSE_ID}/export")
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


def test_auth_requise_import():
    response = TestClient(create_app()).post(
        "/api/v1/courses/import",
        files={"file": ("cours.zip", b"x", "application/zip")},
    )
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


# --- Export -------------------------------------------------------------------


def test_export_nominal():
    user = _user_row()
    course = _course_row(
        titre="Fractions", description="Cours de 6e", preview_settings={"font": "serif"}
    )
    resource = _resource_row(s3_key="courses/c/resources/r/img.png",
                             nom_original="img.png", type="image", mime="image/png")
    module = _module_row()
    question_id = str(uuid.uuid4())
    blocks = [
        _block_row(
            position=0,
            content={"markdown": f"![i](oc-resource:{resource.id})"},
        ),
        _block_row(
            position=1,
            type="exercice",
            content={
                "enonce": "Sujet",
                "questions": [
                    {
                        "id": question_id,
                        "enonce": "Q1",
                        "type": "texte_libre",
                        "reponse_attendue": "42",
                    }
                ],
            },
        ),
        _block_row(
            position=2,
            type="document",
            titre="Schéma",
            content={"legende": None, "affichage": "inline"},
            resource_id=resource.id,
        ),
        _block_row(position=3, type="module", content={}, module_id=module.id),
    ]
    session = _FakeSession(
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
    response = _client(session, storage).get(f"/api/v1/courses/{course.id}/export")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    disposition = response.headers["content-disposition"]
    assert disposition.startswith('attachment; filename="cours-Fractions-')
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
    assert manifest["format_version"] == 1
    assert manifest["course"] == {
        "titre": "Fractions",
        "description": "Cours de 6e",
        "preview_settings": {"font": "serif"},
        "subject_codes": ["mathematiques.fractions"],
        "education_level_codes": ["fr.college.6e"],
    }
    assert [b["type"] for b in manifest["blocks"]] == [
        "texte", "exercice", "document", "module",
    ]
    # Contenus verbatim : refs oc-* et ids de questions inchangés à l'export.
    assert manifest["blocks"][0]["content"]["markdown"] == (
        f"![i](oc-resource:{resource.id})"
    )
    assert manifest["blocks"][1]["content"]["questions"][0]["id"] == question_id
    assert manifest["blocks"][1]["content"]["questions"][0]["reponse_attendue"] == "42"
    assert manifest["blocks"][2]["resource_ref"] == str(resource.id)
    assert manifest["blocks"][3]["module_ref"] == str(module.id)
    assert manifest["resources"] == [
        {
            "id": str(resource.id),
            "type": "image",
            "nom_original": "img.png",
            "taille": 1024,
            "mime": "image/png",
        }
    ]
    assert manifest["modules"][0]["titre"] == "Grapheur"
    assert manifest["modules"][0]["html"] == "<canvas></canvas>"

    assert storage.read_keys == [resource.s3_key]
    # Lecture seule : seul le commit de get_or_create_by_sub.
    assert session.commits == 1
    # Le select des ressources exclut les « en_attente » en SQL.
    selects = [str(stmt) for stmt, _ in session.executed if not isinstance(stmt, Insert)]
    assert any("resources.statut" in s for s in selects)


def test_export_cours_autrui_404():
    user = _user_row()
    session = _FakeSession([[user], []])  # select cours scopé owner → vide
    response = _client(session).get(f"/api/v1/courses/{uuid.uuid4()}/export")
    assert response.status_code == 404


def test_export_cours_vide():
    user = _user_row()
    course = _course_row()
    session = _FakeSession([[user], [course], [], [], [], [], []])
    response = _client(session).get(f"/api/v1/courses/{course.id}/export")

    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
        assert zf.namelist() == ["manifest.json"]
        manifest = json.loads(zf.read("manifest.json"))
    assert manifest["blocks"] == []
    assert manifest["resources"] == []
    assert manifest["modules"] == []


def test_export_detache_le_bloc_document_dune_ressource_non_exportee():
    # Ne peut pas arriver par l'API (un bloc ne pointe qu'une ressource
    # disponible) ; la garde défensive détache plutôt que d'échouer.
    user = _user_row()
    course = _course_row()
    block = _block_row(type="document", content={"legende": None, "affichage": "inline"},
                       resource_id=uuid.uuid4())
    session = _FakeSession([[user], [course], [], [], [block], [], []])
    response = _client(session).get(f"/api/v1/courses/{course.id}/export")

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
            "titre": "Cours importé",
            "description": "desc",
            "preview_settings": {"font": "serif"},
            "subject_codes": ["mathematiques.fractions"],
            "education_level_codes": ["fr.college.6e"],
        },
        blocks=[
            {
                "position": 0,
                "type": "texte",
                "content": {"markdown": f"![i](oc-resource:{old_resource_id})"},
            },
            {
                "position": 1,
                "type": "exercice",
                "content": {
                    "enonce": f"Sujet oc-module:{old_module_id}",
                    "questions": [
                        {
                            "id": question_id,
                            "enonce": f"Q oc-resource:{old_resource_id}",
                            "type": "texte_libre",
                            "reponse_attendue": f"oc-resource:{old_resource_id}",
                        }
                    ],
                },
            },
            {
                "position": 2,
                "type": "document",
                "titre": "Schéma",
                "content": {"legende": "l", "affichage": "telechargement"},
                "resource_ref": old_resource_id,
            },
            {"position": 3, "type": "module", "content": {}, "module_ref": old_module_id},
        ],
        resources=[
            {
                "id": old_resource_id,
                "type": "image",
                "nom_original": "img.png",
                "taille": 5,
                "mime": "image/png",
            }
        ],
        modules=[
            {"id": old_module_id, "titre": "Grapheur", "html": "<b>x</b>", "css": "", "js": ""}
        ],
    )
    contenu = _zip_bytes(manifest, binaries=[(old_resource_id, b"12345")])

    user = _user_row()
    subject_id = uuid.uuid4()
    level_id = uuid.uuid4()
    session = _FakeSession([[user], [subject_id], [level_id], [(_NOW, _NOW)]])
    storage = _FakeStorage(session=session)
    response = _post_import(_client(session, storage), contenu)

    assert response.status_code == 201
    body = response.json()
    assert body["titre"] == "Cours importé"
    assert body["block_count"] == 4
    assert body["visibilite"] == "en_cours"
    assert body["preview_settings"] == {"font": "serif"}
    assert body["subject_ids"] == [str(subject_id)]
    assert body["education_level_ids"] == [str(level_id)]

    # Classement remappé par code.
    [(_, m2m_matieres)] = _inserts(session, "course_subjects")
    assert m2m_matieres == [{"course_id": uuid.UUID(body["id"]), "subject_id": subject_id}]
    [(_, m2m_niveaux)] = _inserts(session, "course_education_levels")
    assert m2m_niveaux[0]["education_level_id"] == level_id

    # Modules et ressources : nouveaux uuid, statut disponible, clé S3 neuve.
    [(_, modules)] = _inserts(session, "modules")
    assert modules[0]["titre"] == "Grapheur"
    new_module_id = modules[0]["id"]
    assert str(new_module_id) != old_module_id
    [(_, resources)] = _inserts(session, "resources")
    new_resource_id = resources[0]["id"]
    assert str(new_resource_id) != old_resource_id
    assert resources[0]["statut"] == "disponible"
    assert resources[0]["s3_key"] == (
        f"courses/{body['id']}/resources/{new_resource_id}/img.png"
    )

    # Blocs : positions réécrites, colonnes remappées, refs oc-* réécrites
    # dans markdown/enonce/questions[].enonce — reponse_attendue intacte,
    # questions[].id verbatim.
    [(_, blocks)] = _inserts(session, "blocks")
    assert [b["position"] for b in blocks] == [0, 1, 2, 3]
    assert blocks[0]["content"]["markdown"] == f"![i](oc-resource:{new_resource_id})"
    exercice = blocks[1]["content"]
    assert exercice["enonce"] == f"Sujet oc-module:{new_module_id}"
    assert exercice["questions"][0]["id"] == question_id
    assert exercice["questions"][0]["enonce"] == f"Q oc-resource:{new_resource_id}"
    assert exercice["questions"][0]["reponse_attendue"] == f"oc-resource:{old_resource_id}"
    assert blocks[2]["resource_id"] == new_resource_id
    assert blocks[2]["titre"] == "Schéma"
    assert blocks[3]["module_id"] == new_module_id

    # Binaire poussé sur la nouvelle clé, AVANT le commit final.
    [(s3_key, content_type, pushed, commits_au_put)] = storage.put_objects
    assert s3_key == resources[0]["s3_key"]
    assert content_type == "image/png"
    assert pushed == b"12345"
    assert commits_au_put == 1  # seul le commit de get_or_create_by_sub
    assert session.commits == 2
    assert session.rollbacks == 0


def test_import_codes_inconnus_ignores():
    manifest = _manifest(
        course={
            "titre": "T",
            "description": None,
            "preview_settings": {},
            "subject_codes": ["inconnu.code"],
            "education_level_codes": ["xx.inconnu"],
        },
    )
    user = _user_row()
    session = _FakeSession([[user], [], [], [(_NOW, _NOW)]])
    response = _post_import(_client(session), _zip_bytes(manifest))

    assert response.status_code == 201
    assert response.json()["subject_ids"] == []
    assert _inserts(session, "course_subjects") == []
    assert _inserts(session, "course_education_levels") == []


def test_import_ref_inconnue_laissee_verbatim():
    ref = f"oc-resource:{uuid.uuid4()}"
    manifest = _manifest(
        blocks=[{"position": 0, "type": "texte", "content": {"markdown": ref}}]
    )
    user = _user_row()
    session = _FakeSession([[user], [], [], [(_NOW, _NOW)]])
    response = _post_import(_client(session), _zip_bytes(manifest))

    assert response.status_code == 201
    [(_, blocks)] = _inserts(session, "blocks")
    assert blocks[0]["content"]["markdown"] == ref


def test_import_question_sans_id_en_recoit_un():
    manifest = _manifest(
        blocks=[
            {
                "position": 0,
                "type": "exercice",
                "content": {"enonce": "S", "questions": [{"enonce": "Q"}]},
            }
        ]
    )
    user = _user_row()
    session = _FakeSession([[user], [], [], [(_NOW, _NOW)]])
    response = _post_import(_client(session), _zip_bytes(manifest))

    assert response.status_code == 201
    [(_, blocks)] = _inserts(session, "blocks")
    question = blocks[0]["content"]["questions"][0]
    assert uuid.UUID(question["id"])  # id frais, jamais None en base
    assert question["type"] == "texte_libre"
    assert question["reponse_attendue"] == ""


@pytest.mark.parametrize(
    ("nom", "contenu"),
    [
        ("pas-un-zip", b"definitivement pas un zip"),
        ("manifest-absent", None),  # zip valide sans manifest.json
        ("version-inconnue", _zip_bytes(_manifest(format_version=2))),
        ("champ-inconnu", _zip_bytes(_manifest(champ_pirate=True))),
        (
            "entree-binaire-absente",
            _zip_bytes(
                _manifest(
                    resources=[
                        {
                            "id": str(uuid.uuid4()),
                            "type": "image",
                            "nom_original": "i.png",
                            "taille": 5,
                            "mime": "image/png",
                        }
                    ]
                )
            ),
        ),
        (
            "taille-incoherente",
            (lambda rid: _zip_bytes(
                _manifest(
                    resources=[
                        {
                            "id": rid,
                            "type": "image",
                            "nom_original": "i.png",
                            "taille": 10,  # l'entrée fait 5 octets
                            "mime": "image/png",
                        }
                    ]
                ),
                binaries=[(rid, b"12345")],
            ))(str(uuid.uuid4())),
        ),
        (
            "trop-de-blocs",
            _zip_bytes(
                _manifest(
                    blocks=[
                        {"position": i, "type": "texte", "content": {"markdown": ""}}
                        for i in range(501)
                    ]
                )
            ),
        ),
        (
            "ref-hors-manifest",
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
            "ref-sur-mauvais-type",
            _zip_bytes(
                _manifest(
                    blocks=[
                        {
                            "position": 0,
                            "type": "texte",
                            "content": {"markdown": ""},
                            "module_ref": str(uuid.uuid4()),
                        }
                    ]
                )
            ),
        ),
        (
            "content-module-non-vide",
            _zip_bytes(
                _manifest(
                    blocks=[{"position": 0, "type": "module", "content": {"x": 1}}]
                )
            ),
        ),
    ],
)
def test_import_archive_invalide_422(nom, contenu):
    if contenu is None:  # zip valide sans manifest.json
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("autre.json", b"{}")
        contenu = buf.getvalue()
    user = _user_row()
    session = _FakeSession([[user]])
    response = _post_import(_client(session), contenu)

    assert response.status_code == 422
    # Rien d'écrit : la validation précède tout execute du service.
    assert _inserts(session, "courses") == []
    assert session.commits == 1  # get_or_create_by_sub uniquement


def test_import_archive_trop_grosse_413(monkeypatch):
    monkeypatch.setattr(settings, "TRANSFER_MAX_ZIP_BYTES", 10)
    user = _user_row()
    session = _FakeSession([[user]])
    response = _post_import(_client(session), _zip_bytes(_manifest()))

    assert response.status_code == 413
    assert _inserts(session, "courses") == []


def test_import_echec_s3_503():
    rid_a, rid_b = str(uuid.uuid4()), str(uuid.uuid4())
    manifest = _manifest(
        resources=[
            {"id": rid_a, "type": "image", "nom_original": "a.png",
             "taille": 3, "mime": "image/png"},
            {"id": rid_b, "type": "image", "nom_original": "b.png",
             "taille": 3, "mime": "image/png"},
        ]
    )
    contenu = _zip_bytes(manifest, binaries=[(rid_a, b"aaa"), (rid_b, b"bbb")])
    user = _user_row()
    session = _FakeSession([[user], [], [], [(_NOW, _NOW)]])
    # Le premier put passe, le second lève.
    storage = _FakeStorage(session=session, put_raises_from=1)
    response = _post_import(_client(session, storage), contenu)

    assert response.status_code == 503
    assert session.rollbacks == 1
    assert session.commits == 1  # jamais le commit final
    # La clé déjà poussée est purgée (best effort).
    assert len(storage.put_objects) == 1
    assert storage.deleted == [storage.put_objects[0][0]]


# --- Helpers purs (archive.py) ------------------------------------------------


def test_rewrite_refs_remplace_et_normalise_la_casse():
    old = str(uuid.uuid4())
    text = f"a oc-resource:{old.upper()} b oc-module:{old} c"
    out = rewrite_refs(text, {old: "NEW-R"}, {old: "NEW-M"})
    assert out == "a oc-resource:NEW-R b oc-module:NEW-M c"


def test_rewrite_refs_ref_inconnue_verbatim():
    old = str(uuid.uuid4())
    text = f"voir oc-resource:{old}"
    assert rewrite_refs(text, {}, {}) == text


def test_rewrite_refs_respecte_les_bornes_de_mots():
    old = str(uuid.uuid4())
    # Préfixe collé : « xoc-resource: » n'est pas une référence.
    text = f"xoc-resource:{old}"
    assert rewrite_refs(text, {old: "NEW"}, {}) == text


def test_rewrite_refs_uuid_invalide_ignore():
    text = "oc-resource:pas-un-uuid"
    assert rewrite_refs(text, {"pas-un-uuid": "NEW"}, {}) == text
