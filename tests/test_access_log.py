"""Tests du middleware de corrélation et d'access-log (app/core/logging.py).

L'essentiel tient sur ``GET /api/v1/health`` (publique) et ``GET /api/v1/me``
sans token (401) : aucune session, aucun S3, aucun réseau. Les cas qu'aucune
route réelle n'offre (crash non rattrapé, sonde du contexte, flux lent) sont
montés sur un ``create_app()`` dédié — le middleware étant le plus externe, ces
routes le traversent exactement comme les vraies.

``caplog`` capture par un handler posé sur la racine, **sans** notre filtre :
c'est pourquoi la ligne d'access-log porte son id dans son propre ``extra``
(cf. :class:`app.core.logging.CorrelationIdFilter`).
"""

import asyncio
import logging
import re
from collections.abc import AsyncIterator

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.core.logging import (
    ACCESS_LOGGER_NAME,
    REQUEST_ID_HEADER,
    get_correlation_id,
)
from app.core.sse import sse_event, sse_response
from app.main import create_app

HEALTH = "/api/v1/health"
ME = "/api/v1/me"
HEX8 = re.compile(r"\A[0-9a-f]{8}\Z")


def _access(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    """Les seuls records d'access-log — caplog voit aussi les autres loggers."""
    return [record for record in caplog.records if record.name == ACCESS_LOGGER_NAME]


# --------------------------------------------------------------- l'id de corrélation


def test_a_response_carries_a_fresh_request_id(client: TestClient) -> None:
    first = client.get(HEALTH).headers[REQUEST_ID_HEADER]
    second = client.get(HEALTH).headers[REQUEST_ID_HEADER]

    assert HEX8.match(first)
    assert first != second


def test_a_valid_incoming_id_is_reflected(client: TestClient) -> None:
    """Le jour où le nginx d'infra injecte son $request_id, rien à changer."""
    incoming = "0123456789abcdef0123456789abcdef"
    response = client.get(HEALTH, headers={REQUEST_ID_HEADER: incoming})

    assert response.headers[REQUEST_ID_HEADER] == incoming


@pytest.mark.parametrize("forged", ["a b", "x" * 100, "a%0Ab", "a/b"])
def test_an_unusable_incoming_id_is_replaced(client: TestClient, forged: str) -> None:
    """Jamais un 400 : un id de corrélation ne fait pas échouer une requête."""
    response = client.get(HEALTH, headers={REQUEST_ID_HEADER: forged})

    assert response.status_code == 200
    assert HEX8.match(response.headers[REQUEST_ID_HEADER])


def test_the_incoming_id_is_not_duplicated(client: TestClient) -> None:
    response = client.get(HEALTH, headers={REQUEST_ID_HEADER: "abc12345"})

    assert response.headers.get_list(REQUEST_ID_HEADER) == ["abc12345"]


def test_the_id_reaches_the_route_and_its_threadpool() -> None:
    """La sonde SYNCHRONE est celle qui compte : elle tourne dans le threadpool,
    là où vivent les appels S3 — leurs logs doivent porter l'id aussi."""
    app = create_app()

    @app.get("/probe-async")
    async def probe_async() -> dict[str, str | None]:
        return {"cid": get_correlation_id()}

    @app.get("/probe-sync")
    def probe_sync() -> dict[str, str | None]:
        return {"cid": get_correlation_id()}

    client = TestClient(app)
    for path in ("/probe-async", "/probe-sync"):
        response = client.get(path)
        assert response.json()["cid"] == response.headers[REQUEST_ID_HEADER], path


# ------------------------------------------------------------------ la ligne de log


def test_the_access_line_carries_method_target_status_and_duration(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger=ACCESS_LOGGER_NAME):
        response = client.get(ME)

    assert response.status_code == 401
    (record,) = _access(caplog)
    # Un 401 est le fonctionnement NORMAL d'un resource server, pas un incident.
    assert record.levelno == logging.INFO
    assert (record.http_method, record.http_target, record.http_status) == ("GET", ME, 401)
    assert record.duration_ms >= 0
    assert record.http_failed is False
    assert record.correlation_id == response.headers[REQUEST_ID_HEADER]
    assert record.getMessage() == f"GET {ME} → 401 en {record.duration_ms} ms"


def test_the_health_probe_is_logged_at_debug(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    """~2880 lignes par jour (healthchecks des deux composes) : invisibles à
    INFO, mais disponibles si on descend le niveau — pas exclues."""
    with caplog.at_level(logging.INFO, logger=ACCESS_LOGGER_NAME):
        client.get(HEALTH)
    assert _access(caplog) == []

    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger=ACCESS_LOGGER_NAME):
        client.get(HEALTH)
    assert len(_access(caplog)) == 1


def test_a_slow_request_is_logged_as_a_warning(
    client: TestClient, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "LOG_SLOW_REQUEST_MS", 0)
    with caplog.at_level(logging.INFO, logger=ACCESS_LOGGER_NAME):
        client.get(ME)

    (record,) = _access(caplog)
    assert record.levelno == logging.WARNING


def test_log_access_off_keeps_the_correlation_header(
    client: TestClient, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """L'access-log et la corrélation sont deux choses distinctes."""
    monkeypatch.setattr(settings, "LOG_ACCESS", False)
    with caplog.at_level(logging.DEBUG, logger=ACCESS_LOGGER_NAME):
        response = client.get(ME)

    assert _access(caplog) == []
    assert HEX8.match(response.headers[REQUEST_ID_HEADER])


# ----------------------------------------------------------- ce qui ne doit PAS fuir


def test_secret_query_values_never_reach_the_logs(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger=ACCESS_LOGGER_NAME):
        client.get(f"{ME}?token=SUPERSECRET&q=pythagore&page=2")

    (record,) = _access(caplog)
    assert "token=***" in record.http_target
    assert "q=pythagore&page=2" in record.http_target  # utile au débogage : conservé
    assert "SUPERSECRET" not in caplog.text


def test_a_percent_encoded_newline_cannot_forge_a_log_line(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger=ACCESS_LOGGER_NAME):
        client.get("/api/v1/%0A2026-01-01%20ERROR%20faux%20log")

    (record,) = _access(caplog)
    assert "\n" not in record.getMessage()
    assert record.http_target == "/api/v1/?2026-01-01 ERROR faux log"


def test_the_bearer_token_never_reaches_the_logs(
    client: TestClient, caplog: pytest.LogCaptureFixture, mock_jwks: None, make_token
) -> None:
    """Verrou de l'invariant « jamais le token dans les logs » : le middleware
    ne lit aucun en-tête hors x-request-id."""
    token = make_token()
    with caplog.at_level(logging.DEBUG):
        response = client.get(ME, headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    assert token not in caplog.text
    assert "Bearer" not in caplog.text


# ---------------------------------------------------------------- crash non rattrapé


def test_an_unhandled_exception_is_logged_with_its_duration(
    caplog: pytest.LogCaptureFixture,
) -> None:
    app = create_app()

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("bim")

    with caplog.at_level(logging.INFO, logger=ACCESS_LOGGER_NAME):
        response = TestClient(app, raise_server_exceptions=False).get("/boom")

    assert response.status_code == 500
    (record,) = _access(caplog)
    assert record.levelno == logging.WARNING
    assert record.http_status == 500
    assert record.http_failed is True
    assert "exception non rattrapée" in record.getMessage()
    # Le 500 est rendu par ServerErrorMiddleware, EN AMONT du nôtre : cette
    # réponse-là ne porte donc pas l'en-tête (cf. TODO.md). La ligne de log,
    # elle, porte bien l'id — c'est elle qui sert au diagnostic.
    assert REQUEST_ID_HEADER not in response.headers


def test_an_unhandled_exception_still_propagates() -> None:
    """Le middleware journalise et ré-émet : ``raise_server_exceptions`` et le
    traceback ``debug`` gardent leur comportement."""
    app = create_app()

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("bim")

    with pytest.raises(RuntimeError, match="bim"):
        TestClient(app).get("/boom")


def test_an_http_error_keeps_its_correlation_header(client: TestClient) -> None:
    """Les erreurs NORMALES (toute HTTPException) sont rendues par
    ExceptionMiddleware, en aval du nôtre : elles passent donc par notre
    ``send`` et portent l'en-tête. Seul un crash y échappe (test ci-dessus)."""
    response = client.get(ME)

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert HEX8.match(response.headers[REQUEST_ID_HEADER])


# --------------------------------------------------------------------- non-régression SSE


def test_a_streamed_response_is_logged_once_for_its_whole_duration(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Le test qui justifie le middleware ASGI pur.

    ``BaseHTTPMiddleware`` rendrait la main dès ``http.response.start`` : la
    durée mesurée serait le time-to-first-byte (~0 ms), et le seuil de lenteur
    ferait de chaque génération IA un WARNING.
    """
    monkeypatch.setattr(settings, "LOG_SLOW_REQUEST_MS", 0)
    app = create_app()

    @app.post("/flux")
    async def flux():
        async def events() -> AsyncIterator[str]:
            yield sse_event("token", {"delta": "Bonjour"})
            await asyncio.sleep(0.05)
            yield sse_event("done", {"usage": None})

        return sse_response(events())

    with caplog.at_level(logging.INFO, logger=ACCESS_LOGGER_NAME):
        with TestClient(app).stream("POST", "/flux") as response:
            assert response.status_code == 200
            body = response.read().decode("utf-8")

    # Les en-têtes du contrat SSE sont intacts : on ne réécrit que le nôtre.
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["x-accel-buffering"] == "no"
    assert response.headers["cache-control"] == "no-store"
    assert HEX8.match(response.headers[REQUEST_ID_HEADER])
    assert body.endswith('event: done\ndata: {"usage": null}\n\n')

    (record,) = _access(caplog)
    assert record.duration_ms >= 40  # la durée couvre le flux, pas son premier octet
    # …et pourtant INFO : un flux est hors seuil de lenteur, il dure par nature.
    assert record.levelno == logging.INFO


# --------------------------------------------------------------------------- CORS


def test_the_correlation_header_is_exposed_to_the_spa(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sans expose_headers, le navigateur reçoit l'en-tête mais le JS ne le lit pas."""
    monkeypatch.setattr(settings, "CORS_ORIGINS", ["https://cartable.test"])
    client = TestClient(create_app())

    response = client.get(HEALTH, headers={"Origin": "https://cartable.test"})

    exposed = response.headers["access-control-expose-headers"]
    assert REQUEST_ID_HEADER.lower() in exposed.lower()


def test_a_preflight_is_logged_and_correlated(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Le middleware est EN DEHORS du CORS : même un préflight est journalisé."""
    monkeypatch.setattr(settings, "CORS_ORIGINS", ["https://cartable.test"])
    client = TestClient(create_app())

    with caplog.at_level(logging.INFO, logger=ACCESS_LOGGER_NAME):
        response = client.options(
            ME,
            headers={
                "Origin": "https://cartable.test",
                "Access-Control-Request-Method": "GET",
            },
        )

    assert response.status_code == 200
    assert HEX8.match(response.headers[REQUEST_ID_HEADER])
    (record,) = _access(caplog)
    assert record.http_method == "OPTIONS"
