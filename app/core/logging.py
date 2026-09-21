"""Journalisation : format unique, horodatage UTC, id de corrélation, access-log.

Trois choses vivent ici, et nulle part ailleurs :

1. :func:`configure_logging` — la **seule** configuration de logging du projet,
   partagée par le process uvicorn et par les deux entrypoints hors-API
   (``python -m app.maintenance`` et ``python -m app.maintenance.scheduler``) ;
2. l'**id de corrélation** (un ``ContextVar``), qui préfixe chaque ligne : un
   ``grep`` rassemble tout ce qu'a produit une requête — ou une passe de job de
   maintenance (:func:`correlation_scope`) ;
3. :class:`AccessLogMiddleware` — une ligne par requête HTTP servie, avec sa
   durée.

Format d'une ligne :

.. code-block:: text

    2026-09-21T18:04:11.902Z INFO     3f9a21c7 [app.access] GET /api/v1/courses → 200 en 43 ms
    2026-09-21T01:10:00.004Z INFO     a71e0f42 [app.maintenance.runner] job « … » : 12 en 7776 ms
    2026-09-21T01:12:00.001Z INFO     -        [app.maintenance.scheduler] statut publié

Horodatage **UTC**, comme tous les timestamps du projet : les conteneurs sont en
UTC mais l'uvicorn de dev est en heure locale, et cette ambiguïté-là est un
piège à diagnostic. La colonne d'id est à largeur fixe et vaut ``-`` hors
requête (import, lifespan, CLI), pour que les colonnes restent alignées entre
une ligne d'API et une ligne de scheduler.

⚠ Ce module s'appelle ``logging`` : les imports étant absolus en Python 3,
``import logging.config`` **ici** atteint bien la stdlib. Chez les
consommateurs, toujours ``from app.core.logging import configure_logging`` —
jamais ``from app.core import logging``, qui masquerait la stdlib.
"""

import logging
import logging.config
import re
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.config import settings

# ── Id de corrélation ────────────────────────────────────────────────────────

_CORRELATION_ID: ContextVar[str | None] = ContextVar("correlation_id", default=None)

NO_CORRELATION = "-"
"""Ce qu'affiche la colonne d'id quand il n'y a pas de contexte (hors requête)."""

# 8 hexas : ça se lit dans une colonne, se double-clique, se dicte au téléphone
# et se colle dans un grep. 2**32 possibilités — une collision reste improbable
# et surtout BÉNIGNE : deux grappes de lignes sans rapport partagent une
# étiquette, les horodatages les séparent aussitôt. Ce n'est pas une clé de
# jointure de tracing distribué. Monter à 12 ou 16 = changer cette constante
# (les lignes déjà écrites gardent l'ancienne forme).
ID_LENGTH = 8

# En-tête lu en ENTRÉE et posé en SORTIE, sous le même nom : un client qui en
# envoie un le retrouve, un client qui n'en envoie pas apprend celui qu'on a
# généré. C'est le de-facto standard, celui qu'injecte nginx ($request_id) —
# mais il n'y a pas de proxy dans ce repo, donc un client peut en forger un
# (inoffensif après validation, cf. sanitize_correlation_id).
REQUEST_ID_HEADER = "X-Request-ID"
_REQUEST_ID_HEADER_BYTES = b"x-request-id"

# Jeu volontairement étroit. Ce n'est pas de la cosmétique : la valeur est
# réfléchie dans un en-tête de réponse (un CR/LF et h11 lève — un 500 offert au
# premier curieux) ET écrite dans chaque ligne de log (une fausse ligne de
# journal). Il couvre le $request_id de nginx (32 hexas), un uuid à tirets, un
# ULID, un traceparent.
_VALID_ID = re.compile(r"\A[A-Za-z0-9_.:-]{1,64}\Z")


def new_correlation_id() -> str:
    """Un id de corrélation frais."""
    return uuid.uuid4().hex[:ID_LENGTH]


def sanitize_correlation_id(raw: str | None) -> str | None:
    """L'id fourni s'il est utilisable, sinon ``None`` — l'appelant en génère un.

    Une valeur invalide n'est **jamais** une erreur 400 : un id de corrélation
    ne doit pas faire échouer une requête métier.
    """
    if not raw:
        return None
    value = raw.strip()
    return value if _VALID_ID.match(value) else None


