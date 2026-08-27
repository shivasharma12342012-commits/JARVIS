"""The always-listening wake daemon.

This is deliberately *not* the assistant. It is a small, cheap process that parks on
the microphone waiting for the call word, says hello, and then opens a **new terminal
window** running the real thing (``main.py``). Keeping the two apart means the heavy
process -- model, HUD, tools, monitor -- only exists while the operator is actually
talking to it, and the machine is not carrying a full ReAct loop around all day.

Two details shape everything else in this module:

**Microphone contention.** The moment the daemon spawns an interactive session, both
processes want the same input device, and two simultaneous streams on one device
produce garbage on every host API worth naming. So the daemon *suspends its own
listening for as long as the child lives* and resumes only once the child exits. That
is why it keeps the :class:`subprocess.Popen` handle at all: not to control the child,
but to know when it is safe to open the microphone again.

**Language.** ``settings.WAKE_GREETING`` is a configured, literal line -- Devanagari by
default. It must be spoken in the language it is *written* in, not in whatever locale
happened to be active, or the operator's honorific comes out as a Latin "Sir" dropped
into the middle of a Hindi sentence, in a British voice. So the locale is detected from
the greeting text itself and carried through both the personalisation and the speech.

Imports :mod:`config`, :mod:`jarvis.locales`, :mod:`jarvis.prompts` and the standard
library. The voice system is *injected*, never imported: the daemon duck-types whatever
``main.py`` hands it, which keeps this module testable without an audio stack.
"""

from __future__ import annotations

import inspect
import logging
import shlex
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

from config import PROJECT_ROOT, settings
from jarvis import locales, prompts

logger = logging.getLogger(__name__)

#: The script a spawned session runs; used to recognise that session later.
MAIN_SCRIPT = PROJECT_ROOT / "main.py"

IS_WINDOWS = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"

#: A wake session must get its *own* console, not inherit ours -- the whole point is a
#: visible window the operator can type into.
if IS_WINDOWS:  # pragma: no cover - platform specific
    _NEW_CONSOLE = getattr(subprocess, "CREATE_NEW_CONSOLE", 0) | getattr(
        subprocess, "CREATE_NEW_PROCESS_GROUP", 0
    )
else:  # pragma: no cover - platform specific
    _NEW_CONSOLE = 0

#: How often :meth:`WakeDaemon.run_forever` wakes to re-check the stop flag. Short
#: enough that Ctrl+C feels instant, long enough to cost nothing.
_TICK_SECONDS = 0.4

#: Upper bound on how long we will hold the microphone shut waiting for a session that
#: never exits. A wedged child must not deafen the daemon forever.
_SESSION_WAIT_LIMIT = 12.0 * 3600.0

#: Type of the terminal-spawning hook. Takes a fully built argv, returns the child
#: handle (or ``None`` when the spawn failed). Replaceable -- see
#: :attr:`WakeDaemon.spawn`.
SpawnHook = Callable[[Sequence[str]], "subprocess.Popen[bytes] | None"]


# ══════════════════════════════════════════════════════════════════════════════════════
# Status
# ══════════════════════════════════════════════════════════════════════════════════════


@dataclass
class DaemonStatus:
    """A snapshot of what the daemon is doing, for ``--check`` and the HUD."""

    listening: bool
    wakes: int
    last_wake: float | None
    session_active: bool

    def describe(self) -> str:
        """One human-readable line."""
        where = "listening" if self.listening else "microphone released"
        if self.session_active:
            where = "suspended (session in progress)"
        when = (
            time.strftime("%H:%M:%S", time.localtime(self.last_wake))
            if self.last_wake
            else "never"
        )
        return f"{where}; {self.wakes} wake(s), last at {when}"


# ══════════════════════════════════════════════════════════════════════════════════════
# Building the terminal command
# ══════════════════════════════════════════════════════════════════════════════════════


def session_argv(command: str = "") -> list[str]:
    """The child *assistant* argv, before any terminal wrapper is put around it.

    ``sys.executable`` rather than a hard-coded path: the daemon is already running
    under the project's virtual environment, and the session must run under the same
    one or it will not find a single dependency.
    """
    argv = [sys.executable, str(PROJECT_ROOT / "main.py")]
    command = (command or "").strip()
    if command:
        argv += ["--ask", command]
    return argv


