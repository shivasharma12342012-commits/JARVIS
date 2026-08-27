"""Stark Protocols — the standing macros J.A.R.V.I.S. fires on command.

Three protocols ship with the workshop:

* **HOUSE PARTY** — everything online at once: full diagnostics, voice unmuted,
  repositories surveyed, the development stack launched, the model host verified.
* **VERONICA** — the defensive posture. Destructive filesystem work is refused
  outright and memory-hogging processes are identified (and, when dry run is
  disabled, terminated).
* **CLEAN SLATE** — context wiped, display cleared, caches and stale byte-code
  purged, oversized logs truncated, lockdown lifted.

Two rules govern this module, and they are the reason it exists as its own file:

1. VERONICA never touches an essential operating-system process, a process this
   interpreter is descended from or responsible for, or PID 0/4. Severing your own
   brain stem is not a security posture.
2. CLEAN SLATE proves every path it is about to unlink resolves *under*
   ``PROJECT_ROOT`` before it removes anything. A protocol that deletes the wrong
   tree once is a protocol nobody trusts again.

Every step of every protocol is individually wrapped: a step that throws becomes a
failed :class:`StepResult`, never an aborted protocol.
"""

from __future__ import annotations

import difflib
import logging
import os
import shlex
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import httpx
import psutil

from config import (
    PALETTE_CLEAN_SLATE,
    PALETTE_HOUSE_PARTY,
    PALETTE_STANDARD,
    PALETTE_VERONICA,
    PROJECT_ROOT,
    settings,
)
from jarvis.monitor import collect_telemetry, telemetry_summary
from jarvis.prompts import PROTOCOL_ACK, personalise

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------------------
# Tunables that are implementation detail rather than operator policy. Anything the
# operator is expected to retune lives in config.settings instead.
# --------------------------------------------------------------------------------------
_MB = 1024 * 1024
_LOG_TRUNCATE_BYTES = 5 * _MB
_GIT_TIMEOUT = 5.0
_REPO_SCAN_MAX_DIRS = 200
_REPO_SCAN_MAX_REPOS = 25
_REPO_SCAN_MAX_DEPTH = 3
_OLLAMA_PING_TIMEOUT = 5.0
_TERMINATE_WAIT_SECONDS = 3.0
_DETAIL_MAX_CHARS = 600

# Directories that are never worth walking into: huge, machine-generated, or ours.
_SKIP_DIRS = frozenset(
    {
        "node_modules",
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        "env",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "site-packages",
        "dist",
        "build",
        ".tox",
    }
)

# PID 0 is the system idle process and PID 4 is the NT kernel. Neither is a process
# in any sense psutil can act on, and asking politely still throws.
_UNTOUCHABLE_PIDS = frozenset({0, 4})

#: Processes VERONICA will never terminate, whatever their memory footprint. Compared
#: case-insensitively with any trailing ``.exe`` stripped. Killing any of these either
#: bluescreens the box, closes the console J.A.R.V.I.S. is speaking through, or takes
#: the model host down mid-sentence.
ESSENTIAL_PROCESS_NAMES: frozenset[str] = frozenset(
    {
        # Windows kernel, session and security infrastructure.
        "system",
        "system idle process",
        "registry",
        "memory compression",
        "smss",
        "csrss",
        "wininit",
        "winlogon",
        "services",
        "lsass",
        "lsaiso",
        "svchost",
        "fontdrvhost",
        "dwm",
        "explorer",
        "sihost",
        "ctfmon",
        "audiodg",
        "spoolsv",
        "dllhost",
        "runtimebroker",
        "taskhostw",
        "wudfhost",
        "searchhost",
        "searchindexer",
        "shellexperiencehost",
        "startmenuexperiencehost",
        "securityhealthservice",
        "securityhealthsystray",
        "msmpeng",
        "nissrv",
        # Consoles and shells — the terminal hosting this session included.
        "windowsterminal",
        "openconsole",
        "conhost",
        "cmd",
        "powershell",
        "pwsh",
        "wt",
        "alacritty",
        "wezterm",
        "wezterm-gui",
        "hyper",
        "terminal",
        "iterm2",
        "gnome-terminal-server",
        "konsole",
        "xterm",
        "tmux",
        "screen",
        "bash",
        "zsh",
        "fish",
        "sh",
        # The model host. Veronica must not sever her own brain stem.
        "ollama",
        "ollama app",
        "ollama_llama_server",
        # POSIX / macOS init and desktop infrastructure.
        "init",
        "systemd",
        "systemd-journald",
        "launchd",
        "kthreadd",
        "kernel_task",
        "windowserver",
        "loginwindow",
        "finder",
        "dock",
    }
)

# Service accounts whose processes are operating-system business, not ours.
_SYSTEM_ACCOUNTS = frozenset(
    {"system", "localsystem", "local service", "network service", "root", "trustedinstaller"}
)

# Words that carry no meaning when identifying a protocol, so "initiate the House Party
# Protocol, please" and "houseparty" collapse to the same thing.
_NOISE_WORDS = frozenset(
    {
        "protocol",
        "protocols",
        "initiate",
        "initiating",
        "engage",
        "engaged",
        "engaging",
        "activate",
        "activated",
        "run",
        "running",
        "execute",
        "executing",
        "start",
        "starting",
        "please",
        "the",
        "a",
        "an",
        "mode",
        "now",
        "jarvis",
        "lets",
        "let",
        "us",
        "go",
    }
)

