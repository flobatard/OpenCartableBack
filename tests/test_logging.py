"""Tests de la configuration de log et de l'id de corrélation (app/core/logging.py).

Unitaires : aucun HTTP ici, le middleware a son propre fichier
(``tests/test_access_log.py``).

⚠ ``configure_logging()`` appelle ``dictConfig``, qui **vide les handlers du
logger racine** — dont celui que pytest y pose pour ``caplog``. Les tests qui la
rejouent (fixture ``reconfigurable``) n'attendent donc rien de ``caplog`` : ils
lisent l'état des loggers, pas des lignes capturées.
"""

import logging
import re

import pytest

from app.core import logging as core_logging
from app.core.config import Settings, settings


@pytest.fixture
def reconfigurable():
    """Rend ``configure_logging()`` rejouable, et rétablit la config du projet.

    Le rétablissement repose le défaut déclaré sur ``Settings`` plutôt que la
    valeur courante : un test a pu la monkeypatcher, et l'ordre de démontage
    des fixtures n'est pas un contrat.
    """
    core_logging._configured = False
    yield
    core_logging._configured = False
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(settings, "LOG_LEVEL", Settings.model_fields["LOG_LEVEL"].default)
        core_logging.configure_logging()


def _record(name: str = "app.demo", level: int = logging.INFO) -> logging.LogRecord:
    return logging.LogRecord(name, level, "x.py", 1, "salut %s", ("monde",), None)


def _format(record: logging.LogRecord) -> str:
    core_logging.CorrelationIdFilter().filter(record)
    formatter = core_logging.UtcFormatter(core_logging.LOG_FORMAT, core_logging.LOG_DATEFMT)
    return formatter.format(record)


# ------------------------------------------------------------- configure_logging


def test_configure_logging_is_idempotent(reconfigurable) -> None:
    core_logging.configure_logging()
    handlers = list(logging.getLogger().handlers)
    core_logging.configure_logging()

    assert list(logging.getLogger().handlers) == handlers
    assert len(handlers) == 1
    assert isinstance(handlers[0].formatter, core_logging.UtcFormatter)


def test_an_invalid_log_level_never_prevents_startup(reconfigurable, monkeypatch) -> None:
    """Même doctrine qu'une expression cron invalide : on retombe, on ne lève pas."""
    monkeypatch.setattr(settings, "LOG_LEVEL", "verbeux")
    core_logging.configure_logging()

    assert logging.getLogger("app").level == logging.INFO


def test_the_log_level_is_normalised(reconfigurable, monkeypatch) -> None:
    monkeypatch.setattr(settings, "LOG_LEVEL", "  debug  ")
    core_logging.configure_logging()

    assert logging.getLogger("app").level == logging.DEBUG


def test_third_party_loggers_stay_quiet(reconfigurable) -> None:
    """La racine à WARNING : httpx ne doit pas journaliser chaque appel sortant."""
    core_logging.configure_logging()

    assert logging.getLogger().level == logging.WARNING
    assert logging.getLogger("httpx").getEffectiveLevel() == logging.WARNING
    # app.* passe quand même en INFO : la propagation ne consulte pas le niveau
    # des ancêtres, seulement celui des handlers (le nôtre est en NOTSET).
    assert logging.getLogger("app.demo").isEnabledFor(logging.INFO)


def test_uvicorn_access_is_muted_and_uvicorn_reuses_our_format(reconfigurable) -> None:
    """Sinon : une SECONDE ligne par requête, sans horodatage ni id."""
    core_logging.configure_logging()

    access = logging.getLogger("uvicorn.access")
    assert access.handlers == []
    assert access.level == logging.WARNING
    for name in ("uvicorn", "uvicorn.error"):
        assert logging.getLogger(name).handlers == []
        assert logging.getLogger(name).propagate is True


def test_existing_loggers_are_never_disabled(reconfigurable) -> None:
    """``disable_existing_loggers: False`` : app/main.py importe les routeurs
    AVANT l'appel, leurs loggers existent déjà — les désactiver supprimerait
    précisément les lignes qu'on cherche à récupérer."""
    early = logging.getLogger("app.starter_course.service")
    core_logging.configure_logging()

    assert early.disabled is False
    assert early.isEnabledFor(logging.WARNING)


# ------------------------------------------------------------------- le format


