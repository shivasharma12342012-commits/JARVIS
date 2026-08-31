"""The turbo engine: streaming, parallelism, measurement and cancellation."""

from __future__ import annotations

import threading
import time
import types

import pytest

from jarvis import engine as engine_mod
from jarvis.engine import TurboAgent, TurnMetrics, build_agent
from jarvis.tools import ToolRegistry, ToolResult, ToolSpec

from tests.fakes import Chunk, RecordingHUD, client_factory, tool_call

EMPTY_SCHEMA = {"type": "object", "properties": {}}


def _registry(**tools) -> ToolRegistry:
    registry = ToolRegistry()
    for name, func in tools.items():
        registry.register(ToolSpec(name, name, EMPTY_SCHEMA, func))
    return registry


@pytest.fixture
def fake_ollama(monkeypatch):
    """Point the engine at a scripted daemon instead of a real one."""

    def install(rounds):
        factory = client_factory(rounds)
        monkeypatch.setattr(
            engine_mod, "ollama", types.SimpleNamespace(AsyncClient=factory)
        )
        return factory

    return install


def _agent(registry, hud=None) -> TurboAgent:
    agent = TurboAgent(registry, hud=hud)
    # The toolchain probe shells out to three dozen compilers; the prompt it
    # feeds is irrelevant to everything tested here.
    agent._toolchain_summary = ""
    return agent


# ──────────────────────────────────────────────────────────────────── streaming
def test_tokens_reach_the_display_as_they_arrive(fake_ollama):
    fake_ollama([[Chunk("Good "), Chunk("evening, "), Chunk("Sir.")]])
    hud = RecordingHUD()
    agent = _agent(_registry(), hud)
    try:
        reply = agent.chat("hello", speak=False)
    finally:
        agent.close()

    assert reply.text == "Good evening, Sir."
    assert reply.error is None
    # Streamed, not delivered in one lump at the end.
    assert hud.tokens == ["Good ", "evening, ", "Sir."]
    assert hud.names("stream_begin") and hud.names("stream_end")


def test_server_timings_become_the_reported_throughput(fake_ollama):
    fake_ollama([[Chunk("done"), Chunk(eval_count=40, eval_duration=500_000_000)]])
    agent = _agent(_registry())
    try:
        agent.chat("hello", speak=False)
        metrics = agent.metrics
    finally:
        agent.close()

    assert metrics.eval_tokens == 40
    # 40 tokens in half a second.
    assert metrics.server_tokens_per_second == pytest.approx(80.0)
    assert metrics.tokens_per_second == pytest.approx(80.0)
    assert 0 < metrics.ttft < 5.0


# ──────────────────────────────────────────────────────────────────── parallelism
def test_independent_instruments_run_side_by_side(fake_ollama):
    """Two half-second searches should cost half a second, not a whole one."""
    fake_ollama(
        [
            [Chunk(tool_calls=[tool_call("search_a"), tool_call("search_b")])],
            [Chunk("Both done, Sir.")],
        ]
    )

    def slow(name):
        def run():
            time.sleep(0.4)
            return ToolResult(name=name, ok=True, content=f"{name} ok")

        return run

    agent = _agent(_registry(search_a=slow("search_a"), search_b=slow("search_b")))
    try:
        started = time.monotonic()
        reply = agent.chat("look both up", speak=False)
        elapsed = time.monotonic() - started
        metrics = agent.metrics
    finally:
        agent.close()

    assert [record.name for record in reply.tool_calls] == ["search_a", "search_b"]
    assert all(record.result.ok for record in reply.tool_calls)
    assert metrics.tool_seconds < 0.7, "the two instruments were serialised"
    assert metrics.tool_seconds_saved > 0.25
    assert metrics.parallel_peak == 2
    assert elapsed < 0.75


def test_dangerous_instruments_are_never_parallelised():
    """They prompt the operator, and two consent dialogs at once is not a race
    anyone should have to win."""
    registry = ToolRegistry()
    registry.register(ToolSpec("read", "safe", EMPTY_SCHEMA, lambda: None))
    registry.register(
        ToolSpec("wipe", "risky", EMPTY_SCHEMA, lambda: None, dangerous=True)
    )
    agent = _agent(registry)
    try:
        assert agent._parallel_safe("read") is True
        assert agent._parallel_safe("wipe") is False
        assert agent._parallel_safe("execute_protocol") is False
        assert agent._parallel_safe("unknown_tool") is False
    finally:
        agent.close()


def test_tool_results_keep_the_order_the_model_asked_for(fake_ollama):
    """Whatever order they finish in, the replies must line up with the calls."""
    fake_ollama(
        [
            [Chunk(tool_calls=[tool_call("slow"), tool_call("quick")])],
            [Chunk("Finished.")],
        ]
    )

    def slow():
        time.sleep(0.25)
        return ToolResult(name="slow", ok=True, content="slow ok")

    def quick():
        return ToolResult(name="quick", ok=True, content="quick ok")

    agent = _agent(_registry(slow=slow, quick=quick))
    try:
        agent.chat("both", speak=False)
        names = [m.get("name") for m in agent.memory.messages() if m["role"] == "tool"]
    finally:
        agent.close()

    assert names == ["slow", "quick"]


# ──────────────────────────────────────────────────────────────────── control
def test_interrupt_stops_the_turn_promptly(fake_ollama):
    def crawl():
        time.sleep(5.0)
        return ToolResult(name="crawl", ok=True, content="never gets here")

    fake_ollama([[Chunk(tool_calls=[tool_call("crawl")])], [Chunk("done")]])
    agent = _agent(_registry(crawl=crawl))
    try:
        threading.Timer(0.15, agent.interrupt).start()
        started = time.monotonic()
        reply = agent.chat("crawl the disk", speak=False)
        elapsed = time.monotonic() - started
    finally:
        agent.close()

    assert "Stopped at your word" in (reply.error or "")
    assert elapsed < 2.0, "cancellation waited for the tool instead of tearing it down"


def test_a_second_caller_is_told_to_wait(fake_ollama):
    fake_ollama([[Chunk(tool_calls=[tool_call("wait")])], [Chunk("done")]])

    def wait():
        time.sleep(0.6)
        return ToolResult(name="wait", ok=True, content="ok")

    agent = _agent(_registry(wait=wait))
    try:
        thread = threading.Thread(target=agent.chat, args=("first",), kwargs={"speak": False})
        thread.start()
        time.sleep(0.2)
        second = agent.chat("second", speak=False)
        thread.join(timeout=5)
    finally:
        agent.close()

    assert "still working on the previous request" in second.text


def test_warm_up_loads_the_model_before_it_is_needed(fake_ollama):
    factory = fake_ollama([[Chunk("hi")]])
    agent = _agent(_registry())
    try:
        assert agent.warm_up(blocking=True, timeout=5.0) is True
        assert agent.warm is True
        assert factory.holder["client"].generated == 1
    finally:
        agent.close()


# ──────────────────────────────────────────────────────────────────── plumbing
def test_metrics_summary_reads_as_a_status_line():
    metrics = TurnMetrics(
        ttft=0.31, chunks=210, model_seconds=3.4, tool_calls=2,
        tool_seconds_saved=1.2, total=4.1,
    )
    summary = metrics.summary()
    assert "4.10s" in summary and "310ms" in summary and "2 tools" in summary
    assert "saved in parallel" in summary


def test_build_agent_falls_back_when_async_is_unavailable(monkeypatch):
    monkeypatch.setattr(engine_mod, "ollama", types.SimpleNamespace())
    agent = build_agent(_registry(), turbo=True)
    assert type(agent).__name__ == "JarvisAgent"
