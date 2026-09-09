"""J.A.R.V.I.S. Desktop — the windowed front end.

The terminal HUDs are excellent at what they do and completely unable to render
a colour wheel. This module is the third front end: a local web application,
served over the loopback interface and opened in its own chromeless window, that
gives the operator a genuinely graphical J.A.R.V.I.S. — and, more to the point,
lets them pick *any* colour they like and watch the whole interface follow.

Launch it with any of::

    jarvis-desktop                 # the launcher script
    python main.py desktop         # the bare word
    python main.py --desktop       # the flag
    /desktop                       # from inside a running session

Why a local web app rather than Qt or Tk. Three reasons, in order of weight.
The interface has to be *beautiful* and it has to animate at sixty frames a
second, which is CSS's home ground and nobody else's. It has to recolour itself
completely from one operator-chosen seed, which is four lines of custom
properties in CSS and a rewrite in every toolkit. And it must not add a
hundred-megabyte dependency to a program that currently installs from a short
requirements file — so the server here is standard library only: no Flask, no
websockets package, no bundler. Events reach the browser over Server-Sent
Events, which is one long-lived ``GET`` and needs nothing that is not already in
``http.server``.

Two ways to run:

*Standalone.* The app builds its own agent, tools, protocols and monitor exactly
as ``main.py`` does, owns a dispatch thread, and is the only thing driving the
model.

*Attached.* A session already running in the terminal opens a window onto
itself. Input is handed to the existing queue and the existing dispatcher, so a
line typed in the browser and a line typed in the terminal are the same kind of
event and can never interleave.

Security. The server binds to 127.0.0.1 only, mints a random token at startup,
and rejects any request that does not carry it. That matters more than it might
seem: J.A.R.V.I.S. runs shell commands, so an unguarded local port would be a
remote code execution hole for any page in the browser. The ``Host`` header is
checked too, which is what closes the DNS-rebinding version of the same attack.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import os
import queue
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import parse_qs, urlparse

from config import (
    STATE_IDLE,
    STATE_LISTENING,
    STATE_SPEAKING,
    STATE_THINKING,
    STATE_WORKING,
    settings,
)
from jarvis import theme as theme_mod

LOG = logging.getLogger("jarvis.desktop")

#: Where the front end lives on disk. Three files, served verbatim.
WEB_ROOT = Path(__file__).resolve().parent / "web"

#: Loopback only. Never widen this — see the module docstring.
DEFAULT_HOST = "127.0.0.1"
#: 0 asks the operating system for a free port, which avoids the "address
#: already in use" dance when two sessions are open at once.
DEFAULT_PORT = 0

#: How many browser tabs may hold an event stream open at once. Generous for a
#: single-operator desktop app, and a bound on the thread count either way.
MAX_CLIENTS = 8
#: Events buffered per client before the slowest one starts losing history. A
#: tab left in the background for an hour must not grow the server's memory.
CLIENT_BACKLOG = 512
#: Seconds between keep-alive comments on an idle stream. Proxies and some
#: browsers close a silent connection; a colon-comment costs two bytes.
HEARTBEAT_SECONDS = 15.0
#: How long a standalone app waits, after its last window closes, before it
#: stops. Long enough to survive a page reload, short enough to not linger.
IDLE_EXIT_GRACE = 25.0

#: Every host spelling that legitimately means "this machine".
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "[::1]", "::1"}


# ══════════════════════════════════════════════════════════════════════════════════════
# Event fan-out
# ══════════════════════════════════════════════════════════════════════════════════════
@dataclass
class Event:
    """One thing that happened, on its way to every open window."""

    kind: str
    data: dict[str, Any] = field(default_factory=dict)
    seq: int = 0

    def encode(self) -> bytes:
        """Render as a Server-Sent Events frame."""
        payload = json.dumps({"kind": self.kind, "seq": self.seq, **self.data})
        return f"id: {self.seq}\nevent: {self.kind}\ndata: {payload}\n\n".encode("utf-8")


class EventHub:
    """Thread-safe publish/subscribe between the agent and the open windows.

    The agent's callbacks arrive on whichever thread happens to be running the
    turn; the HTTP handlers each sit on their own. A lock and one queue per
    subscriber is the whole design — a slow client drops its oldest events
    rather than blocking the agent, which is the correct trade for a transcript.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: list[queue.Queue[Event | None]] = []
        self._seq = 0
        #: Replayed to any window that opens later, so a tab reloaded mid-turn
        #: comes back with the transcript rather than an empty screen.
        self._history: list[Event] = []
        self._history_limit = 400

    def publish(self, kind: str, **data: Any) -> None:
        """Send one event to every subscriber. Never raises, never blocks."""
        with self._lock:
            self._seq += 1
            event = Event(kind=kind, data=data, seq=self._seq)
            if kind not in _EPHEMERAL_EVENTS:
                self._history.append(event)
                if len(self._history) > self._history_limit:
                    del self._history[: len(self._history) - self._history_limit]
            targets = list(self._subscribers)
        for sink in targets:
            try:
                sink.put_nowait(event)
            except queue.Full:
                # Drop the oldest and try once more. If that fails too the
                # client is beyond help and will resynchronise on reload.
                try:
                    sink.get_nowait()
                    sink.put_nowait(event)
                except (queue.Empty, queue.Full):
                    pass

    def subscribe(self) -> queue.Queue[Event | None] | None:
        """Register a new window. None when the client limit is reached."""
        sink: queue.Queue[Event | None] = queue.Queue(maxsize=CLIENT_BACKLOG)
        with self._lock:
            if len(self._subscribers) >= MAX_CLIENTS:
                return None
            self._subscribers.append(sink)
        return sink

    def unsubscribe(self, sink: queue.Queue[Event | None]) -> None:
        """Drop a window that has gone away."""
        with self._lock:
            if sink in self._subscribers:
                self._subscribers.remove(sink)

    def history(self) -> list[Event]:
        """The replayable backlog, oldest first."""
        with self._lock:
            return list(self._history)

    def clear_history(self) -> None:
        """Forget the backlog — paired with a transcript wipe."""
        with self._lock:
            self._history.clear()

    @property
    def client_count(self) -> int:
        with self._lock:
            return len(self._subscribers)

    def close(self) -> None:
        """Release every stream so its thread can end."""
        with self._lock:
            targets, self._subscribers = list(self._subscribers), []
        for sink in targets:
            try:
                sink.put_nowait(None)
            except queue.Full:
                pass


#: Events not worth replaying to a window that opens later: they describe an
#: instant, not a fact, and a stale one would be actively misleading.
_EPHEMERAL_EVENTS = {
    "telemetry", "amplitude", "token", "heartbeat", "state",
    # The shell keeps its own scrollback in the pane; replaying a thousand
    # lines of it into a reloaded tab would be neither useful nor cheap.
    "shell_out", "shell_done", "shell_exit",
}


