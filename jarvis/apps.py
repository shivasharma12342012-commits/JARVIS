"""Opening and closing applications on the operator's machine.

Resolution is the hard part, not launching. "open vscode" has to find an application
that might be a binary on ``PATH``, a Start Menu shortcut, a Windows Store package with
a ``ms-`` URI, a registered protocol handler, or a plain document that wants whatever
program owns its extension. This module tries all of those, in the order most likely to
produce what the operator meant.

Nothing here asks for permission -- :mod:`jarvis.permissions` owns that decision, and
:mod:`jarvis.tools` calls it before calling anything here. Keeping consent out of this
module means the resolution logic can be tested and reasoned about on its own.

Imports :mod:`config` and :mod:`psutil` only.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import psutil

from config import settings

logger = logging.getLogger(__name__)

IS_WINDOWS = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"

if IS_WINDOWS:  # pragma: no cover - platform specific
    _DETACHED = (
        getattr(subprocess, "DETACHED_PROCESS", 0)
        | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    )
    _NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
else:  # pragma: no cover - platform specific
    _DETACHED = 0
    _NO_WINDOW = 0


@dataclass
class AppTarget:
    """Something that can be opened."""

    name: str
    kind: str          # binary | shortcut | uri | document | shell
    path: str
    args: list[str] = field(default_factory=list)

    def describe(self) -> str:
        return f"{self.name} ({self.kind})"


# ══════════════════════════════════════════════════════════════════════════════════════
# Known applications
# ══════════════════════════════════════════════════════════════════════════════════════

#: alias -> candidate launch targets, tried in order. A ``ms-`` or ``http`` entry is a
#: URI; anything else is looked up on PATH first, then in the Start Menu.
KNOWN_APPS: dict[str, tuple[str, ...]] = {
    # --- Windows built-ins ---
    "notepad": ("notepad.exe",),
    "calculator": ("calc.exe", "ms-calculator:"),
    "calc": ("calc.exe", "ms-calculator:"),
    "paint": ("mspaint.exe",),
    "wordpad": ("write.exe",),
    "explorer": ("explorer.exe",),
    "file explorer": ("explorer.exe",),
    "task manager": ("taskmgr.exe",),
    "taskmgr": ("taskmgr.exe",),
    "control panel": ("control.exe",),
    "settings": ("ms-settings:",),
    "cmd": ("cmd.exe",),
    "command prompt": ("cmd.exe",),
    "powershell": ("pwsh.exe", "powershell.exe"),
    "terminal": ("wt.exe", "pwsh.exe", "powershell.exe"),
    "windows terminal": ("wt.exe",),
    "registry editor": ("regedit.exe",),
    "snipping tool": ("snippingtool.exe", "ms-screenclip:"),
    "camera": ("microsoft.windows.camera:",),
    "photos": ("ms-photos:",),
    "store": ("ms-windows-store:",),
    "maps": ("bingmaps:",),
    "clock": ("ms-clock:",),
    # --- Editors and developer tools ---
    "code": ("code.cmd", "code.exe", "code"),
    "vscode": ("code.cmd", "code.exe", "code"),
    "visual studio code": ("code.cmd", "code.exe", "code"),
    "notepad++": ("notepad++.exe",),
    "sublime": ("subl.exe", "sublime_text.exe"),
    "pycharm": ("pycharm64.exe", "pycharm.exe"),
    "idea": ("idea64.exe",),
    "intellij": ("idea64.exe",),
    "git bash": ("git-bash.exe",),
    "postman": ("postman.exe",),
    "docker": ("docker desktop.exe", "dockerdesktop.exe"),
    # --- Browsers ---
    "chrome": ("chrome.exe",),
    "google chrome": ("chrome.exe",),
    "edge": ("msedge.exe",),
    "microsoft edge": ("msedge.exe",),
    "firefox": ("firefox.exe",),
    "brave": ("brave.exe",),
    "opera": ("opera.exe",),
    # --- Office and productivity ---
    "word": ("winword.exe",),
    "excel": ("excel.exe",),
    "powerpoint": ("powerpnt.exe",),
    "outlook": ("outlook.exe",),
    "onenote": ("onenote.exe",),
    "teams": ("teams.exe", "ms-teams:"),
    "notion": ("notion.exe",),
    "obsidian": ("obsidian.exe",),
    # --- Media and chat ---
    "spotify": ("spotify.exe",),
    "vlc": ("vlc.exe",),
    "discord": ("discord.exe", "update.exe"),
    "slack": ("slack.exe",),
    "whatsapp": ("whatsapp.exe",),
    "telegram": ("telegram.exe",),
    "zoom": ("zoom.exe",),
    "steam": ("steam.exe",),
    "obs": ("obs64.exe", "obs.exe"),
    # --- macOS / Linux friendly names ---
    "finder": ("open",),
    "safari": ("safari",),
    "textedit": ("textedit",),
}

#: Windows shell protocols we can hand straight to the OS.
_URI_RE = re.compile(r"^[a-z][a-z0-9+.\-]*:", re.IGNORECASE)
_WEB_RE = re.compile(r"^(https?://|www\.)", re.IGNORECASE)


def _start_menu_dirs() -> list[Path]:
    """Where Windows keeps its shortcuts."""
    roots: list[Path] = []
    for variable in ("APPDATA", "ProgramData"):
        base = os.environ.get(variable)
        if base:
            roots.append(Path(base) / "Microsoft" / "Windows" / "Start Menu" / "Programs")
    desktop = os.environ.get("USERPROFILE")
    if desktop:
        roots.append(Path(desktop) / "Desktop")
    return [r for r in roots if r.is_dir()]


def search_start_menu(name: str, limit: int = 400) -> str | None:
    """Find a ``.lnk`` whose name looks like ``name``.

    Scored rather than first-match: "code" should find "Visual Studio Code" and not
    "Codec Pack Uninstaller", so an exact stem beats a prefix, which beats a substring.
    """
    if not IS_WINDOWS:
        return None
    wanted = name.strip().lower()
    if not wanted:
        return None

    best: tuple[int, str] | None = None
    scanned = 0
    for root in _start_menu_dirs():
        try:
            for path in root.rglob("*.lnk"):
                scanned += 1
                if scanned > limit:
                    break
                stem = path.stem.lower()
                if stem == wanted:
                    score = 100
                elif stem.startswith(wanted):
                    score = 70 - len(stem)
                elif wanted in stem:
                    score = 40 - len(stem)
                else:
                    continue
                if best is None or score > best[0]:
                    best = (score, str(path))
        except (OSError, PermissionError):
            continue
    return best[1] if best else None


def resolve(target: str) -> AppTarget | None:
    """Work out what the operator meant. ``None`` when nothing matches."""
    raw = str(target or "").strip().strip('"')
    if not raw:
        return None
    lowered = raw.lower()

    # 1. A web address.
    if _WEB_RE.match(raw):
        url = raw if raw.lower().startswith("http") else f"https://{raw}"
        return AppTarget(raw, "uri", url)

    # 2. A known alias.
    for candidate in KNOWN_APPS.get(lowered, ()):
        if _URI_RE.match(candidate) and not Path(candidate).suffix == ".exe":
            return AppTarget(raw, "uri", candidate)
        found = shutil.which(candidate)
        if found:
            return AppTarget(raw, "binary", found)
        shortcut = search_start_menu(Path(candidate).stem)
        if shortcut:
            return AppTarget(raw, "shortcut", shortcut)

    # 3. Something already on PATH.
    found = shutil.which(raw)
    if found:
        return AppTarget(raw, "binary", found)

    # 4. An existing file or directory -- open it with whatever owns it.
    try:
        path = Path(raw).expanduser()
        if path.exists():
            return AppTarget(path.name, "document", str(path.resolve()))
    except (OSError, ValueError):
        pass

    # 5. A shell protocol such as ms-settings: or mailto:.
    if _URI_RE.match(raw):
        return AppTarget(raw, "uri", raw)

    # 6. A Start Menu entry under any name.
    shortcut = search_start_menu(raw)
    if shortcut:
        return AppTarget(raw, "shortcut", shortcut)

    return None


def launch(
    app: AppTarget, args: list[str] | None = None, wait: bool = False
) -> tuple[bool, str, int | None]:
    """Start ``app``. Returns ``(ok, message, pid)``.

    Launched detached on purpose: the operator's editor should outlive the assistant
    that opened it, and a GUI application inheriting our console produces a stray
    window on Windows.
    """
    extra = [str(a) for a in (args or [])]
    try:
        if app.kind in {"uri", "document", "shortcut"} and not extra:
            if IS_WINDOWS:
                # startfile is the only call that honours file associations and
                # shell protocols properly.
                os.startfile(app.path)  # noqa: S606 - intentional shell association
                return True, f"Opened {app.name}.", None
            opener = "open" if IS_MAC else "xdg-open"
            proc = subprocess.Popen(
                [opener, app.path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return True, f"Opened {app.name}.", proc.pid

        argv = [app.path, *extra]
        proc = subprocess.Popen(
            argv,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            creationflags=_DETACHED if IS_WINDOWS else 0,
            start_new_session=not IS_WINDOWS,
        )
        if wait:
            try:
                code = proc.wait(timeout=settings.APP_WAIT_TIMEOUT)
                return code == 0, f"{app.name} exited with code {code}.", proc.pid
            except subprocess.TimeoutExpired:
                return True, f"{app.name} is still running.", proc.pid
        # A process that dies instantly did not really launch.
        time.sleep(0.25)
        if proc.poll() not in (None, 0):
            return (
                False,
                f"{app.name} exited immediately with code {proc.returncode}.",
                proc.pid,
            )
        return True, f"Opened {app.name}.", proc.pid
    except FileNotFoundError:
        return False, f"Could not find {app.path}.", None
    except PermissionError as exc:
        return False, f"Windows refused to launch {app.name}: {exc}", None
    except OSError as exc:
        return False, f"Could not launch {app.name}: {exc}", None


#: Processes that are part of the machine, not the operator's work.
_SYSTEM_PROCESSES = {
    "system", "system idle process", "registry", "csrss.exe", "wininit.exe",
    "winlogon.exe", "services.exe", "lsass.exe", "smss.exe", "dwm.exe",
    "svchost.exe", "fontdrvhost.exe", "conhost.exe", "explorer.exe",
    "ollama.exe", "ollama app.exe", "kernel_task", "launchd", "systemd",
}


def _protected_pids() -> set[int]:
    """This process, its parents and its children -- never to be closed."""
    protected = {0, 4}
    try:
        me = psutil.Process()
        protected.add(me.pid)
        for parent in me.parents():
            protected.add(parent.pid)
        for child in me.children(recursive=True):
            protected.add(child.pid)
    except Exception:
        logger.debug("Could not map the protected process tree", exc_info=True)
    return protected


def running_apps(limit: int = 40) -> list[dict]:
    """User-facing processes, heaviest first, one row per application name."""
    grouped: dict[str, dict] = {}
    for proc in psutil.process_iter(["pid", "name", "memory_info", "username"]):
        try:
            info = proc.info
            name = (info.get("name") or "").strip()
            if not name or name.lower() in _SYSTEM_PROCESSES:
                continue
            memory = info.get("memory_info")
            megabytes = (memory.rss / (1024 * 1024)) if memory else 0.0
            row = grouped.setdefault(
                name, {"name": name, "pids": [], "memory_mb": 0.0, "count": 0}
            )
            row["pids"].append(info.get("pid"))
            row["memory_mb"] += megabytes
            row["count"] += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied, AttributeError):
            continue
    rows = sorted(grouped.values(), key=lambda r: r["memory_mb"], reverse=True)
    return rows[:limit]


#: Below this length a substring match is meaningless -- "e" matched 96 processes on the
#: development machine, which is not a request to close an application, it is a massacre.
_MIN_FUZZY_LEN = 4


def find_processes(name: str) -> list[psutil.Process]:
    """Closable processes matching ``name``, most specific interpretation first.

    Exact matches win outright. Substring matching is only consulted when nothing
    matched exactly *and* the query is long enough to mean something -- otherwise a
    vague name quietly selects half the process table, and the operator approves a
    prompt that says "close e" without any idea of the blast radius.
    """
    wanted = str(name or "").strip().lower()
    if not wanted:
        return []
    stem = Path(wanted).stem
    protected = _protected_pids()

    exact: list[psutil.Process] = []
    fuzzy: list[psutil.Process] = []
    for proc in psutil.process_iter(["pid", "name"]):
        try:
            if proc.pid in protected:
                continue
            process_name = (proc.info.get("name") or "").lower()
            if not process_name or process_name in _SYSTEM_PROCESSES:
                continue
            if process_name == wanted or Path(process_name).stem == stem:
                exact.append(proc)
            elif len(stem) >= _MIN_FUZZY_LEN and stem in process_name:
                fuzzy.append(proc)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return exact or fuzzy


def close_app(name: str, force: bool = False) -> tuple[int, list[str]]:
    """Ask an application to close. Returns ``(closed_count, notes)``.

    Politely first: ``terminate`` lets the program save and shut down. ``force`` only
    escalates to ``kill`` for the ones that ignored the request.
    """
    processes = find_processes(name)
    if not processes:
        return 0, [f"Nothing matching '{name}' is running."]

    notes: list[str] = []
    for proc in processes:
        try:
            proc.terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied) as exc:
            notes.append(f"pid {proc.pid}: {exc.__class__.__name__}")

    gone, alive = psutil.wait_procs(processes, timeout=settings.APP_CLOSE_TIMEOUT)
    closed = len(gone)

    if alive and force:
        for proc in alive:
            try:
                proc.kill()
                closed += 1
                notes.append(f"pid {proc.pid} had to be forced.")
            except (psutil.NoSuchProcess, psutil.AccessDenied) as exc:
                notes.append(f"pid {proc.pid} refused to close: {exc.__class__.__name__}")
    elif alive:
        notes.append(
            f"{len(alive)} process(es) ignored the close request; "
            f"ask again with force if you want them killed outright."
        )
    return closed, notes


def focus_app(name: str) -> tuple[bool, str]:
    """Bring a window to the front. Windows only, best effort."""
    if not IS_WINDOWS:
        return False, "Window focus is only implemented on Windows."
    processes = find_processes(name)
    if not processes:
        return False, f"Nothing matching '{name}' is running."
    title = Path(processes[0].info.get("name") or name).stem
    script = (
        "$ErrorActionPreference='SilentlyContinue';"
        "Add-Type -AssemblyName Microsoft.VisualBasic;"
        f"[Microsoft.VisualBasic.Interaction]::AppActivate('{title}')"
    )
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True, text=True, timeout=10,
            encoding="utf-8", errors="replace", creationflags=_NO_WINDOW,
        )
        if proc.returncode == 0:
            return True, f"Brought {title} to the front."
        return False, f"Could not focus {title}."
    except (subprocess.TimeoutExpired, OSError) as exc:
        return False, f"Could not focus {title}: {exc}"