def get_correlation_id() -> str | None:
    """L'id de la requête (ou de la passe de job) en cours, ``None`` hors contexte."""
    return _CORRELATION_ID.get()


def set_correlation_id(value: str) -> Token[str | None]:
    """Pose l'id et rend le jeton à passer à :func:`reset_correlation_id`."""
    return _CORRELATION_ID.set(value)


def reset_correlation_id(token: Token[str | None]) -> None:
    """Rétablit l'id précédent — à appeler dans un ``finally``."""
    _CORRELATION_ID.reset(token)


@contextmanager
def correlation_scope(value: str) -> Iterator[str]:
    """Pose un id le temps d'un bloc, hors requête HTTP (une passe de job).

    Le pendant synchrone de ce que fait :class:`AccessLogMiddleware` autour
    d'une requête. Même mécanique que le ``_CURRENT_TOOL_CALL_ID`` de
    :mod:`app.core.ai.agent` : un ``ContextVar`` suffit, l'API est mono-worker.
    """
    token = _CORRELATION_ID.set(value)
    try:
        yield value
    finally:
        _CORRELATION_ID.reset(token)


class CorrelationIdFilter(logging.Filter):
    """Pose ``record.correlation_id`` sur chaque enregistrement.

    Attaché au **handler**, jamais à un logger : la propagation ne consulte que
    les *handlers* des ancêtres, pas leurs filtres — un filtre posé sur le
    logger racine ne verrait aucun record émis par un logger enfant.

    Un ``extra={"correlation_id": …}`` explicite gagne : l'appelant qui nomme la
    requête à laquelle sa ligne appartient en sait plus que le contexte ambiant
    (c'est ce que fait :class:`AccessLogMiddleware`, dont la ligne devient ainsi
    autoportante — utile à un futur handler JSON, et aux tests, qui n'ont pas ce
    filtre sur le handler de ``caplog``).
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "correlation_id"):
            record.correlation_id = _CORRELATION_ID.get() or NO_CORRELATION
        return True


class UtcFormatter(logging.Formatter):
    """Le formateur du projet : identique à la stdlib, mais horodaté en UTC."""

    converter = time.gmtime


LOG_FORMAT = (
    "%(asctime)s.%(msecs)03dZ %(levelname)-8s %(correlation_id)-8s [%(name)s] %(message)s"
)
LOG_DATEFMT = "%Y-%m-%dT%H:%M:%S"

# ── Configuration ────────────────────────────────────────────────────────────

_LEVEL_NAMES = frozenset({"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"})
_FALLBACK_LEVEL = "INFO"
_configured = False


def configure_logging() -> None:
    """Applique la configuration de logging du projet. **Idempotente**.

    Appelée au **niveau module** de :mod:`app.main` — donc après celle
    d'uvicorn, qui a lieu dans ``Config.__init__`` avant que l'app ne soit
    importée (``Config.load``) : on reprend la main sans ``--log-config``, en
    ``--reload`` comme en direct. Les deux ``__main__`` de maintenance
    l'appellent explicitement.

    Surtout **pas** depuis ``create_app()`` : ``dictConfig`` vide les handlers
    du logger racine, dont celui que pytest y pose pour ``caplog`` — un
    ``make_client()`` sous ``caplog.at_level(...)`` arracherait la capture.
    """
    global _configured
    if _configured:
        return

    requested = settings.LOG_LEVEL.strip().upper()
    level = requested if requested in _LEVEL_NAMES else _FALLBACK_LEVEL

    logging.config.dictConfig(
        {
            "version": 1,
            # NON NÉGOCIABLE : app/main.py importe les routeurs AVANT cet appel
            # (E402 interdit l'inverse), donc app.starter_course.service,
            # app.core.ai.* … existent déjà. À True ils seraient tous
            # DÉSACTIVÉS — exactement les lignes qu'on cherche à récupérer.
            "disable_existing_loggers": False,
            "filters": {"correlation": {"()": CorrelationIdFilter}},
            "formatters": {
                "standard": {
                    "()": UtcFormatter,
                    "format": LOG_FORMAT,
                    "datefmt": LOG_DATEFMT,
                }
            },
            "handlers": {
                "console": {
                    "class": "logging.StreamHandler",
                    "stream": "ext://sys.stderr",
                    "formatter": "standard",
                    "filters": ["correlation"],
                }
            },
            # La racine reste à WARNING : à INFO, httpx journaliserait chaque
            # appel sortant (Zitadel, provider IA) et botocore/apscheduler/
            # langchain deviendraient bavards. Le handler, lui, est en NOTSET —
            # la propagation ne consulte pas le niveau des ancêtres, l'INFO
            # d'app.* passe donc. Corollaire : LOG_LEVEL=DEBUG ne rend verbeux
            # que app.* (déboguer une lib tierce reste un bricolage, cf. TODO).
            "root": {"handlers": ["console"], "level": "WARNING"},
            "loggers": {
                "app": {"level": level},
                # Handlers retirés, propagation rétablie : « Application
                # startup complete » et les tracebacks passent par NOTRE
                # format, horodatés et avec la colonne d'id.
                "uvicorn": {"handlers": [], "propagate": True, "level": "INFO"},
                "uvicorn.error": {"handlers": [], "propagate": True, "level": "INFO"},
                # Muet : son access-log ferait une SECONDE ligne par requête,
                # sans horodatage ni id (la nôtre vient d'AccessLogMiddleware).
                # La propagation reste ouverte pour ne rien perdre s'il émettait
                # un jour un WARNING.
                "uvicorn.access": {
                    "handlers": [],
                    "propagate": True,
                    "level": "WARNING",
                },
            },
        }
    )
    _configured = True

    if level != requested:
        # Après la config, sinon la ligne se perdrait. Même doctrine qu'un cron
        # de maintenance invalide : une faute de frappe dans un YAML ne doit
        # jamais empêcher le démarrage.
        logging.getLogger(__name__).warning(
            "LOG_LEVEL invalide (%r) — %s retenu", settings.LOG_LEVEL, level
        )


# ── Access-log ───────────────────────────────────────────────────────────────

ACCESS_LOGGER_NAME = "app.access"

# Valeurs de query string masquées dans l'access-log. Le token de partage élève
# voyage en `?token=` : le masquer ne coûte AUCUNE information de débogage et
# rend un extrait de log collable dans un ticket sans réfléchir (le nginx
# d'infra, lui, le voit toujours dans ses propres access logs).
# Regex sur les OCTETS de scope["query_string"] : parse_qsl perdrait les valeurs
# vides et réordonnerait, alors qu'on veut la query telle qu'elle est venue.
_SECRET_PARAMS = re.compile(
    rb"(?i)\b(token|access_token|refresh_token|code|key|secret|signature)=[^&]*"
)

# scope["path"] est percent-DÉCODÉ (par uvicorn comme par le TestClient) : sans
# ça, un GET /api/v1/%0A2026-01-01%20ERROR%20… injecterait une fausse ligne de
# journal.
_UNSAFE_CHARS = str.maketrans(dict.fromkeys("\r\n\t\v\f\x00", "?"))
# Une URL peut faire 8 ko (plafond d'uvicorn) : elle ne doit pas noyer la ligne.
_TARGET_MAX_CHARS = 200


def _incoming_id(scope: Scope) -> str | None:
    """La valeur de l'en-tête de corrélation entrant, si le client en a posé un."""
    for name, value in scope.get("headers") or ():
        if name == _REQUEST_ID_HEADER_BYTES:
            return value.decode("latin-1")
    return None


