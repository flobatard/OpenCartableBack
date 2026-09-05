"""Fakes propres aux tests de l'assistant de cours et du tuteur d'exercice.

Pas un module de tests (pas de préfixe ``test_``) : lignes de données,
faux client IA scriptable et files FIFO des flux, au-dessus des fakes
génériques de :mod:`tests.fakes`.

Ordre FIFO du flux de stream (docstring de ``sse_stream``) : [user] (router),
[course], [conversation], [messages], [user] (cascade ``effective_config``),
[blocks], [resources], [modules] — puis le generator insère le tour. Celui de
la reprise (``sse_resume_stream``) : idem SANS la cascade IA.
"""

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

from sqlalchemy.sql.dml import Insert

from tests.fakes import FakeSession
from tests.fakes import make_client as _make_client

NOW = datetime.now(UTC)
USER_ID = uuid.uuid4()
COURSE_ID = uuid.uuid4()
CONVERSATION_ID = uuid.uuid4()
BLOCK_ID = uuid.uuid4()
RESOURCE_ID = uuid.uuid4()
MODULE_ID = uuid.uuid4()

BASE = f"/api/v1/courses/{COURSE_ID}/assistant"
STREAM_PATH = f"{BASE}/conversations/{CONVERSATION_ID}/messages/stream"


def user_row(**overrides):
    defaults = dict(
        id=USER_ID,
        sub="prof-123",
        email=None,
        ai_provider="ollama",
        ai_model="llama3.2",
        ai_base_url=None,
        ai_api_key_encrypted=None,
        ai_encryption_salt=None,
        ai_daily_call_quota=None,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def course_row():
    return SimpleNamespace(
        id=COURSE_ID,
        owner_id=USER_ID,
        title="Géométrie",
        description=None,
        updated_at=NOW,
    )


def conversation_row(**overrides):
    defaults = dict(
        id=CONVERSATION_ID,
        course_id=COURSE_ID,
        owner_id=USER_ID,
        context="course",
        block_id=None,
        module_id=None,
        title=None,
        created_at=NOW,
        updated_at=NOW,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def message_row(position, role="user", **overrides):
    defaults = dict(
        id=uuid.uuid4(),
        conversation_id=CONVERSATION_ID,
        role=role,
        position=position,
        content=f"Message {position}",
        tool_calls=[],
        tool_call_id="call_x" if role == "tool" else None,
        is_error=False,
        provider=None,
        sources={},
        input_tokens=None,
        output_tokens=None,
        created_at=NOW,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def block_row(**overrides):
    defaults = dict(
        id=BLOCK_ID,
        course_id=COURSE_ID,
        type="text",
        title="Intro",
        description=None,
        content={"markdown": "Pythagore."},
        resource_id=None,
        module_id=None,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def module_row(**overrides):
    defaults = dict(
        id=MODULE_ID,
        course_id=COURSE_ID,
        title="Compteur",
        html="<button id=\"go\">Go</button>",
        css="button { color: red; }",
        js="document.getElementById('go').onclick = () => {};",
        created_at=NOW,
        updated_at=NOW,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def resource_row():
    return SimpleNamespace(
        id=RESOURCE_ID,
        course_id=COURSE_ID,
        original_name="cours.pdf",
        type="document",
        mime="application/pdf",
        size=1234,
        status="available",
        s3_key="k",
    )


class FakeAssistantAI:
    """Faux AIClient pour stream_agent : validation eager scriptable."""

    def __init__(self, events=None, eager_error=None, mid_stream_error=None):
        self.events = events or []
        self.eager_error = eager_error
        self.mid_stream_error = mid_stream_error
        self.calls = []
        self.dropped_threads = []

    def stream_agent(
        self,
        messages,
        config=None,
        *,
        tools,
        tool_executor,
        max_tool_rounds=5,
        thread_id=None,
        resume=None,
        trace_name=None,
        user_id=None,
    ):
        if self.eager_error is not None:
            raise self.eager_error
        self.calls.append(
            {
                "messages": messages,
                "config": config,
                "tools": tools,
                "tool_executor": tool_executor,
                "user_id": user_id,
                "thread_id": thread_id,
                "resume": resume,
            }
        )
        return self._gen()

    def drop_agent_thread(self, thread_id):
        self.dropped_threads.append(thread_id)

    async def _gen(self):
        for event in self.events:
            if self.mid_stream_error is not None and event.type == "done":
                raise self.mid_stream_error
            yield event


def make_client(session, ai_client=None):
    """:func:`tests.fakes.make_client` avec un faux client IA d'assistant ;
    retourne ``(client, fake_ai)``."""
    fake_ai = ai_client or FakeAssistantAI()
    return _make_client(session, ai_client=fake_ai), fake_ai


def inserted_message_rows(session):
    """Les params de l'insert executemany du tour (liste de dicts), ou None."""
    for stmt, params in session.executed:
        if isinstance(stmt, Insert) and isinstance(params, list):
            return params
    return None


def stream_session(messages=(), conversation=None, user=None, blocks=None, modules=()):
    """FIFO de ``sse_stream`` (docstring du module)."""
    return FakeSession(
        [
            [user or user_row()],  # router : get_or_create_by_sub
            [course_row()],
            [conversation or conversation_row()],
            list(messages),
            [user or user_row()],  # cascade effective_config
            list(blocks) if blocks is not None else [block_row()],
            [resource_row()],
            list(modules),
        ]
    )


def resume_session(messages=(), conversation=None, blocks=None, modules=()):
    """FIFO de ``sse_resume_stream`` : [user] (router), [course],
    [conversation], [messages], [blocks], [resources], [modules] — pas de
    cascade IA."""
    return FakeSession(
        [
            [user_row()],
            [course_row()],
            [conversation or conversation_row(context="block_text", block_id=BLOCK_ID, title="T")],
            list(messages),
            list(blocks) if blocks is not None else [block_row()],
            [resource_row()],
            list(modules),
        ]
    )
