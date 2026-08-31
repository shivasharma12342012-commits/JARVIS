"""A fake Ollama daemon and a fake HUD, so the agent can be tested with no I/O."""

from __future__ import annotations

import asyncio
import types
from typing import Any, Iterable


class Chunk:
    """One streamed chunk, shaped like an ``ollama.ChatResponse``."""

    def __init__(
        self,
        content: str = "",
        tool_calls: list | None = None,
        thinking: str = "",
        eval_count: int | None = None,
        eval_duration: int | None = None,
    ) -> None:
        self.message = types.SimpleNamespace(
            content=content, thinking=thinking, tool_calls=tool_calls
        )
        self.eval_count = eval_count
        self.eval_duration = eval_duration


def tool_call(name: str, **arguments: Any) -> dict:
    return {"function": {"name": name, "arguments": dict(arguments)}}


class FakeAsyncClient:
    """Replays a scripted list of rounds, one per ``chat`` call."""

    def __init__(self, rounds: Iterable[list[Chunk]] | None = None, **_: Any) -> None:
        self.rounds = [list(r) for r in (rounds or [])]
        self.calls: list[dict] = []
        self.generated = 0

    async def generate(self, **kwargs: Any) -> dict:
        self.generated += 1
        return {}

    async def chat(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        index = min(len(self.calls) - 1, len(self.rounds) - 1)
        chunks = self.rounds[index] if self.rounds else []

        if not kwargs.get("stream"):
            content = "".join(c.message.content for c in chunks)
            calls = [c for chunk in chunks for c in (chunk.message.tool_calls or [])]
            return types.SimpleNamespace(
                message=types.SimpleNamespace(
                    content=content, thinking="", tool_calls=calls
                )
            )

        async def stream() -> Any:
            for chunk in chunks:
                await asyncio.sleep(0)
                yield chunk

        return stream()


def client_factory(rounds: Iterable[list[Chunk]]):
    """A drop-in for ``ollama.AsyncClient`` that always replays ``rounds``."""
    frozen = [list(r) for r in rounds]
    holder: dict[str, FakeAsyncClient] = {}

    def build(**kwargs: Any) -> FakeAsyncClient:
        client = FakeAsyncClient(frozen, **kwargs)
        holder["client"] = client
        return client

    build.holder = holder  # type: ignore[attr-defined]
    return build


class RecordingHUD:
    """Records every HUD call the agent makes, and answers nothing back."""

    def __init__(self) -> None:
        self.tokens: list[str] = []
        self.calls: list[tuple[str, tuple]] = []
        self.metrics: Any = None

    def __getattr__(self, name: str):
        def record(*args: Any, **kwargs: Any) -> None:
            self.calls.append((name, args))

        return record

    def stream_token(self, token: str) -> None:
        self.tokens.append(token)
        self.calls.append(("stream_token", (token,)))

    def set_metrics(self, metrics: Any) -> None:
        self.metrics = metrics
        self.calls.append(("set_metrics", (metrics,)))

    def names(self, kind: str) -> list[tuple]:
        return [args for name, args in self.calls if name == kind]
