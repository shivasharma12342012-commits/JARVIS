"""A real, persistent system shell, driven from the desktop window.

The agentic terminal shows what J.A.R.V.I.S. did. This shows what *you* did —
PowerShell on Windows, the login shell everywhere else — running as a long-lived
child process so that ``cd`` sticks, variables persist and a virtualenv you
activate is still activated on the next line. It is the shell you would have had
in a terminal, in the pane next to the conversation.

How a command is known to have finished. There is no pseudo-terminal here (the
standard library has no portable one, and pulling in a PTY layer for one pane is
not a trade worth making), so the shell never prints a prompt of its own. The
usual answer to that is the right one: after every command a sentinel is written
that carries the exit status and the working directory::

    <whatever you typed>
    <sentinel command>          ->  __JARVIS_a1b2c3__0|C:\\Users\\Shiva

The reader treats that line as the end of the command rather than as output, and
the front end draws a prompt from it. The marker carries a per-session random
suffix, so a command that happens to print the word cannot forge one.

Standard error is merged into standard output on purpose: that is the order a
terminal shows them in, and keeping two streams in sync without a PTY is a
losing game.

This is exactly as dangerous as a terminal, which is to say: completely. It is
reachable only over the loopback interface, only with the session token, and
only when ``DESKTOP_SHELL_ENABLED`` is on. That is the same boundary the
``run_command`` instrument already sits behind — this adds a pane, not a
privilege.
"""

from __future__ import annotations

import logging
import os
import queue
import secrets
import shutil
import signal
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

LOG = logging.getLogger("jarvis.shell")

#: How long to wait for a shell to die politely before killing it.
STOP_GRACE = 3.0
#: Longest single line accepted from the operator. A runaway paste is not input.
MAX_INPUT = 16_000
#: Output lines buffered when nobody is reading. Beyond this the oldest go.
MAX_BACKLOG = 4_000


@dataclass(frozen=True)
class ShellFlavour:
    """One shell, and the three things this module needs to know about it."""

    name: str          #: what to show the operator
    argv: list[str]    #: how to start it, reading commands from stdin
    sentinel: str      #: printf-style template taking the marker
    prompt: str        #: the prompt shape the front end should draw