# ══════════════════════════════════════════════════════════════════════════════════════
# The HUD adapter
# ══════════════════════════════════════════════════════════════════════════════════════
class DesktopHUD:
    """Speaks the HUD protocol; renders to a browser instead of a terminal.

    ``core.py`` calls a fixed set of methods on whatever it was handed as its
    HUD — ``stream_token``, ``log_tool_start``, ``set_state`` and so on. This
    class implements that surface and turns every call into an event. It is
    duck-typed on purpose: the agent has no idea it is not talking to a terminal.

    Interactive methods (``prompt_input``, ``ask_permission``) have to block a
    background thread until a human clicks something in the window, which is
    what ``_pending`` is for.
    """

    def __init__(self, hub: EventHub, mirror: Any = None) -> None:
        self.hub = hub
        #: An optional second HUD — the terminal one — kept in step, so an
        #: attached session shows the same transcript in both places.
        self.mirror = mirror
        self.state = STATE_IDLE
        self._streaming = False
        self._pending: dict[str, tuple[threading.Event, list[Any]]] = {}
        self._pending_lock = threading.Lock()

    # -- the mirror ---------------------------------------------------------------------
    def _mirrored(self, method: str, *args: Any, **kwargs: Any) -> None:
        """Forward one call to the terminal HUD, if there is one."""
        if self.mirror is None:
            return
        handler = getattr(self.mirror, method, None)
        if not callable(handler):
            return
        try:
            handler(*args, **kwargs)
        except Exception:
            LOG.debug("Mirror HUD refused %s", method, exc_info=True)

    # -- transcript ---------------------------------------------------------------------
    def log_user(self, text: str) -> None:
        self.hub.publish("user", text=str(text))
        self._mirrored("log_user", text)

    def log_agent(self, text: str, markdown: bool = True) -> None:
        self.hub.publish("agent", text=str(text), markdown=bool(markdown))
        self._mirrored("log_agent", text, markdown)

    def log_system(self, text: str, level: str = "info") -> None:
        self.hub.publish("system", text=str(text), level=str(level))
        self._mirrored("log_system", text, level)

    def log_interim(self, text: str) -> None:
        self.hub.publish("interim", text=str(text))
        self._mirrored("log_interim", text)

    def log_thought(self, text: str) -> None:
        self.hub.publish("thought", text=str(text))
        self._mirrored("log_thought", text)

    def log_tool_start(self, name: str, arguments: Any = None) -> None:
        self.hub.publish("tool_start", name=str(name), arguments=_jsonable(arguments))
        self._mirrored("log_tool_start", name, arguments)

    def log_tool(self, name: str, result: Any = None, *args: Any, **kwargs: Any) -> None:
        self.hub.publish(
            "tool_end",
            name=str(name),
            ok=bool(getattr(result, "ok", True)),
            summary=_short(getattr(result, "output", result)),
        )
        self._mirrored("log_tool", name, result, *args, **kwargs)

    def push_alert(self, alert: Any) -> None:
        self.hub.publish(
            "alert",
            title=str(getattr(alert, "title", "") or getattr(alert, "kind", "Alert")),
            text=str(getattr(alert, "message", alert)),
            severity=str(getattr(alert, "severity", "info")),
        )
        self._mirrored("push_alert", alert)

    def render_table(self, title: str, columns: Any = (), rows: Any = ()) -> None:
        """Publish a table the window can actually draw, not just mirror it."""
        self.hub.publish(
            "table",
            title=str(title or ""),
            columns=[str(c) for c in (columns or [])],
            rows=[[str(cell) for cell in row] for row in (rows or [])],
        )
        self._mirrored("render_table", title, columns, rows)

    def render_code(self, code: str, language: str = "python", title: str = "") -> None:
        self.hub.publish("code", code=str(code), language=str(language), title=str(title))
        self._mirrored("render_code", code, language, title)

    def clear_transcript(self) -> None:
        self.hub.clear_history()
        self.hub.publish("clear")
        self._mirrored("clear_transcript")

    # -- streaming ----------------------------------------------------------------------
    def stream_begin(self) -> None:
        self._streaming = True
        self.hub.publish("stream_begin")
        self._mirrored("stream_begin")

    def stream_token(self, token: str) -> None:
        # Deliberately not buffered: the point of the desktop app is that words
        # appear as the model produces them.
        self.hub.publish("token", text=str(token))
        self._mirrored("stream_token", token)

    def stream_end(self, final_text: str | None = None, interim: bool = False) -> None:
        self._streaming = False
        self.hub.publish("stream_end", text=str(final_text or ""), interim=bool(interim))
        self._mirrored("stream_end", final_text, interim)

    # -- instruments --------------------------------------------------------------------
    def set_state(self, state: str, detail: str = "") -> None:
        self.state = state
        self.hub.publish("state", state=str(state), detail=str(detail))
        self._mirrored("set_state", state, detail)

    def set_amplitude(self, value: float) -> None:
        self.hub.publish("amplitude", value=float(value))
        self._mirrored("set_amplitude", value)

    def set_telemetry(self, telemetry: Any) -> None:
        self.hub.publish("telemetry", **_telemetry_payload(telemetry))
        self._mirrored("set_telemetry", telemetry)

    def set_protocol(self, name: str | None) -> None:
        self.hub.publish("protocol", name=str(name or ""))
        self._mirrored("set_protocol", name)

    def set_voice_status(self, text: str) -> None:
        self.hub.publish("voice_status", text=str(text))
        self._mirrored("set_voice_status", text)

    def set_model_status(self, text: str) -> None:
        self.hub.publish("model_status", text=str(text))
        self._mirrored("set_model_status", text)

    def set_metrics(self, metrics: Any) -> None:
        payload = metrics.as_dict() if hasattr(metrics, "as_dict") else _jsonable(metrics)
        summary = metrics.summary() if hasattr(metrics, "summary") else ""
        self.hub.publish("metrics", metrics=payload, summary=str(summary))
        self._mirrored("set_metrics", metrics)

    def set_warm(self, warm: bool) -> None:
        self.hub.publish("warm", warm=bool(warm))
        self._mirrored("set_warm", warm)

    def set_palette(self, name: str) -> None:
        """A protocol changed the posture; recolour the window to match."""
        found = theme_mod.preset(name)
        if found is not None:
            self.hub.publish("theme", theme=found.to_dict(), variables=found.css_variables())
        self._mirrored("set_palette", name)

    def print_banner(self) -> None:
        self._mirrored("print_banner")

    def start(self) -> None:
        self._mirrored("start")

    def stop(self) -> None:
        self._mirrored("stop")

    # -- questions that block on a human -------------------------------------------------
    def ask_permission(self, request: Any) -> str:
        """Put a consent card in the window and wait for the operator's answer.

        Falls back to the terminal's own prompt when no window is open, and to a
        refusal when there is nobody to ask at all — the safe default for
        something about to touch the machine.
        """
        if self.hub.client_count == 0:
            if self.mirror is not None and hasattr(self.mirror, "ask_permission"):
                return self.mirror.ask_permission(request)
            return "n"
        question = getattr(request, "question", None)
        answer = self._ask(
            "permission",
            {
                "question": str(question() if callable(question) else question or request),
                "scope": str(getattr(request, "label", "") or getattr(request, "scope", "")),
                "action": str(getattr(request, "action", "")),
                "target": str(getattr(request, "target", "")),
                "detail": str(getattr(request, "detail", "")),
                "reversible": bool(getattr(request, "reversible", True)),
            },
            timeout=180.0,
        )
        return str(answer or "n")

    def confirm(self, question: str) -> bool:
        if self.hub.client_count == 0:
            if self.mirror is not None and hasattr(self.mirror, "confirm"):
                return bool(self.mirror.confirm(question))
            return False
        return str(self._ask("confirm", {"question": str(question)}, timeout=120.0)).lower() in {
            "y", "yes", "true", "1",
        }

    def choose(self, question: str, options: Iterable[Any], *args: Any, **kwargs: Any) -> Any:
        items = [str(option) for option in options]
        if self.hub.client_count == 0:
            if self.mirror is not None and hasattr(self.mirror, "choose"):
                return self.mirror.choose(question, options, *args, **kwargs)
            return items[0] if items else ""
        answer = self._ask("choose", {"question": str(question), "options": items}, timeout=120.0)
        return answer if answer in items else (items[0] if items else "")

    def prompt_input(self, prompt_text: str = "") -> str:
        """Ask for a line of input.

        With a mirror there is a terminal behind this window, and that terminal
        owns its console reader — asking the browser as well would give the
        session two mouths and one queue. Standalone, the window *is* the
        console, so the question goes there.
        """
        if self.mirror is not None and hasattr(self.mirror, "prompt_input"):
            return str(self.mirror.prompt_input(prompt_text))
        return str(self._ask("prompt", {"prompt": str(prompt_text)}, timeout=None) or "")

    def _ask(self, kind: str, payload: dict[str, Any], timeout: float | None) -> Any:
        """Publish a question, park this thread, and wait for ``resolve``."""
        token = secrets.token_hex(8)
        gate, box = threading.Event(), []
        with self._pending_lock:
            self._pending[token] = (gate, box)
        try:
            self.hub.publish("ask", ask=kind, token=token, **payload)
            if not gate.wait(timeout):
                LOG.info("No answer to a %s question after %ss", kind, timeout)
                return None
            return box[0] if box else None
        finally:
            with self._pending_lock:
                self._pending.pop(token, None)

    def resolve(self, token: str, answer: Any) -> bool:
        """Deliver a window's answer back to the thread waiting on it."""
        with self._pending_lock:
            entry = self._pending.get(token)
        if entry is None:
            return False
        gate, box = entry
        box.append(answer)
        gate.set()
        self.hub.publish("ask_done", token=token)
        return True

    def cancel_pending(self) -> None:
        """Release every parked question — used on shutdown."""
        with self._pending_lock:
            entries = list(self._pending.values())
            self._pending.clear()
        for gate, _ in entries:
            gate.set()