# Action synonyms mapped onto the three operations Veronica actually reasons about.
# Anything not in this table is non-destructive and is waved through.
_DESTRUCTIVE_ACTIONS: dict[str, str] = {
    "delete": "delete",
    "remove": "delete",
    "rm": "delete",
    "unlink": "delete",
    "rmtree": "delete",
    "rmdir": "delete",
    "move": "delete",
    "rename": "delete",
    "write": "write",
    "overwrite": "write",
    "create": "write",
    "truncate": "write",
    "save": "write",
    "append": "append",
}

_STATUS_LABEL = {"ok": "OK", "warn": "WARN", "fail": "FAIL", "skipped": "SKIP"}


# --------------------------------------------------------------------------------------
# Result types
# --------------------------------------------------------------------------------------
@dataclass
class StepResult:
    """The outcome of a single protocol step. ``status`` is ok|warn|fail|skipped."""

    label: str
    status: str
    detail: str = ""

    @property
    def ok(self) -> bool:
        """True when the step did not fail (a warning is still a completed step)."""
        return self.status != "fail"

    def format_line(self) -> str:
        """One plain-text line, safe for a legacy console."""
        marker = _STATUS_LABEL.get(self.status, self.status.upper())
        return f"[{marker}] {self.label}" + (f" - {self.detail}" if self.detail else "")


@dataclass
class ProtocolResult:
    """Everything a protocol run produced, ready for the HUD, the model, or a log."""

    key: str
    display_name: str
    success: bool
    steps: list[StepResult]
    summary: str
    palette: str

    def to_markdown(self) -> str:
        """Render the run as Markdown — this is what gets fed back to the model."""
        verdict = "COMPLETE" if self.success else "COMPLETED WITH FAULTS"
        lines = [f"### {self.display_name} — {verdict}", "", self.summary, ""]
        if self.steps:
            lines.append("| Step | Status | Detail |")
            lines.append("| --- | --- | --- |")
            for step in self.steps:
                detail = _clean_cell(step.detail)
                marker = _STATUS_LABEL.get(step.status, step.status.upper())
                lines.append(f"| {_clean_cell(step.label)} | {marker} | {detail or '-'} |")
        return "\n".join(lines)


@dataclass
class Protocol:
    """A registered Stark Protocol: identity, how to name it, and how to run it."""

    key: str
    display_name: str
    aliases: list[str]
    description: str
    palette: str
    handler: Callable[[], ProtocolResult]


# --------------------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------------------
def _clean_cell(text: str) -> str:
    """Flatten text so it cannot break out of a Markdown table cell."""
    flat = " ".join(str(text or "").split())
    flat = flat.replace("|", "/")
    if len(flat) > _DETAIL_MAX_CHARS:
        flat = flat[: _DETAIL_MAX_CHARS - 1].rstrip() + "…"
    return flat


def _normalise(text: str) -> str:
    """Lower-case, strip punctuation, and drop filler words used to invoke a protocol."""
    lowered = "".join(ch if ch.isalnum() else " " for ch in str(text or "").lower())
    tokens = [tok for tok in lowered.split() if tok and tok not in _NOISE_WORDS]
    return " ".join(tokens)


def _compress(text: str) -> str:
    """Normalised form with the spaces removed: ``house party`` -> ``houseparty``."""
    return _normalise(text).replace(" ", "")


def _process_base_name(name: str) -> str:
    """Lower-cased executable name without its extension, for allow-list comparison."""
    base = (name or "").strip().lower()
    for suffix in (".exe", ".com", ".bat", ".cmd"):
        if base.endswith(suffix):
            return base[: -len(suffix)]
    return base


def _is_system_account(username: str | None) -> bool:
    """True for OS service accounts, whose processes are never ours to reap."""
    if not username:
        return False
    # Windows reports "NT AUTHORITY\\SYSTEM"; only the account part matters.
    account = username.replace("/", "\\").split("\\")[-1].strip().lower()
    return account in _SYSTEM_ACCOUNTS


def _resolve_quietly(path: str | os.PathLike[str]) -> Path | None:
    """Resolve a path without requiring it to exist; ``None`` when it cannot be parsed."""
    try:
        return Path(path).expanduser().resolve()
    except (OSError, ValueError, RuntimeError):
        return None


def assert_under_project_root(path: str | os.PathLike[str]) -> Path:
    """Return ``path`` resolved, or raise ``ValueError`` if it escapes ``PROJECT_ROOT``.

    Every CLEAN SLATE deletion goes through here first. ``resolve()`` collapses ``..``
    and follows symlinks, so a crafted or careless path cannot smuggle the purge out of
    the project tree — and the project root itself is never a valid deletion target.
    """
    resolved = _resolve_quietly(path)
    if resolved is None:
        raise ValueError(f"unresolvable path: {path!r}")
    if resolved == PROJECT_ROOT:
        raise ValueError("refusing to delete the project root itself")
    if not resolved.is_relative_to(PROJECT_ROOT):
        raise ValueError(f"{resolved} is outside {PROJECT_ROOT}")
    return resolved


