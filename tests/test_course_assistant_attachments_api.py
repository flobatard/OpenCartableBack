"""Routes /courses/{id}/assistant/attachments — flow presigned, aucun réseau.

Même motif que ``tests/test_resources_api.py`` : fausse session FIFO (résultats
des SELECT servis dans l'ordre des ``execute`` du service) + faux client S3
injecté via ``get_storage``. Le premier ``[user]`` de la file est consommé par
``get_or_create_by_sub``.

Ce que ces tests gardent, et qui n'est PAS du confort :

- la whitelist de mimes est **fermée** (un format inconnu est refusé avant tout
  accès S3) et le plafond de taille est **par famille** ;
- la confirmation re-vérifie taille **et** type au HEAD (une URL présignée PUT
  ne borne rien) et **purge l'objet hors gabarit** ;
- une pièce déjà envoyée est **indélébile** : la retirer invaliderait l'``enum``
  du tool ``read_attachment`` d'une reprise HITL en attente.
"""

import uuid

import pytest
from sqlalchemy.sql.dml import Delete

from app.course_assistant.attachments import ATTACHMENT_MAX_BYTES, ATTACHMENT_TYPES
from app.course_assistant.tools import IMAGE_MAX_BYTES, PDF_MAX_BYTES
from app.models.ai_attachment import KIND_IMAGE, KIND_PDF
from tests.course_assistant_fakes import (
    ATTACHMENT_ID,
    BASE,
    COURSE_ID,
    attachment_row,
    course_row,
    user_row,
)
from tests.fakes import FakeSession, FakeStorage, inserts, make_client

ATTACHMENTS = f"{BASE}/attachments"
PNG = "image/png"


def _presign_body(**overrides):
    body = {"original_name": "photo.png", "mime": PNG, "size": 2048}
    body.update(overrides)
    return body


def _inserted_attachment(session) -> dict:
    """Les valeurs de l'INSERT : elles vivent dans le statement (``.values()``),
    pas dans les params d'exécution."""
    [(stmt, _)] = inserts(session, "ai_attachments")
    return stmt.compile().params


# ─────────────────────────────────────────────
# Plafonds : une seule vérité
# ─────────────────────────────────────────────


def test_caps_match_the_reading_caps_of_the_tools():
    """Les pièces jointes empruntent les chemins de lecture de ``tools.py`` :
    accepter à l'upload ce que la lecture refusera serait un piège."""
    assert ATTACHMENT_MAX_BYTES[KIND_IMAGE] == IMAGE_MAX_BYTES
    assert ATTACHMENT_MAX_BYTES[KIND_PDF] == PDF_MAX_BYTES


def test_every_whitelisted_kind_has_a_cap():
    assert {kind for kind, _ in ATTACHMENT_TYPES.values()} <= set(ATTACHMENT_MAX_BYTES)


# ─────────────────────────────────────────────
# Presign
# ─────────────────────────────────────────────


def test_presign_creates_pending_row_and_returns_upload_url():
    session = FakeSession([[user_row()], [course_row()]])
    client = make_client(session)
    response = client.post(ATTACHMENTS, json=_presign_body())

    assert response.status_code == 201
    payload = response.json()
    assert payload["status"] == "pending"
    assert payload["upload_url"].startswith("https://s3.test/put/")
    # La clé S3 n'est PAS exposée au client (motif AvatarPresign).
    assert "s3_key" not in payload

    row = _inserted_attachment(session)
    assert row["status"] == "pending"
    assert row["kind"] == KIND_IMAGE
    assert row["conversation_id"] is None and row["message_id"] is None
    assert (
        row["s3_key"]
        == f"courses/{COURSE_ID}/assistant/{payload['attachment_id']}/photo.png"
    )


def test_presign_sanitizes_the_file_name():
    """Traversée de chemin neutralisée : le nom d'origine reste intact en base,
    seule la clé S3 est assainie."""
    session = FakeSession([[user_row()], [course_row()]])
    client = make_client(session)
    response = client.post(
        ATTACHMENTS, json=_presign_body(original_name="../../etc/passwd.png")
    )

    assert response.status_code == 201
    row = _inserted_attachment(session)
    assert row["original_name"] == "../../etc/passwd.png"
    assert row["s3_key"].endswith("/passwd.png")
    assert ".." not in row["s3_key"]


@pytest.mark.parametrize(
    "mime", ["application/zip", "image/gif", "text/html", "application/x-msdownload"]
)
def test_presign_rejects_mime_outside_the_whitelist(mime):
    session = FakeSession([[user_row()]])
    client = make_client(session)
    response = client.post(ATTACHMENTS, json=_presign_body(mime=mime))
    assert response.status_code == 422


def test_presign_rejects_size_above_the_family_cap():
    """Le plafond est PAR FAMILLE : une image de 20 Mo est refusée là où un PDF
    de 20 Mo passe."""
    too_big_for_an_image = ATTACHMENT_MAX_BYTES[KIND_IMAGE] + 1
    client = make_client(FakeSession([[user_row()]]))
    assert (
        client.post(ATTACHMENTS, json=_presign_body(size=too_big_for_an_image)).status_code
        == 422
    )

    session = FakeSession([[user_row()], [course_row()]])
    client = make_client(session)
    response = client.post(
        ATTACHMENTS,
        json=_presign_body(
            original_name="sujet.pdf", mime="application/pdf", size=too_big_for_an_image
        ),
    )
    assert response.status_code == 201


