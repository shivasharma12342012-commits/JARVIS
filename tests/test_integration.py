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


# ══════════════════════════════════════════════════════════════════════════════════════
# Colour and the desktop window, from inside a running session
# ══════════════════════════════════════════════════════════════════════════════════════
def _headless(monkeypatch, tmp_path):
    """A wired session on the pinned strip, with its theme file in a sandbox."""
    from jarvis import theme as theme_mod

    monkeypatch.setattr(theme_mod, "THEME_PATH", tmp_path / "theme.json")
    args = jmain.parse_args(["--text", "--no-monitor", "--no-hud"])
    jmain.apply_overrides(args)
    app = jmain.JarvisApplication(args)
    app.tui_wanted = lambda: False
    app.build_frontend()
    app.wire_backend()
    return app


def test_theme_accepts_any_colour_not_just_the_four_palettes(monkeypatch, tmp_path):
    from jarvis import theme as theme_mod

    app = _headless(monkeypatch, tmp_path)
    said: list[tuple[str, str]] = []
    monkeypatch.setattr(app, "_log_system", lambda text, level="info": said.append((text, level)))
    try:
        app._cmd_theme("#ff8c42")
        assert theme_mod.load().seed == "#ff8c42"
        assert any(level == "success" and "#ff8c42" in text for text, level in said), said

        # And a colour name, and a preset, and the dice.
        app._cmd_theme("violet")
        assert theme_mod.load().seed == "#8b5cf6"
        app._cmd_theme("veronica")
        assert theme_mod.load().name == "Veronica"
        app._cmd_theme("surprise")
        assert theme_mod.load().name == "Surprise"
    finally:
        app.shutdown(spoken=False)


def test_a_custom_colour_reaches_the_terminal_palette(monkeypatch, tmp_path):
    """/theme is not a desktop-only setting: the strip recolours too."""
    from jarvis.ui import PALETTES

    app = _headless(monkeypatch, tmp_path)
    try:
        app._cmd_theme("#ff2d55")
        assert "custom" in PALETTES
        assert PALETTES["custom"].accent.startswith("#")
    finally:
        PALETTES.pop("custom", None)
        app.shutdown(spoken=False)


def test_an_unknown_colour_is_explained_rather_than_applied(monkeypatch, tmp_path):
    from jarvis import theme as theme_mod

    app = _headless(monkeypatch, tmp_path)
    said: list[tuple[str, str]] = []
    monkeypatch.setattr(app, "_log_system", lambda text, level="info": said.append((text, level)))
    try:
        app._cmd_theme("aubergine-flavoured")
        assert any(level == "warn" and "#ff8c42" in text for text, level in said), said
        assert not theme_mod.THEME_PATH.exists()
    finally:
        app.shutdown(spoken=False)


def test_desktop_opens_a_window_onto_this_very_session(monkeypatch, tmp_path):
    """One assistant, two front ends -- not a second J.A.R.V.I.S."""
    import urllib.request

    from jarvis import desktop as desktop_mod

    monkeypatch.setenv("JARVIS_DESKTOP_NO_WINDOW", "1")
    app = _headless(monkeypatch, tmp_path)
    terminal_hud = app.hud
    try:
        app._cmd_desktop("")
        window = app.desktop
        assert window is not None

        # The agent now reports into the window, and the window mirrors back
        # into the terminal HUD it replaced.
        assert app.hud is window.hud
        assert window.hud.mirror is terminal_hud
        assert app.agent.hud is window.hud

        # A line typed in the browser joins the terminal's own input queue.
        base = window.url.split("?")[0].rstrip("/")
        request = urllib.request.Request(
            f"{base}/api/chat", data=b'{"text": "from the window"}', method="POST"
        )
        request.add_header("Content-Type", "application/json")
        request.add_header("X-Jarvis-Token", window.token)
        urllib.request.urlopen(request, timeout=5).read()
        assert app._queue.get(timeout=5).text == "from the window"

        # The window knows what this session can do.
        assert set(window.state_payload()["tools"]) == set(app.registry.names())

        # Asking twice does not open a second one.
        app._cmd_desktop("")
        assert app.desktop is window
    finally:
        app.shutdown(spoken=False)


def test_closing_the_window_gives_the_terminal_its_hud_back(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_DESKTOP_NO_WINDOW", "1")
    app = _headless(monkeypatch, tmp_path)
    terminal_hud = app.hud
    try:
        app._cmd_desktop("")
        assert app.hud is not terminal_hud
        app._cmd_desktop("close")
        assert app.desktop is None
        assert app.hud is terminal_hud
        assert app.agent.hud is terminal_hud
    finally:
        app.shutdown(spoken=False)


def test_a_colour_chosen_in_the_window_recolours_the_terminal(monkeypatch, tmp_path):
    import json
    import urllib.request

    from jarvis.ui import PALETTES

    monkeypatch.setenv("JARVIS_DESKTOP_NO_WINDOW", "1")
    app = _headless(monkeypatch, tmp_path)
    try:
        app._cmd_desktop("")
        window = app.desktop
        base = window.url.split("?")[0].rstrip("/")
        request = urllib.request.Request(
            f"{base}/api/theme",
            data=json.dumps({"seed": "#00ff88"}).encode(),
            method="POST",
        )
        request.add_header("Content-Type", "application/json")
        request.add_header("X-Jarvis-Token", window.token)
        urllib.request.urlopen(request, timeout=5).read()

        assert "custom" in PALETTES
        assert PALETTES["custom"].accent.startswith("#")
    finally:
        PALETTES.pop("custom", None)
        app.shutdown(spoken=False)


def test_a_remembered_colour_is_worn_at_startup(monkeypatch, tmp_path):
    from jarvis import theme as theme_mod
    from jarvis.ui import PALETTES

    path = tmp_path / "theme.json"
    monkeypatch.setattr(theme_mod, "THEME_PATH", path)
    theme_mod.save(theme_mod.Theme(name="Mine", seed="#ff2d55"), path)

    args = jmain.parse_args(["--text", "--no-monitor", "--no-hud"])
    jmain.apply_overrides(args)
    app = jmain.JarvisApplication(args)
    app.tui_wanted = lambda: False
    try:
        app.build_frontend()
        assert "custom" in PALETTES
    finally:
        PALETTES.pop("custom", None)
        app.shutdown(spoken=False)


def test_a_fresh_install_keeps_the_shipped_palettes(monkeypatch, tmp_path):
    """No theme file means no derived approximation of the hand-tuned colours."""
    from jarvis import theme as theme_mod
    from jarvis.ui import PALETTES

    PALETTES.pop("custom", None)
    monkeypatch.setattr(theme_mod, "THEME_PATH", tmp_path / "never-written.json")
    args = jmain.parse_args(["--text", "--no-monitor", "--no-hud"])
    jmain.apply_overrides(args)
    app = jmain.JarvisApplication(args)
    app.tui_wanted = lambda: False
    try:
        app.build_frontend()
        assert "custom" not in PALETTES
    finally:
        app.shutdown(spoken=False)