def _powershell(binary: str, name: str) -> ShellFlavour:
    return ShellFlavour(
        name=name,
        # -Command - reads and runs statements from standard input, and does not
        # echo them, which is what keeps the pane free of your own typing.
        argv=[binary, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", "-"],
        sentinel='Write-Host "{marker}$(if($?){{0}}else{{1}})|$($PWD.Path)"',
        prompt="PS {cwd}> ",
    )


def _posix(binary: str, name: str) -> ShellFlavour:
    return ShellFlavour(
        name=name,
        argv=[binary, "-s"],
        sentinel="printf '{marker}%s|%s\\n' \"$?\" \"$PWD\"",
        prompt="{cwd} $ ",
    )


def detect() -> ShellFlavour | None:
    """The best shell this machine has, or None when it has none.

    Windows prefers PowerShell 7 over Windows PowerShell, and falls back to
    ``cmd`` only if neither is present. Elsewhere the operator's own ``$SHELL``
    comes first, because that is the one their configuration is written for.
    """
    if sys.platform.startswith("win"):
        for binary, label in (("pwsh.exe", "PowerShell"), ("powershell.exe", "Windows PowerShell")):
            found = shutil.which(binary)
            if found:
                return _powershell(found, label)
        found = shutil.which("cmd.exe")
        if found:
            return ShellFlavour(
                name="Command Prompt",
                argv=[found, "/Q", "/K"],
                sentinel="echo {marker}%ERRORLEVEL%^|%CD%",
                prompt="{cwd}> ",
            )
        return None

    candidates = [os.environ.get("SHELL", ""), "bash", "zsh", "sh"]
    for binary in candidates:
        if not binary:
            continue
        found = shutil.which(binary) if not os.path.isabs(binary) else binary
        if found and os.path.exists(found):
            return _posix(found, Path(found).name)
    return None


class ShellSession:
    """One long-lived shell process, its reader thread, and its bookkeeping."""

    def __init__(
        self,
        on_output: Callable[[str], None],
        on_done: Callable[[int, str], None],
        on_exit: Callable[[int | None], None] | None = None,
        cwd: Path | None = None,
        flavour: ShellFlavour | None = None,
    ) -> None:
        self.on_output = on_output
        self.on_done = on_done
        self.on_exit = on_exit
        self.cwd = str(cwd or Path.cwd())
        self.flavour = flavour or detect()
        #: Random per session, so printed text cannot impersonate the sentinel.
        self.marker = f"__JARVIS_{secrets.token_hex(5)}__"
        self.process: subprocess.Popen[str] | None = None
        self.busy = False
        self._reader: threading.Thread | None = None
        self._lock = threading.Lock()
        self._stopping = threading.Event()

    # -- lifecycle ----------------------------------------------------------------------
    @property
    def available(self) -> bool:
        return self.flavour is not None

    @property
    def running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def start(self) -> bool:
        """Spawn the shell. Returns False when this machine has none to spawn."""
        if self.running:
            return True
        if self.flavour is None:
            return False

        kwargs: dict = {}
        if sys.platform.startswith("win"):
            # Its own process group, so an interrupt reaches the child and not
            # the Python process that owns the window.
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            kwargs["start_new_session"] = True

        try:
            self.process = subprocess.Popen(
                self.flavour.argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,   # one stream, in the order a terminal shows
                cwd=self.cwd,
                text=True,
                bufsize=1,                  # line buffered
                encoding="utf-8",
                errors="replace",
                **kwargs,
            )
        except (OSError, ValueError):
            LOG.warning("Could not start %s", self.flavour.name, exc_info=True)
            self.process = None
            return False

        self._stopping.clear()
        self._reader = threading.Thread(target=self._pump, name="jarvis-shell", daemon=True)
        self._reader.start()
        LOG.info("Shell started: %s in %s", self.flavour.name, self.cwd)
        # An empty command settles the working directory and draws the first prompt.
        self.send("")
        return True

    def stop(self) -> None:
        """Close stdin, then terminate, then kill. In that order."""
        self._stopping.set()
        process, self.process = self.process, None
        if process is None:
            return
        try:
            if process.stdin and not process.stdin.closed:
                process.stdin.close()
        except OSError:
            pass
        try:
            process.terminate()
            process.wait(timeout=STOP_GRACE)
        except subprocess.TimeoutExpired:
            process.kill()
        except OSError:
            pass
        LOG.info("Shell stopped")

    def restart(self) -> bool:
        self.stop()
        self.busy = False
        return self.start()

    # -- input --------------------------------------------------------------------------
    def send(self, line: str) -> bool:
        """Run one line. Returns False when there is no shell to run it in."""
        if not self.running and not self.start():
            return False
        process = self.process
        if process is None or process.stdin is None:
            return False

        text = (line or "")[:MAX_INPUT]
        sentinel = self.flavour.sentinel.format(marker=self.marker)  # type: ignore[union-attr]
        with self._lock:
            try:
                # The command and its sentinel go together, so nothing can be
                # interleaved between them by another writer.
                process.stdin.write(text + "\n" + sentinel + "\n")
                process.stdin.flush()
            except (OSError, ValueError, BrokenPipeError):
                LOG.debug("Shell would not take input", exc_info=True)
                return False
        self.busy = True
        return True

    def interrupt(self) -> bool:
        """Ctrl+C, as far as the platform allows."""
        process = self.process
        if process is None or process.poll() is not None:
            return False
        try:
            if sys.platform.startswith("win"):
                process.send_signal(signal.CTRL_BREAK_EVENT)  # type: ignore[attr-defined]
            else:
                os.killpg(os.getpgid(process.pid), signal.SIGINT)
            return True
        except (OSError, ValueError, AttributeError):
            LOG.debug("Interrupt did not reach the shell", exc_info=True)
            return False

    # -- output -------------------------------------------------------------------------
    def _pump(self) -> None:
        """Read the shell forever, splitting sentinels out of the output."""
        process = self.process
        if process is None or process.stdout is None:
            return
        try:
            for raw in process.stdout:
                if self._stopping.is_set():
                    break
                line = raw.rstrip("\n").rstrip("\r")
                if self.marker in line:
                    self._finish(line)
                    continue
                try:
                    self.on_output(line)
                except Exception:
                    LOG.debug("Shell output handler failed", exc_info=True)
        except (OSError, ValueError):
            LOG.debug("Shell reader ended", exc_info=True)
        finally:
            code = process.poll()
            self.busy = False
            if not self._stopping.is_set() and self.on_exit is not None:
                try:
                    self.on_exit(code)
                except Exception:
                    LOG.debug("Shell exit handler failed", exc_info=True)

    def _finish(self, line: str) -> None:
        """Turn a sentinel line into an exit status and a working directory."""
        payload = line.split(self.marker, 1)[1].strip()
        status, _, cwd = payload.partition("|")
        try:
            code = int(status.strip() or 0)
        except ValueError:
            code = 0
        if cwd.strip():
            self.cwd = cwd.strip()
        self.busy = False
        try:
            self.on_done(code, self.cwd)
        except Exception:
            LOG.debug("Shell completion handler failed", exc_info=True)

    # -- description --------------------------------------------------------------------
    def describe(self) -> dict:
        """What the window needs to draw the pane before anything has run."""
        return {
            "available": self.available,
            "name": self.flavour.name if self.flavour else "",
            "prompt": self.flavour.prompt if self.flavour else "",
            "cwd": self.cwd,
            "running": self.running,
            "platform": "windows" if sys.platform.startswith("win") else "posix",
        }


__all__ = ["ShellFlavour", "ShellSession", "detect", "MAX_INPUT"]