def _ps_quote(value: str) -> str:
    """Single-quote a token for PowerShell, doubling any embedded quote."""
    return "'" + str(value).replace("'", "''") + "'"


def _applescript_quote(value: str) -> str:
    """Escape a string for embedding inside an AppleScript double-quoted literal."""
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


def _first_on_path(*candidates: str) -> str | None:
    """The first of ``candidates`` that :func:`shutil.which` can actually resolve."""
    for candidate in candidates:
        found = shutil.which(candidate)
        if found:
            return found
    return None


def _wt_argv(binary: str, child: Sequence[str], title: str) -> list[str]:
    """Windows Terminal: a brand-new window (``-w new``) with a named tab."""
    return [binary, "-w", "new", "nt", "--title", title, *child]


def _powershell_argv(binary: str, child: Sequence[str]) -> list[str]:
    """PowerShell: ``-NoExit`` so the window survives the session and can be read."""
    inner = "& " + " ".join(_ps_quote(part) for part in child)
    return [binary, "-NoExit", "-Command", inner]


def _cmd_argv(binary: str, child: Sequence[str], title: str) -> list[str]:
    """``cmd.exe``: ``start`` takes the window title as its first *quoted* argument.

    The title is deliberately empty. ``start`` decides between "title" and "program" by
    whether the argument is quoted, and Python only adds quotes to arguments containing
    spaces -- so a title like ``J.A.R.V.I.S.`` arrives bare and ``start`` tries to
    execute it. An empty string always renders as ``""``, which ``start`` reads as an
    empty title, and the real program follows.
    """
    return [binary, "/c", "start", "", *child]


def _osascript_argv(binary: str, child: Sequence[str]) -> list[str]:
    """macOS: drive Terminal.app properly.

    ``open -a Terminal`` cannot carry arguments through to the program it starts, so
    where ``osascript`` exists we ask Terminal to run the command as a script instead.
    """
    inner = _applescript_quote(shlex.join(child))
    return [binary, "-e", f'tell application "Terminal" to do script "{inner}"']


def terminal_argv(command: str = "", terminal: str | None = None) -> list[str] | None:
    """Build the full argv that opens a terminal window running the assistant.

    ``terminal`` overrides ``settings.DAEMON_TERMINAL``; either may be ``"auto"`` (try
    everything in order of preference) or the name of one specific binary, which is
    still resolved through ``PATH`` and still shaped correctly for whichever program it
    turns out to be.

    Returns ``None`` when this machine has no terminal we know how to drive -- the
    caller is expected to carry on listening rather than treat that as fatal.
    """
    child = session_argv(command)
    title = settings.AGENT_NAME or "Assistant"
    wanted = str(terminal if terminal is not None else settings.DAEMON_TERMINAL or "auto").strip()
    auto = (not wanted) or wanted.lower() == "auto"

    def shaped(binary: str) -> list[str] | None:
        """Wrap ``child`` in the flags that this particular terminal understands."""
        stem = Path(binary).name.lower()
        if stem in {"wt.exe", "wt"}:
            return _wt_argv(binary, child, title)
        if stem in {"powershell.exe", "powershell", "pwsh.exe", "pwsh"}:
            return _powershell_argv(binary, child)
        if stem in {"cmd.exe", "cmd"}:
            return _cmd_argv(binary, child, title)
        if stem in {"osascript"}:
            return _osascript_argv(binary, child)
        if stem in {"open"}:
            # Terminal.app takes no argv from ``open``; this opens a plain shell window
            # and is the last thing we try on macOS.
            return [binary, "-a", "Terminal"]
        if stem in {"gnome-terminal"}:
            return [binary, "--title", title, "--", *child]
        if stem in {"konsole"}:
            return [binary, "-p", f"tabtitle={title}", "-e", *child]
        if stem in {"x-terminal-emulator", "xterm", "xfce4-terminal", "alacritty", "kitty"}:
            return [binary, "-T", title, "-e", *child]
        # Something the operator named explicitly that we have no recipe for. Hand it
        # the command and hope it follows the -e convention; better than refusing.
        return [binary, "-e", *child]

    if not auto:
        resolved = _first_on_path(wanted)
        if resolved is None:
            logger.warning("DAEMON_TERMINAL=%r is not on PATH; falling back to auto", wanted)
        else:
            return shaped(resolved)

    order: tuple[str, ...]
    if IS_WINDOWS:
        order = ("wt.exe", "powershell.exe", "pwsh.exe", "cmd.exe")
    elif IS_MAC:
        order = ("osascript", "open")
    else:
        order = ("x-terminal-emulator", "gnome-terminal", "konsole", "xterm")

    resolved = _first_on_path(*order)
    if resolved is None:
        return None
    return shaped(resolved)


