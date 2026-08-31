"""The full-screen HUD, driven through Textual's pilot."""

from __future__ import annotations

import asyncio
import threading
import types

import pytest

from config import PALETTE_HOUSE_PARTY, PALETTE_STANDARD, STATE_SPEAKING, STATE_THINKING
from jarvis import tui as tui_mod
from jarvis.tui import JarvisTUI


TELEMETRY = types.SimpleNamespace(
    cpu_percent=38.0, ram_percent=71.5, ram_used_gb=11.4, ram_total_gb=16.0,
    disks=[types.SimpleNamespace(percent=94.0, mountpoint="/")],
    gpus=[types.SimpleNamespace(utilization_percent=55.0)],
    battery_percent=76.0, battery_plugged=True, uptime_seconds=7200,
)

METRICS = types.SimpleNamespace(
    ttft=0.31, tokens_per_second=68.0, total=4.10, tool_calls=2,
    tool_seconds_saved=1.2, model_seconds=3.0, tool_seconds=0.4,
    parallel_peak=2, iterations=2, eval_tokens=210,
)


async def _settle(pilot, frames: int = 6) -> None:
    """Give the pump a few frames to drain what the test just queued."""
    for _ in range(frames):
        await pilot.pause()
        await asyncio.sleep(0.05)
    await pilot.pause()


def _kinds(app) -> list[str]:
    return [type(w).__name__ for w in app.query_one("#transcript").children]


async def test_the_hud_offers_everything_the_agent_expects():
    """The TUI is bound in place of StarkHUD, so it has to answer to the same calls."""
    from jarvis.ui import StarkHUD

    expected = [
        name
        for name in vars(StarkHUD)
        if not name.startswith("_") and callable(getattr(StarkHUD, name, None))
    ]
    missing = [name for name in expected if not hasattr(JarvisTUI, name)]
    assert not missing, f"the full-screen HUD is missing {missing}"


async def test_a_streamed_reply_becomes_one_growing_entry():
    app = JarvisTUI()
    async with app.run_test(size=(110, 36)) as pilot:
        await _settle(pilot)
        app.log_user("what is the state of the workshop?")
        app.stream_begin()
        for token in ["The workshop is ", "**nominal**, ", "Sir."]:
            app.stream_token(token)
        await _settle(pilot)

        # One entry, not one per token.
        assert _kinds(app).count("AgentEntry") == 1
        assert app._live_entry is not None
        assert app._live_entry.text == "The workshop is **nominal**, Sir."

        app.stream_end("The workshop is **nominal**, Sir.")
        await _settle(pilot)
        assert app._live_entry is None
        assert _kinds(app).count("AgentEntry") == 1


async def test_tokens_arriving_before_the_widget_exists_are_not_lost():
    """The first chunks land while the entry is still being mounted."""
    app = JarvisTUI()
    async with app.run_test(size=(100, 30)) as pilot:
        await _settle(pilot)
        app.stream_begin()
        app.stream_token("first ")  # queued in the same frame as stream_begin
        app.stream_token("second")
        await _settle(pilot)
        assert app._live_entry.text == "first second"


async def test_narration_alongside_a_tool_call_is_demoted_not_duplicated():
    app = JarvisTUI()
    async with app.run_test(size=(100, 30)) as pilot:
        await _settle(pilot)
        app.stream_begin()
        app.stream_token("I will look that up.")
        await _settle(pilot)
        app.stream_end("I will look that up.", interim=True)
        await _settle(pilot)
        kinds = _kinds(app)
        assert "InterimEntry" in kinds
        assert "AgentEntry" not in kinds


async def test_every_kind_of_transcript_entry_renders():
    alert = types.SimpleNamespace(
        severity="critical", title="Battery", message="Battery at 12 percent, Sir.",
        suggestion="Plug it in.",
    )
    app = JarvisTUI()
    async with app.run_test(size=(110, 36)) as pilot:
        await _settle(pilot)
        app._show_reasoning = True
        app.log_user("(voice) run diagnostics")
        app.log_agent("All clear.")
        app.log_system("Model warm.", "success")
        app.log_interim("One moment.")
        app.log_thought("Deciding which instrument to reach for.")
        app.log_tool_start("web_search", {"query": "arc reactor"})
        app.log_tool("web_search", {"query": "arc reactor"}, "3 results", True)
        app.push_alert(alert)
        app.render_table("Diagnostics", ["Item", "Value"], [["CPU", "38%"]])
        app.render_code("print('hi')", "python", "sample")
        await _settle(pilot)

        kinds = set(_kinds(app))
        for want in (
            "UserEntry", "AgentEntry", "SystemEntry", "InterimEntry", "ThoughtEntry",
            "ToolEntry", "AlertEntry", "TableEntry", "CodeEntry",
        ):
            assert want in kinds, f"{want} never appeared: {sorted(kinds)}"


async def test_a_tool_card_is_updated_in_place_rather_than_repeated():
    app = JarvisTUI()
    async with app.run_test(size=(100, 30)) as pilot:
        await _settle(pilot)
        app.log_tool_start("run_command", {"cmd": "ls"})
        await _settle(pilot)
        cards = [w for w in app.query_one("#transcript").children
                 if type(w).__name__ == "ToolEntry"]
        assert len(cards) == 1 and cards[0].ok is None

        app.log_tool("run_command", {"cmd": "ls"}, "no such thing", False)
        await _settle(pilot)
        cards = [w for w in app.query_one("#transcript").children
                 if type(w).__name__ == "ToolEntry"]
        assert len(cards) == 1, "the result mounted a second card"
        assert cards[0].ok is False
        assert cards[0].has_class("failed")


