"""The whole application, booted behind the full-screen HUD.

No Ollama daemon is running during a test, which is the point: J.A.R.V.I.S. is
supposed to come up degraded and say so, not fail to start.
"""

from __future__ import annotations

import asyncio

import pytest

import main as jmain


async def _boot(pilot, app, timeout: float = 12.0) -> None:
    """Wait for the boot worker to finish wiring the backend."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        await pilot.pause()
        await asyncio.sleep(0.05)
        if app.agent is not None and app._consumer is not None:
            return
    raise AssertionError("the boot sequence never finished")


def _application():
    args = jmain.parse_args(["--text", "--no-monitor"])
    jmain.apply_overrides(args)
    app = jmain.JarvisApplication(args)
    # stdin is not a terminal under pytest, which is the only reason the
    # full-screen front end would be declined here.
    app.tui_wanted = lambda: True
    return app


async def test_the_full_screen_front_end_boots_and_wires_the_turbo_engine():
    app = _application()
    app.build_frontend()
    assert app.tui is not None
    try:
        async with app.tui.run_test(size=(120, 40)) as pilot:
            await _boot(pilot, app)
            assert type(app.agent).__name__ == "TurboAgent"
            assert len(app.registry.names()) >= 8
            assert app.hud is app.tui
            # The instruments were reported to the sidebar.
            assert app.tui.query_one("#systems").tools
    finally:
        app.shutdown(spoken=False)


async def test_a_slash_command_runs_from_the_composer():
    app = _application()
    app.build_frontend()
    try:
        async with app.tui.run_test(size=(120, 40)) as pilot:
            await _boot(pilot, app)
            app.tui.query_one("#prompt").value = "/tools"
            await pilot.press("enter")

            for _ in range(120):
                await pilot.pause()
                await asyncio.sleep(0.05)
                kinds = [
                    type(w).__name__
                    for w in app.tui.query_one("#transcript").children
                ]
                if "TableEntry" in kinds:
                    break
            assert "TableEntry" in kinds, kinds
    finally:
        app.shutdown(spoken=False)


async def test_a_question_with_no_daemon_degrades_instead_of_hanging():
    app = _application()
    app.build_frontend()
    try:
        async with app.tui.run_test(size=(120, 40)) as pilot:
            await _boot(pilot, app)
            app.tui.query_one("#prompt").value = "good evening"
            await pilot.press("enter")

            for _ in range(160):
                await pilot.pause()
                await asyncio.sleep(0.05)
                kinds = [
                    type(w).__name__
                    for w in app.tui.query_one("#transcript").children
                ]
                if kinds[-2:] == ["UserEntry", "SystemEntry"]:
                    break
            # The question was shown, and the unreachable daemon was reported.
            assert kinds[-2:] == ["UserEntry", "SystemEntry"], kinds[-4:]
    finally:
        app.shutdown(spoken=False)


def test_the_pinned_strip_is_chosen_when_the_terminal_cannot_be_taken_over():
    for argv in (["--classic"], ["--no-hud"], ["--check"], ["--ask", "hello"]):
        args = jmain.parse_args(argv + ["--text"])
        jmain.apply_overrides(args)
        app = jmain.JarvisApplication(args)
        assert app.tui_wanted() is False, argv
        app.build_frontend()
        assert app.tui is None, argv
        assert type(app.hud).__name__ == "StarkHUD", argv


def test_the_synchronous_core_can_still_be_asked_for():
    args = jmain.parse_args(["--text", "--no-turbo", "--classic"])
    jmain.apply_overrides(args)
    app = jmain.JarvisApplication(args)
    app.build_frontend()
    app.wire_backend()
    assert type(app.agent).__name__ == "JarvisAgent"


def test_falling_back_to_the_classic_display_always_says_why():
    """A display that is silently not the one you were promised is the bug."""
    import jarvis.tui as tui_mod

    cases = {
        "--no-hud": "--no-hud",
        "--classic": "",          # asked for by name: nothing to explain
        "--check": "",            # pipe-friendly by design
        "--ask": "",
    }
    for flag, expected in cases.items():
        argv = ["--text", flag] + (["hello"] if flag == "--ask" else [])
        args = jmain.parse_args(argv)
        jmain.apply_overrides(args)
        app = jmain.JarvisApplication(args)
        used, why = app.tui_verdict()
        assert used is False, flag
        if expected:
            assert expected in why, (flag, why)
        else:
            assert why == "", (flag, why)


def test_a_missing_textual_names_the_command_that_fixes_it(monkeypatch):
    monkeypatch.setattr(jmain, "JarvisTUI", None)
    args = jmain.parse_args(["--text"])
    jmain.apply_overrides(args)
    app = jmain.JarvisApplication(args)
    used, why = app.tui_verdict()
    assert used is False
    assert "textual is not installed" in why
    assert "pip install -r requirements.txt" in why


def test_the_reason_reaches_the_screen_not_just_the_log(monkeypatch):
    """It has to be visible where the operator is looking, which is the transcript."""
    monkeypatch.setattr(jmain, "JarvisTUI", None)
    args = jmain.parse_args(["--text", "--no-monitor"])
    jmain.apply_overrides(args)
    app = jmain.JarvisApplication(args)
    app.build_frontend()
    assert "textual is not installed" in app._tui_declined

    said: list[tuple[str, str]] = []
    monkeypatch.setattr(app, "_log_system", lambda text, level="info": said.append((text, level)))
    app.wire_backend()
    app.boot()
    assert any("textual is not installed" in text and level == "warn" for text, level in said), said