def test_a_line_without_any_request_falls_back_to_a_dash() -> None:
    record = _record()
    line = _format(record)

    assert record.correlation_id == core_logging.NO_CORRELATION
    assert line.endswith("[app.demo] salut monde")
    # Colonne à largeur fixe : les lignes d'API et de scheduler restent alignées.
    assert f"INFO     {core_logging.NO_CORRELATION:<8} [" in line


def test_a_line_emitted_in_a_scope_carries_its_id() -> None:
    with core_logging.correlation_scope("abc12345"):
        line = _format(_record())

    assert "abc12345 [app.demo] salut monde" in line


def test_timestamps_are_utc_iso8601() -> None:
    """Tous les timestamps du projet sont UTC ; l'uvicorn de dev, lui, est en
    heure locale — cette ambiguïté-là serait un piège à diagnostic."""
    record = _record()
    record.created, record.msecs = 1_700_000_000.5, 500.0

    assert _format(record).startswith("2023-11-14T22:13:20.500Z ")


# ------------------------------------------------- id de corrélation : validation


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "   ",
        "a b",  # espace
        "a\nb",  # ⚠ fausse ligne de journal
        "a\rb",  # ⚠ et h11 lève sur l'en-tête de réponse
        "a\tb",
        "é",  # non-ASCII
        'a"b',
        "a%0Ab",
        "a/b",
        "a" * 65,  # au-delà du plafond
    ],
)
def test_an_unusable_incoming_id_is_refused(raw) -> None:
    assert core_logging.sanitize_correlation_id(raw) is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("3f9a21c7", "3f9a21c7"),
        ("  3f9a21c7  ", "3f9a21c7"),
        ("0123456789abcdef0123456789abcdef", "0123456789abcdef0123456789abcdef"),  # nginx
        ("1e5f4a2b-9c8d-4e7f-8a1b-2c3d4e5f6a7b", "1e5f4a2b-9c8d-4e7f-8a1b-2c3d4e5f6a7b"),
        ("req-1.2:3_4", "req-1.2:3_4"),
        ("a" * 64, "a" * 64),
    ],
)
def test_a_usable_incoming_id_is_kept(raw, expected) -> None:
    assert core_logging.sanitize_correlation_id(raw) == expected


def test_a_fresh_id_is_short_and_unique() -> None:
    ids = {core_logging.new_correlation_id() for _ in range(200)}

    assert len(ids) == 200
    assert all(re.fullmatch(r"[0-9a-f]{8}", value) for value in ids)


# ------------------------------------------------------ id de corrélation : portée


def test_the_scope_nests_and_never_leaks() -> None:
    assert core_logging.get_correlation_id() is None
    with core_logging.correlation_scope("outer"):
        assert core_logging.get_correlation_id() == "outer"
        with core_logging.correlation_scope("inner"):
            assert core_logging.get_correlation_id() == "inner"
        assert core_logging.get_correlation_id() == "outer"
    assert core_logging.get_correlation_id() is None


def test_the_scope_is_restored_after_an_exception() -> None:
    with pytest.raises(RuntimeError), core_logging.correlation_scope("boom"):
        raise RuntimeError("bim")

    assert core_logging.get_correlation_id() is None


# --------------------------------------------------------- assainissement de la cible


def _scope(path: str, query: bytes = b"") -> dict:
    return {"type": "http", "method": "GET", "path": path, "query_string": query}


def test_secret_query_values_are_masked() -> None:
    target = core_logging._safe_target(
        _scope("/api/v1/public/courses/abc", b"token=Xk3pSECRET&q=pythagore&page=2")
    )

    assert target == "/api/v1/public/courses/abc?token=***&q=pythagore&page=2"


def test_a_decoded_newline_cannot_forge_a_log_line() -> None:
    """scope["path"] est percent-DÉCODÉ par uvicorn comme par le TestClient."""
    target = core_logging._safe_target(_scope("/api/v1/\n2026-01-01 ERROR faux log"))

    assert "\n" not in target
    assert target == "/api/v1/?2026-01-01 ERROR faux log"


def test_a_very_long_target_is_truncated() -> None:
    """Une URL peut faire 8 ko : elle ne doit pas noyer la ligne."""
    target = core_logging._safe_target(_scope("/api/v1/" + "x" * 9_000))

    assert len(target) == core_logging._TARGET_MAX_CHARS + 1
    assert target.endswith("…")


def test_a_target_without_query_keeps_its_path_verbatim() -> None:
    assert core_logging._safe_target(_scope("/api/v1/courses")) == "/api/v1/courses"
