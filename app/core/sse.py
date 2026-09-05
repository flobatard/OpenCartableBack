"""Contrat SSE des routes IA streamées, et helpers d'émission.

Un flux est un **POST** servi en ``text/event-stream`` : le front le lit via
``fetch`` + ``ReadableStream`` (``EventSource`` ne sait ni POSTer ni porter un
Bearer). Format de référence :

.. code-block:: text

    event: token
    data: {"delta": "…"}

    event: thinking
    data: {"delta": "…"}

    event: done
    data: {"usage": {"input_tokens": 12, "output_tokens": 87}}

    event: error
    data: {"status": 503, "detail": "Fournisseur IA injoignable"}

``thinking`` relaie les deltas de raisonnement quand le provider en émet
(absent sinon — le front doit le tolérer). Les routes agent étendent ce
contrat avec ``tool_call``/``tool_result``/``interrupt`` et enrichissent le
payload de ``done`` — voir :mod:`app.course_assistant.streaming` et
:mod:`app.student_exercises.streaming`. Le contrat est **additif** : le
parseur du front tolère les événements inconnus.

JSON compact ``ensure_ascii=False``, chaque événement terminé par ``\\n\\n``,
flux clos après ``done`` ou ``error``. Rationale de l'événement ``error`` : une
erreur survenue APRÈS le premier octet ne peut plus changer le status HTTP
(déjà parti en 200) — l'événement porte donc le status « qu'aurait eu » la
requête (celui du mapping de :mod:`app.core.ai.errors`). Tout ce qui peut
échouer en vraie HTTPException doit donc être résolu AVANT de retourner la
réponse (cascade de config, validation eager du client IA).

Headers : ``Cache-Control: no-store`` et ``X-Accel-Buffering: no`` (le nginx
d'infra ne doit pas bufferiser ; son ``proxy_read_timeout`` doit couvrir une
génération — un keepalive périodique reste à faire, cf. TODO.md).
"""

import json
from collections.abc import AsyncIterator, Mapping
from typing import Any

from fastapi.responses import StreamingResponse

SSE_HEADERS = {
    "Cache-Control": "no-store",
    "X-Accel-Buffering": "no",
}


def sse_event(event: str, data: Mapping[str, Any]) -> str:
    """Un événement SSE sérialisé (JSON compact, terminé par une ligne vide)."""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def sse_response(events: AsyncIterator[str]) -> StreamingResponse:
    """La réponse streamée d'une route IA (media type + headers du contrat)."""
    return StreamingResponse(events, media_type="text/event-stream", headers=SSE_HEADERS)