#: How long a launcher gets to exit before we conclude it was only a launcher.
#: Measured on this machine: `wt.exe` returns in 0.14 s having handed the real work to
#: a separate process, so waiting on its handle resumes the microphone almost
#: immediately -- with the assistant still running and holding the same device.
_LAUNCHER_GRACE = 3.0


class _SessionHandle:
    """Something that answers ``poll()`` for the assistant session, however it started.

    Windows Terminal, ``cmd /c start`` and most Linux terminal emulators fork the real
    program and exit. Their ``Popen`` handle therefore reports "finished" within a
    fraction of a second, which would tell the daemon the session is over and let it
    re-open the microphone underneath a live assistant.

    So when the launcher exits suspiciously fast, we go looking for the process it
    actually started -- a Python interpreter running our own ``main.py``, begun after we
    spawned -- and track that instead. If nothing is found we report "finished", which
    is the safe direction: the daemon resumes listening rather than going deaf forever.
    """

    def __init__(self, proc: Any, spawned_at: float) -> None:
        self._proc = proc
        self._spawned_at = spawned_at
        self._tracked: Any = None
        self._resolved = False

    def poll(self) -> "int | None":
        if self._proc is not None and self._proc.poll() is None:
            return None  # the launcher is still the session
        if not self._resolved:
            self._resolved = True
            self._tracked = self._find_session()
        if self._tracked is None:
            return 0
        try:
            return None if self._tracked.is_running() else 0
        except Exception:
            return 0

    def _find_session(self) -> Any:
        """Locate the interpreter running main.py that our launcher started."""
        elapsed = time.monotonic() - self._spawned_at
        if elapsed > _LAUNCHER_GRACE:
            # It ran long enough to have been the session itself.
            return None
        try:
            import psutil
        except ImportError:
            return None

        target = str(MAIN_SCRIPT).lower()
        me = None
        try:
            me = psutil.Process().pid
        except Exception:
            pass
        best = None
        for proc in psutil.process_iter(["pid", "name", "cmdline", "create_time"]):
            try:
                if proc.info.get("pid") == me:
                    continue
                cmdline = proc.info.get("cmdline") or []
                if not any(target in str(part).lower() for part in cmdline):
                    continue
                started = proc.info.get("create_time") or 0
                if started < self._wall_floor():
                    continue
                if best is None or started > (best.info.get("create_time") or 0):
                    best = proc
            except Exception:
                continue
        if best is not None:
            logger.debug("Tracking session pid %s", best.info.get("pid"))
        return best

    def _wall_floor(self) -> float:
        """Wall-clock instant just before we spawned, for comparing create_time."""
        return time.time() - (time.monotonic() - self._spawned_at) - 2.0


def spawn_detached(argv: Sequence[str]) -> "subprocess.Popen[bytes] | None":
    """Start ``argv`` in its own console, detached from ours.

    The default :attr:`WakeDaemon.spawn` hook. Returns ``None`` rather than raising:
    a terminal that will not open is a degraded daemon, not a dead one.
    """
    try:
        return subprocess.Popen(
            list(argv),
            cwd=str(PROJECT_ROOT),
            creationflags=_NEW_CONSOLE,
            close_fds=True,
        )
    except (OSError, ValueError) as exc:
        logger.warning("Could not open a terminal (%s): %s", exc.__class__.__name__, exc)
        return None


# ══════════════════════════════════════════════════════════════════════════════════════
# The daemon
# ══════════════════════════════════════════════════════════════════════════════════════