def test_presign_on_someone_elses_course_is_404():
    client = make_client(FakeSession([[user_row()], []]))
    assert client.post(ATTACHMENTS, json=_presign_body()).status_code == 404


# ─────────────────────────────────────────────
# Confirmation
# ─────────────────────────────────────────────


def test_confirm_marks_available_when_the_object_matches():
    row = attachment_row(status="pending", message_id=None)
    session = FakeSession([[user_row()], [course_row()], [row]])
    storage = FakeStorage({"ContentLength": row.size, "ContentType": PNG})
    client = make_client(session, storage)

    response = client.post(f"{ATTACHMENTS}/{ATTACHMENT_ID}/confirm")
    assert response.status_code == 200
    assert response.json()["status"] == "available"
    assert row.status == "available"
    assert storage.head_calls == [row.s3_key]
    assert storage.deleted == []


def test_confirm_without_object_is_409():
    row = attachment_row(status="pending", message_id=None)
    session = FakeSession([[user_row()], [course_row()], [row]])
    storage = FakeStorage(None)
    client = make_client(session, storage)

    assert client.post(f"{ATTACHMENTS}/{ATTACHMENT_ID}/confirm").status_code == 409
    assert row.status == "pending"
    assert storage.deleted == []


def test_confirm_purges_an_object_larger_than_declared():
    """Une URL présignée PUT ne borne pas la taille : seul le HEAD fait foi."""
    row = attachment_row(status="pending", message_id=None)
    session = FakeSession([[user_row()], [course_row()], [row]])
    oversized = ATTACHMENT_MAX_BYTES[KIND_IMAGE] + 1
    storage = FakeStorage({"ContentLength": oversized, "ContentType": PNG})
    client = make_client(session, storage)

    assert client.post(f"{ATTACHMENTS}/{ATTACHMENT_ID}/confirm").status_code == 409
    assert storage.deleted == [row.s3_key]
    assert row.status == "pending"


def test_confirm_purges_an_object_of_another_type():
    row = attachment_row(status="pending", message_id=None)
    session = FakeSession([[user_row()], [course_row()], [row]])
    storage = FakeStorage({"ContentLength": row.size, "ContentType": "application/zip"})
    client = make_client(session, storage)

    assert client.post(f"{ATTACHMENTS}/{ATTACHMENT_ID}/confirm").status_code == 409
    assert storage.deleted == [row.s3_key]


def test_confirm_twice_is_409():
    row = attachment_row(status="available", message_id=None)
    session = FakeSession([[user_row()], [course_row()], [row]])
    storage = FakeStorage({"ContentLength": row.size, "ContentType": PNG})
    client = make_client(session, storage)

    assert client.post(f"{ATTACHMENTS}/{ATTACHMENT_ID}/confirm").status_code == 409
    assert storage.head_calls == []  # sortie avant tout appel S3


# ─────────────────────────────────────────────
# Téléchargement
# ─────────────────────────────────────────────


def test_download_returns_an_inline_presigned_url():
    row = attachment_row()
    session = FakeSession([[user_row()], [course_row()], [row]])
    storage = FakeStorage()
    client = make_client(session, storage)

    response = client.get(f"{ATTACHMENTS}/{ATTACHMENT_ID}/download")
    assert response.status_code == 200
    assert response.json()["download_url"].endswith(row.s3_key)
    assert storage.inline_calls == [True]


def test_download_of_a_pending_attachment_is_409():
    session = FakeSession(
        [[user_row()], [course_row()], [attachment_row(status="pending", message_id=None)]]
    )
    client = make_client(session)
    assert client.get(f"{ATTACHMENTS}/{ATTACHMENT_ID}/download").status_code == 409


# ─────────────────────────────────────────────
# Suppression
# ─────────────────────────────────────────────


def test_delete_an_unsent_attachment_removes_row_and_object():
    row = attachment_row(conversation_id=None, message_id=None)
    session = FakeSession([[user_row()], [course_row()], [row]])
    storage = FakeStorage()
    client = make_client(session, storage)

    assert client.delete(f"{ATTACHMENTS}/{ATTACHMENT_ID}").status_code == 204
    assert any(isinstance(stmt, Delete) for stmt, _ in session.executed)
    # Purge du bucket APRÈS le commit.
    assert storage.deleted == [row.s3_key]


def test_delete_a_sent_attachment_is_409():
    """Indélébile une fois envoyée : l'``enum`` du tool ``read_attachment`` doit
    rester identique entre un tour et sa reprise HITL."""
    session = FakeSession([[user_row()], [course_row()], [attachment_row()]])
    storage = FakeStorage()
    client = make_client(session, storage)

    assert client.delete(f"{ATTACHMENTS}/{ATTACHMENT_ID}").status_code == 409
    assert storage.deleted == []
    assert not any(isinstance(stmt, Delete) for stmt, _ in session.executed)


def test_unknown_attachment_is_404():
    session = FakeSession([[user_row()], [course_row()], []])
    client = make_client(session)
    response = client.delete(f"{ATTACHMENTS}/{uuid.uuid4()}")
    assert response.status_code == 404
    assert response.json()["detail"] == "Pièce jointe introuvable"