# --------------------------------------------------------------------------------------
# The engine
# --------------------------------------------------------------------------------------
class ProtocolEngine:
    """Registry and executor for the Stark Protocols.

    The HUD, voice system, ambient monitor and agent are all optional, duck-typed and
    may be bound after construction — ``main.py`` builds them in dependency order and
    calls :meth:`bind` to close the loop. Every outbound call to them is defensive: a
    protocol must not fail because a display refused to repaint.
    """

    def __init__(
        self, hud: Any = None, voice: Any = None, monitor: Any = None, agent: Any = None
    ) -> None:
        self._hud = hud
        self._voice = voice
        self._monitor = monitor
        self._agent = agent

        self._state_lock = threading.RLock()
        self._exec_lock = threading.RLock()  # one protocol at a time, whoever asks
        self._active: str | None = None
        self._lockdown = False
        self._protected_paths: list[Path] = []
        self._spawned: list[subprocess.Popen] = []
        self._last_telemetry: Any = None

        # Protected paths are policy, not protocol state: they hold from construction so
        # a destructive tool call made before VERONICA ever runs is still checked.
        self._register_protected_paths()

        self._protocols: dict[str, Protocol] = {
            "house_party": Protocol(
                key="house_party",
                display_name="HOUSE PARTY PROTOCOL",
                aliases=[
                    "house party",
                    "houseparty",
                    "house_party",
                    "house party protocol",
                    "party",
                    "hpp",
                    "full power",
                    "all systems",
                    "everything online",
                ],
                description=(
                    "Everything online at once: full diagnostics, voice unmuted, repositories "
                    "surveyed, development stack launched, model host verified."
                ),
                palette=PALETTE_HOUSE_PARTY,
                handler=self._run_house_party,
            ),
            "veronica": Protocol(
                key="veronica",
                display_name="VERONICA PROTOCOL",
                aliases=[
                    "veronica",
                    "vero",
                    "lockdown",
                    "lock down",
                    "seal the perimeter",
                    "perimeter",
                    "defensive",
                ],
                description=(
                    "Defensive lockdown: destructive filesystem work is refused and "
                    "memory-hogging processes are identified, then terminated unless dry "
                    "run is enabled."
                ),
                palette=PALETTE_VERONICA,
                handler=self._run_veronica,
            ),
            "clean_slate": Protocol(
                key="clean_slate",
                display_name="CLEAN SLATE PROTOCOL",
                aliases=[
                    "clean slate",
                    "cleanslate",
                    "clean_slate",
                    "clean slate protocol",
                    "wipe",
                    "reset",
                    "fresh start",
                    "clear context",
                    "scrub",
                ],
                description=(
                    "Wipe the slate: conversation memory reset, transcript cleared, caches and "
                    "stale byte-code purged, oversized logs truncated, lockdown lifted."
                ),
                palette=PALETTE_CLEAN_SLATE,
                handler=self._run_clean_slate,
            ),
        }

    # -- wiring ------------------------------------------------------------------------
    def bind(
        self, hud: Any = None, voice: Any = None, monitor: Any = None, agent: Any = None
    ) -> None:
        """Attach collaborators after construction. ``None`` leaves a binding untouched."""
        with self._state_lock:
            if hud is not None:
                self._hud = hud
            if voice is not None:
                self._voice = voice
            if monitor is not None:
                self._monitor = monitor
            if agent is not None:
                self._agent = agent

    # -- state -------------------------------------------------------------------------
    @property
    def active(self) -> str | None:
        """Key of the protocol currently in force, or ``None``."""
        with self._state_lock:
            return self._active

    @property
    def lockdown(self) -> bool:
        """True while VERONICA is engaged and destructive work is forbidden."""
        with self._state_lock:
            return self._lockdown

    def names(self) -> list[str]:
        """Registered protocol keys, in presentation order."""
        return list(self._protocols)

    def describe(self) -> list[tuple[str, str]]:
        """``(display_name, description)`` for every registered protocol."""
        return [(p.display_name, p.description) for p in self._protocols.values()]

    # -- resolution --------------------------------------------------------------------
    def resolve(self, text: str) -> str | None:
        """Map free-form speech or typing onto a protocol key.

        Handles "PROTOCOL HOUSE PARTY", "house party", "houseparty", "house_party" and
        the near-misses a speech recogniser produces ("house partie", "veronika").
        Returns ``None`` when nothing matches well enough to act on.
        """
        raw = str(text or "").strip()
        if not raw:
            return None

        # A bare key wins immediately, before any normalisation can mangle it.
        if raw in self._protocols:
            return raw

        normal = _normalise(raw)
        compressed = _compress(raw)
        if not compressed:
            return None

        candidates: dict[str, str] = {}  # normalised candidate -> key
        compact: dict[str, str] = {}  # compressed candidate -> key
        for key, protocol in self._protocols.items():
            for candidate in (key, protocol.display_name, *protocol.aliases):
                cand_norm = _normalise(candidate)
                if cand_norm:
                    candidates.setdefault(cand_norm, key)
                cand_compact = _compress(candidate)
                if cand_compact:
                    compact.setdefault(cand_compact, key)

        if normal in candidates:
            return candidates[normal]
        if compressed in compact:
            return compact[compressed]

        # Containment: "initiate the house party protocol now" collapses to a string that
        # still holds "houseparty". Longest candidate first so "cleanslate" wins over
        # any shorter alias that happens to be a substring of the same phrase.
        for cand_compact in sorted(compact, key=len, reverse=True):
            if len(cand_compact) >= 4 and cand_compact in compressed:
                return compact[cand_compact]

        # Finally, fuzzy: mishearings and typos. The cutoff is deliberately strict — the
        # wrong protocol is far worse than an honest "I did not catch that".
        close = difflib.get_close_matches(compressed, list(compact), n=1, cutoff=0.78)
        if close:
            return compact[close[0]]
        close = difflib.get_close_matches(normal, list(candidates), n=1, cutoff=0.78)
        if close:
            return candidates[close[0]]
        return None

    # -- execution ---------------------------------------------------------------------
    def execute(self, name: str) -> ProtocolResult:
        """Resolve ``name`` and run the protocol, never raising whatever goes wrong."""
        key = name if name in self._protocols else self.resolve(name)
        if key is None:
            known = ", ".join(p.display_name for p in self._protocols.values())
            summary = (
                f"I have no protocol by that name, {settings.USER_TITLE}. "
                f"Registered protocols are: {known}."
            )
            return ProtocolResult(
                key=str(name),
                display_name=str(name).strip().upper() or "UNKNOWN PROTOCOL",
                success=False,
                steps=[StepResult("resolve", "fail", f"unknown protocol {name!r}")],
                summary=summary,
                palette=PALETTE_STANDARD,
            )

        protocol = self._protocols[key]
        with self._exec_lock:
            self._enter(protocol)
            try:
                result = protocol.handler()
            except Exception as exc:  # a handler bug must not take the agent down
                logger.exception("Protocol %s raised", key)
                result = ProtocolResult(
                    key=key,
                    display_name=protocol.display_name,
                    success=False,
                    steps=[StepResult("execution", "fail", f"{type(exc).__name__}: {exc}")],
                    summary=(
                        f"{protocol.display_name} aborted, {settings.USER_TITLE}: "
                        f"{type(exc).__name__}: {exc}."
                    ),
                    palette=protocol.palette,
                )
            self._report(result)
            return result

    def deactivate(self) -> None:
        """Stand every protocol down: no active protocol, no lockdown, standard palette."""
        with self._state_lock:
            self._active = None
            self._lockdown = False
        self._hud_call("set_protocol", None)
        self._hud_call("set_palette", PALETTE_STANDARD)
        logger.info("Protocols stood down")

    # -- safety ------------------------------------------------------------------------
    def guard_destructive(self, action: str, path: str) -> str | None:
        """Veto a destructive filesystem action, or return ``None`` to allow it.

        The returned string is a finished sentence in J.A.R.V.I.S.'s voice, addressed with
        the operator's chosen title, suitable for handing straight back to the model.
        """
        operation = _DESTRUCTIVE_ACTIONS.get(str(action or "").strip().lower())
        if operation is None:
            return None  # reads, listings and stats are always permitted

        title = settings.USER_TITLE
        target = str(path or "").strip() or "that path"

        if self.lockdown:
            return (
                f"Veronica is engaged, {title}. The protocol forbids {operation} operations — "
                f"{target} stays exactly as it is until the lockdown is lifted. "
                f"Clean Slate stands it down."
            )

        protected = self._match_protected(target)
        if protected is not None:
            return (
                f"{target} lies inside the protected path {protected}, {title}. "
                f"Veronica's standing orders forbid {operation} operations there, "
                f"lockdown or no lockdown."
            )
        return None

    # ----------------------------------------------------------------------------------
    # HOUSE PARTY
    # ----------------------------------------------------------------------------------
    def _run_house_party(self) -> ProtocolResult:
        """Bring every subsystem online and report what came back."""
        steps = [
            self._step("Full diagnostics", self._step_diagnostics),
            self._step("Voice systems", self._step_unmute_voice),
            self._step("Repository sweep", self._step_scan_repositories),
            self._step("Development stack", self._step_launch_dev_commands),
            self._step("Model host", self._step_check_ollama),
        ]
        success = all(step.ok for step in steps)
        faults = [step.label for step in steps if step.status in ("fail", "warn")]
        title = settings.USER_TITLE

        if success and not faults:
            summary = (
                f"House Party Protocol complete, {title}. Every subsystem is online and "
                f"reporting nominal."
            )
        elif success:
            summary = (
                f"House Party Protocol complete, {title}, with reservations on "
                f"{', '.join(faults)}. The detail is below."
            )
        else:
            summary = (
                f"House Party Protocol ran with faults, {title}: {', '.join(faults)}. "
                f"The remaining subsystems are online."
            )
        return ProtocolResult(
            key="house_party",
            display_name=self._protocols["house_party"].display_name,
            success=success,
            steps=steps,
            summary=summary,
            palette=PALETTE_HOUSE_PARTY,
        )

    def _step_diagnostics(self) -> tuple[str, str]:
        """Take a fresh telemetry reading and push it at the HUD."""
        telemetry = collect_telemetry(top_n=5)
        with self._state_lock:
            self._last_telemetry = telemetry
        self._hud_call("set_telemetry", telemetry)
        return "ok", telemetry_summary(telemetry)

    def _step_unmute_voice(self) -> tuple[str, str]:
        """House Party is not a quiet affair — the voice comes back up."""
        voice = self._voice
        if voice is None:
            return "skipped", "no voice system bound"
        setter = getattr(voice, "set_muted", None)
        if not callable(setter):
            return "skipped", "voice system does not support muting"
        setter(False)
        speaks = bool(getattr(voice, "tts_available", False))
        return "ok", "voice unmuted" + ("" if speaks else " (no TTS engine available)")

    def _step_scan_repositories(self) -> tuple[str, str]:
        """Survey git repositories under the scan root, reporting branch and dirt."""
        root = Path(settings.scan_root)
        if not root.is_dir():
            return "warn", f"scan root {root} is not a directory"

        git_exe = shutil.which("git")
        repos: list[str] = []
        walked = 0

        for dirpath, dirnames, _filenames in os.walk(root, topdown=True, onerror=lambda _e: None):
            walked += 1
            if walked > _REPO_SCAN_MAX_DIRS or len(repos) >= _REPO_SCAN_MAX_REPOS:
                break
            current = Path(dirpath)
            try:
                depth = len(current.relative_to(root).parts)
            except ValueError:
                depth = _REPO_SCAN_MAX_DEPTH

            is_repo = ".git" in dirnames or (current / ".git").exists()
            if is_repo:
                repos.append(self._describe_repo(current, git_exe))
                dirnames[:] = []  # a repository is a leaf for our purposes
                continue

            if depth >= _REPO_SCAN_MAX_DEPTH:
                dirnames[:] = []
            else:
                dirnames[:] = [
                    d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")
                ]

        if not repos:
            return "ok", f"no repositories found under {root} ({walked} directories walked)"
        noun = "repository" if len(repos) == 1 else "repositories"
        return "ok", f"{len(repos)} {noun}: " + "; ".join(repos)

    def _describe_repo(self, path: Path, git_exe: str | None) -> str:
        """One-line branch/dirty description for a single repository."""
        name = path.name or str(path)
        if git_exe is None:
            return f"{name} [git binary not on PATH]"
        try:
            proc = subprocess.run(
                [git_exe, "-C", str(path), "status", "--porcelain", "--branch"],
                capture_output=True,
                text=True,
                timeout=_GIT_TIMEOUT,
                encoding="utf-8",
                errors="replace",
            )
        except subprocess.TimeoutExpired:
            return f"{name} [status timed out]"
        except OSError as exc:
            return f"{name} [git failed: {exc}]"

        if proc.returncode != 0:
            reason = " ".join((proc.stderr or "").split())[:80] or f"exit {proc.returncode}"
            return f"{name} [{reason}]"

        lines = (proc.stdout or "").splitlines()
        branch = "detached"
        if lines and lines[0].startswith("##"):
            head = lines[0][2:].strip()
            branch = head.split("...")[0].split(" ")[0] or "detached"
            if branch.upper().startswith("HEAD"):
                branch = "detached HEAD"
        changed = sum(1 for line in lines[1:] if line.strip())
        state = "clean" if changed == 0 else f"{changed} change{'' if changed == 1 else 's'}"
        return f"{name} [{branch}] {state}"

    def _step_launch_dev_commands(self) -> tuple[str, str]:
        """Fire every configured development command as a detached process."""
        commands = [c for c in list(settings.PROTOCOL_DEV_COMMANDS) if str(c).strip()]
        if not commands:
            return "skipped", "no PROTOCOL_DEV_COMMANDS configured"

        launched: list[str] = []
        failed: list[str] = []
        for command in commands:
            try:
                pid = self._spawn_detached(str(command))
                launched.append(f"{command} (pid {pid})")
            except Exception as exc:  # one bad command must not stop the rest
                logger.warning("Dev command failed to launch: %s", command, exc_info=True)
                failed.append(f"{command}: {type(exc).__name__}: {exc}")

        detail = "; ".join(launched) if launched else "nothing launched"
        if failed:
            return "warn", f"{detail} | failed: {'; '.join(failed)}"
        return "ok", detail

    def _spawn_detached(self, command: str) -> int:
        """Start ``command`` fully detached so it outlives this process. Returns the PID."""
        scan_root = Path(settings.scan_root)
        cwd = scan_root if scan_root.is_dir() else PROJECT_ROOT
        kwargs: dict[str, Any] = {
            "cwd": str(cwd),
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "close_fds": True,
        }
        if os.name == "nt":
            # DETACHED_PROCESS keeps the child off our console so its output can never
            # scribble over the HUD; a new process group stops Ctrl+C propagating to it.
            flags = getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
            flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
            kwargs["creationflags"] = flags
            args: Any = command  # CreateProcess does its own parsing on Windows
        else:
            kwargs["start_new_session"] = True
            args = shlex.split(command)
        proc = subprocess.Popen(args, **kwargs)
        with self._state_lock:
            self._spawned.append(proc)
        return proc.pid

    def _step_check_ollama(self) -> tuple[str, str]:
        """Confirm the model host answers before the operator asks it anything."""
        url = f"{str(settings.OLLAMA_HOST).rstrip('/')}/api/version"
        try:
            response = httpx.get(url, timeout=_OLLAMA_PING_TIMEOUT)
        except httpx.HTTPError as exc:
            # A cold daemon is a finding, not a protocol failure — hence warn, not fail.
            return "warn", (
                f"{url} unreachable ({type(exc).__name__}); bring it up with `ollama serve`"
            )
        if response.status_code != 200:
            return "warn", f"{url} returned HTTP {response.status_code}"
        try:
            version = str(response.json().get("version", "unknown"))
        except ValueError:
            version = "unknown"
        return "ok", (
            f"Ollama {version} responding at {settings.OLLAMA_HOST}, model {settings.MODEL_NAME}"
        )

    # ----------------------------------------------------------------------------------
    # VERONICA
    # ----------------------------------------------------------------------------------
    def _run_veronica(self) -> ProtocolResult:
        """Seal the perimeter: lockdown on, protected paths armed, memory hogs handled."""
        steps = [
            self._step("Lockdown", self._step_engage_lockdown),
            self._step("Protected paths", self._step_arm_protected_paths),
            self._step("Process sweep", self._step_process_sweep),
        ]
        success = all(step.ok for step in steps)
        title = settings.USER_TITLE
        mode = "dry run" if settings.VERONICA_DRY_RUN else "live"
        if success:
            summary = (
                f"Veronica is engaged, {title}. Destructive filesystem operations are refused "
                f"and the process sweep ran in {mode} mode. Clean Slate lifts the lockdown."
            )
        else:
            faults = ", ".join(step.label for step in steps if step.status == "fail")
            summary = (
                f"Veronica is engaged, {title}, though {faults} did not complete cleanly. "
                f"The lockdown itself holds."
            )
        return ProtocolResult(
            key="veronica",
            display_name=self._protocols["veronica"].display_name,
            success=success,
            steps=steps,
            summary=summary,
            palette=PALETTE_VERONICA,
        )

    def _step_engage_lockdown(self) -> tuple[str, str]:
        """Raise the flag every destructive tool call is checked against."""
        with self._state_lock:
            self._lockdown = True
        return "ok", "destructive filesystem operations are now refused"

    def _step_arm_protected_paths(self) -> tuple[str, str]:
        """Re-read the protected path list so an edited .env takes effect on re-run."""
        paths = self._register_protected_paths()
        if not paths:
            return "skipped", "no VERONICA_PROTECTED_PATHS configured"
        return "ok", f"{len(paths)} protected path(s): " + "; ".join(str(p) for p in paths)

    def _step_process_sweep(self) -> tuple[str, str]:
        """Find processes above the RSS threshold, sparing everything that matters.

        The exclusion set is deliberately generous: essential OS processes, terminals,
        the model host, service-account processes, PID 0/4, and this interpreter's entire
        process tree — ancestors and descendants alike.
        """
        threshold = float(settings.VERONICA_KILL_THRESHOLD_MB)
        protected_pids = self._protected_pids()
        candidates: list[tuple[psutil.Process, str, float]] = []
        skipped_essential = 0
        inspected = 0

        for proc in psutil.process_iter(["pid", "name", "memory_info", "username"]):
            try:
                info = proc.info
                pid = int(info.get("pid") or proc.pid)
                inspected += 1
                if pid in _UNTOUCHABLE_PIDS or pid in protected_pids:
                    skipped_essential += 1
                    continue
                name = str(info.get("name") or "").strip() or f"pid {pid}"
                if _process_base_name(name) in ESSENTIAL_PROCESS_NAMES:
                    skipped_essential += 1
                    continue
                if _is_system_account(info.get("username")):
                    skipped_essential += 1
                    continue
                mem = info.get("memory_info")
                rss_mb = float(getattr(mem, "rss", 0) or 0) / _MB
                if rss_mb < threshold:
                    continue
                candidates.append((proc, name, rss_mb))
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
            except (ValueError, TypeError, OSError):
                continue

        candidates.sort(key=lambda item: item[2], reverse=True)
        listing = ", ".join(
            f"{name} (pid {proc.pid}, {rss:.0f} MB)" for proc, name, rss in candidates[:12]
        )
        preamble = (
            f"{inspected} processes inspected, {skipped_essential} protected, "
            f"threshold {threshold:.0f} MB"
        )

        if not candidates:
            return "ok", f"{preamble}; nothing above threshold"

        if settings.VERONICA_DRY_RUN:
            return "ok", (
                f"{preamble}; {len(candidates)} above threshold, dry run — "
                f"nothing terminated: {listing}"
            )

        terminated: list[str] = []
        refused: list[str] = []
        for proc, name, rss in candidates:
            try:
                proc.terminate()  # polite SIGTERM equivalent; never SIGKILL
                terminated.append(f"{name} (pid {proc.pid}, {rss:.0f} MB)")
            except psutil.NoSuchProcess:
                continue
            except (psutil.Error, OSError) as exc:
                refused.append(f"{name} (pid {proc.pid}): {type(exc).__name__}")

        _gone, alive = psutil.wait_procs(
            [proc for proc, _name, _rss in candidates], timeout=_TERMINATE_WAIT_SECONDS
        )
        survivors = [f"pid {proc.pid}" for proc in alive]

        detail = f"{preamble}; terminated {len(terminated)}: {', '.join(terminated) or 'none'}"
        if refused:
            detail += f" | access denied: {', '.join(refused)}"
        if survivors:
            detail += (
                f" | still running after {_TERMINATE_WAIT_SECONDS:.0f}s: {', '.join(survivors)}"
            )
        return ("warn" if (refused or survivors) else "ok"), detail

    def _protected_pids(self) -> set[int]:
        """PIDs VERONICA must never signal: the kernel stubs and our own process tree."""
        pids: set[int] = set(_UNTOUCHABLE_PIDS)
        pids.add(os.getpid())
        if hasattr(os, "getppid"):
            pids.add(os.getppid())
        try:
            me = psutil.Process()
            pids.add(me.pid)
            parents = getattr(me, "parents", None)
            if callable(parents):
                pids.update(parent.pid for parent in parents())
            else:  # very old psutil: walk the ancestry by hand
                node: psutil.Process | None = me
                for _ in range(32):
                    node = node.parent() if node is not None else None
                    if node is None:
                        break
                    pids.add(node.pid)
            # Children too: the dev commands House Party launched are our responsibility,
            # and a build server is exactly the sort of thing that crosses the RSS line.
            pids.update(child.pid for child in me.children(recursive=True))
        except (psutil.Error, OSError):
            logger.debug("Could not enumerate the local process tree", exc_info=True)
        with self._state_lock:
            pids.update(proc.pid for proc in self._spawned)
        return pids

    # ----------------------------------------------------------------------------------
    # CLEAN SLATE
    # ----------------------------------------------------------------------------------
    def _run_clean_slate(self) -> ProtocolResult:
        """Reset context and display, purge the project's disposable bytes, stand down."""
        steps = [
            self._step("Conversation memory", self._step_reset_agent),
            self._step("Transcript", self._step_clear_transcript),
            self._step("Byte-code cache", self._step_purge_pycache),
            self._step("Working cache", self._step_purge_jarvis_cache),
            self._step("Log files", self._step_truncate_logs),
            self._step("Posture", self._step_reset_posture),
        ]
        success = all(step.ok for step in steps)
        title = settings.USER_TITLE
        if success:
            summary = (
                f"Clean Slate complete, {title}. Context cleared, caches purged, display reset "
                f"and any lockdown lifted."
            )
        else:
            faults = ", ".join(step.label for step in steps if step.status == "fail")
            summary = (
                f"Clean Slate ran, {title}, though {faults} did not complete. "
                f"Everything else is reset."
            )
        return ProtocolResult(
            key="clean_slate",
            display_name=self._protocols["clean_slate"].display_name,
            success=success,
            steps=steps,
            summary=summary,
            palette=PALETTE_CLEAN_SLATE,
        )

    def _step_reset_agent(self) -> tuple[str, str]:
        """Drop the conversation history the agent is carrying."""
        agent = self._agent
        if agent is None:
            return "skipped", "no agent bound"
        reset = getattr(agent, "reset", None)
        if not callable(reset):
            return "skipped", "agent does not support reset"
        reset()
        return "ok", "conversation memory cleared"

    def _step_clear_transcript(self) -> tuple[str, str]:
        """Blank the HUD transcript panel."""
        hud = self._hud
        if hud is None:
            return "skipped", "no HUD bound"
        clear = getattr(hud, "clear_transcript", None)
        if not callable(clear):
            return "skipped", "HUD does not support clearing the transcript"
        clear()
        return "ok", "transcript cleared"

    def _step_purge_pycache(self) -> tuple[str, str]:
        """Remove ``__pycache__`` trees and loose byte-code beneath the project root."""
        removed_dirs = 0
        removed_files = 0
        errors: list[str] = []

        for dirpath, dirnames, filenames in os.walk(
            PROJECT_ROOT, topdown=True, onerror=lambda _e: None
        ):
            current = Path(dirpath)
            for name in list(dirnames):
                if name == "__pycache__":
                    dirnames.remove(name)  # never descend into what we are deleting
                    if self._safe_rmtree(current / name, errors):
                        removed_dirs += 1
                elif name in _SKIP_DIRS:
                    dirnames.remove(name)
            for filename in filenames:
                if filename.endswith((".pyc", ".pyo")):
                    if self._safe_unlink(current / filename, errors):
                        removed_files += 1

        detail = (
            f"{removed_dirs} __pycache__ director(y/ies) and "
            f"{removed_files} byte-code file(s) removed"
        )
        if errors:
            return "warn", f"{detail}; {len(errors)} could not be removed: {'; '.join(errors[:5])}"
        return "ok", detail

    def _step_purge_jarvis_cache(self) -> tuple[str, str]:
        """Empty ``PROJECT_ROOT/.jarvis_cache`` without removing the directory itself."""
        cache_dir = PROJECT_ROOT / ".jarvis_cache"
        if not cache_dir.exists():
            return "skipped", f"{cache_dir} does not exist"
        if not cache_dir.is_dir():
            return "warn", f"{cache_dir} is not a directory"

        try:
            entries = list(cache_dir.iterdir())
        except OSError as exc:
            return "warn", f"{cache_dir} could not be listed: {exc}"

        removed = 0
        errors: list[str] = []
        for entry in entries:
            if entry.is_dir() and not entry.is_symlink():
                if self._safe_rmtree(entry, errors):
                    removed += 1
            elif self._safe_unlink(entry, errors):
                removed += 1

        detail = f"{removed} cache entr(y/ies) removed from {cache_dir}"
        if errors:
            return "warn", f"{detail}; {len(errors)} failed: {'; '.join(errors[:5])}"
        return "ok", detail

    def _step_truncate_logs(self) -> tuple[str, str]:
        """Truncate project log files that have grown past 5 MB."""
        candidates: list[Path] = []
        log_file = Path(settings.LOG_FILE)
        if log_file.exists():
            candidates.append(log_file)

        for dirpath, dirnames, filenames in os.walk(
            PROJECT_ROOT, topdown=True, onerror=lambda _e: None
        ):
            current = Path(dirpath)
            try:
                depth = len(current.relative_to(PROJECT_ROOT).parts)
            except ValueError:
                depth = _REPO_SCAN_MAX_DEPTH
            if depth >= _REPO_SCAN_MAX_DEPTH:
                dirnames[:] = []
            else:
                dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
            for filename in filenames:
                if filename.endswith(".log"):
                    candidates.append(current / filename)

        truncated: list[str] = []
        errors: list[str] = []
        skipped = 0
        seen: set[Path] = set()

        for candidate in candidates:
            try:
                resolved = assert_under_project_root(candidate)
            except ValueError:
                skipped += 1  # a log outside the project is not ours to touch
                continue
            if resolved in seen:
                continue
            seen.add(resolved)
            try:
                size = resolved.stat().st_size
                if size <= _LOG_TRUNCATE_BYTES:
                    continue
                stamp = time.strftime("%Y-%m-%d %H:%M:%S")
                # Rewriting in place rather than deleting keeps any live logging handler's
                # file object valid; it simply carries on appending after the marker.
                with open(resolved, "w", encoding="utf-8", errors="replace") as handle:
                    handle.write(
                        f"# truncated by CLEAN SLATE at {stamp} (was {size / _MB:.1f} MB)\n"
                    )
                truncated.append(f"{resolved.name} ({size / _MB:.1f} MB)")
            except OSError as exc:
                errors.append(f"{resolved.name}: {exc}")

        if not truncated and not errors:
            note = f"no log file exceeded {_LOG_TRUNCATE_BYTES // _MB} MB"
            if skipped:
                note += f"; {skipped} log(s) outside the project root left alone"
            return "ok", note
        detail = f"truncated {len(truncated)}: {', '.join(truncated) or 'none'}"
        if errors:
            return "warn", f"{detail}; failures: {'; '.join(errors[:5])}"
        return "ok", detail

    def _step_reset_posture(self) -> tuple[str, str]:
        """Standard palette, no active protocol, no lockdown."""
        self.deactivate()
        return "ok", f"lockdown cleared, palette reset to {PALETTE_STANDARD}"

    # -- deletion primitives -----------------------------------------------------------
    def _safe_unlink(self, path: Path, errors: list[str]) -> bool:
        """Delete a single file, but only after proving it lives under ``PROJECT_ROOT``."""
        try:
            if path.is_symlink():
                # Resolving a symlink would judge its target, not the link. Removing the
                # link itself is safe as long as the link lives inside the project.
                parent = assert_under_project_root(path.parent)
                (parent / path.name).unlink()
                return True
            resolved = assert_under_project_root(path)
            if not resolved.is_file():
                return False
            resolved.unlink()
            return True
        except ValueError as exc:
            logger.warning("Refused deletion outside project root: %s (%s)", path, exc)
            errors.append(f"{path}: outside project root")
            return False
        except OSError as exc:
            errors.append(f"{path}: {exc}")
            return False

    def _safe_rmtree(self, path: Path, errors: list[str]) -> bool:
        """Recursively delete a directory that provably resolves under ``PROJECT_ROOT``."""
        try:
            if path.is_symlink():
                # Never follow a symlinked directory into someone else's tree; the link
                # is inside the project, its target may very well not be.
                parent = assert_under_project_root(path.parent)
                (parent / path.name).unlink()
                return True
            resolved = assert_under_project_root(path)
            if not resolved.is_dir():
                return False
            shutil.rmtree(resolved, ignore_errors=False)
            return True
        except ValueError as exc:
            logger.warning("Refused recursive deletion outside project root: %s (%s)", path, exc)
            errors.append(f"{path}: outside project root")
            return False
        except OSError as exc:
            errors.append(f"{path}: {exc}")
            return False

    # -- internals ---------------------------------------------------------------------
    def _step(self, label: str, action: Callable[[], tuple[str, str]]) -> StepResult:
        """Run one protocol step, converting any exception into a failed ``StepResult``."""
        started = time.monotonic()
        try:
            status, detail = action()
        except Exception as exc:  # a step is allowed to fail; a protocol is not
            logger.exception("Protocol step %r failed", label)
            return StepResult(label=label, status="fail", detail=f"{type(exc).__name__}: {exc}")
        logger.debug("Protocol step %r -> %s in %.2fs", label, status, time.monotonic() - started)
        if status not in _STATUS_LABEL:
            status = "ok"
        return StepResult(label=label, status=status, detail=detail)

    def _enter(self, protocol: Protocol) -> None:
        """Mark a protocol active, repaint the HUD and speak the acknowledgement."""
        with self._state_lock:
            self._active = protocol.key
        self._hud_call("set_palette", protocol.palette)
        self._hud_call("set_protocol", protocol.display_name)
        self._hud_call("log_system", f"{protocol.display_name} engaged", "warn")

        template = PROTOCOL_ACK.get(protocol.key)
        if template:
            # personalise() interpolates settings.USER_TITLE at call time, so a title
            # chosen during onboarding or changed via /title is honoured immediately.
            self._voice_call("speak", personalise(template))
        logger.info("Protocol %s engaged", protocol.key)

    def _report(self, result: ProtocolResult) -> None:
        """Push the outcome of a run at the HUD."""
        levels = {"ok": "info", "warn": "warn", "fail": "error", "skipped": "info"}
        self._hud_call(
            "log_system",
            f"{result.display_name}: {result.summary}",
            "success" if result.success else "error",
        )
        for step in result.steps:
            self._hud_call("log_system", step.format_line(), levels.get(step.status, "info"))
        logger.info("Protocol %s finished (success=%s)", result.key, result.success)

    def _register_protected_paths(self) -> list[Path]:
        """Resolve ``settings.VERONICA_PROTECTED_PATHS`` into the guard's veto list."""
        resolved: list[Path] = []
        for raw in list(settings.VERONICA_PROTECTED_PATHS):
            text = str(raw).strip()
            if not text:
                continue
            candidate = _resolve_quietly(os.path.expandvars(text))
            if candidate is not None:
                resolved.append(candidate)
        with self._state_lock:
            self._protected_paths = resolved
        return resolved

    def _match_protected(self, path: str) -> str | None:
        """Return the protected root containing ``path``, or ``None``."""
        with self._state_lock:
            protected = list(self._protected_paths)
        if not protected:
            return None
        target = _resolve_quietly(path)
        if target is None:
            return None
        for root in protected:
            if target == root or target.is_relative_to(root):
                return str(root)
        return None

    def _hud_call(self, method: str, *args: Any) -> None:
        """Call a HUD method if one is bound; a display fault never fails a protocol."""
        hud = self._hud
        if hud is None:
            return
        func = getattr(hud, method, None)
        if not callable(func):
            return
        try:
            func(*args)
        except Exception:
            logger.debug("HUD call %s failed", method, exc_info=True)

    def _voice_call(self, method: str, *args: Any) -> None:
        """Call a voice method if one is bound, swallowing audio-stack faults."""
        voice = self._voice
        if voice is None:
            return
        func = getattr(voice, method, None)
        if not callable(func):
            return
        try:
            func(*args)
        except Exception:
            logger.debug("Voice call %s failed", method, exc_info=True)