def _safe_target(scope: Scope) -> str:
    """Chemin + query, secrets masqués et sans quoi forger une fausse ligne."""
    target = scope.get("path") or "/"
    query = scope.get("query_string") or b""
    if query:
        redacted = _SECRET_PARAMS.sub(rb"\1=***", query)
        target = f"{target}?{redacted.decode('utf-8', 'replace')}"
    target = target.translate(_UNSAFE_CHARS)
    if len(target) > _TARGET_MAX_CHARS:
        target = f"{target[:_TARGET_MAX_CHARS]}…"
    return target


def _is_event_stream(headers: list[tuple[bytes, bytes]]) -> bool:
    return any(
        name.lower() == b"content-type" and value.lstrip().lower().startswith(b"text/event-stream")
        for name, value in headers
    )


class AccessLogMiddleware:
    """Corrélation et une ligne d'access-log par requête HTTP servie.

    Middleware **ASGI pur** (pas ``BaseHTTPMiddleware``), pour deux raisons :

    * ``call_next`` retourne dès ``http.response.start`` : on mesurerait le
      time-to-first-byte, jamais la durée d'un flux SSE. Ici,
      ``await self.app(...)`` ne rend la main qu'après le dernier
      ``http.response.body`` — **une seule ligne, à la fin, avec la durée
      réelle**. Corollaire : une ``BackgroundTask`` est comptée dans la durée.
    * le ``ContextVar`` est posé en tête de la tâche de requête, donc visible de
      tout ce qui s'exécute en dessous : générateurs SSE compris, et jusque dans
      le threadpool (anyio y copie le contexte) où vivent les appels S3.

    Accessoirement, une exception non rattrapée remonte telle quelle :
    ``ServerErrorMiddleware``, le traceback ``debug`` et le
    ``raise_server_exceptions`` du TestClient gardent leur comportement.
    """

    def __init__(self, app: ASGIApp, *, quiet_paths: frozenset[str] = frozenset()) -> None:
        self.app = app
        # Journalisés en DEBUG plutôt qu'exclus : la sonde /health est appelée
        # toutes les 30 s par les healthchecks des deux composes (~2880 lignes
        # par jour). Invisible à INFO, disponible si on descend le niveau.
        self.quiet_paths = quiet_paths
        self.logger = logging.getLogger(ACCESS_LOGGER_NAME)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # EN PREMIER : le scope `lifespan` traverse la pile des middlewares
        # utilisateur, et il n'a ni méthode ni chemin.
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        correlation_id = sanitize_correlation_id(_incoming_id(scope)) or new_correlation_id()
        token = set_correlation_id(correlation_id)
        started = time.perf_counter()  # monotone : insensible à un réglage d'horloge
        # Si `http.response.start` ne part jamais, on est tombé avant la
        # réponse : 500 est alors la vérité.
        status = 500
        streamed = False

        async def send_wrapper(message: Message) -> None:
            nonlocal status, streamed
            if message["type"] == "http.response.start":
                status = message["status"]
                headers = [
                    (name, value)
                    for name, value in message.get("headers") or ()
                    if name.lower() != _REQUEST_ID_HEADER_BYTES
                ]
                streamed = _is_event_stream(headers)
                headers.append((_REQUEST_ID_HEADER_BYTES, correlation_id.encode("ascii")))
                # Message reconstruit : la liste de Starlette ne nous appartient pas.
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            # Une exception APRÈS le premier octet d'un flux laisse `status` à
            # 200 : on ne le réécrit pas en 500, le client a bien reçu un 200.
            self._log(scope, started=started, status=status, streamed=streamed, failed=True)
            raise
        else:
            self._log(scope, started=started, status=status, streamed=streamed, failed=False)
        finally:
            reset_correlation_id(token)

    def _log(
        self,
        scope: Scope,
        *,
        started: float,
        status: int,
        streamed: bool,
        failed: bool,
    ) -> None:
        if not settings.LOG_ACCESS:
            return
        duration_ms = int((time.perf_counter() - started) * 1000)
        method = scope.get("method") or "?"

        if scope.get("path") in self.quiet_paths:
            level = logging.DEBUG
        elif failed or status >= 500:
            level = logging.WARNING
        elif not streamed and duration_ms >= settings.LOG_SLOW_REQUEST_MS:
            # Les flux SSE sont hors seuil : une génération IA dure une minute
            # par nature, les y soumettre ferait de chaque flux un WARNING et
            # le seuil ne voudrait plus rien dire.
            level = logging.WARNING
        else:
            level = logging.INFO
        if not self.logger.isEnabledFor(level):
            return

        target = _safe_target(scope)
        # Aucun en-tête n'est lu hors x-request-id : « jamais le token dans les
        # logs » est tenu par construction, et un test le verrouille.
        self.logger.log(
            level,
            "%s %s → %d en %d ms%s",
            method,
            target,
            status,
            duration_ms,
            " — exception non rattrapée" if failed else "",
            extra={
                "correlation_id": get_correlation_id() or NO_CORRELATION,
                "http_method": method,
                "http_target": target,
                "http_status": status,
                "duration_ms": duration_ms,
                "http_failed": failed,
            },
        )