def _short(value: Any, limit: int = 240) -> str:
    """One-line, length-capped rendering of a tool result."""
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _jsonable(value: Any) -> Any:
    """Best-effort conversion of anything into something ``json`` will take."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    if hasattr(value, "as_dict"):
        try:
            return _jsonable(value.as_dict())
        except Exception:
            pass
    return str(value)


def _telemetry_payload(telemetry: Any) -> dict[str, Any]:
    """The handful of numbers the instrument rail actually draws."""
    if telemetry is None:
        return {}
    disks = getattr(telemetry, "disks", None) or []
    first_disk = disks[0] if disks else None
    return {
        "cpu": _number(getattr(telemetry, "cpu_percent", None)),
        "cores": [_number(c) for c in (getattr(telemetry, "cpu_per_core", None) or [])][:32],
        "ram": _number(getattr(telemetry, "ram_percent", None)),
        "ram_used_gb": _number(getattr(telemetry, "ram_used_gb", None)),
        "ram_total_gb": _number(getattr(telemetry, "ram_total_gb", None)),
        "disk": _number(getattr(first_disk, "percent", None)),
        "battery": _number(getattr(telemetry, "battery_percent", None)),
        "plugged": getattr(telemetry, "battery_plugged", None),
        "processes": _number(getattr(telemetry, "process_count", None)),
        "uptime": _number(getattr(telemetry, "uptime_seconds", None)),
        "platform": str(getattr(telemetry, "platform", "") or ""),
    }


def _number(value: Any) -> float | None:
    """Coerce to float, or None — the front end draws a dash for None."""
    try:
        return None if value is None else round(float(value), 2)
    except (TypeError, ValueError):
        return None


# ══════════════════════════════════════════════════════════════════════════════════════
# The workspace
#
# What the file browser and the code viewer are allowed to see. Which is: the
# workspace, and nothing else. Every path the window asks for is resolved and
# then checked against the root before a single byte is read, because the
# alternative is a file browser that will happily serve ``../../.ssh/id_rsa`` to
# anything holding the session token.
# ══════════════════════════════════════════════════════════════════════════════════════
#: Directories never worth showing an operator. Skipped in listings entirely.
HIDDEN_DIRS = {
    ".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    "node_modules", ".venv", "venv", ".idea", ".vscode", ".tox", "dist", "build",
    ".jarvis_cache", ".DS_Store",
}

#: Extension to language name, for the code viewer's highlighter and its header.
LANGUAGES: dict[str, str] = {
    ".py": "python", ".pyi": "python", ".js": "javascript", ".mjs": "javascript",
    ".ts": "typescript", ".tsx": "typescript", ".jsx": "javascript",
    ".json": "json", ".html": "html", ".htm": "html", ".css": "css",
    ".scss": "css", ".md": "markdown", ".markdown": "markdown", ".rs": "rust",
    ".go": "go", ".c": "c", ".h": "c", ".cpp": "cpp", ".cc": "cpp", ".hpp": "cpp",
    ".java": "java", ".kt": "kotlin", ".swift": "swift", ".rb": "ruby",
    ".php": "php", ".sh": "bash", ".bash": "bash", ".zsh": "bash", ".fish": "bash",
    ".ps1": "powershell", ".bat": "batch", ".sql": "sql", ".yml": "yaml",
    ".yaml": "yaml", ".toml": "toml", ".ini": "ini", ".cfg": "ini", ".env": "ini",
    ".xml": "xml", ".svg": "xml", ".lua": "lua", ".r": "r", ".jl": "julia",
    ".ex": "elixir", ".exs": "elixir", ".hs": "haskell", ".scala": "scala",
    ".dart": "dart", ".vim": "vim", ".txt": "text", ".log": "text", ".cfg": "ini",
}

#: Read no more than this from one file. The viewer says when it truncated.
MAX_FILE_BYTES = 400_000
#: A directory with more entries than this is listed up to here and marked.
MAX_DIR_ENTRIES = 400


#: Interpreters worth recognising from a shebang. An extensionless script is
#: normal on Unix — `jarvis-desktop` is one — and colouring it as plain text
#: when its first line says otherwise is a small, avoidable failure.
_SHEBANGS: tuple[tuple[str, str], ...] = (
    ("python", "python"), ("bash", "bash"), ("zsh", "bash"), ("sh", "bash"),
    ("node", "javascript"), ("ruby", "ruby"), ("perl", "perl"), ("php", "php"),
)


def _shebang_language(text: str) -> str:
    """The language a `#!` line names, or ``text`` when there is not one."""
    first = text[:200].split("\n", 1)[0]
    if not first.startswith("#!"):
        return "text"
    for needle, language in _SHEBANGS:
        if needle in first:
            return language
    return "bash"


class Workspace:
    """Read-only, sandboxed access to the operator's working directory."""

    def __init__(self, root: Path | None = None) -> None:
        self._root = root

    @property
    def root(self) -> Path:
        """Resolved every time: an operator may retarget the workspace mid-session."""
        if self._root is not None:
            return self._root.resolve()
        return Path(settings.WORKSPACE_ROOT).resolve()

    def resolve(self, relative: str) -> Path | None:
        """Turn a browser-supplied path into a real one, or None if it escapes.

        ``None`` covers every refusal — traversal, absolute paths, symlinks
        pointing out of the tree — so callers have exactly one thing to check.
        """
        root = self.root
        try:
            candidate = (root / (relative or "").lstrip("/\\")).resolve()
        except (OSError, ValueError, RuntimeError):
            return None
        # resolve() has already followed every symlink and collapsed every "..",
        # so this one comparison is the whole boundary. tools.py makes the same
        # check; it is not imported here because doing so would drag httpx and
        # the ollama client into a module that needs neither.
        try:
            return candidate if candidate == root or candidate.is_relative_to(root) else None
        except ValueError:
            return None

    def listing(self, relative: str = "") -> dict[str, Any]:
        """One directory, directories first, then files, both alphabetical."""
        target = self.resolve(relative)
        if target is None or not target.is_dir():
            return {"error": "no such directory"}

        root = self.root
        entries: list[dict[str, Any]] = []
        try:
            children = sorted(
                target.iterdir(), key=lambda p: (p.is_file(), p.name.lower())
            )
        except OSError:
            return {"error": "that directory cannot be read"}

        for child in children[:MAX_DIR_ENTRIES]:
            if child.name in HIDDEN_DIRS or child.name.endswith((".pyc", ".pyo")):
                continue
            try:
                is_dir = child.is_dir()
                size = 0 if is_dir else child.stat().st_size
            except OSError:
                continue
            entries.append(
                {
                    "name": child.name,
                    "path": str(child.relative_to(root)).replace("\\", "/"),
                    "kind": "dir" if is_dir else "file",
                    "size": size,
                    "language": LANGUAGES.get(child.suffix.lower(), "") if not is_dir else "",
                }
            )

        rel = "" if target == root else str(target.relative_to(root)).replace("\\", "/")
        parent = "" if not rel else str(Path(rel).parent).replace("\\", "/")
        return {
            "path": rel,
            "parent": "" if parent == "." else parent,
            "root": root.name,
            "atRoot": rel == "",
            "entries": entries,
            "truncated": len(children) > MAX_DIR_ENTRIES,
        }

    def read(self, relative: str) -> dict[str, Any]:
        """One file's text, capped, with the language the viewer should colour it as."""
        target = self.resolve(relative)
        if target is None or not target.is_file():
            return {"error": "no such file"}
        try:
            raw = target.read_bytes()[: MAX_FILE_BYTES + 1]
        except OSError:
            return {"error": "that file cannot be read"}

        truncated = len(raw) > MAX_FILE_BYTES
        raw = raw[:MAX_FILE_BYTES]
        # A NUL byte in the first block is the oldest binary test there is, and
        # still the right one: it beats guessing from the extension.
        if b"\x00" in raw[:8192]:
            return {
                "path": relative,
                "name": target.name,
                "binary": True,
                "size": target.stat().st_size,
                "content": "",
            }
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("utf-8", errors="replace")

        return {
            "path": str(target.relative_to(self.root)).replace("\\", "/"),
            "name": target.name,
            "language": LANGUAGES.get(target.suffix.lower()) or _shebang_language(text),
            "content": text,
            "lines": text.count("\n") + 1,
            "size": len(raw),
            "truncated": truncated,
            "binary": False,
        }