async def test_the_sidebar_reports_telemetry_and_latency():
    app = JarvisTUI(tools=["a", "b"])
    async with app.run_test(size=(110, 36)) as pilot:
        await _settle(pilot)
        app.set_telemetry(TELEMETRY)
        app.set_metrics(METRICS)
        app.set_model_status("gemma4:31b-cloud · warm")
        app.set_voice_status("listening")
        await _settle(pilot)

        vitals = app.query_one("#vitals")
        assert vitals.telemetry is TELEMETRY
        assert list(vitals._history["cpu"]) == [38.0]
        meter = app.query_one("#meter")
        assert meter.metrics is METRICS and meter.turns == 1
        # Panels grow rows as readings arrive; they have to be re-measured, not
        # merely repainted, or the extra rows are clipped away.
        assert vitals.size.height >= 5


async def test_a_protocol_repaints_the_whole_scheme():
    app = JarvisTUI()
    async with app.run_test(size=(100, 30)) as pilot:
        await _settle(pilot)
        assert tui_mod.INK.primary == tui_mod.INKS[PALETTE_STANDARD].primary
        app.set_palette(PALETTE_HOUSE_PARTY)
        await _settle(pilot)
        assert app.theme == tui_mod.THEMES[PALETTE_HOUSE_PARTY].name
        assert tui_mod.INK.primary == tui_mod.INKS[PALETTE_HOUSE_PARTY].primary
        app.set_palette(PALETTE_STANDARD)
        await _settle(pilot)
        assert tui_mod.INK.primary == tui_mod.INKS[PALETTE_STANDARD].primary


async def test_the_composer_submits_and_remembers():
    sent: list[str] = []
    app = JarvisTUI(on_submit=sent.append)
    async with app.run_test(size=(100, 30)) as pilot:
        await _settle(pilot)
        prompt = app.query_one("#prompt")
        prompt.value = "first question"
        await pilot.press("enter")
        await _settle(pilot)
        assert sent == ["first question"]
        assert prompt.value == ""

        await pilot.press("up")
        await pilot.pause()
        assert prompt.value == "first question"
        await pilot.press("down")
        await pilot.pause()
        assert prompt.value == ""


async def test_a_permission_prompt_can_be_answered_from_another_thread():
    """The broker asks from the agent's thread while the UI keeps running."""
    request = types.SimpleNamespace(
        label="filesystem", action="write", target="/etc/hosts", detail="",
        reversible=False, question=lambda: "May I write there?",
    )
    app = JarvisTUI()
    async with app.run_test(size=(110, 36)) as pilot:
        await _settle(pilot)
        answer: dict[str, str] = {}
        asker = threading.Thread(
            target=lambda: answer.update(value=app.ask_permission(request))
        )
        asker.start()

        for _ in range(80):
            await pilot.pause()
            if len(app.screen_stack) == 2:
                break
            await asyncio.sleep(0.02)
        assert len(app.screen_stack) == 2, "the dialog never appeared"

        await pilot.press("a")
        for _ in range(80):
            await pilot.pause()
            await asyncio.sleep(0.02)
            if answer:
                break
        asker.join(timeout=3)
        assert answer.get("value") == "a"
        assert len(app.screen_stack) == 1


async def test_interrupt_stops_the_turn_while_one_is_running():
    """Ctrl-C cancels work in progress and leaves the session alone."""
    quits: list[bool] = []
    app = JarvisTUI(on_interrupt=lambda: True, on_quit=lambda: quits.append(True))
    async with app.run_test(size=(100, 30)) as pilot:
        await _settle(pilot)
        await pilot.press("ctrl+c")
        await _settle(pilot)
        assert quits == [], "an interrupted turn should not end the session"


async def test_interrupt_falls_through_to_quit_at_an_idle_prompt(monkeypatch):
    """With nothing to interrupt, Ctrl-C means goodbye."""
    quits: list[bool] = []
    app = JarvisTUI(on_interrupt=lambda: False, on_quit=lambda: quits.append(True))
    async with app.run_test(size=(100, 30)) as pilot:
        await _settle(pilot)
        # Stubbed so the harness is not torn down mid-test; the call is the claim.
        exits: list[bool] = []
        monkeypatch.setattr(app, "exit", lambda *a, **k: exits.append(True))
        app.action_interrupt()
        await pilot.pause()
        assert quits == [True]
        assert exits == [True]


async def test_keys_clear_the_transcript_and_hide_the_panel():
    app = JarvisTUI()
    async with app.run_test(size=(110, 36)) as pilot:
        await _settle(pilot)
        app.log_system("something", "info")
        await _settle(pilot)
        assert _kinds(app)

        await pilot.press("ctrl+l")
        await _settle(pilot)
        assert _kinds(app) == []

        await pilot.press("ctrl+b")
        await pilot.pause()
        assert app.query_one("#sidebar").has_class("hidden")
        await pilot.press("ctrl+b")
        await pilot.pause()
        assert not app.query_one("#sidebar").has_class("hidden")


async def test_the_transcript_is_bounded():
    app = JarvisTUI()
    async with app.run_test(size=(100, 30)) as pilot:
        await _settle(pilot)
        for index in range(tui_mod._MAX_ENTRIES + 40):
            app.log_system(f"line {index}", "info")
        await _settle(pilot, frames=40)
        assert len(app.query_one("#transcript").children) <= tui_mod._MAX_ENTRIES
