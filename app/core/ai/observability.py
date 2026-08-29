"""Observabilité Langfuse — opt-in total, no-op par défaut.

Active ssi les trois settings ``LANGFUSE_PUBLIC_KEY``/``LANGFUSE_SECRET_KEY``/
``LANGFUSE_HOST`` sont renseignés. Sinon, **le SDK n'est même pas importé**
(import paresseux) : aucune dépendance runtime pour une instance qui n'en veut
pas. Les credentials sont passés explicitement depuis les settings (ils peuvent
venir du YAML/.env, pas forcément de l'environnement du process, que le SDK
lirait sinon par défaut).

Jamais de clé API provider transmise à Langfuse — seuls transitent les
métadonnées de trace (nom, ``sub`` utilisateur) et ce que le CallbackHandler
capture du run LangChain.
"""

from functools import lru_cache
from typing import Any

from app.core.config import settings


def _langfuse_enabled() -> bool:
    return bool(
        settings.LANGFUSE_PUBLIC_KEY and settings.LANGFUSE_SECRET_KEY and settings.LANGFUSE_HOST
    )


@lru_cache
def _get_langfuse_client() -> Any:
    """Client Langfuse partagé (enregistré au registre global du SDK).

    N'appeler que si :func:`_langfuse_enabled` — l'import et la construction
    vivent ici pour rester paresseux.
    """
    from langfuse import Langfuse

    return Langfuse(
        public_key=settings.LANGFUSE_PUBLIC_KEY,
        secret_key=settings.LANGFUSE_SECRET_KEY,
        host=settings.LANGFUSE_HOST,
    )


def build_callbacks() -> list[Any]:
    """Callbacks LangChain à attacher au run — ``[]`` si Langfuse est désactivé."""
    if not _langfuse_enabled():
        return []
    from langfuse.langchain import CallbackHandler

    _get_langfuse_client()  # garantit un client initialisé avec NOS credentials
    return [CallbackHandler(public_key=settings.LANGFUSE_PUBLIC_KEY)]


def build_run_config(trace_name: str | None = None, user_id: str | None = None) -> dict[str, Any]:
    """``RunnableConfig`` à passer à ``ainvoke``/``astream``.

    ``langfuse_user_id`` est la clé de métadonnée reconnue par le
    CallbackHandler pour rattacher la trace à un utilisateur — on y met le
    ``sub`` OIDC, jamais l'e-mail.
    """
    config: dict[str, Any] = {"callbacks": build_callbacks()}
    if trace_name:
        config["run_name"] = trace_name
    if user_id:
        config["metadata"] = {"langfuse_user_id": user_id}
    return config


def shutdown_langfuse() -> None:
    """Flush des traces en attente au shutdown de l'app (no-op si désactivé)."""
    if not _langfuse_enabled():
        return
    _get_langfuse_client().shutdown()
