"""Tests de la boucle agent (AIClient.stream_agent) et du thinking — aucun réseau.

``GenericFakeChatModel`` ne sait ni streamer des ``tool_calls`` ni exposer
``bind_tools`` : le fake maison :class:`SeqToolModel` (motif validé sur le
``create_agent`` réellement installé) rejoue une séquence d'``AIMessage`` —
contenu découpé en pseudo-tokens, blocs « reasoning » relayés, tool_calls émis
en ``tool_call_chunks`` sur le dernier chunk — et enregistre les messages reçus
à chaque round (assertion du ToolMessage réinjecté).
"""

import json
from typing import Any

import httpx
import pytest
from fastapi import HTTPException
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from pydantic import Field

from app.core.ai import (
    AIClient,
    AIProvider,
    AIRequestConfig,
    AIToolCall,
    AIToolImage,
    AIToolResult,
    AIToolSpec,
    ChatMessage,
)
from app.core.ai import client as client_module
from app.core.ai.client import _to_langchain_messages

MESSAGES = [ChatMessage(role="user", content="Bonjour")]
CONFIG = AIRequestConfig(provider=AIProvider.OLLAMA, model="llama3.2")

READ_BLOCK = AIToolSpec(
    name="read_block",
    description="Lit un bloc du cours",
    parameters={
        "type": "object",
        "properties": {"block_id": {"type": "string"}},
        "required": ["block_id"],
    },
)


class SeqToolModel(BaseChatModel):
    """Rejoue une séquence d'AIMessage, en stream compatible tool-calling."""

    responses: list[AIMessage]
    calls: int = 0
    received: list[list[Any]] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "seq-tool-model"

    def bind_tools(self, tools: Any, **kwargs: Any) -> "SeqToolModel":
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        self.received.append(list(messages))
        message = self.responses[self.calls]
        self.calls += 1
        return ChatResult(generations=[ChatGeneration(message=message)])

    def _stream(self, messages, stop=None, run_manager=None, **kwargs):
        self.received.append(list(messages))
        message = self.responses[self.calls]
        self.calls += 1
        blocks = (
            message.content
            if isinstance(message.content, list)
            else ([{"type": "text", "text": message.content}] if message.content else [])
        )
        for block in blocks:
            if block["type"] == "reasoning":
                yield ChatGenerationChunk(message=AIMessageChunk(content=[block]))
            else:
                for token in block["text"].split(" "):
                    yield ChatGenerationChunk(message=AIMessageChunk(content=token + " "))
        tool_call_chunks = [
            {"name": tc["name"], "args": json.dumps(tc["args"]), "id": tc["id"], "index": i}
            for i, tc in enumerate(message.tool_calls)
        ]
        yield ChatGenerationChunk(
            message=AIMessageChunk(
                content="",
                tool_call_chunks=tool_call_chunks,
                usage_metadata=message.usage_metadata,
            )
        )


def _tool_call_message(block_id: str = "b1", call_id: str = "call_1") -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": "read_block", "args": {"block_id": block_id}, "id": call_id}],
        usage_metadata={"input_tokens": 10, "output_tokens": 3, "total_tokens": 13},
    )


def _final_message(text: str = "Synthèse finale") -> AIMessage:
    return AIMessage(
        content=text,
        usage_metadata={"input_tokens": 20, "output_tokens": 5, "total_tokens": 25},
    )


@pytest.fixture
def fake_build(monkeypatch: pytest.MonkeyPatch):
    """Substitue build_chat_model ; retourne un holder pour poser le modèle."""
    holder: dict[str, Any] = {"model": None}
    monkeypatch.setattr(client_module, "build_chat_model", lambda cfg: holder["model"])
    return holder


async def _ok_executor(call: AIToolCall) -> AIToolResult:
    return AIToolResult(content=f"CONTENU du bloc {call.arguments['block_id']}")


def _agent_events(holder, executor=_ok_executor, max_tool_rounds: int = 5):
    return AIClient().stream_agent(
        MESSAGES, CONFIG, tools=[READ_BLOCK], tool_executor=executor,
        max_tool_rounds=max_tool_rounds,
    )


# ---------------------------------------------------------------- nominal


@pytest.mark.anyio
async def test_agent_without_tool_call(fake_build) -> None:
    fake_build["model"] = SeqToolModel(responses=[_final_message("Bonjour le monde")])
    events = [e async for e in _agent_events(fake_build)]
    tokens = [e for e in events if e.type == "token"]
    assert "".join(t.delta for t in tokens).strip() == "Bonjour le monde"
    assert [e.type for e in events if e.type in ("tool_call", "tool_result")] == []
    assert events[-1].type == "done"
    assert events[-1].usage.input_tokens == 20
    assert events[-1].usage.output_tokens == 5


