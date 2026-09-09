"""The system shell behind the desktop window's Terminal pane.

These tests run a real shell — the one this machine actually has — because the
whole point of the module is that it is not a simulation. They are still fast:
every command is `echo`, `cd` or `exit`, and each session is torn down by its
fixture.
"""

from __future__ import annotations

import sys
import threading
import time

import pytest

from jarvis import shell as shell_mod
from jarvis.shell import ShellSession


def _settle(predicate, timeout: float = 6.0) -> bool:
    """Wait for something the reader thread will eventually make true."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def session():
    """A live shell, with its output and completions collected."""
    out: list[str] = []
    done: list[tuple[int, str]] = []
    exits: list[int | None] = []
    live = ShellSession(
        on_output=out.append,
        on_done=lambda code, cwd: done.append((code, cwd)),
        on_exit=exits.append,
    )
    if not live.available:
        pytest.skip("this machine has no shell to drive")
    assert live.start()
    assert _settle(lambda: len(done) >= 1), "the shell never drew its first prompt"
    live.out, live.done, live.exits = out, done, exits   # type: ignore[attr-defined]
    try:
        yield live
    finally:
        live.stop()


# ══════════════════════════════════════════════════════════════════════════════════════
# Detection
# ══════════════════════════════════════════════════════════════════════════════════════
def test_this_machine_has_a_shell():
    flavour = shell_mod.detect()
    assert flavour is not None
    assert flavour.argv and flavour.name
    assert "{marker}" in flavour.sentinel
    assert "{cwd}" in flavour.prompt


def test_windows_would_be_offered_powershell(monkeypatch):
    """The pane is a PowerShell on Windows, not a cmd shell, when one exists."""
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(shell_mod.shutil, "which",
                        lambda name: r"C:\pwsh.exe" if name == "pwsh.exe" else None)
    flavour = shell_mod.detect()
    assert flavour is not None
    assert flavour.name == "PowerShell"
    assert "-NoProfile" in flavour.argv and "-Command" in flavour.argv
    assert flavour.prompt.startswith("PS ")


def test_windows_falls_back_through_the_shells_it_finds(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(shell_mod.shutil, "which",
                        lambda name: r"C:\powershell.exe" if name == "powershell.exe" else None)
    assert shell_mod.detect().name == "Windows PowerShell"

    monkeypatch.setattr(shell_mod.shutil, "which",
                        lambda name: r"C:\cmd.exe" if name == "cmd.exe" else None)
    assert shell_mod.detect().name == "Command Prompt"

    monkeypatch.setattr(shell_mod.shutil, "which", lambda name: None)
    assert shell_mod.detect() is None


def test_a_machine_with_no_shell_reports_so(monkeypatch):
    monkeypatch.setattr(shell_mod.shutil, "which", lambda name: None)
    monkeypatch.setenv("SHELL", "")
    monkeypatch.setattr(shell_mod.os.path, "exists", lambda path: False)
    live = ShellSession(on_output=lambda t: None, on_done=lambda c, d: None)
    assert live.available is False
    assert live.start() is False
    assert live.send("echo hello") is False


# ══════════════════════════════════════════════════════════════════════════════════════
# Running things
# ══════════════════════════════════════════════════════════════════════════════════════
def test_a_command_runs_and_its_output_is_streamed(session):
    session.send("echo hello-from-the-pane")
    assert _settle(lambda: any("hello-from-the-pane" in line for line in session.out))


def test_a_command_reports_when_it_finished_and_where(session):
    before = len(session.done)
    session.send("echo done")
    assert _settle(lambda: len(session.done) > before)
    code, cwd = session.done[-1]
    assert code == 0 and cwd


def test_a_failing_command_carries_its_exit_status(session):
    before = len(session.done)
    session.send("exit 0" if False else "sh -c 'exit 3'" if not sys.platform.startswith("win") else "cmd /c exit 3")
    assert _settle(lambda: len(session.done) > before)
    assert session.done[-1][0] == 3


def test_the_working_directory_persists_between_commands(session):
    """The whole reason for a long-lived process rather than one per command."""
    start = session.cwd
    session.send("cd ..")
    assert _settle(lambda: session.cwd != start), "cd did not stick"
    deeper = session.cwd
    session.send("echo still-here")
    assert _settle(lambda: any("still-here" in line for line in session.out))
    assert session.cwd == deeper


def test_state_persists_between_commands(session):
    """A variable set on one line is still set on the next."""
    if sys.platform.startswith("win"):
        session.send("$env:JARVIS_PROBE = 'kept'")
        session.send("Write-Host $env:JARVIS_PROBE")
    else:
        session.send("JARVIS_PROBE=kept")
        session.send("echo $JARVIS_PROBE")
    assert _settle(lambda: any("kept" in line for line in session.out))


def test_standard_error_arrives_in_the_same_stream(session):
    session.send("nosuchcommand-here-at-all")
    assert _settle(lambda: any("nosuchcommand-here-at-all" in line for line in session.out))


def test_the_sentinel_is_not_mistaken_for_output(session):
    """A command that prints an exit status must not look like a completion."""
    before = len(session.done)
    session.send("echo 0")
    assert _settle(lambda: len(session.done) > before)
    # One command, exactly one completion.
    assert len(session.done) == before + 1
    assert any(line.strip() == "0" for line in session.out)


def test_output_cannot_forge_a_completion(session):
    """The marker carries a per-session random suffix for exactly this reason."""
    before = len(session.done)
    session.send("echo __JARVIS_00000000__0|/tmp")
    assert _settle(lambda: len(session.done) > before)
    assert len(session.done) == before + 1          # the echo did not count as one
    assert session.cwd != "/tmp" or before == 0     # and did not move us


def test_a_very_long_line_is_truncated_not_refused(session):
    assert session.send("echo " + "x" * (shell_mod.MAX_INPUT * 2)) is True


def test_the_marker_is_unique_per_session():
    a = ShellSession(on_output=lambda t: None, on_done=lambda c, d: None)
    b = ShellSession(on_output=lambda t: None, on_done=lambda c, d: None)
    assert a.marker != b.marker


# ══════════════════════════════════════════════════════════════════════════════════════
# Lifecycle
# ══════════════════════════════════════════════════════════════════════════════════════
def test_describe_says_what_the_window_needs(session):
    described = session.describe()
    assert described["available"] is True
    assert described["running"] is True
    assert described["name"] and described["prompt"] and described["cwd"]
    assert described["platform"] in {"windows", "posix"}


def test_stopping_ends_the_process(session):
    session.stop()
    assert _settle(lambda: not session.running)
    assert session.describe()["running"] is False


def test_stopping_twice_is_harmless(session):
    session.stop()
    session.stop()
    assert not session.running


def test_restart_gives_a_working_shell_back(session):
    session.stop()
    assert session.restart()
    before = len(session.done)
    session.send("echo after-restart")
    assert _settle(lambda: any("after-restart" in line for line in session.out))
    assert len(session.done) > before


def test_sending_to_a_stopped_shell_starts_a_new_one(session):
    session.stop()
    assert _settle(lambda: not session.running)
    assert session.send("echo revived") is True
    assert _settle(lambda: any("revived" in line for line in session.out))


def test_interrupt_does_not_raise_when_there_is_nothing_to_interrupt():
    live = ShellSession(on_output=lambda t: None, on_done=lambda c, d: None)
    assert live.interrupt() is False


def test_a_handler_that_raises_does_not_kill_the_reader():
    """The pane is not allowed to take the shell down with it."""
    seen: list[str] = []

    def hostile(line: str) -> None:
        seen.append(line)
        raise RuntimeError("the window fell over")

    live = ShellSession(on_output=hostile, on_done=lambda c, d: None)
    if not live.available:
        pytest.skip("this machine has no shell to drive")
    live.start()
    try:
        live.send("echo one")
        assert _settle(lambda: any("one" in line for line in seen))
        live.send("echo two")
        assert _settle(lambda: any("two" in line for line in seen))
    finally:
        live.stop()