class WakeDaemon:
    """Background wake-word listener that opens interactive sessions on demand.

    ``voice`` is duck-typed: anything exposing ``speak``, ``start_listening`` and
    ``stop_listening`` will do. The daemon attaches itself to the voice system's wake
    and utterance callbacks, saving whatever was there before so :meth:`stop` can put
    them back -- ``main.py`` may well be sharing the same instance.
    """

    def __init__(
        self,
        voice: Any,
        on_wake: Callable[[str], None] | None = None,
        console: Any = None,
    ) -> None:
        self._voice = voice
        self._on_wake_cb = on_wake
        self._console = console

        #: Injection point. Tests (and anyone wanting a different launcher) replace
        #: this with their own callable; nothing else in the class spawns processes.
        self.spawn: SpawnHook = spawn_detached

        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._listening = False
        self._wakes = 0
        self._last_wake: float | None = None       # wall clock, for reporting
        self._last_wake_mono = 0.0                 # monotonic, for the cooldown
        self._session: "subprocess.Popen[bytes] | None" = None
        self._watcher: threading.Thread | None = None
        self._attached = False
        self._prev_wake: Any = None
        self._prev_utterance: Any = None
        self._speak_locale_kwarg: bool | None = None

    # ----------------------------------------------------------------------------------
    # Lifecycle
    # ----------------------------------------------------------------------------------

    def start(self) -> bool:
        """Begin background listening. ``False`` when there is no usable microphone."""
        with self._lock:
            if self._listening:
                return True
            self._stop_event.clear()
            self._attach()
            started = False
            starter = getattr(self._voice, "start_listening", None)
            if callable(starter):
                try:
                    started = bool(starter())
                except Exception:
                    logger.exception("The voice system refused to start listening")
                    started = False
            else:
                logger.error("The injected voice system has no start_listening()")
            if not started:
                self._detach()
                self._report("No microphone available; the wake daemon cannot listen.", "warn")
                return False
            self._listening = True
        words = ", ".join(f'"{w}"' for w in settings.WAKE_WORDS) or "(none configured)"
        self._report(f"Wake daemon armed. Waiting for {words}.", "info")
        return True

    def stop(self) -> None:
        """Release the microphone and unhook. Idempotent, and safe from any thread."""
        self._stop_event.set()
        with self._lock:
            was_listening = self._listening
            self._listening = False
        self._suspend_listening()
        self._detach()
        if was_listening:
            self._report("Wake daemon stood down.", "info")

    def run_forever(self) -> int:
        """Block until Ctrl+C. Returns the process exit code.

        Whatever happens -- clean interrupt, unexpected fault -- the ``finally`` runs
        :meth:`stop`, because the one outcome that is genuinely unacceptable is exiting
        with the microphone still open.
        """
        if not self.start():
            return 1
        code = 0
        try:
            while not self._stop_event.wait(_TICK_SECONDS):
                pass
        except KeyboardInterrupt:
            self._report("Interrupted.", "info")
        except Exception:
            logger.exception("The wake daemon fell over")
            code = 1
        finally:
            self.stop()
        return code

    @property
    def status(self) -> DaemonStatus:
        """A consistent snapshot of the daemon's state."""
        with self._lock:
            session = self._session
            return DaemonStatus(
                listening=self._listening,
                wakes=self._wakes,
                last_wake=self._last_wake,
                session_active=session is not None and session.poll() is None,
            )

    # ----------------------------------------------------------------------------------
    # Wiring into the voice system
    # ----------------------------------------------------------------------------------

    def _attach(self) -> None:
        """Redirect the voice system's callbacks at us, remembering the originals.

        ``VoiceSystem`` fires ``on_wake()`` for a bare call word and ``on_utterance``
        for a call word with a command trailing it. Both mean "the operator wants a
        session"; they differ only in whether we have something to pass through.
        """
        if self._attached:
            return
        if not hasattr(self._voice, "_on_wake") or not hasattr(self._voice, "_on_utterance"):
            logger.warning(
                "The injected voice system exposes no wake callbacks; "
                "the daemon will never hear a call word"
            )
            return
        self._prev_wake = getattr(self._voice, "_on_wake", None)
        self._prev_utterance = getattr(self._voice, "_on_utterance", None)
        try:
            setattr(self._voice, "_on_wake", lambda: self._on_wake(""))
            setattr(self._voice, "_on_utterance", self._on_wake)
        except AttributeError:
            logger.warning("Could not attach the daemon's wake callbacks", exc_info=True)
            return
        self._attached = True

    def _detach(self) -> None:
        """Give the voice system its own callbacks back."""
        if not self._attached:
            return
        try:
            setattr(self._voice, "_on_wake", self._prev_wake)
            setattr(self._voice, "_on_utterance", self._prev_utterance)
        except AttributeError:
            logger.debug("Could not restore the voice callbacks", exc_info=True)
        self._attached = False

    # ----------------------------------------------------------------------------------
    # The wake itself
    # ----------------------------------------------------------------------------------

    def _on_wake(self, command: str = "") -> None:
        """Handle one wake. ``command`` is the remainder of the utterance, if any.

        Runs on the recogniser's own callback thread, which is exactly where we want
        it: the greeting is spoken *synchronously* with the microphone already shut, so
        the daemon cannot hear itself say hello and wake all over again. Only the wait
        on the child session is pushed onto its own thread, because that can last hours.
        """
        command = (command or "").strip()
        now = time.monotonic()

        with self._lock:
            if self._stop_event.is_set():
                return
            session = self._session
            if session is not None and session.poll() is None:
                logger.debug("Ignored a wake: a session is already running")
                return
            cooldown = max(0.0, float(settings.DAEMON_WAKE_COOLDOWN))
            if self._last_wake_mono and (now - self._last_wake_mono) < cooldown:
                logger.debug(
                    "Ignored a wake %.1fs into a %.1fs cooldown",
                    now - self._last_wake_mono,
                    cooldown,
                )
                return
            self._last_wake_mono = now
            self._last_wake = time.time()
            self._wakes += 1

        logger.info("Wake word heard%s", f" with command: {command}" if command else "")
        self._report(
            prompts.personalise("Call word heard. Opening a session, {user_title}."),
            "success",
        )

        # Shut the microphone *before* greeting: from here until the session exits the
        # daemon is deaf on purpose. Two processes on one input device is the bug this
        # whole class exists to avoid.
        self._suspend_listening()

        try:
            self._greet(command)
        except Exception:
            logger.exception("Failed to speak the wake greeting")

        spawned_at = time.monotonic()
        try:
            proc = self._launch(command)
        except Exception:
            # A spawn hook that raises must not leave the microphone switched off
            # forever; the daemon is degraded, not dead.
            logger.exception("The spawn hook raised")
            proc = None
        if proc is not None:
            proc = _SessionHandle(proc, spawned_at)

        if self._on_wake_cb is not None:
            try:
                self._on_wake_cb(command)
            except Exception:
                logger.exception("The on_wake callback failed")

        if proc is None:
            # Nothing to wait for, so the microphone goes straight back on.
            self._resume_listening()
            return

        with self._lock:
            self._session = proc
            watcher = threading.Thread(
                target=self._watch_session,
                args=(proc,),
                name="jarvis-wake-session",
                daemon=True,
            )
            self._watcher = watcher
        watcher.start()

    # ----------------------------------------------------------------------------------
    # Greeting
    # ----------------------------------------------------------------------------------

    def _greeting(self, utterance: str = "") -> tuple[str, Any]:
        """The line to speak and the locale to speak it in.

        A configured ``WAKE_GREETING`` is a literal written in one specific language,
        so the language is read off the *greeting*, not off what the operator happened
        to say. Only when there is no configured line do we fall back to the locale
        registry, and then the utterance is the best evidence available.
        """
        template = str(settings.WAKE_GREETING or "").strip()
        if template:
            code = locales.detect_locale(template)
            locale = locales.get_locale(code) or locales.active()
        else:
            code = locales.detect_locale(utterance) if utterance else locales.active().code
            locale = locales.get_locale(code) or locales.active()
            template = locales.localise("wake_greeting", locale=locale)
        # personalise_for resolves {honorific} through the locale's own honorifics, so a
        # Hindi line says "सर" instead of dropping a Latin word into Devanagari.
        return locales.personalise_for(template, locale=locale), locale

    def _greet(self, utterance: str = "") -> str:
        """Speak the wake greeting in its own language. Returns what was said."""
        text, locale = self._greeting(utterance)
        if text:
            self._speak(text, locale)
        return text

    def _speak_accepts_locale(self, speak: Callable[..., Any]) -> bool:
        """Whether the injected ``speak`` takes a ``locale`` keyword. Cached.

        The shipped ``VoiceSystem.speak`` does not -- it reads the active locale at
        render time -- but a richer voice implementation might, and asking is cheaper
        than mutating global state for no reason.
        """
        if self._speak_locale_kwarg is None:
            try:
                params = inspect.signature(speak).parameters
            except (TypeError, ValueError):
                self._speak_locale_kwarg = False
            else:
                self._speak_locale_kwarg = "locale" in params
        return self._speak_locale_kwarg

    def _speak(self, text: str, locale: Any) -> None:
        """Say ``text`` in ``locale``, blocking until it has actually been said.

        Blocking matters twice over: the neural voice is chosen when the utterance is
        *rendered*, so the locale must still be switched then; and the session window
        should not open while J.A.R.V.I.S. is still mid-sentence.
        """
        speak = getattr(self._voice, "speak", None)
        if not callable(speak):
            logger.warning("The injected voice system cannot speak")
            return

        if self._speak_accepts_locale(speak):
            try:
                speak(text, blocking=True, locale=locale)
            except Exception:
                logger.exception("Speaking the wake greeting failed")
            return

        previous = locales.active().code
        switched = locale is not None and locales.set_active(getattr(locale, "code", "")) is not None
        if locale is not None and not switched and getattr(locale, "code", "") != previous:
            logger.debug(
                "Locale %r is not enabled; greeting in %r instead",
                getattr(locale, "code", "?"),
                previous,
            )
        try:
            speak(text, blocking=True)
        except Exception:
            logger.exception("Speaking the wake greeting failed")
        finally:
            if switched and locales.set_active(previous) is None:
                locales.reset_active()

    # ----------------------------------------------------------------------------------
    # Sessions
    # ----------------------------------------------------------------------------------

    def _launch(self, command: str = "") -> "subprocess.Popen[bytes] | None":
        """Open a terminal window running the assistant. ``None`` if we could not."""
        argv = terminal_argv(command)
        if argv is None:
            self._report(
                "No terminal emulator found; cannot open a session window.", "warn"
            )
            logger.warning(
                "No terminal on PATH. Set DAEMON_TERMINAL to the binary you want used."
            )
            return None
        logger.info("Launching session: %s", " ".join(argv))
        proc = self.spawn(argv)
        if proc is None:
            self._report("The session window would not open.", "error")
        return proc

    def _watch_session(self, proc: "subprocess.Popen[bytes]") -> None:
        """Wait out the child session, then hand the microphone back."""
        try:
            proc.wait(timeout=_SESSION_WAIT_LIMIT)
        except subprocess.TimeoutExpired:
            logger.warning("Session ran past the wait limit; resuming listening anyway")
        except Exception:
            logger.exception("Waiting on the session failed")
        finally:
            with self._lock:
                if self._session is proc:
                    self._session = None
            if not self._stop_event.is_set():
                # The cooldown is measured from the *end* of a session too, so the
                # operator's parting "goodbye" cannot immediately open another window.
                with self._lock:
                    self._last_wake_mono = time.monotonic()
                self._resume_listening()

    # ----------------------------------------------------------------------------------
    # Microphone
    # ----------------------------------------------------------------------------------

    def _suspend_listening(self) -> None:
        """Release the microphone without forgetting that we mean to hold it."""
        stopper = getattr(self._voice, "stop_listening", None)
        if not callable(stopper):
            return
        try:
            stopper()
        except Exception:
            logger.exception("Could not release the microphone")

    def _resume_listening(self) -> None:
        """Re-open the microphone after a session, if we are still meant to be up."""
        with self._lock:
            if self._stop_event.is_set() or not self._listening:
                return
        starter = getattr(self._voice, "start_listening", None)
        if not callable(starter):
            return
        try:
            if starter():
                self._report("Listening again.", "info")
                return
        except Exception:
            logger.exception("Could not re-open the microphone")
        with self._lock:
            self._listening = False
        self._report("Lost the microphone; the wake daemon is deaf.", "error")

    # ----------------------------------------------------------------------------------
    # Output
    # ----------------------------------------------------------------------------------

    def _report(self, message: str, level: str = "info") -> None:
        """Log, and echo to the console the caller gave us, if it gave us one."""
        getattr(logger, "warning" if level == "warn" else level, logger.info)(message)
        console = self._console
        if console is None:
            return
        # A StarkHUD takes the level and styles it; a bare rich Console does not, and
        # handing it one as a second argument would simply print the word.
        hud_writer = getattr(console, "log_system", None)
        plain_writer = getattr(console, "print", None)
        try:
            if callable(hud_writer):
                hud_writer(message, level)
            elif callable(plain_writer):
                plain_writer(message)
        except Exception:
            logger.debug("Console echo failed", exc_info=True)