@pytest.mark.anyio
async def test_agent_one_tool_round(fake_build) -> None:
    """Séquence tool_call → tool_result → tokens, ToolMessage réinjecté, usage cumulé."""
    model = SeqToolModel(responses=[_tool_call_message(), _final_message()])
    fake_build["model"] = model
    events = [e async for e in _agent_events(fake_build)]

    kinds = [e.type for e in events]
    assert kinds.index("tool_call") < kinds.index("tool_result") < kinds.index("token")

    tool_call = next(e for e in events if e.type == "tool_call")
    assert tool_call.tool_call.id == "call_1"
    assert tool_call.tool_call.name == "read_block"
    assert tool_call.tool_call.arguments == {"block_id": "b1"}

    tool_result = next(e for e in events if e.type == "tool_result")
    assert tool_result.tool_call.id == "call_1"
    assert tool_result.tool_result_error is False
    assert tool_result.delta == "CONTENU du bloc b1"

    # Le second round du modèle reçoit bien le ToolMessage du premier.
    round_two = model.received[1]
    tool_messages = [m for m in round_two if isinstance(m, ToolMessage)]
    assert len(tool_messages) == 1
    assert tool_messages[0].tool_call_id == "call_1"
    assert tool_messages[0].content == "CONTENU du bloc b1"

    # Usage cumulé sur les deux rounds.
    assert events[-1].type == "done"
    assert events[-1].usage.input_tokens == 30
    assert events[-1].usage.output_tokens == 8


@pytest.mark.anyio
async def test_agent_thinking_deltas(fake_build) -> None:
    fake_build["model"] = SeqToolModel(
        responses=[
            AIMessage(
                content=[
                    {"type": "reasoning", "reasoning": "je réfléchis"},
                    {"type": "text", "text": "Réponse"},
                ]
            )
        ]
    )
    events = [e async for e in _agent_events(fake_build)]
    thinking = [e for e in events if e.type == "thinking"]
    assert [t.delta for t in thinking] == ["je réfléchis"]
    tokens = "".join(e.delta for e in events if e.type == "token")
    assert tokens.strip() == "Réponse"


@pytest.mark.anyio
async def test_stream_thinking_deltas(fake_build) -> None:
    """Le stream simple (sans agent) relaie aussi les blocs reasoning."""
    fake_build["model"] = SeqToolModel(
        responses=[
            AIMessage(
                content=[
                    {"type": "reasoning", "reasoning": "hmm"},
                    {"type": "text", "text": "Réponse simple"},
                ]
            )
        ]
    )
    events = [e async for e in AIClient().stream(MESSAGES, CONFIG)]
    assert [e.delta for e in events if e.type == "thinking"] == ["hmm"]
    assert "".join(e.delta for e in events if e.type == "token").strip() == "Réponse simple"
    assert events[-1].type == "done"


# ---------------------------------------------------------------- erreurs métier


@pytest.mark.anyio
async def test_agent_tool_error_relayed(fake_build) -> None:
    """is_error=True → tool_result en échec + ToolMessage status=error au modèle."""
    model = SeqToolModel(responses=[_tool_call_message("inconnu"), _final_message()])
    fake_build["model"] = model

    async def failing_executor(call: AIToolCall) -> AIToolResult:
        return AIToolResult(content="Bloc introuvable dans ce cours", is_error=True)

    events = [e async for e in _agent_events(fake_build, executor=failing_executor)]
    tool_result = next(e for e in events if e.type == "tool_result")
    assert tool_result.tool_result_error is True
    assert tool_result.delta == "Bloc introuvable dans ce cours"
    tool_messages = [m for m in model.received[1] if isinstance(m, ToolMessage)]
    assert tool_messages[0].status == "error"
    assert events[-1].type == "done"


@pytest.mark.anyio
async def test_agent_executor_exception_contained(fake_build) -> None:
    """Une exception de l'exécuteur (contrat violé) devient un échec générique."""
    model = SeqToolModel(responses=[_tool_call_message(), _final_message()])
    fake_build["model"] = model

    async def exploding_executor(call: AIToolCall) -> AIToolResult:
        raise RuntimeError("secret interne sk-123")

    events = [e async for e in _agent_events(fake_build, executor=exploding_executor)]
    tool_result = next(e for e in events if e.type == "tool_result")
    assert tool_result.tool_result_error is True
    assert "sk-123" not in tool_result.delta
    assert events[-1].type == "done"


# ---------------------------------------------------------------- images d'outils


def _image_calls_message(call_ids: list[str]) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {"name": "read_block", "args": {"block_id": f"img-{i}"}, "id": call_id}
            for i, call_id in enumerate(call_ids)
        ],
    )


async def _image_executor(call: AIToolCall) -> AIToolResult:
    ref = call.arguments["block_id"]
    return AIToolResult(
        content=f"Image {ref} transmise",
        image=AIToolImage(mime_type="image/png", data="QUJD", caption=f"Légende {ref}"),
    )