# ══════════════════════════════════════════════════════════════════════════════════════
# Backends
#
# The window needs somewhere to send what the operator types. Two shapes exist:
# one that owns a whole J.A.R.V.I.S. and one that hands lines to a session that
# is already running in a terminal.
# ══════════════════════════════════════════════════════════════════════════════════════
class Backend:
    """What :class:`DesktopApp` requires of whatever is driving the assistant."""

    #: Set by the app once the HUD exists, so a backend can talk to the window.
    hud: DesktopHUD | None = None

    def submit(self, text: str) -> None:
        """Accept one line from the window. Must not block the HTTP thread."""
        raise NotImplementedError

    def interrupt(self) -> None:
        """Stop whatever is in flight."""

    def snapshot(self) -> dict[str, Any]:
        """Everything the window shows before the first message arrives."""
        return {}

    def start(self) -> None:
        """Bring the assistant up. Called once, before the window opens."""

    def shutdown(self) -> None:
        """Put the assistant away."""


class AttachedBackend(Backend):
    """A window onto a J.A.R.V.I.S. that is already running in a terminal.

    Nothing is built here. Lines go straight onto the caller's input queue, so
    the terminal session's single dispatcher remains the only thing that ever
    talks to the agent — which is the invariant the whole program is built on.
    """

    def __init__(
        self,
        on_submit: Callable[[str], None],
        on_interrupt: Callable[[], None] | None = None,
        snapshot: Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        self._on_submit = on_submit
        self._on_interrupt = on_interrupt
        self._snapshot = snapshot

    def submit(self, text: str) -> None:
        self._on_submit(text)

    def interrupt(self) -> None:
        if self._on_interrupt is not None:
            self._on_interrupt()

    def snapshot(self) -> dict[str, Any]:
        return dict(self._snapshot() or {}) if self._snapshot is not None else {}


class StandaloneBackend(Backend):
    """A complete J.A.R.V.I.S., built for the window and driven by it.

    The wiring mirrors ``main.py``'s deliberately: monitor, protocol engine,
    permission broker, tool registry, agent, then a second pass to bind the
    parts that could not exist in the first. One worker thread drains the queue,
    for the same reason the terminal has exactly one dispatcher — two threads in
    the agent at once would interleave two conversations.
    """

    def __init__(self, *, turbo: bool = True, monitor: bool = True, model: str | None = None) -> None:
        self.turbo = turbo
        self.want_monitor = monitor
        self.model = model
        self.agent: Any = None
        self.registry: Any = None
        self.engine: Any = None
        self.monitor: Any = None
        self.broker: Any = None
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._worker: threading.Thread | None = None
        self._stopping = threading.Event()

    # -- lifecycle ----------------------------------------------------------------------
    def start(self) -> None:
        # Imported here rather than at module scope: someone running only the
        # theme engine or the tests should not pay for ollama, psutil and the
        # whole tool registry just by importing ``jarvis.desktop``.
        from jarvis.engine import build_agent
        from jarvis.monitor import AmbientMonitor
        from jarvis.permissions import PermissionBroker
        from jarvis.protocols import ProtocolEngine
        from jarvis.tools import build_registry

        if self.model:
            settings.MODEL_NAME = self.model

        hud = self.hud
        self.monitor = AmbientMonitor(
            on_alert=self._on_alert,
            on_telemetry=lambda t: hud and hud.set_telemetry(t),
        )
        self.engine = ProtocolEngine(hud=hud, voice=None, monitor=self.monitor)
        self.broker = PermissionBroker(hud=hud, voice=None, protocol_engine=self.engine)
        self.registry = build_registry(self.engine, hud, self.monitor, self.broker)
        self.agent = build_agent(
            self.registry,
            turbo=self.turbo,
            hud=hud,
            voice=None,
            protocol_engine=self.engine,
            monitor=self.monitor,
        )
        self.engine.bind(agent=self.agent)
        self.broker.bind(hud=hud, voice=None, protocol_engine=self.engine)

        if self.want_monitor:
            try:
                self.monitor.start()
            except Exception:
                LOG.warning("Ambient monitor would not start", exc_info=True)

        self._worker = threading.Thread(target=self._drain, name="desktop-dispatch", daemon=True)
        self._worker.start()

        # The model check runs off the hot path: the window is already usable,
        # and a missing Ollama should be a line in the transcript, not a stall.
        threading.Thread(target=self._preflight, name="desktop-preflight", daemon=True).start()

    def _preflight(self) -> None:
        """Report the model's availability into the window, once, at startup."""
        hud = self.hud
        if hud is None or self.agent is None:
            return
        try:
            ready, message = self.agent.ensure_model()
        except Exception as exc:
            LOG.debug("Model preflight failed", exc_info=True)
            ready, message = False, str(exc)
        hud.set_model_status(f"{settings.MODEL_NAME} · {'ready' if ready else 'unreachable'}")
        hud.log_system(message, "success" if ready else "warn")
        if ready:
            warm = getattr(self.agent, "warm_up", None)
            if callable(warm):
                try:
                    warm()
                except Exception:
                    LOG.debug("Warm-up declined", exc_info=True)

    def shutdown(self) -> None:
        self._stopping.set()
        self._queue.put(None)
        for closer, label in ((self.monitor, "monitor"), (self.agent, "agent")):
            close = getattr(closer, "stop", None) or getattr(closer, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    LOG.debug("%s did not close cleanly", label, exc_info=True)
        worker = self._worker
        if worker is not None and worker.is_alive():
            worker.join(timeout=3.0)

    # -- dispatch -----------------------------------------------------------------------
    def submit(self, text: str) -> None:
        self._queue.put(text)

    def interrupt(self) -> None:
        if self.agent is not None:
            try:
                self.agent.interrupt()
            except Exception:
                LOG.debug("Interrupt refused", exc_info=True)

    def _drain(self) -> None:
        """The single thread that is ever allowed inside the agent."""
        while not self._stopping.is_set():
            try:
                line = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if line is None:
                return
            try:
                self._handle(line)
            except Exception:
                LOG.exception("Desktop dispatch failed")
                if self.hud is not None:
                    self.hud.log_system("That did not go to plan; the log has the details.", "error")

    def _handle(self, line: str) -> None:
        text = (line or "").strip()
        if not text:
            return
        hud = self.hud
        if text.startswith("/"):
            self._command(text)
            return
        if hud is not None:
            hud.log_user(text)
        if self.agent is None:
            return
        # speak=False: this front end has no voice of its own. A window opened
        # onto a terminal session keeps the terminal's voice, via AttachedBackend.
        reply = self.agent.chat(text, speak=False)
        if getattr(reply, "error", "") and not getattr(reply, "surfaced", False) and hud is not None:
            hud.log_system(str(reply.error), "error")

    def _command(self, line: str) -> None:
        """The slash commands that make sense in a window.

        A deliberately short list. The terminal's set is much larger, but most of
        it is about audio hardware and console furniture; what is here is what an
        operator reaches for while looking at a graphical transcript.
        """
        hud = self.hud
        parts = line[1:].split(maxsplit=1)
        name = parts[0].lower() if parts else ""
        argument = parts[1].strip() if len(parts) > 1 else ""

        if hud is None:
            return
        if name in {"clear", "reset"}:
            if self.agent is not None:
                self.agent.reset()
            hud.clear_transcript()
            hud.log_system("Memory and transcript cleared.", "success")
        elif name in {"tools", "instruments"}:
            names = sorted(self.registry.names()) if self.registry else []
            hud.log_system(f"{len(names)} instruments: {', '.join(names) or 'none'}", "info")
        elif name == "protocols":
            names = sorted(self.engine.names()) if self.engine else []
            hud.log_system(f"Protocols: {', '.join(names) or 'none'}", "info")
        elif name == "protocol":
            if not argument:
                hud.log_system("Usage: /protocol <name>", "warn")
            elif self.engine is not None:
                resolved = self.engine.resolve(argument) or argument
                result = self.engine.execute(resolved)
                hud.log_system(
                    str(getattr(result, "summary", "") or f"Protocol {resolved} complete."),
                    "success" if getattr(result, "success", True) else "warn",
                )
        elif name == "model":
            ready, message = (self.agent.ensure_model() if self.agent else (False, "No agent."))
            hud.log_system(f"{settings.MODEL_NAME} @ {settings.OLLAMA_HOST} — {message}",
                           "success" if ready else "warn")
        elif name == "metrics":
            metrics = getattr(self.agent, "metrics", None)
            hud.log_system(metrics.summary() if metrics is not None else "No turn measured yet.", "info")
        elif name == "diag":
            from jarvis.monitor import telemetry_report
            hud.log_system(telemetry_report(), "info")
        elif name in {"quit", "exit"}:
            hud.log_system("Shutting down.", "info")
            hud.hub.publish("shutdown")
        else:
            hud.log_system(
                "Commands here: /clear /tools /protocols /protocol <name> "
                "/model /metrics /diag /quit — everything else goes to the model.",
                "info",
            )

    def _on_alert(self, alert: Any) -> None:
        if self.hud is not None:
            self.hud.push_alert(alert)

    def snapshot(self) -> dict[str, Any]:
        return {
            "model": settings.MODEL_NAME,
            "host": settings.OLLAMA_HOST,
            "title": settings.USER_TITLE,
            "tools": sorted(self.registry.names()) if self.registry else [],
            "protocols": sorted(self.engine.names()) if self.engine else [],
            "standalone": True,
        }


# ══════════════════════════════════════════════════════════════════════════════════════
# HTTP
# ══════════════════════════════════════════════════════════════════════════════════════
class _Handler(BaseHTTPRequestHandler):
    """The whole API surface. Small, because the front end is doing the work."""

    server_version = "JarvisDesktop/1.0"
    protocol_version = "HTTP/1.1"

    #: Injected by :class:`DesktopServer` before the server starts serving.
    app: "DesktopApp"

    # -- plumbing -----------------------------------------------------------------------
    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003 - base class name
        """Keep the request log in the log file, out of the operator's terminal."""
        LOG.debug("%s - %s", self.address_string(), fmt % args)

    def _authorised(self) -> bool:
        """Reject anything that is not this machine holding this session's token.

        The ``Host`` check is the part that matters. Without it, any web page
        the operator visits could point a hostname it controls at 127.0.0.1 and
        talk to this server from inside the browser's origin — and this server
        can run shell commands.
        """
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip().lower()
        if host and host not in _LOCAL_HOSTS:
            LOG.warning("Rejected a request claiming Host: %s", host)
            return False
        origin = (self.headers.get("Origin") or "").strip()
        if origin and urlparse(origin).hostname not in _LOCAL_HOSTS:
            LOG.warning("Rejected a cross-origin request from %s", origin)
            return False
        supplied = self.headers.get("X-Jarvis-Token") or _query(self.path).get("token", [""])[0]
        return secrets.compare_digest(str(supplied), self.app.token)

    def _send(self, code: int, body: bytes, content_type: str, extra: dict[str, str] | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # Nothing here should ever be cached: the token is in the URL and the
        # state changes constantly.
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, payload: dict[str, Any], code: int = 200) -> None:
        self._send(code, json.dumps(payload).encode("utf-8"), "application/json; charset=utf-8")

    def _body(self) -> dict[str, Any]:
        """Read and decode a JSON request body, capped so a stray upload cannot
        exhaust memory."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        if length <= 0 or length > 1_000_000:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8")) or {}
        except (json.JSONDecodeError, UnicodeDecodeError, OSError):
            return {}

    # -- routes -------------------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802 - base class name
        route = urlparse(self.path).path
        if not self._authorised():
            self._json({"error": "not authorised"}, 403)
            return
        self.app.touch()

        if route in ("/", "/index.html"):
            self._page()
        elif route.startswith("/static/"):
            self._static(route[len("/static/"):])
        elif route == "/api/state":
            self._json(self.app.state_payload())
        elif route == "/api/events":
            self._stream()
        elif route == "/api/ping":
            self._json({"ok": True, "clients": self.app.hub.client_count})
        elif route == "/api/files":
            self._json(self.app.workspace.listing(_query(self.path).get("path", [""])[0]))
        elif route == "/api/file":
            self._json(self.app.workspace.read(_query(self.path).get("path", [""])[0]))
        else:
            self._json({"error": "no such route"}, 404)

    def do_POST(self) -> None:  # noqa: N802 - base class name
        route = urlparse(self.path).path
        if not self._authorised():
            self._json({"error": "not authorised"}, 403)
            return
        self.app.touch()
        body = self._body()

        if route == "/api/chat":
            text = str(body.get("text") or "").strip()
            if not text:
                self._json({"error": "nothing to send"}, 400)
                return
            # 202: the line is queued. The answer arrives on the event stream,
            # token by token, rather than as this response.
            self.app.submit(text)
            self._json({"queued": True}, 202)
        elif route == "/api/interrupt":
            self.app.backend.interrupt()
            self._json({"ok": True})
        elif route == "/api/theme":
            self._json(self.app.apply_theme(body))
        elif route == "/api/answer":
            resolved = self.app.hud.resolve(str(body.get("token") or ""), body.get("answer"))
            self._json({"ok": resolved})
        elif route == "/api/shell":
            self._json(self._shell_action(body))
        elif route == "/api/quit":
            self._json({"ok": True})
            self.app.request_stop()
        else:
            self._json({"error": "no such route"}, 404)

    def _shell_action(self, body: dict[str, Any]) -> dict[str, Any]:
        """Run one line in the system shell, or steer it."""
        session = self.app.shell()
        if session is None:
            return {"ok": False, "error": "the shell pane is switched off"}
        if not session.available:
            return {"ok": False, "error": "no shell was found on this machine"}

        if body.get("restart"):
            return dict({"ok": session.restart()}, **session.describe())
        if body.get("interrupt"):
            return {"ok": session.interrupt()}
        if body.get("start"):
            return dict({"ok": session.start()}, **session.describe())

        line = body.get("input")
        if line is None:
            return {"ok": False, "error": "nothing to run"}
        # Logged before it runs: a shell pane whose history is absent from the
        # log file is a hole in the record of what happened this session.
        LOG.info("shell: %s", str(line)[:400])
        return {"ok": session.send(str(line))}

    # -- responses ----------------------------------------------------------------------
    def _page(self) -> None:
        """The single HTML page, with this session's token and theme baked in.

        Substituting server-side means the front end never has to guess its own
        token, and the first paint already has the operator's colours — no
        flash of the wrong theme while a stylesheet loads.
        """
        try:
            html = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
        except OSError:
            self._send(500, b"The desktop front end is missing from this install.", "text/plain")
            return
        html = html.replace("__JARVIS_BOOT__", json.dumps(self.app.state_payload()))
        html = html.replace("__JARVIS_THEME_CSS__", self.app.theme.css())
        # The stylesheet and the script are subresources: the browser fetches
        # them itself, with no chance to attach a header, so the token has to
        # travel in their URLs or the page loads naked.
        html = html.replace("__JARVIS_TOKEN__", self.app.token)
        self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")

    def _static(self, name: str) -> None:
        """Serve one file from ``jarvis/web``, and nothing outside it."""
        try:
            target = (WEB_ROOT / name).resolve()
            target.relative_to(WEB_ROOT.resolve())  # raises if `name` escaped
            payload = target.read_bytes()
        except (OSError, ValueError):
            self._json({"error": "no such file"}, 404)
            return
        guess = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        self._send(200, payload, guess)

    def _stream(self) -> None:
        """Hold a Server-Sent Events connection open for one window."""
        sink = self.app.hub.subscribe()
        if sink is None:
            self._json({"error": "too many windows are open"}, 429)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        # Chunked would be legal; SSE is a stream with no length, and the
        # simplest correct thing for HTTP/1.1 is to close it when done.
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        self.app.on_client_change()
        try:
            # Replay first, so a reloaded tab is not staring at nothing.
            for event in self.app.hub.history():
                self.wfile.write(event.encode())
            self.wfile.write(b": ready\n\n")
            self.wfile.flush()
            while not self.app.stopping.is_set():
                try:
                    event = sink.get(timeout=HEARTBEAT_SECONDS)
                except queue.Empty:
                    self.wfile.write(b": keep-alive\n\n")  # two bytes, keeps NAT happy
                    self.wfile.flush()
                    continue
                if event is None:
                    break
                self.wfile.write(event.encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            LOG.debug("A window closed its event stream")
        finally:
            self.app.hub.unsubscribe(sink)
            self.app.on_client_change()
            self.close_connection = True


def _query(path: str) -> dict[str, list[str]]:
    return parse_qs(urlparse(path).query)


class DesktopServer(ThreadingHTTPServer):
    """Loopback-only threading server. One thread per open window, plus one
    short-lived thread per API call."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, app: "DesktopApp", host: str, port: int) -> None:
        handler = type("_BoundHandler", (_Handler,), {"app": app})
        super().__init__((host, port), handler)


# ══════════════════════════════════════════════════════════════════════════════════════
# Opening the window
# ══════════════════════════════════════════════════════════════════════════════════════
def open_window(url: str, title: str = "J.A.R.V.I.S.") -> str:
    """Show ``url`` in the most application-like window available.

    Three tiers, best first. ``pywebview`` gives a genuinely native window with no
    browser furniture at all, but it is optional and rarely installed. A
    Chromium-family browser in ``--app`` mode is nearly as good and is on most
    machines. Failing both, an ordinary tab — which works, and simply looks like
    a tab. Returns which one was used, for the log and the operator.
    """
    if os.environ.get("JARVIS_DESKTOP_NO_WINDOW"):
        return "none"

    for launcher in (_open_pywebview, _open_chromium, _open_browser):
        try:
            used = launcher(url, title)
        except Exception:
            LOG.debug("%s could not open the window", launcher.__name__, exc_info=True)
            continue
        if used:
            return used
    return "none"


def _open_pywebview(url: str, title: str) -> str:
    """A real native window, when the operator has pywebview installed."""
    import webview  # type: ignore[import-not-found]

    def run() -> None:
        webview.create_window(title, url, width=1240, height=820, min_size=(880, 600))
        webview.start()

    # pywebview insists on the main thread on macOS; where that is not this
    # thread, fall through to the browser rather than crashing.
    if threading.current_thread() is not threading.main_thread():
        return ""
    threading.Thread(target=run, name="jarvis-window", daemon=True).start()
    return "pywebview"


#: Chromium-family binaries, by platform, in the order worth trying.
_CHROMIUM_CANDIDATES: dict[str, tuple[str, ...]] = {
    "darwin": (
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
    ),
    "win32": (
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ),
    "linux": (
        "google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
        "microsoft-edge", "brave-browser", "vivaldi",
    ),
}


def _open_chromium(url: str, title: str) -> str:
    """A Chromium-family browser in app mode: no tabs, no address bar."""
    platform_key = "darwin" if sys.platform == "darwin" else "win32" if sys.platform.startswith("win") else "linux"
    for candidate in _CHROMIUM_CANDIDATES[platform_key]:
        binary = candidate if os.path.isfile(candidate) else shutil.which(candidate)
        if not binary:
            continue
        # A separate profile directory keeps the app window out of the
        # operator's ordinary browsing session — no shared cookies, no tab
        # restore, and closing it does not close their real browser.
        profile = Path(os.path.expanduser("~")) / ".jarvis_cache" / "window"
        profile.mkdir(parents=True, exist_ok=True)
        command = [
            binary,
            f"--app={url}",
            f"--user-data-dir={profile}",
            "--window-size=1240,820",
            "--no-first-run",
            "--no-default-browser-check",
        ]
        subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=(platform_key != "win32"),
        )
        return f"app window ({Path(binary).name})"
    return ""


def _open_browser(url: str, title: str) -> str:
    """Last resort: whatever the operating system considers the browser."""
    return "browser tab" if webbrowser.open(url, new=1, autoraise=True) else ""


# ══════════════════════════════════════════════════════════════════════════════════════
# The application
# ══════════════════════════════════════════════════════════════════════════════════════
class DesktopApp:
    """Server, event hub, HUD adapter and window, as one object.

    Construct it, call :meth:`start`, and either block on :meth:`wait` (the
    standalone case) or leave it running in the background (the attached case,
    where the terminal session is still the foreground program).
    """

    def __init__(
        self,
        backend: Backend | None = None,
        *,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        mirror: Any = None,
        theme: theme_mod.Theme | None = None,
        open_window_on_start: bool = True,
        exit_when_closed: bool = False,
    ) -> None:
        self.hub = EventHub()
        self.hud = DesktopHUD(self.hub, mirror=mirror)
        #: What the file browser and the code viewer may see: the workspace, and
        #: nothing outside it.
        self.workspace = Workspace()
        #: The system shell behind the Terminal pane. Built lazily on first use,
        #: so a session that never opens the pane never spawns a process.
        self._shell: Any = None
        self._shell_lock = threading.Lock()
        self.backend = backend or StandaloneBackend()
        self.backend.hud = self.hud
        self.host = host
        self.port = port
        self.theme = theme or theme_mod.load()
        self.token = secrets.token_urlsafe(24)
        self.open_window_on_start = open_window_on_start
        #: Standalone windows own the process, so closing the last one should
        #: end it. An attached window must not take the terminal down with it.
        self.exit_when_closed = exit_when_closed
        self.stopping = threading.Event()
        self.server: DesktopServer | None = None
        self._server_thread: threading.Thread | None = None
        self._reaper: threading.Thread | None = None
        self._last_seen = time.monotonic()
        self._window_kind = ""
        #: Notified when a protocol or the operator changes the colours, so an
        #: attached terminal HUD can follow along.
        self.on_theme_change: Callable[[theme_mod.Theme], None] | None = None

    # -- lifecycle ----------------------------------------------------------------------
    @property
    def url(self) -> str:
        """The address of the window, token included."""
        port = self.server.server_address[1] if self.server else self.port
        return f"http://{self.host}:{port}/?token={self.token}"

    def start(self) -> str:
        """Bring up the backend and the server, then open the window."""
        self.backend.start()
        try:
            self.server = DesktopServer(self, self.host, self.port)
        except OSError as exc:
            raise RuntimeError(f"Could not open a local port for the desktop app: {exc}") from exc
        self._server_thread = threading.Thread(
            target=self.server.serve_forever, name="jarvis-desktop-http", daemon=True
        )
        self._server_thread.start()
        LOG.info("Desktop app serving on %s", self.url.split("?")[0])

        if self.open_window_on_start:
            self._window_kind = open_window(self.url, f"{settings.AGENT_NAME} Desktop")
            LOG.info("Window: %s", self._window_kind or "none opened")
        if self.exit_when_closed:
            self._reaper = threading.Thread(target=self._reap, name="jarvis-desktop-reaper", daemon=True)
            self._reaper.start()
        return self.url

    def wait(self) -> int:
        """Block until the app stops. Ctrl+C in the terminal ends it too."""
        try:
            while not self.stopping.wait(0.5):
                pass
        except KeyboardInterrupt:
            LOG.info("Desktop app interrupted from the terminal")
        finally:
            self.stop()
        return 0

    def request_stop(self) -> None:
        """Ask the app to stop, without blocking the caller's thread."""
        self.stopping.set()

    def stop(self) -> None:
        """Shut everything down, in the order that avoids a hang."""
        if self.stopping.is_set() and self.server is None:
            return
        self.stopping.set()
        self.hud.cancel_pending()   # release any thread parked on a question
        self.hub.close()            # let the stream threads finish
        server, self.server = self.server, None
        if server is not None:
            try:
                server.shutdown()
                server.server_close()
            except Exception:
                LOG.debug("Server did not close cleanly", exc_info=True)
        if self._shell is not None:
            try:
                self._shell.stop()
            except Exception:
                LOG.debug("Shell did not close cleanly", exc_info=True)
            self._shell = None
        try:
            self.backend.shutdown()
        except Exception:
            LOG.debug("Backend did not close cleanly", exc_info=True)
        LOG.info("Desktop app stopped")

    # -- client bookkeeping ---------------------------------------------------------------
    def touch(self) -> None:
        """Record that a window is alive. Feeds the idle-exit timer."""
        self._last_seen = time.monotonic()

    def on_client_change(self) -> None:
        self.touch()

    def _reap(self) -> None:
        """Stop the standalone app once its last window has been gone a while.

        The grace period is what makes a page reload survivable: the stream
        drops and comes back a second later, and the app must not have quit in
        between.
        """
        while not self.stopping.wait(1.0):
            if self.hub.client_count > 0:
                self.touch()
                continue
            if time.monotonic() - self._last_seen > IDLE_EXIT_GRACE:
                LOG.info("No windows open for %ss; shutting down", IDLE_EXIT_GRACE)
                self.request_stop()
                return

    # -- the system shell -------------------------------------------------------------
    @property
    def shell_enabled(self) -> bool:
        """Whether the operator has left the Terminal pane switched on."""
        return bool(getattr(settings, "DESKTOP_SHELL_ENABLED", True))

    def shell(self) -> Any:
        """The shell session, built on first use. None when the pane is off.

        Lazy on purpose: a session that never opens the Terminal pane never
        spawns a shell process, and one that opens it once keeps the same
        process — which is what makes ``cd`` and an activated virtualenv stick.
        """
        if not self.shell_enabled:
            return None
        with self._shell_lock:
            if self._shell is not None:
                return self._shell
            from jarvis.shell import ShellSession

            self._shell = ShellSession(
                on_output=lambda line: self.hub.publish("shell_out", text=line),
                on_done=lambda code, cwd: self.hub.publish("shell_done", code=code, cwd=cwd),
                on_exit=lambda code: self.hub.publish("shell_exit", code=code),
                cwd=self.workspace.root,
            )
            return self._shell

    def shell_payload(self) -> dict[str, Any]:
        """What the window needs to draw the pane before anything has run."""
        if not self.shell_enabled:
            return {"available": False, "disabled": True, "name": "", "prompt": "", "cwd": ""}
        if self._shell is not None:
            return self._shell.describe()
        try:
            from jarvis import shell as shell_mod

            flavour = shell_mod.detect()
        except Exception:
            LOG.debug("Shell detection failed", exc_info=True)
            return {"available": False, "name": "", "prompt": "", "cwd": ""}
        return {
            "available": flavour is not None,
            "name": flavour.name if flavour else "",
            "prompt": flavour.prompt if flavour else "",
            "cwd": str(self.workspace.root),
            "running": False,
        }

    # -- the API's working parts ------------------------------------------------------
    def submit(self, text: str) -> None:
        """Hand one line from the window to whatever is driving the assistant."""
        self.backend.submit(text)

    def state_payload(self) -> dict[str, Any]:
        """Everything a freshly opened window needs to draw itself."""
        payload: dict[str, Any] = {
            "token": self.token,
            "agent": settings.AGENT_NAME,
            "fullName": settings.AGENT_FULL_NAME,
            "title": settings.USER_TITLE,
            "state": self.hud.state,
            "theme": self.theme.to_dict(),
            "variables": self.theme.css_variables(),
            "presets": theme_mod.presets_payload(),
            "modes": list(theme_mod.MODES),
            "fonts": [{"key": key, "label": label} for key, label in theme_mod.FONTS],
            "standalone": False,
            "workspace": str(self.workspace.root),
            "workspaceName": self.workspace.root.name,
            # So the shell prompt can shorten a path to ~ the way a shell does.
            "home": str(Path.home()),
            "shell": self.shell_payload(),
        }
        try:
            payload.update(self.backend.snapshot())
        except Exception:
            LOG.debug("Backend would not describe itself", exc_info=True)
        return payload

    def apply_theme(self, body: dict[str, Any]) -> dict[str, Any]:
        """Adopt the colours the operator just chose, and tell every window.

        Accepts a whole theme dict, a single ``preset`` key, or a bare ``seed``
        colour laid over the current theme — whichever the front end found
        easiest to send for the control the operator touched.
        """
        current = self.theme
        if body.get("surprise"):
            new = theme_mod.surprise()
        elif body.get("preset"):
            new = theme_mod.preset(str(body["preset"])) or current
        elif isinstance(body.get("theme"), dict):
            new = theme_mod.Theme.from_dict(body["theme"])
        else:
            changes = {
                key: body[key]
                for key in ("seed", "secondary", "mode", "tint", "contrast", "radius", "glow", "font", "name")
                if key in body
            }
            if not changes:
                return {"ok": False, "error": "nothing to change"}
            # A hand-picked colour is no longer any named preset.
            if "seed" in changes and "name" not in changes:
                changes["name"] = "Custom"
            new = current.evolve(**changes)

        self.theme = new
        persisted = theme_mod.save(new) if body.get("persist", True) else False
        self.hub.publish("theme", theme=new.to_dict(), variables=new.css_variables())
        if self.on_theme_change is not None:
            try:
                self.on_theme_change(new)
            except Exception:
                LOG.debug("Theme listener failed", exc_info=True)
        return {
            "ok": True,
            "persisted": persisted,
            "theme": new.to_dict(),
            "variables": new.css_variables(),
        }


# ══════════════════════════════════════════════════════════════════════════════════════
# Entry points
# ══════════════════════════════════════════════════════════════════════════════════════
def available() -> tuple[bool, str]:
    """Whether the desktop app can run here, and why not when it cannot."""
    missing = [
        name for name in ("index.html", "app.css", "app.js")
        if not (WEB_ROOT / name).is_file()
    ]
    if missing:
        return False, f"the front end is incomplete (missing {', '.join(missing)})"
    return True, ""


def run(
    *,
    port: int = DEFAULT_PORT,
    open_window_on_start: bool = True,
    turbo: bool = True,
    monitor: bool = True,
    model: str | None = None,
) -> int:
    """Run J.A.R.V.I.S. Desktop standalone. Returns a process exit code."""
    ok, why = available()
    if not ok:
        print(f"The desktop app cannot start: {why}.", file=sys.stderr)
        return 1

    app = DesktopApp(
        StandaloneBackend(turbo=turbo, monitor=monitor, model=model),
        port=port,
        open_window_on_start=open_window_on_start,
        exit_when_closed=open_window_on_start,
    )
    try:
        url = app.start()
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(f"{settings.AGENT_NAME} Desktop is up.", flush=True)
    print(f"  window : {app._window_kind or 'open the address below yourself'}")
    print(f"  address: {url}")
    # Flushed deliberately: the address is the only way in when no window
    # opened, and a block-buffered pipe would hold it until shutdown.
    print("  Ctrl+C here, or close the window, to shut down.", flush=True)
    app.hud.log_system(
        f"Good to see you, {settings.USER_TITLE}. Pick any colour you like — "
        "the palette button is top right.",
        "info",
    )
    return app.wait()


def attach(
    on_submit: Callable[[str], None],
    *,
    on_interrupt: Callable[[], None] | None = None,
    snapshot: Callable[[], dict[str, Any]] | None = None,
    mirror: Any = None,
    port: int = DEFAULT_PORT,
    theme: theme_mod.Theme | None = None,
    on_theme_change: Callable[[theme_mod.Theme], None] | None = None,
) -> DesktopApp:
    """Open a window onto a session that is already running in a terminal.

    Returns the started app. The caller keeps the reference so it can mirror
    HUD output into the window and stop it on shutdown.
    """
    ok, why = available()
    if not ok:
        raise RuntimeError(f"The desktop app cannot start: {why}.")
    app = DesktopApp(
        AttachedBackend(on_submit, on_interrupt=on_interrupt, snapshot=snapshot),
        mirror=mirror,
        port=port,
        theme=theme,
        open_window_on_start=True,
        exit_when_closed=False,
    )
    app.on_theme_change = on_theme_change
    app.start()
    return app


if __name__ == "__main__":  # pragma: no cover - convenience for `python -m jarvis.desktop`
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    sys.exit(run())