@pytest.mark.anyio
async def test_agent_tool_image_attached_after_tool_results(fake_build) -> None:
    """Deux lectures d'images en parallèle : chaque image est jointe au modèle
    dans un message utilisateur, tous placés APRÈS les résultats d'outils du
    round (contiguïté exigée par les providers) ; le flux ne relaie que le
    texte des résultats, jamais un événement pour les messages image."""
    model = SeqToolModel(
        responses=[_image_calls_message(["call_a", "call_b"]), _final_message()]
    )
    fake_build["model"] = model
    events = [e async for e in _agent_events(fake_build, executor=_image_executor)]

    results = [e for e in events if e.type == "tool_result"]
    assert [r.tool_call.id for r in results] == ["call_a", "call_b"]
    assert results[0].delta == "Image img-0 transmise"
    assert results[0].tool_result_error is False
    assert events[-1].type == "done"

    round_two = model.received[1]
    assert [type(m).__name__ for m in round_two] == [
        "HumanMessage",  # la question
        "AIMessage",  # les deux tool_calls
        "ToolMessage",
        "ToolMessage",
        "HumanMessage",  # image a
        "HumanMessage",  # image b
    ]
    assert [m.tool_call_id for m in round_two[2:4]] == ["call_a", "call_b"]
    assert round_two[2].content == "Image img-0 transmise"
    assert round_two[2].artifact is None  # base64 vidé de l'état
    attachment = round_two[4]
    assert attachment.content == [
        {"type": "text", "text": "Légende img-0"},
        {"type": "image", "base64": "QUJD", "mime_type": "image/png"},
    ]
    assert round_two[5].content[0]["text"] == "Légende img-1"


@pytest.mark.anyio
async def test_agent_tool_without_image_unchanged(fake_build) -> None:
    """Sans image, aucun message utilisateur n'est joint (chemin nominal intact)."""
    model = SeqToolModel(responses=[_tool_call_message(), _final_message()])
    fake_build["model"] = model
    [e async for e in _agent_events(fake_build)]
    assert [type(m).__name__ for m in model.received[1]] == [
        "HumanMessage",
        "AIMessage",
        "ToolMessage",
    ]


# ---------------------------------------------------------------- plafond de rounds


@pytest.mark.anyio
async def test_agent_max_tool_rounds(fake_build) -> None:
    """Un modèle qui ne conclut jamais est coupé, avec l'avis de troncature."""
    fake_build["model"] = SeqToolModel(
        responses=[_tool_call_message(call_id=f"call_{i}") for i in range(10)]
    )
    events = [e async for e in _agent_events(fake_build, max_tool_rounds=1)]
    assert events[-1].type == "done"
    tokens = "".join(e.delta for e in events if e.type == "token")
    assert "Limite d'appels au modèle atteinte" in tokens
    # 1 round d'outils + l'appel « final » (qui redemande un outil) : jamais plus.
    assert len([e for e in events if e.type == "tool_call"]) <= 2


# ---------------------------------------------------------------- erreurs de flux


def test_agent_eager_config_error() -> None:
    """Sans config ni fallback, l'erreur sort AVANT tout événement (méthode sync)."""
    with pytest.raises(HTTPException) as exc:
        AIClient().stream_agent(
            MESSAGES, None, tools=[READ_BLOCK], tool_executor=_ok_executor
        )
    assert exc.value.status_code == 422


class _ExplodingToolModel(SeqToolModel):
    def _stream(self, messages, stop=None, run_manager=None, **kwargs):
        yield ChatGenerationChunk(message=AIMessageChunk(content="début "))
        raise httpx.ConnectError("down")


@pytest.mark.anyio
async def test_agent_mid_stream_error_translated(fake_build) -> None:
    fake_build["model"] = _ExplodingToolModel(responses=[_final_message()])
    events = []
    with pytest.raises(HTTPException) as exc:
        async for event in _agent_events(fake_build):
            events.append(event)
    assert exc.value.status_code == 503
    assert [e.type for e in events] == ["token"]


# ---------------------------------------------------------------- conversion d'historique


def test_history_conversion_tool_turns() -> None:
    """Les tours tool/assistant-à-tool_calls rejoués reprennent ids et statuts."""
    history = [
        ChatMessage(role="user", content="Question"),
        ChatMessage(
            role="assistant",
            content="",
            tool_calls=[AIToolCall(id="call_1", name="read_block", arguments={"block_id": "b1"})],
        ),
        ChatMessage(role="tool", content="CONTENU", tool_call_id="call_1"),
        ChatMessage(role="tool", content="Introuvable", tool_call_id="call_2", is_error=True),
        ChatMessage(role="assistant", content="Réponse"),
    ]
    converted = _to_langchain_messages(history)
    assert converted[1].tool_calls == [
        {"name": "read_block", "args": {"block_id": "b1"}, "id": "call_1", "type": "tool_call"}
    ]
    assert isinstance(converted[2], ToolMessage)
    assert converted[2].tool_call_id == "call_1"
    assert converted[2].status == "success"
    assert converted[3].status == "error"
    assert converted[4].content == "Réponse"
    assert not converted[4].tool_calls
