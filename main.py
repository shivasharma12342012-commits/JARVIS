"""J.A.R.V.I.S. — application entry point.

Boots the workshop intelligence: parses the command line, configures file-only
logging, asks a first-time operator how they wish to be addressed, verifies the
Ollama daemon, wires the HUD / monitor / voice / protocols / tools / agent
together, and then runs the read-eval-print loop.

Everything the operator can reach from a terminal starts here::

    python main.py                 # full experience: HUD, voice, ambient monitor
    python main.py --text          # text-only, no audio hardware touched
    python main.py --check         # preflight report, then exit
    python main.py --ask "status"  # one-shot question, then exit

Design note: exactly one thread ever drives the agent. Typed lines and spoken
utterances are both funnelled onto a single ``queue.Queue`` which the main
thread drains, so a voice command can never interleave with a typed one.
"""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import platform
import queue
import re
import random
import signal
import sys
import tempfile
import threading
import types
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

import config
from config import (
    INTERRUPT_WORDS,
    PALETTE_STANDARD,
    QUIET_WORDS,
    STATE_IDLE,
    TALK_WORDS,
    settings,
)
from jarvis import __version__, prompts
from jarvis import apps, languages, locales, speaker
from jarvis import theme as theme_mod
from jarvis.core import JarvisAgent
from jarvis.engine import build_agent
from jarvis.monitor import AmbientMonitor, telemetry_report
from jarvis.permissions import PermissionBroker
from jarvis.protocols import ProtocolEngine
from jarvis.tools import build_registry
from jarvis.ui import PALETTES, StarkHUD

# The full-screen front end is optional: without textual installed J.A.R.V.I.S.
# still runs, on the pinned-strip HUD, and says so rather than failing to start.
try:
    from jarvis.tui import JarvisTUI
    from jarvis.tui import available as tui_available
except ImportError:  # pragma: no cover - optional dependency
    JarvisTUI = None  # type: ignore[assignment]

    def tui_available() -> bool:  # type: ignore[misc]
        return False
from jarvis.voice import VoiceSystem

LOG = logging.getLogger("jarvis.main")

# Rotating log files: five generations of two megabytes is plenty to reconstruct
# a session without letting a runaway loop fill the disk.
LOG_MAX_BYTES = 2_000_000
LOG_BACKUP_COUNT = 5
LOG_FORMAT = "%(asctime)s %(levelname)-8s [%(threadName)s] %(name)s: %(message)s"
_LOG_LEVELS = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "NOTSET"}

# The slash-command reference, rendered by /help and by any unknown command.
COMMANDS: list[tuple[str, str]] = [
    ("talk", "Speaking mode on — just the word, no slash"),
    ("quiet", "Shut him up immediately (also: shut up, chup, bas)"),
    ("/voiceprint", "Show the enrolled voiceprint (/voiceprint forget deletes it)"),
    ("/enroll", "How to record your voice sample"),
    ("/apps", "List the applications currently running"),
    ("/permissions", "Show what he has been allowed to do outside the workspace"),
    ("/allow <app>", "Pre-approve an application so he stops asking"),
    ("/revoke", "Forget every permission granted this session"),
    ("/lang <name>", "Reply in English, Hindi, Bengali, Telugu, Marathi or Tamil (or auto)"),
    ("/locales", "List every supported human language and its voice"),
    ("/toolchains", "List the programming languages this machine can compile and run"),
    ("/help", "Show this list of commands."),
    ("/quit, /exit", "Shut everything down cleanly."),
    ("/clear", "Wipe the conversation memory and the transcript."),
    ("/protocol <name>", "Execute a Stark protocol by name."),
    ("/protocols", "List the registered protocols."),
    ("/voice on|off", "Start or stop the wake-word listener."),
    ("/mute, /unmute", "Silence or restore the spoken output."),
    ("/diag", "Run a full diagnostics sweep and report."),
    ("/tools", "List the instruments available to the agent."),
    ("/model", "Report the model, the host and live availability."),
    ("/history", "Show the recent conversation memory."),
    ("/mics", "Enumerate the available input devices."),
    ("/theme <colour>", "Any colour you like: #ff8c42, violet, veronica, surprise."),
    ("/desktop", "Open the windowed front end onto this session."),
    ("/title <value>", "Change how I address you. Persisted."),
    ("/metrics", "Latency of the last turn: first token, throughput, tool time."),
]

# Spoken answers to the onboarding question arrive as words, not digits, and
# usually wrapped in a polite phrase. Both are normalised away before matching.
_SPOKEN_DIGITS = {"one": "1", "two": "2", "three": "3"}
_TITLE_ALIASES: dict[str, tuple[str, str]] = {
    "sir": ("male", "Sir"),
    "mister": ("male", "Sir"),
    "mr": ("male", "Sir"),
    "ma'am": ("female", "Ma'am"),
    "maam": ("female", "Ma'am"),
    "ma am": ("female", "Ma'am"),
    "madam": ("female", "Ma'am"),
    "miss": ("female", "Ma'am"),
    "ms": ("female", "Ma'am"),
    "mrs": ("female", "Ma'am"),
    "boss": ("neutral", "Boss"),
}
_ANSWER_PREFIXES = (
    "you may call me ",
    "you can call me ",
    "please call me ",
    "address me as ",
    "call me ",
    "my title is ",
    "i would prefer ",
    "i prefer ",
    "make it ",
    "just ",
)
_STRIPPABLE = " \t'\"“”‘’.,!?;:"
_MAX_TITLE_CHARS = 32


# ======================================================================================
# Command line
# ======================================================================================
def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse the command line into a namespace of overrides."""
    parser = argparse.ArgumentParser(
        prog="jarvis",
        description=f"{settings.AGENT_NAME} - {settings.AGENT_FULL_NAME}",
        epilog="Type /help once inside for the full command set.",
    )
    parser.add_argument(
        "--text",
        action="store_true",
        help="force text mode: no microphone, no speech synthesis, no audio imports",
    )
    parser.add_argument("--no-hud", action="store_true", help="disable the live HUD; plain lines only")
    parser.add_argument(
        "--classic",
        action="store_true",
        help="use the pinned status strip instead of the full-screen HUD",
    )
    parser.add_argument(
        "command",
        nargs="?",
        default=None,
        choices=["desktop", "app", "gui", "window", "set-password", "clear-password"],
        help='"desktop" opens the windowed front end; "set-password" locks it',
    )
    parser.add_argument(
        "--desktop",
        action="store_true",
        help="open the windowed front end: graphical transcript and a full colour picker",
    )
    parser.add_argument(
        "--port",
        type=int,
        metavar="N",
        default=0,
        help="port for the desktop app (default: whatever the OS hands out)",
    )
    parser.add_argument(
        "--no-window",
        action="store_true",
        help="serve the desktop app but do not open a window; print the address instead",
    )
    parser.add_argument(
        "--theme",
        metavar="COLOUR",
        default=None,
        help="start in a given colour: a hex code, a colour name, a preset or 'surprise'",
    )
    parser.add_argument(
        "--no-turbo",
        action="store_true",
        help="drive the model synchronously: no warm-up, no parallel instruments",
    )
    parser.add_argument("--no-monitor", action="store_true", help="do not start the ambient monitor")
    parser.add_argument("--model", metavar="NAME", default=None, help="override the Ollama model name")
    parser.add_argument("--host", metavar="URL", default=None, help="override the Ollama host URL")
    parser.add_argument("--list-mics", action="store_true", help="list the input devices and exit")
    parser.add_argument("--check", action="store_true", help="run the preflight checks and exit")
    parser.add_argument(
        "--protocol", metavar="NAME", default=None, help="fire a protocol immediately after boot"
    )
    parser.add_argument(
        "--ask", metavar="TEXT", default=None, help="ask one question, print the answer, then exit"
    )
    parser.add_argument(
        "--reset-profile",
        action="store_true",
        help="forget the saved form of address and ask for it again",
    )
    parser.add_argument(
        "--locale",
        metavar="CODE",
        default=None,
        help="reply language: auto, en, hi, bn, te, mr or ta",
    )
    parser.add_argument(
        "--locales", action="store_true", help="list the supported languages and exit"
    )
    parser.add_argument(
        "--toolchains",
        action="store_true",
        help="list the programming languages this machine can run, and exit",
    )
    parser.add_argument(
        "--enroll-voice",
        action="store_true",
        help="record a voice sample so the call word only answers to you, then exit",
    )
    parser.add_argument(
        "--forget-voice",
        action="store_true",
        help="delete the stored voiceprint and exit",
    )
    parser.add_argument(
        "--daemon",
        "--listen",
        dest="daemon",
        action="store_true",
        help="run in the background waiting for the call word; opens a terminal on wake",
    )
    parser.add_argument(
        "--talk",
        action="store_true",
        help="start in speaking mode instead of silent",
    )
    parser.add_argument(
        "--allow-all",
        action="store_true",
        help="approve every out-of-workspace action without prompting (scripting only)",
    )
    parser.add_argument("--debug", action="store_true", help="log at DEBUG level")
    parser.add_argument(
        "--version", action="version", version=f"{settings.AGENT_NAME} {__version__}"
    )
    return parser.parse_args(argv)


def apply_overrides(args: argparse.Namespace) -> None:
    """Fold the CLI flags into ``settings`` before anything else is constructed.

    Assignment order matters: every component reads ``settings`` at construction
    time, so the overrides have to land first or half the system ends up
    disagreeing with the other half about whether audio exists.
    """
    if args.model:
        settings.MODEL_NAME = args.model
    if args.host:
        settings.OLLAMA_HOST = args.host.rstrip("/")
    if args.text:
        settings.VOICE_ENABLED = False
        settings.TTS_ENABLED = False
        settings.STT_ENABLED = False
    if args.no_hud:
        settings.HUD_ENABLED = False
    if args.no_monitor:
        settings.MONITOR_ENABLED = False
    if args.debug:
        settings.LOG_LEVEL = "DEBUG"
    if getattr(args, "allow_all", False):
        settings.PERMISSION_MODE = "allow"
    if getattr(args, "talk", False):
        settings.START_MUTED = False
    if args.locale:
        wanted = args.locale.strip().lower()
        if wanted == "auto":
            settings.RESPONSE_LOCALE = "auto"
        else:
            locale = locales.get_locale(wanted)
            if locale is not None:
                settings.RESPONSE_LOCALE = locale.code
                locales.set_active(locale.code)
    # --ask, --check and --list-mics are transient, pipe-friendly modes. A Live
    # layout that repaints the screen would only fight with the captured output.
    if args.ask or args.check or args.list_mics:
        settings.HUD_ENABLED = False


# ======================================================================================
# Logging
# ======================================================================================
def configure_logging(debug: bool = False) -> Path:
    """Send all logging to a rotating file and nowhere else.

    Console handlers are deliberately absent: a stray log line written while the
    Rich Live layout owns the terminal corrupts the HUD beyond repair.
    """
    log_path = Path(settings.LOG_FILE)
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        log_path = Path(tempfile.gettempdir()) / "jarvis.log"

    name = "DEBUG" if debug else str(settings.LOG_LEVEL).strip().upper()
    level = getattr(logging, name) if name in _LOG_LEVELS else logging.INFO

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
        try:
            existing.close()
        except Exception:  # a handler that dies on close must not abort the boot
            pass

    try:
        handler: logging.Handler = logging.handlers.RotatingFileHandler(
            log_path,
            maxBytes=LOG_MAX_BYTES,
            backupCount=LOG_BACKUP_COUNT,
            encoding="utf-8",
            delay=True,
        )
    except OSError:
        # A read-only or locked log directory is not a reason to refuse to boot.
        handler = logging.NullHandler()
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    root.addHandler(handler)
    root.setLevel(level)

    # Third-party chatter at DEBUG is noise that buries our own trail.
    for noisy in ("httpx", "httpcore", "urllib3", "asyncio", "comtypes", "markdown_it", "PIL"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    logging.captureWarnings(True)
    return log_path


def install_excepthooks() -> None:
    """Route uncaught exceptions to the log file instead of over the HUD."""

    def _hook(
        exc_type: type[BaseException], exc: BaseException, tb: types.TracebackType | None
    ) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            return
        LOG.critical("unhandled exception", exc_info=(exc_type, exc, tb))
        print(f"\n{settings.AGENT_NAME} encountered a fault: {exc}", file=sys.stderr)
        print(f"Details are in {settings.LOG_FILE}.", file=sys.stderr)

    sys.excepthook = _hook

    def _thread_hook(hook_args: Any) -> None:
        if issubclass(hook_args.exc_type, SystemExit):
            return
        LOG.error(
            "unhandled exception in thread %s",
            getattr(hook_args.thread, "name", "?"),
            exc_info=(hook_args.exc_type, hook_args.exc_value, hook_args.exc_traceback),
        )

    if hasattr(threading, "excepthook"):
        threading.excepthook = _thread_hook


# ======================================================================================
# Onboarding helpers
# ======================================================================================
def _clean_title(raw: str) -> str:
    """Normalise a free-text form of address into something printable."""
    cleaned = " ".join(str(raw).split())
    cleaned = "".join(ch for ch in cleaned if ch.isprintable())
    cleaned = cleaned.strip(_STRIPPABLE)[:_MAX_TITLE_CHARS].strip()
    if cleaned.islower():
        # "captain" reads as a typo; "Captain" reads as an instruction.
        cleaned = cleaned.title()
    return cleaned


def resolve_honorific(answer: str) -> tuple[str, str] | None:
    """Turn an onboarding answer into a ``(gender, title)`` pair.

    Accepts the menu keys (``1``/``2``/``3`` and the words a recogniser returns
    for them), the known honorifics in any casing, and any free-text title the
    operator invents. Returns ``None`` only when nothing usable survives.
    """
    raw = " ".join(str(answer or "").split())
    if not raw:
        return None

    low = raw.lower().strip(_STRIPPABLE)
    for prefix in _ANSWER_PREFIXES:
        if low.startswith(prefix):
            raw = raw[len(prefix) :].strip()
            low = raw.lower().strip(_STRIPPABLE)
            break
    if low.endswith(" please"):
        raw = raw[: -len(" please")].strip()
        low = raw.lower().strip(_STRIPPABLE)
    if not low:
        return None

    key = _SPOKEN_DIGITS.get(low, low)
    for option_key, gender, title in prompts.ONBOARDING_OPTIONS:
        if key == option_key or low == title.lower():
            return gender, title

    alias = _TITLE_ALIASES.get(low)
    if alias is not None:
        return alias

    title = _clean_title(raw)
    if not title:
        return None
    return "custom", title


# ======================================================================================
# Input plumbing
# ======================================================================================
@dataclass(slots=True)
class InputEvent:
    """One unit of work for the dispatcher, whatever its origin."""

    source: str  # "typed" | "voice" | "system"
    text: str


# ======================================================================================
# The application
# ======================================================================================
class JarvisApplication:
    """Owns every subsystem and the single loop that drives them."""

    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.hud: Any = None
        self.tui: Any = None
        self.voice: VoiceSystem | None = None
        self.monitor: AmbientMonitor | None = None
        self.engine: ProtocolEngine | None = None
        self.registry: Any = None
        self.agent: JarvisAgent | None = None

        self._queue: queue.Queue[InputEvent] = queue.Queue()
        self._dispatch_lock = threading.Lock()  # a voice command may not interleave with a typed one
        self._accept_input = threading.Event()  # gates the console reader while the agent works
        self._stopping = threading.Event()
        self._reader: threading.Thread | None = None
        self._running = False
        self._shutdown_done = False
        self._shutdown_lock = threading.Lock()
        self._listening = False
        self._exit_code = 0
        self._consumer: threading.Thread | None = None
        #: Why the full-screen HUD was declined, when it was and it is worth saying.
        self._tui_declined = ""
        #: The desktop window, once /desktop has opened one. None otherwise.
        self.desktop: Any = None

    # -- construction ------------------------------------------------------------------
    def build_frontend(self) -> None:
        """Construct the HUD and the voice system.

        These two come up before everything else because onboarding needs a way
        to ask its question and, ideally, a voice to ask it with.
        """
        if self.tui_wanted():
            assert JarvisTUI is not None
            self.tui = JarvisTUI(
                on_submit=self._on_tui_submit,
                on_ready=self._tui_boot,
                on_quit=self._on_tui_quit,
                on_interrupt=self._interrupt_or_quit,
                on_toggle_speech=self._toggle_speech,
            )
            self.hud = self.tui
            LOG.info("front end: full-screen HUD")
        else:
            self.hud = StarkHUD(palette=PALETTE_STANDARD, enabled=settings.HUD_ENABLED)
            self._tui_declined = self.tui_verdict()[1]
            LOG.info(
                "front end: pinned status strip%s",
                f" ({self._tui_declined})" if self._tui_declined else "",
            )
        # Wear whatever colours the operator last chose — in the terminal as
        # well as in the window. A theme picked in the desktop app is not a
        # desktop-app setting; it is how they want J.A.R.V.I.S. to look.
        #
        # Only when they have actually chosen one, though. A fresh install has
        # no theme file, and the terminal HUDs keep the hand-tuned palettes they
        # shipped with rather than a derived approximation of them.
        if theme_mod.THEME_PATH.exists():
            try:
                self._recolour_terminal(theme_mod.load())
            except Exception:
                LOG.debug("Could not apply the remembered theme", exc_info=True)

        if settings.voice_wanted:
            self.voice = VoiceSystem(
                on_wake=self._on_wake,
                on_utterance=self._on_utterance,
                on_state=self._on_state,
                on_amplitude=self._on_amplitude,
                on_log=self._on_voice_log,
            )
            # Silent until invited. `talk` lifts this.
            if self.voice is not None and settings.START_MUTED:
                try:
                    self.voice.set_muted(True)
                except Exception:
                    LOG.debug('Could not start muted', exc_info=True)
            LOG.info("voice subsystem: %s", self.voice.status.describe())
        else:
            LOG.info("voice subsystem disabled by configuration")

    def wire_backend(self) -> None:
        """Build the monitor, protocols, tools and agent, then cross-bind them."""
        # The monitor object exists even under --no-monitor: /diag and HOUSE PARTY
        # still want a telemetry source, they simply do not want a live thread.
        self.monitor = AmbientMonitor(on_alert=self._on_alert, on_telemetry=self._on_telemetry)
        self.engine = ProtocolEngine(hud=self.hud, voice=self.voice, monitor=self.monitor)
        # The consent layer sits between every outward-reaching tool and the machine.
        self.broker = PermissionBroker(
            hud=self.hud, voice=self.voice, protocol_engine=self.engine
        )
        self.registry = build_registry(
            self.engine, self.hud, self.monitor, self.broker
        )
        # build_agent hands back the asynchronous core where the installed
        # ollama client supports it, and the synchronous one where it does not.
        # The two behave identically; only the waiting differs.
        self.agent = build_agent(
            self.registry,
            turbo=not getattr(self.args, "no_turbo", False),
            hud=self.hud,
            voice=self.voice,
            protocol_engine=self.engine,
            monitor=self.monitor,
        )
        # Late bind: CLEAN SLATE needs agent.reset(), and the agent could not
        # exist before the registry, which needs the engine. Hence the second pass.
        self.engine.bind(agent=self.agent)
        self.broker.bind(
            hud=self.hud, voice=self.voice, protocol_engine=self.engine
        )
        LOG.info(
            "wired: %d tools, %d protocols, model=%s host=%s engine=%s",
            len(self.registry.names()),
            len(self.engine.names()),
            settings.MODEL_NAME,
            settings.OLLAMA_HOST,
            type(self.agent).__name__,
        )

    # -- small helpers -----------------------------------------------------------------
    def _log_system(self, text: str, level: str = "info") -> None:
        """Write a line to the transcript, whatever state the HUD is in."""
        if self.hud is not None:
            try:
                self.hud.log_system(text, level)
                return
            except Exception:
                LOG.exception("HUD refused a system line")
        print(text)

    def _voice_ok(self) -> bool:
        """True when there is a voice that can actually be heard right now."""
        voice = self.voice
        if voice is None or not voice.tts_available:
            return False
        try:
            return not voice.is_muted()
        except Exception:
            LOG.exception("voice mute state unavailable")
            return False

    def _speak(self, text: str, blocking: bool = False, interrupt: bool = False) -> None:
        """Speak a line if speech exists; do nothing at all if it does not."""
        if not text or self.voice is None or not self.voice.tts_available:
            return
        try:
            self.voice.speak(text, blocking=blocking, interrupt=interrupt)
        except Exception:
            LOG.exception("speech failed for %r", text[:60])

    def _refresh_voice_status(self) -> None:
        """Push the current audio state into the HUD header."""
        if self.hud is None:
            return
        if self.voice is None:
            self.hud.set_voice_status("voice offline")
            return
        try:
            described = self.voice.status.describe()
        except Exception:
            LOG.exception("voice status unavailable")
            described = "voice status unknown"
        suffix = " | listening" if self._listening else ""
        try:
            if self.voice.is_muted():
                suffix += " | muted"
        except Exception:
            LOG.exception("voice mute state unavailable")
        self.hud.set_voice_status(f"{described}{suffix}")

    # -- voice callbacks (all fire on the voice thread) ---------------------------------
    def _on_wake(self) -> None:
        """Acknowledge the wake word before the recogniser opens for the command."""
        line = prompts.personalise(random.choice(prompts.WAKE_ACKNOWLEDGEMENTS))
        self._log_system(line, "info")
        # Blocking so the acknowledgement finishes before the microphone reopens;
        # the voice system suppresses the recogniser during playback in any case.
        self._speak(line, blocking=True)

    def _on_utterance(self, text: str) -> None:
        """Queue a transcribed command for the single dispatcher thread."""
        cleaned = (text or "").strip()
        if not cleaned:
            return
        LOG.info("voice utterance: %s", cleaned)
        self._queue.put(InputEvent("voice", cleaned))

    def _on_state(self, state: str) -> None:
        """Mirror a voice state change onto the HUD."""
        if self.hud is not None:
            try:
                self.hud.set_state(state)
            except Exception:
                LOG.exception("HUD state update failed")

    def _on_amplitude(self, value: float) -> None:
        """Drive the HUD waveform from the live audio amplitude."""
        if self.hud is not None:
            try:
                self.hud.set_amplitude(value)
            except Exception:
                LOG.exception("HUD amplitude update failed")

    def _on_voice_log(self, message: str, level: str) -> None:
        """Surface a voice subsystem message in the transcript and the log."""
        LOG.log(
            logging.WARNING if level in {"warn", "error"} else logging.INFO, "voice: %s", message
        )
        self._log_system(message, level)

    # -- monitor callbacks (fire on the monitor thread) ---------------------------------
    def _on_telemetry(self, telemetry: Any) -> None:
        """Feed a fresh telemetry sample to the HUD gauges."""
        if self.hud is not None:
            try:
                self.hud.set_telemetry(telemetry)
            except Exception:
                LOG.exception("HUD telemetry update failed")

    def _on_alert(self, alert: Any) -> None:
        """Fan an ambient alert out to the display and to the agent's voice."""
        LOG.warning("alert: %s", getattr(alert, "key", "?"))
        if self.hud is not None:
            try:
                self.hud.push_alert(alert)
            except Exception:
                LOG.exception("HUD alert render failed")
        if self.agent is not None:
            try:
                # ambient_report is a one-shot spoken warning, not a full turn; the
                # TTS queue keeps it from talking over an answer already in flight.
                self.agent.ambient_report(alert)
            except Exception:
                LOG.exception("ambient report failed")

    # ==================================================================================
    # Step 3 - first-run onboarding
    # ==================================================================================
    def run_onboarding(self) -> None:
        """Ask, once and only once, how the operator wishes to be addressed.

        Runs before the system prompt is built or the greeting is spoken, because
        every one of those strings interpolates ``settings.USER_TITLE``.
        """
        forced = bool(self.args.reset_profile)
        if forced:
            try:
                config.PROFILE_PATH.unlink(missing_ok=True)
            except OSError:
                LOG.exception("could not remove %s", config.PROFILE_PATH)
            settings.USER_GENDER = "unset"  # type: ignore[assignment]
            settings.USER_NAME = ""
        if not forced and not config.needs_onboarding():
            LOG.debug("onboarding not required; address is %r", settings.USER_TITLE)
            return
        if sys.stdin is None or not sys.stdin.isatty():
            # A piped or absent stdin cannot answer. Persisting a guess would be
            # worse than asking properly on the next interactive launch.
            LOG.info("stdin is not interactive; deferring onboarding")
            return

        assert self.hud is not None
        options = [(key, title) for key, _gender, title in prompts.ONBOARDING_OPTIONS]
        self._speak(prompts.ONBOARDING_SPOKEN, blocking=True)

        resolved: tuple[str, str] | None = None
        for attempt in range(3):
            answer = ""
            # A spoken answer is offered on the first pass only, and only when a
            # microphone exists; nobody wants to be re-interrogated by a
            # recogniser that has already misheard them once.
            if attempt == 0 and self.voice is not None and self.voice.stt_available:
                self._log_system("Speak your answer, or type it below.", "info")
                try:
                    heard = self.voice.listen_once(timeout=7.0, phrase_time_limit=6.0)
                except Exception:
                    LOG.exception("onboarding listen failed")
                    heard = None
                if heard:
                    answer = heard
                    self._log_system(f"Heard: {heard}", "info")
            if not answer:
                try:
                    answer = self.hud.choose(
                        prompts.ONBOARDING_QUESTION, options, prompts.ONBOARDING_HINT
                    )
                except (EOFError, KeyboardInterrupt):
                    LOG.info("onboarding abandoned by the operator")
                    return
                except Exception:
                    LOG.exception("onboarding prompt failed")
                    return
            resolved = resolve_honorific(answer)
            if resolved is not None:
                break
            self._log_system(prompts.ONBOARDING_HINT, "warn")

        if resolved is None:
            LOG.info("onboarding produced no usable answer; the profile stays unset")
            return

        gender, title = resolved
        applied = config.set_honorific(settings, gender, title)
        LOG.info("form of address set to %r (%s)", applied, gender)
        confirmation = prompts.personalise(prompts.ONBOARDING_CONFIRM)
        self._log_system(confirmation, "success")
        self._speak(confirmation)

    # ==================================================================================
    # Step 4 - preflight
    # ==================================================================================
    def preflight(self) -> tuple[bool, str]:
        """Verify the Ollama daemon and the model, reporting either way."""
        assert self.agent is not None
        try:
            ok, message = self.agent.ensure_model()
        except Exception as exc:  # ensure_model is defensive, but the network is not
            LOG.exception("preflight raised")
            ok, message = False, f"Model check failed: {exc}"
        LOG.info("preflight ok=%s: %s", ok, message)
        if self.hud is not None:
            self.hud.set_model_status(
                f"{settings.MODEL_NAME} {'online' if ok else 'unreachable'}"
            )
            # The full-screen greeting reports what preflight found rather than
            # opening with a pleasantry the machine cannot back up.
            note = getattr(self.hud, "set_model_ready", None)
            if callable(note):
                note(ok)
        self._log_system(message, "success" if ok else "error")
        if not ok:
            # Print the literal fix rather than a shrug. Degraded, not dead: the
            # HUD, telemetry, tools and protocols all work without a model.
            self._log_system("Start the daemon with:  ollama serve", "warn")
            self._log_system(f"Install the model with: ollama pull {settings.MODEL_NAME}", "warn")
            self._log_system(
                f"Running degraded, {settings.USER_TITLE} - diagnostics and protocols remain available.",
                "warn",
            )
        return ok, message

    def check_report(self, model_ok: bool) -> int:
        """Render the ``--check`` report. Returns the process exit code."""
        assert self.hud is not None and self.registry is not None and self.engine is not None
        rows: list[tuple[str, str]] = [
            ("Agent", f"{settings.AGENT_NAME} {__version__}"),
            ("Python", f"{sys.version.split()[0]} on {platform.platform()}"),
            ("Form of address", f"{settings.USER_TITLE} ({settings.USER_GENDER})"),
            ("Ollama host", settings.OLLAMA_HOST),
            ("Model", f"{settings.MODEL_NAME} - {'reachable' if model_ok else 'UNREACHABLE'}"),
            ("Instruments", ", ".join(self.registry.names()) or "none"),
            ("Protocols", ", ".join(self.engine.names()) or "none"),
            ("Workspace", str(settings.WORKSPACE_ROOT)),
            ("Log file", str(settings.LOG_FILE)),
            ("Profile", str(config.PROFILE_PATH)),
        ]
        if self.voice is not None:
            rows.append(("Voice", self.voice.status.describe()))
            rows.append(("Microphone", self.voice.status.microphone or "none detected"))
            for error in self.voice.status.errors[:5]:
                rows.append(("Voice fault", error))
        else:
            rows.append(("Voice", "disabled"))
        if self.monitor is not None:
            try:
                telemetry = self.monitor.snapshot()
                rows.append(
                    (
                        "Telemetry",
                        f"CPU {telemetry.cpu_percent:.0f}% | RAM {telemetry.ram_percent:.0f}%"
                        f" | {telemetry.process_count} processes",
                    )
                )
            except Exception as exc:
                LOG.exception("preflight telemetry failed")
                rows.append(("Telemetry", f"unavailable: {exc}"))
        self.hud.render_table("Preflight", ["Check", "Result"], rows)
        return 0 if model_ok else 1

    # ==================================================================================
    # Step 6 - boot
    # ==================================================================================
    def boot(self) -> None:
        """Banner, greeting, Live layout, monitor thread, wake-word listener."""
        assert self.hud is not None
        self.hud.print_banner()
        greeting = prompts.personalise(random.choice(prompts.BOOT_GREETINGS))
        self._log_system(greeting, "success")
        self._speak(greeting)  # suppressed while muted, which is the default
        if self.voice is not None and self.voice.is_muted():
            self._log_system(
                "Running silent. Type `talk` to have me speak, `quiet` to stop me.",
                "info",
            )

        self.hud.set_state(STATE_IDLE)
        self._refresh_voice_status()
        self.hud.start()

        if settings.MONITOR_ENABLED and self.monitor is not None:
            try:
                self.monitor.start()
                LOG.info(
                    "ambient monitor running at %.1fs intervals",
                    settings.MONITOR_INTERVAL_SECONDS,
                )
            except Exception:
                LOG.exception("ambient monitor failed to start")
                self._log_system("Ambient monitoring is unavailable.", "warn")

        if self.voice is not None and settings.STT_ENABLED:
            try:
                self._listening = bool(self.voice.start_listening())
            except Exception:
                LOG.exception("wake-word listener failed to start")
                self._listening = False
            if self._listening:
                words = ", ".join(f'"{w}"' for w in settings.WAKE_WORDS[:3])
                self._log_system(f"Listening for {words}.", "info")
            else:
                self._log_system("No microphone available - text input only.", "warn")
            self._refresh_voice_status()

        if self._tui_declined:
            self._log_system(self._tui_declined, "warn")

        self._log_system("Type /help for the command set.", "info")


    # ==================================================================================
    # The full-screen front end
    # ==================================================================================
    def tui_verdict(self) -> tuple[bool, str]:
        """Which front end this invocation gets, and — when it is not the
        full-screen one — why not, in words the operator can act on.

        Falling back silently is worse than not falling back at all: the display
        simply is not the one you were promised and nothing on screen says so.
        Every refusal below therefore carries its own explanation, and
        :meth:`build_frontend` prints the surprising ones.
        """
        # Deliberately quiet: these modes are pipe-friendly by design and the
        # operator asked for them by name.
        if self.args.ask or self.args.check or self.args.list_mics:
            return False, ""
        if getattr(self.args, "classic", False):
            return False, ""

        if self.args.no_hud:
            return False, "--no-hud was passed, so the display is plain lines."
        if not settings.HUD_ENABLED:
            return False, "HUD_ENABLED is false in your .env, so the display is plain lines."
        if JarvisTUI is None or not tui_available():
            return False, (
                "textual is not installed, so I am on the classic display. "
                "Install it with:  pip install -r requirements.txt"
            )
        try:
            interactive = bool(
                sys.stdout.isatty() and sys.stdin is not None and sys.stdin.isatty()
            )
        except Exception:
            interactive = False
        if not interactive:
            return False, (
                "this is not an interactive terminal, so I am on the classic display."
            )
        return True, ""

    def tui_wanted(self) -> bool:
        """True when the full-screen HUD should own the terminal."""
        return self.tui_verdict()[0]

    def _tui_boot(self) -> None:
        """Everything ``run`` does for the classic front end, but on screen.

        Called from a worker thread once the display is mounted, so the operator
        watches the system come up inside the HUD rather than staring at a bare
        terminal while it does.
        """
        assert self.tui is not None
        self.run_onboarding()
        self.tui.refresh_prompt_label()

        self.wire_backend()
        self.tui.set_tools(self.registry.names())
        self.tui.bind_busy(self._agent_busy)

        model_ok, _message = self.preflight()
        if model_ok:
            self.warm_up()

        self.boot()
        if self.args.protocol:
            self.run_protocol(self.args.protocol)
        self._start_consumer()
        self._exit_code = 0 if model_ok else 0  # a cold daemon is degraded, not fatal

    def warm_up(self) -> None:
        """Load the model into the daemon while the operator reads the banner.

        The first question of a session otherwise pays for the weights coming off
        disk — several seconds, every time, for nothing.
        """
        warmer = getattr(self.agent, "warm_up", None)
        if not callable(warmer):
            return
        try:
            warmer(blocking=False)
            LOG.info("model warm-up requested")
        except Exception:
            LOG.debug("warm-up could not be started", exc_info=True)

    def _on_tui_submit(self, text: str) -> None:
        """One line from the composer. Runs on a worker thread, not the UI's."""
        self._dispatch(InputEvent("typed", text))

    def _on_tui_quit(self) -> None:
        """The operator asked to leave; stop the producers before the screen goes."""
        self._running = False
        self._stopping.set()

    def _toggle_speech(self) -> None:
        """Ctrl-S: speak, or stop speaking."""
        if self.voice is None:
            self._log_system(
                "The voice subsystem is off for this session — relaunch without --text.",
                "warn",
            )
            return
        if self.voice.is_muted():
            self.enable_talking()
        else:
            self.silence()

    def _start_consumer(self) -> None:
        """Drain the shared queue while the TUI owns the main thread.

        Spoken utterances and signal-driven quits arrive on the same queue the
        classic REPL reads; under the full-screen HUD nobody is reading it, so
        this thread takes that job.
        """
        if self._consumer is not None:
            return
        self._running = True
        self._consumer = threading.Thread(
            target=self._consume_queue, name="tui-queue", daemon=True
        )
        self._consumer.start()

    def _consume_queue(self) -> None:
        while self._running and not self._stopping.is_set():
            try:
                event = self._queue.get(timeout=0.25)
            except queue.Empty:
                continue
            try:
                self._dispatch(event)
            except Exception:
                LOG.exception("dispatch failed for %r", event.text[:80])
                self._log_system("That request faulted. The details are in the log.", "error")

    def run_tui(self) -> int:
        """Own the terminal until the operator leaves. Returns the exit code."""
        self.build_frontend()
        self.install_signal_handlers()
        assert self.tui is not None
        self.tui.run()
        return self._exit_code

    # ==================================================================================
    # Step 7 - the classic loop
    # ==================================================================================
    def repl(self) -> None:
        """Drain the input queue on the main thread until told to stop.

        The console reader runs on its own daemon thread purely as a *producer*
        so that a spoken command does not have to wait for someone to press
        Enter. Only this method ever calls the agent.
        """
        self._running = True
        self._accept_input.set()
        self._reader = threading.Thread(target=self._console_reader, name="console", daemon=True)
        self._reader.start()

        while self._running:
            try:
                event = self._queue.get(timeout=0.25)
            except queue.Empty:
                # Nothing pending: re-arm the prompt in case a voice command has
                # just been handled while the reader was gated.
                if not self._agent_busy():
                    self._accept_input.set()
                continue
            except KeyboardInterrupt:
                if not self._interrupt_or_quit():
                    break
                continue

            self._accept_input.clear()
            try:
                self._dispatch(event)
            except KeyboardInterrupt:
                if not self._interrupt_or_quit():
                    break
            except Exception:
                LOG.exception("dispatch failed for %r", event.text[:80])
                self._log_system("That request faulted. The details are in the log.", "error")
            finally:
                if self._running:
                    self._accept_input.set()

    def _agent_busy(self) -> bool:
        """True while the agent is mid-turn."""
        try:
            return bool(self.agent is not None and self.agent.busy)
        except Exception:
            LOG.exception("agent busy flag unavailable")
            return False

    def _interrupt_or_quit(self) -> bool:
        """Ctrl-C cancels work in progress; at an idle prompt it means goodbye.

        Returns True when the loop should continue.
        """
        if self._agent_busy() and self.agent is not None:
            self.agent.interrupt()
            if self.voice is not None:
                try:
                    self.voice.stop_speaking()
                except Exception:
                    LOG.exception("could not stop speech")
            self._log_system("Interrupted.", "warn")
            return True
        self._running = False
        return False

    def _prompt_text(self) -> str:
        """The input prompt, addressed however the operator asked to be addressed."""
        return f"{settings.USER_TITLE} >"

    def _console_reader(self) -> None:
        """Read typed lines on a side thread and feed them to the shared queue."""
        assert self.hud is not None
        while not self._stopping.is_set():
            if not self._accept_input.wait(0.2):
                continue
            if self._stopping.is_set():
                return
            try:
                text = self.hud.prompt_input(self._prompt_text())
            except (EOFError, KeyboardInterrupt):
                self._queue.put(InputEvent("system", "/quit"))
                return
            except Exception:
                LOG.exception("console reader failed; requesting shutdown")
                self._queue.put(InputEvent("system", "/quit"))
                return
            text = (text or "").strip()
            if not text:
                continue
            # Gate the next prompt until the dispatcher has finished this line.
            self._accept_input.clear()
            self._queue.put(InputEvent("typed", text))

    def _dispatch(self, event: InputEvent) -> None:
        """Handle one queued event. Only ever called from the main thread."""
        with self._dispatch_lock:
            text = event.text.strip()
            if not text:
                return
            if text.startswith("/"):
                self._handle_command(text)
                return
            if self._bare_keyword(text):
                return
            self._ask_agent(text, event.source)

    def _bare_keyword(self, text: str) -> bool:
        """Act on a bare ``talk`` / ``quiet`` typed with no slash.

        Returns True when the line was a keyword and has been handled. Matching is on
        the whole line, never a prefix, so a real question that happens to begin with
        "talk" still goes to the model.
        """
        key = _bare_key(text)
        if not key:
            return False
        if key in TALK_WORDS:
            self.enable_talking()
            return True
        if key in QUIET_WORDS:
            self.silence()
            return True
        if key in INTERRUPT_WORDS:
            # "stop" is a perfectly ordinary thing to say to an assistant, so it only
            # means "stop talking" while there is talking to stop.
            if self.voice is not None and self.voice.is_speaking():
                self.silence(mute=False)
                return True
        return False

    def enable_talking(self) -> None:
        """Speaking mode on: unmute, open the microphone if there is one, confirm aloud."""
        if self.voice is None:
            self._log_system(
                "The voice subsystem is off for this session — relaunch without --text.",
                "warn",
            )
            return
        if not self.voice.tts_available:
            reason = "; ".join(self.voice.status.errors) or "no speech engine available"
            self._log_system(f"I have no voice to speak with: {reason}", "warn")
            return

        self.voice.set_muted(False)

        opened = False
        if self.voice.stt_available and not self._listening:
            try:
                opened = bool(self.voice.start_listening())
                self._listening = opened
            except Exception:
                LOG.exception("start_listening failed while enabling talk mode")

        self._refresh_voice_status()

        line = locales.localise("ack")
        detail = "Speaking mode on."
        if opened or self._listening:
            detail += f' Say "{settings.WAKE_WORDS[0]}" whenever you need me.'
        else:
            detail += " No microphone, so type to me and I will answer aloud."
        self._log_system(detail, "success")
        self._speak(line)

    def silence(self, mute: bool = True) -> None:
        """Stop talking. Right now, mid-word if necessary."""
        if self.voice is None:
            return
        try:
            self.voice.stop_speaking()
        except Exception:
            LOG.debug("stop_speaking failed", exc_info=True)
        try:
            self.voice.reset_speech_stream()
        except Exception:
            LOG.debug("reset_speech_stream failed", exc_info=True)
        if mute:
            self.voice.set_muted(True)
        self._refresh_voice_status()
        # Deliberately not spoken: confirming out loud that you have been asked to be
        # quiet is the single most annoying thing an assistant can do.
        self._log_system(
            "Quiet. Type `talk` when you want me to speak again."
            if mute
            else "Stopped.",
            "info",
        )

    def _ask_agent(self, text: str, source: str = "typed") -> None:
        """Send one turn to the agent and let it render its own reply."""
        if self.agent is None:
            self._log_system("The agent is not available.", "error")
            return
        assert self.hud is not None
        self.hud.log_user(f"(voice) {text}" if source == "voice" else text)
        LOG.info("turn (%s): %s", source, text[:200])
        reply = self.agent.chat(text, speak=self._voice_ok())
        # Keep the microphone warm briefly so a follow-up needs no second call word --
        # the difference between a conversation and a sequence of orders.
        if self._voice_ok() and self.voice is not None and not reply.error:
            try:
                self.voice.arm_follow_up()
            except Exception:
                LOG.debug("Could not arm the follow-up window", exc_info=True)
        if reply.error:
            LOG.error("agent error: %s", reply.error)
            # The agent already put it on screen; saying it twice helps nobody.
            if not getattr(reply, "surfaced", False):
                self._log_system(reply.error, "error")
        else:
            LOG.info(
                "reply in %.2fs over %d iteration(s), %d tool call(s)",
                reply.duration,
                reply.iterations,
                len(reply.tool_calls),
            )
        self.hud.set_state(STATE_IDLE)

    # -- slash commands ------------------------------------------------------------------
    def _handle_command(self, line: str) -> None:
        """Route a slash command. An unknown one prints the friendly list."""
        parts = line[1:].split(maxsplit=1)
        name = parts[0].lower() if parts else ""
        argument = parts[1].strip() if len(parts) > 1 else ""
        LOG.info("command: /%s %s", name, argument)

        handlers = {
            "help": self._cmd_help,
            "quit": self._cmd_quit,
            "exit": self._cmd_quit,
            "clear": self._cmd_clear,
            "protocol": self._cmd_protocol,
            "protocols": self._cmd_protocols,
            "voice": self._cmd_voice,
            "mute": self._cmd_mute,
            "unmute": self._cmd_unmute,
            "diag": self._cmd_diag,
            "tools": self._cmd_tools,
            "model": self._cmd_model,
            "history": self._cmd_history,
            "metrics": self._cmd_metrics,
            "mics": self._cmd_mics,
            "theme": self._cmd_theme,
            "desktop": self._cmd_desktop,
            "app": self._cmd_desktop,
            "colour": self._cmd_theme,
            "color": self._cmd_theme,
            "title": self._cmd_title,
            "lang": self._cmd_lang,
            "locales": self._cmd_locales,
            "toolchains": self._cmd_toolchains,
            "langs": self._cmd_toolchains,
            "permissions": self._cmd_permissions,
            "allow": self._cmd_allow,
            "revoke": self._cmd_revoke,
            "apps": self._cmd_apps,
            "enroll": self._cmd_enroll,
            "talk": lambda _arg: self.enable_talking(),
            "quiet": lambda _arg: self.silence(),
            "shutup": lambda _arg: self.silence(),
            "voiceprint": self._cmd_voiceprint,
        }
        handler = handlers.get(name)
        if handler is None:
            self._log_system(f"Unknown command: /{name}", "warn")
            self._cmd_help("")
            return
        handler(argument)

    def _cmd_help(self, argument: str) -> None:
        """Render the command reference."""
        assert self.hud is not None
        self.hud.render_table("Commands", ["Command", "Effect"], COMMANDS)

    def _cmd_quit(self, argument: str) -> None:
        """Leave the loop; ``run`` handles the orderly teardown."""
        self._running = False
        if self.tui is not None:
            # The classic loop exits by falling out of `repl`; the full-screen one
            # is blocking the main thread and has to be told.
            self.tui.stop()

    def _cmd_clear(self, argument: str) -> None:
        """Drop the conversation memory and wipe the transcript."""
        if self.agent is not None:
            self.agent.reset()
        if self.hud is not None:
            self.hud.clear_transcript()
        self._log_system(f"Context cleared, {settings.USER_TITLE}.", "success")

    def _cmd_protocol(self, argument: str) -> None:
        """Execute a named protocol."""
        if not argument:
            self._log_system("Name a protocol. /protocols lists them.", "warn")
            return
        self.run_protocol(argument)

    def _cmd_protocols(self, argument: str) -> None:
        """List the registered protocols and the active one, if any."""
        assert self.hud is not None
        if self.engine is None:
            self._log_system("No protocol engine is bound.", "error")
            return
        self.hud.render_table("Stark Protocols", ["Protocol", "Description"], self.engine.describe())
        active = self.engine.active
        if active:
            self._log_system(f"Active protocol: {active.replace('_', ' ').upper()}", "info")

    def _cmd_voice(self, argument: str) -> None:
        """Start or stop the wake-word listener."""
        mode = argument.strip().lower()
        if mode not in {"on", "off"}:
            state = "on" if self._listening else "off"
            self._log_system(f"Usage: /voice on|off (currently {state})", "warn")
            return
        if self.voice is None:
            self._log_system("The voice subsystem is disabled for this session.", "warn")
            return
        if mode == "on":
            if not self.voice.stt_available:
                self._log_system("No microphone is available to listen with.", "warn")
                return
            try:
                self._listening = bool(self.voice.start_listening())
            except Exception:
                LOG.exception("start_listening failed")
                self._listening = False
            self._log_system(
                "Listening." if self._listening else "The listener refused to start.",
                "success" if self._listening else "error",
            )
        else:
            try:
                self.voice.stop_listening()
            except Exception:
                LOG.exception("stop_listening failed")
            self._listening = False
            self._log_system("Microphone closed.", "info")
        self._refresh_voice_status()

    def _cmd_mute(self, argument: str) -> None:
        """Silence the spoken output without touching the microphone."""
        if self.voice is None:
            self._log_system("There is no voice to mute.", "warn")
            return
        self.voice.set_muted(True)
        self._refresh_voice_status()
        self._log_system("Muted.", "info")

    def _cmd_unmute(self, argument: str) -> None:
        """Restore the spoken output."""
        if self.voice is None:
            self._log_system("There is no voice to restore.", "warn")
            return
        self.voice.set_muted(False)
        self._refresh_voice_status()
        line = f"Voice restored, {settings.USER_TITLE}."
        self._log_system(line, "success")
        self._speak(line)

    def _cmd_diag(self, argument: str) -> None:
        """Run a full diagnostics sweep and report it in the transcript."""
        assert self.hud is not None
        if self.monitor is None:
            self._log_system("No monitor is bound.", "error")
            return
        try:
            telemetry = self.monitor.snapshot()
        except Exception as exc:
            LOG.exception("diagnostics sweep failed")
            self._log_system(f"Diagnostics failed: {exc}", "error")
            return
        self.hud.set_telemetry(telemetry)
        self.hud.log_agent(telemetry_report(telemetry), markdown=True)
        try:
            alerts = self.monitor.force_check()
        except Exception:
            LOG.exception("force_check failed")
            alerts = []
        for alert in alerts:
            self.hud.push_alert(alert)
        if not alerts:
            self._log_system(f"Nothing above threshold, {settings.USER_TITLE}.", "success")

    def _cmd_tools(self, argument: str) -> None:
        """List the instruments the agent may reach for."""
        assert self.hud is not None
        if self.registry is None:
            self._log_system("No tool registry is bound.", "error")
            return
        self.hud.render_table("Instruments", ["Tool", "Purpose"], self.registry.describe())

    def _cmd_model(self, argument: str) -> None:
        """Report the model configuration and its live availability."""
        assert self.hud is not None
        rows: list[tuple[str, str]] = [
            ("Model", settings.MODEL_NAME),
            ("Host", settings.OLLAMA_HOST),
            ("Temperature", f"{settings.MODEL_TEMPERATURE}"),
            ("Top-p", f"{settings.MODEL_TOP_P}"),
            ("Context", f"{settings.MODEL_NUM_CTX} tokens"),
            ("Thinking", "on" if settings.MODEL_THINKING else "off"),
            ("Streaming", "on" if settings.STREAM_RESPONSES else "off"),
            ("Keep alive", settings.OLLAMA_KEEP_ALIVE),
            ("Max tool loops", f"{settings.MAX_TOOL_ITERATIONS}"),
        ]
        if self.agent is not None:
            try:
                ok, message = self.agent.ensure_model()
            except Exception as exc:
                LOG.exception("ensure_model failed")
                ok, message = False, str(exc)
            rows.append(("Status", message))
            self.hud.set_model_status(
                f"{settings.MODEL_NAME} {'online' if ok else 'unreachable'}"
            )
        self.hud.render_table("Model", ["Setting", "Value"], rows)

    def _cmd_metrics(self, argument: str) -> None:
        """Report what the last turn cost, and where the time went."""
        metrics = getattr(self.agent, "metrics", None)
        if metrics is None or not getattr(metrics, "total", 0.0):
            self._log_system(
                "Nothing measured yet — ask me something first.", "info"
            )
            return
        rows = [
            ("First token", f"{metrics.ttft * 1000:.0f} ms"),
            ("Throughput", f"{metrics.tokens_per_second:.1f} tokens/second"),
            ("Model time", f"{metrics.model_seconds:.2f} s"),
            ("Instrument time", f"{metrics.tool_seconds:.2f} s"),
            ("Instruments", str(metrics.tool_calls)),
            ("Run in parallel", "yes" if metrics.parallel_peak > 1 else "no"),
            ("Saved by parallelism", f"{metrics.tool_seconds_saved:.2f} s"),
            ("Iterations", str(metrics.iterations)),
            ("Total", f"{metrics.total:.2f} s"),
        ]
        if getattr(metrics, "eval_tokens", 0):
            rows.insert(2, ("Tokens generated", str(metrics.eval_tokens)))
        assert self.hud is not None
        self.hud.render_table("Last turn", ["Measure", "Value"], rows)

    def _cmd_history(self, argument: str) -> None:
        """Show the tail of the conversation memory."""
        assert self.hud is not None
        if self.agent is None:
            self._log_system("No agent is bound.", "error")
            return
        messages = self.agent.memory.messages()
        if not messages:
            self._log_system("The conversation is empty.", "info")
            return
        rows: list[tuple[str, str]] = []
        for message in messages[-20:]:
            role = str(message.get("role", "?"))
            content = str(message.get("content") or "").strip().replace("\n", " ")
            if not content and message.get("tool_calls"):
                names = []
                for call in message.get("tool_calls") or []:
                    function = call.get("function") if isinstance(call, dict) else None
                    names.append(str((function or {}).get("name", "?")))
                content = f"<tool call: {', '.join(names)}>"
            rows.append((role, content[:160] + ("..." if len(content) > 160 else "")))
        self.hud.render_table(f"Memory ({len(messages)} messages)", ["Role", "Content"], rows)

    def _cmd_mics(self, argument: str) -> None:
        """Enumerate the input devices the recogniser can see."""
        assert self.hud is not None
        devices = VoiceSystem.list_microphones()
        if not devices:
            self._log_system("No input devices were detected.", "warn")
            return
        rows = [
            (str(index), f"{name}{'  <- selected' if settings.MIC_INDEX == index else ''}")
            for index, name in devices
        ]
        self.hud.render_table("Input devices", ["Index", "Device"], rows)

    def _cmd_theme(self, argument: str) -> None:
        """Recolour everything — the terminal, and any open desktop window.

        The old four-palette version is still in here: ``/theme veronica`` does
        what it always did. What is new is that ``/theme #ff8c42``, ``/theme
        violet`` and ``/theme surprise`` work too, because the colours are now
        derived from a seed rather than picked off a list.
        """
        spec = argument.strip()
        if not spec:
            current = theme_mod.load()
            self._log_system(
                f"Currently: {theme_mod.describe(current)}.  "
                f"/theme <hex, colour name, or one of: "
                f"{', '.join(sorted(theme_mod.PRESETS))}, surprise>",
                "info",
            )
            return

        # A shipped palette name keeps its original terminal behaviour as well
        # as recolouring the window, so nothing an operator already knows breaks.
        legacy = spec.lower().replace(" ", "_").replace("-", "_")
        if legacy in PALETTES and self.hud is not None:
            try:
                self.hud.set_palette(legacy)
            except Exception:
                LOG.debug("HUD refused the palette %s", legacy, exc_info=True)

        chosen = theme_mod.resolve(spec, theme_mod.load())
        if chosen is None:
            self._log_system(
                f"I do not know the colour {spec!r}. Give me a hex code such as "
                f"#ff8c42, a colour name such as violet, a preset "
                f"({', '.join(sorted(theme_mod.PRESETS))}) or 'surprise'.",
                "warn",
            )
            return

        self.apply_theme(chosen)
        self._log_system(f"Colours: {theme_mod.describe(chosen)}.", "success")

    def apply_theme(self, chosen: "theme_mod.Theme") -> None:
        """Persist a theme and push it everywhere it can be seen."""
        theme_mod.save(chosen)
        self._recolour_terminal(chosen)
        if self.desktop is not None:
            try:
                self.desktop.theme = chosen
                self.desktop.hub.publish(
                    "theme", theme=chosen.to_dict(), variables=chosen.css_variables()
                )
            except Exception:
                LOG.debug("Desktop window would not take the theme", exc_info=True)

    def _recolour_terminal(self, chosen: "theme_mod.Theme") -> None:
        """Register the theme with whichever terminal front end is running.

        Both terminal HUDs look their palettes up by name in a module-level
        dict, so a custom theme becomes real by being registered under the key
        ``custom`` and then selected. Rich and Textual both accept ``#rrggbb``
        wherever they accept a colour name, so one derivation serves all three
        front ends.
        """
        key = "custom"
        try:
            from jarvis.ui import PALETTES as RICH_PALETTES, Palette

            RICH_PALETTES[key] = Palette(**chosen.rich_palette())
        except Exception:
            LOG.debug("Could not register the palette with the status strip", exc_info=True)
        try:
            from textual.theme import Theme as TextualTheme

            from jarvis import tui as tui_mod

            tui_mod.THEMES[key] = TextualTheme(**chosen.textual_theme())
            tui_mod.INKS[key] = tui_mod.Ink(**chosen.textual_ink())
            # A Textual app has to be told about a theme before it can wear it.
            register = getattr(self.tui, "register_theme", None)
            if callable(register):
                register(tui_mod.THEMES[key])
        except Exception:
            LOG.debug("Could not register the theme with the full-screen HUD", exc_info=True)
        if self.hud is not None:
            try:
                self.hud.set_palette(key)
            except Exception:
                LOG.debug("HUD refused the custom palette", exc_info=True)

    def _cmd_desktop(self, argument: str) -> None:
        """Open a window onto this very session.

        Not a second J.A.R.V.I.S.: the window shares this one's memory, tools
        and permissions, and anything typed into it joins the same queue the
        terminal uses. Two front ends, one assistant.
        """
        action = argument.strip().lower()
        if action in {"close", "stop", "off", "quit"}:
            self._close_desktop()
            return

        if self.desktop is not None:
            self._log_system(f"The window is already open: {self.desktop.url}", "info")
            return

        try:
            from jarvis import desktop as desktop_mod
        except ImportError:
            self._log_system("The desktop front end is not installed.", "error")
            return

        ok, why = desktop_mod.available()
        if not ok:
            self._log_system(f"I cannot open a window: {why}.", "error")
            return

        try:
            app = desktop_mod.attach(
                on_submit=lambda text: self._queue.put(InputEvent("typed", text)),
                on_interrupt=self._interrupt_or_quit,
                snapshot=self._desktop_snapshot,
                mirror=self.hud,
                theme=theme_mod.load(),
                on_theme_change=self._recolour_terminal,
            )
        except Exception as exc:
            LOG.exception("The desktop window would not open")
            self._log_system(f"The window would not open: {exc}", "error")
            return

        self.desktop = app
        # From here on, everything the agent says goes to the window *and* the
        # terminal: app.hud mirrors into the HUD it was handed.
        self.hud = app.hud
        for target, kwargs in (
            (self.agent, {"hud": app.hud}),
            (self.engine, {"hud": app.hud}),
            (self.broker, {"hud": app.hud}),
        ):
            if target is None:
                continue
            binder = getattr(target, "bind", None)
            if callable(binder):
                binder(**kwargs)
            else:
                setattr(target, "hud", app.hud)
        self._log_system(
            f"Window open — {app.url}  "
            "Everything you type there lands in this same session.",
            "success",
        )

    def _desktop_snapshot(self) -> dict[str, Any]:
        """What the window shows about this session before its first message."""
        return {
            "model": settings.MODEL_NAME,
            "host": settings.OLLAMA_HOST,
            "title": settings.USER_TITLE,
            "tools": sorted(self.registry.names()) if self.registry else [],
            "protocols": sorted(self.engine.names()) if self.engine else [],
            "standalone": False,
        }

    def _close_desktop(self) -> None:
        """Shut the window and give the terminal its HUD back."""
        app, self.desktop = self.desktop, None
        if app is None:
            self._log_system("There is no window open.", "info")
            return
        terminal_hud = app.hud.mirror
        try:
            app.stop()
        except Exception:
            LOG.debug("The desktop app did not close cleanly", exc_info=True)
        if terminal_hud is not None:
            self.hud = terminal_hud
            for target in (self.agent, self.engine, self.broker):
                if target is None:
                    continue
                binder = getattr(target, "bind", None)
                if callable(binder):
                    binder(hud=terminal_hud)
                else:
                    setattr(target, "hud", terminal_hud)
        self._log_system("Window closed.", "info")

    def _cmd_title(self, argument: str) -> None:
        """Change the form of address on the fly, and remember it."""
        if not argument:
            self._log_system(
                f"I address you as {settings.USER_TITLE}. "
                "Usage: /title <Sir|Ma'am|Boss|anything you like>",
                "info",
            )
            return
        resolved = resolve_honorific(argument)
        if resolved is None:
            self._log_system("That is not a usable title.", "warn")
            return
        gender, title = resolved
        config.set_honorific(settings, gender, title)
        LOG.info("form of address changed to %r (%s)", settings.USER_TITLE, gender)
        confirmation = prompts.personalise(prompts.ONBOARDING_CONFIRM)
        self._log_system(confirmation, "success")
        self._speak(confirmation)

    def _cmd_voiceprint(self, argument: str) -> None:
        """Report, or forget, the enrolled voiceprint."""
        if argument.strip().lower() in {"forget", "delete", "clear"}:
            removed = speaker.forget()
            self._log_system(
                "Voiceprint deleted; the call word answers to any voice again."
                if removed
                else "There was no voiceprint to delete.",
                "success" if removed else "info",
            )
            return
        self._log_system(speaker.describe(), "info")
        if not speaker.enrolled():
            self._log_system(
                "Run `python main.py --enroll-voice` to record one, or /enroll here.",
                "info",
            )

    def _cmd_enroll(self, argument: str) -> None:
        """Point the operator at enrolment, which needs exclusive use of the microphone."""
        self._log_system(
            "Enrolment needs sole use of the microphone, so it runs on its own. "
            "Quit here and run:  python main.py --enroll-voice",
            "info",
        )

    def _cmd_permissions(self, argument: str) -> None:
        """Show what J.A.R.V.I.S. has been allowed to do outside the workspace."""
        assert self.hud is not None
        mode = settings.PERMISSION_MODE
        grants = self.broker.grants()
        history = self.broker.history()

        rows = [["Mode", mode], ["Workspace", str(settings.WORKSPACE_ROOT)]]
        if grants:
            for grant in grants:
                scope = "tree" if grant.prefix else "exact"
                rows.append([f"Allowed ({grant.scope}, {scope})", grant.target])
        else:
            rows.append(["Allowed", "nothing yet this session"])
        granted = sum(1 for _, _, ok in history if ok)
        rows.append(["Asked this session", f"{len(history)} ({granted} approved)"])
        self.hud.render_table("Permissions", ["Setting", "Value"], rows)
        self._log_system(
            "Grants last for this session only and are never written to disk. "
            "Use /allow to pre-approve, /revoke to clear.",
            "info",
        )

    def _cmd_allow(self, argument: str) -> None:
        """Pre-approve an application so he stops asking about it."""
        target = argument.strip()
        if not target:
            self._log_system(
                "Usage: /allow <app name>  — pre-approves opening that application. "
                "Example: /allow chrome",
                "info",
            )
            return
        self.broker.allow("launch", target)
        self._log_system(f"'{target}' may now be opened without asking.", "success")

    def _cmd_revoke(self, argument: str) -> None:
        """Forget every permission granted this session."""
        removed = self.broker.revoke_all()
        self._log_system(
            f"Cleared {removed} grant(s). I will ask again from here on.", "success"
        )

    def _cmd_apps(self, argument: str) -> None:
        """List the applications currently running."""
        assert self.hud is not None
        rows = [
            [row["name"], str(row["count"]), f"{row['memory_mb']:.0f} MB"]
            for row in apps.running_apps(25)
        ]
        if not rows:
            self._log_system("Nothing notable is running.", "info")
            return
        self.hud.render_table(
            "Running applications", ["Application", "Processes", "Memory"], rows
        )

    def _cmd_lang(self, argument: str) -> None:
        """Switch the language J.A.R.V.I.S. replies in."""
        if not argument:
            current = locales.active()
            enabled = ", ".join(
                f"{loc.name} ({loc.code})" for loc in locales.enabled_locales()
            )
            mode = "automatic" if settings.RESPONSE_LOCALE == "auto" else "pinned"
            self._log_system(
                f"Replying in {current.name} ({current.native_name}); detection is "
                f"{mode}. Available: {enabled}. Usage: /lang <name|code|auto>",
                "info",
            )
            return

        wanted = argument.strip().lower()
        if wanted == "auto":
            settings.RESPONSE_LOCALE = "auto"
            locales.reset_active()
            self._log_system(
                "Language detection is automatic again; I will follow your lead.",
                "success",
            )
            return

        locale = locales.set_active(wanted)
        if locale is None:
            enabled = ", ".join(loc.name for loc in locales.enabled_locales())
            self._log_system(
                f"'{argument}' is not an enabled language. Available: {enabled}. "
                f"Add more with ENABLED_LOCALES in .env.",
                "warn",
            )
            return

        # Pin it, so auto-detection cannot immediately undo a deliberate choice.
        settings.RESPONSE_LOCALE = locale.code
        confirmation = locales.localise("ack", locale)
        self._log_system(
            f"Now replying in {locale.name} ({locale.native_name}). {confirmation}",
            "success",
        )
        self._speak(confirmation)

    def _cmd_locales(self, argument: str) -> None:
        """Show every supported human language."""
        assert self.hud is not None
        rows = []
        enabled = {loc.code for loc in locales.enabled_locales()}
        active = locales.active().code
        for locale in locales.LOCALES.values():
            if locale.code == active:
                status = "active"
            elif locale.code in enabled:
                status = "enabled"
            else:
                status = "off"
            rows.append(
                [locale.code, locale.name, locale.native_name, locale.script,
                 status, locales.voice_for(locale)]
            )
        self.hud.render_table(
            "Languages",
            ["Code", "Language", "Native", "Script", "Status", "Voice"],
            rows,
        )

    def _cmd_toolchains(self, argument: str) -> None:
        """Show which programming languages this machine can actually run."""
        assert self.hud is not None
        refresh = argument.strip().lower() in {"refresh", "--refresh", "-r"}
        if refresh:
            self._log_system("Re-probing every toolchain...", "info")
        detections = languages.detect_all(refresh)
        rows = []
        for key, lang in languages.LANGUAGES.items():
            detection = detections.get(key)
            if not lang.executable:
                status, tool, version = "write-only", "--", "--"
            elif detection is not None and detection.available:
                status = "online"
                tool = (
                    detection.toolchain.describe()
                    if detection.toolchain
                    else (detection.binary or "--")
                )
                version = detection.version or "--"
            else:
                status, tool, version = "absent", "--", "--"
            rows.append([lang.display, status, tool, version, lang.extension])
        self.hud.render_table(
            "Language toolchains",
            ["Language", "Status", "Toolchain", "Version", "Ext"],
            rows,
        )
        online, total = languages.count_online()
        self._log_system(
            f"{online} of {total} executable language toolchains online.", "info"
        )

    # -- protocols and one-shots ----------------------------------------------------------
    def run_protocol(self, name: str) -> bool:
        """Resolve and execute a protocol by name. Returns success."""
        if self.engine is None:
            self._log_system("No protocol engine is bound.", "error")
            return False
        key = self.engine.resolve(name)
        if key is None:
            self._log_system(
                f"No such protocol: {name}. Known: {', '.join(self.engine.names()) or 'none'}",
                "warn",
            )
            return False
        try:
            result = self.engine.execute(key)
        except Exception as exc:
            LOG.exception("protocol %s raised", key)
            self._log_system(f"Protocol {key} faulted: {exc}", "error")
            return False
        if self.hud is not None:
            self.hud.log_agent(result.to_markdown(), markdown=True)
        LOG.info("protocol %s success=%s", key, result.success)
        return bool(result.success)

    def one_shot(self, question: str) -> None:
        """Answer a single ``--ask`` question with no loop and no live layout."""
        self._ask_agent(question, source="typed")
        if self.voice is not None and self.voice.tts_available:
            try:
                # Exiting mid-sentence would cut the answer off in the operator's ear.
                self.voice.wait_until_spoken(timeout=45.0)
            except Exception:
                LOG.exception("waiting for speech failed")

    # ==================================================================================
    # Step 9 - shutdown
    # ==================================================================================
    def shutdown(self, spoken: bool = True) -> None:
        """Stop every thread, close the microphone, tear the display down.

        Idempotent, and safe to call from a ``finally`` even when the boot never
        finished. Order matters: the microphone closes first so nothing new can
        arrive, the farewell is spoken while the engine is still alive, and the
        Live layout goes last so those lines remain on screen.
        """
        with self._shutdown_lock:
            if self._shutdown_done:
                return
            self._shutdown_done = True

        self._running = False
        self._stopping.set()
        self._accept_input.set()  # release the reader from its gate

        if self.voice is not None:
            try:
                self.voice.stop_listening()
            except Exception:
                LOG.exception("stop_listening failed during shutdown")
            self._listening = False

        if spoken and self.voice is not None and self.voice.tts_available:
            farewell = prompts.personalise(random.choice(prompts.SHUTDOWN_LINES))
            self._log_system(farewell, "info")
            try:
                self.voice.speak(farewell, interrupt=True)
                self.voice.wait_until_spoken(timeout=8.0)
            except Exception:
                LOG.exception("farewell failed")

        if self.voice is not None:
            try:
                self.voice.shutdown()
            except Exception:
                LOG.exception("voice shutdown failed")

        if self.desktop is not None:
            try:
                self.desktop.stop()
            except Exception:
                LOG.exception("desktop window shutdown failed")
            self.desktop = None

        if self.monitor is not None:
            try:
                self.monitor.stop(timeout=3.0)
            except Exception:
                LOG.exception("monitor shutdown failed")

        closer = getattr(self.agent, "close", None)
        if callable(closer):
            try:
                closer()
            except Exception:
                LOG.exception("engine shutdown failed")

        if self.hud is not None:
            try:
                self.hud.stop()
            except Exception:
                LOG.exception("HUD shutdown failed")

        # The console reader is a daemon parked in input(); a blocking stdin read
        # cannot be cancelled portably, so it is given a moment in case it is
        # between prompts and then left to die with the interpreter.
        reader = self._reader
        if reader is not None and reader.is_alive():
            reader.join(timeout=0.5)

        LOG.info("shutdown complete")
        logging.shutdown()

    def install_signal_handlers(self) -> None:
        """Turn SIGTERM / SIGBREAK into an orderly /quit rather than a hard stop."""

        def _handler(signum: int, frame: types.FrameType | None) -> None:
            LOG.info("signal %s received; requesting shutdown", signum)
            self._queue.put(InputEvent("system", "/quit"))

        for name in ("SIGTERM", "SIGBREAK"):
            sig = getattr(signal, name, None)
            if sig is None:
                continue
            try:
                signal.signal(sig, _handler)
            except (ValueError, OSError):
                LOG.debug("could not install a %s handler", name)

    # ==================================================================================
    # Orchestration
    # ==================================================================================
    def run(self) -> int:
        """Run whichever mode the command line selected. Returns the exit code."""
        exit_code = 0
        if self.tui_wanted():
            # The full-screen HUD owns the main thread and boots itself once it is
            # on screen, so the whole sequence below happens inside `run_tui`.
            try:
                return self.run_tui()
            except KeyboardInterrupt:
                LOG.info("interrupted")
                return 0
            except Exception:
                LOG.exception("fatal error in the full-screen front end")
                return 1
            finally:
                self.shutdown(spoken=True)
        try:
            self.build_frontend()
            self.run_onboarding()
            self.wire_backend()
            model_ok, _message = self.preflight()

            if self.args.check:
                return self.check_report(model_ok)

            if self.args.ask:
                if self.args.protocol:
                    self.run_protocol(self.args.protocol)
                self.one_shot(self.args.ask)
                return 0 if model_ok else 1

            self.install_signal_handlers()
            self.boot()
            if self.args.protocol:
                self.run_protocol(self.args.protocol)
            self.repl()
        except KeyboardInterrupt:
            LOG.info("interrupted before the loop began")
        except Exception:
            LOG.exception("fatal error")
            self._log_system("A fatal error occurred. See the log for details.", "error")
            exit_code = 1
        finally:
            # A farewell belongs to an interactive session, not to a scripted probe.
            self.shutdown(spoken=not (self.args.check or self.args.ask))
        return exit_code


# ======================================================================================
# Entry point
# ======================================================================================
def list_microphones() -> int:
    """Print the available input devices without constructing the whole system."""
    devices = VoiceSystem.list_microphones()
    if not devices:
        print("No input devices detected. Install pyaudio, or check the OS sound settings.")
        return 1
    print("Input devices (set MIC_INDEX in .env to pin one):")
    for index, name in devices:
        marker = "  <- selected" if settings.MIC_INDEX == index else ""
        print(f"  [{index:>2}] {name}{marker}")
    return 0


def force_utf8_output() -> None:
    """Reconfigure stdout/stderr to UTF-8.

    The default Windows console encoding is cp1252, which cannot represent a single
    Devanagari or Tamil character -- printing one raises ``UnicodeEncodeError`` and
    takes the reply down with it. A stream that refuses to be reconfigured is not fatal;
    ``jarvis.ui`` keeps an ASCII fallback for exactly that case.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass


ENROLL_PHRASES = [
    "Hello J.A.R.V.I.S.",
    "Namaste, J.A.R.V.I.S.",
    "This is my voice, and I would like you to remember it.",
]


def _bare_key(text: str) -> str:
    """Normalise a typed line for keyword matching: lowercase, no punctuation."""
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", str(text).lower())).strip()


def enroll_voice() -> int:
    """Record a few clips and build the operator's voiceprint.

    Deliberately uses the call words themselves for two of the three samples: the print
    is compared against wake-word audio, so enrolling on the phrase it will actually hear
    gives a far better match than enrolling on unrelated speech.
    """
    from rich.console import Console

    console = Console()
    try:
        import speech_recognition as sr
    except ImportError:
        console.print("[red]SpeechRecognition is not installed; cannot enrol.[/]")
        return 1

    recognizer = sr.Recognizer()
    recognizer.dynamic_energy_threshold = settings.STT_DYNAMIC_ENERGY

    try:
        microphone = (
            sr.Microphone(device_index=settings.MIC_INDEX)
            if settings.MIC_INDEX is not None
            else sr.Microphone()
        )
    except Exception as exc:
        console.print(f"[red]No microphone available: {exc}[/]")
        return 1

    console.print()
    console.print("[bold cyan]Voice enrolment[/]")
    console.print(
        f"[dim]I will record {settings.SPEAKER_ENROLL_PHRASES} short samples. "
        f"Speak normally, at the distance you usually would.[/]"
    )
    console.print()

    clips = []
    try:
        with microphone as source:
            recognizer.adjust_for_ambient_noise(source, duration=1.0)
            console.print(
                f"[dim]Room noise measured; threshold {recognizer.energy_threshold:.0f}.[/]"
            )
            for index in range(settings.SPEAKER_ENROLL_PHRASES):
                phrase = ENROLL_PHRASES[index % len(ENROLL_PHRASES)]
                console.print(
                    f"\n[bold gold1]{index + 1}/{settings.SPEAKER_ENROLL_PHRASES}[/] "
                    f"Say: [bold]{phrase}[/]"
                )
                console.input("[dim]press Enter when ready[/] ")
                console.print("[cyan]listening...[/]")
                try:
                    audio = recognizer.listen(
                        source,
                        timeout=10.0,
                        phrase_time_limit=settings.SPEAKER_ENROLL_SECONDS,
                    )
                except Exception as exc:
                    console.print(f"[yellow]Nothing captured ({exc}); skipping.[/]")
                    continue
                clips.append(audio)
                console.print("[green]captured[/]")
    except KeyboardInterrupt:
        console.print("\n[yellow]Enrolment cancelled.[/]")
        return 1
    except Exception as exc:
        console.print(f"[red]Enrolment failed: {exc}[/]")
        return 1

    if not clips:
        console.print("[red]No usable audio was captured.[/]")
        return 1

    print_ = speaker.enroll(clips)
    if print_ is None:
        console.print(
            "[red]Could not build a voiceprint from those samples — too little "
            "speech in them. Try again somewhere quieter.[/]"
        )
        return 1
    if not speaker.save(print_):
        console.print("[red]Voiceprint could not be saved.[/]")
        return 1

    console.print()
    console.print(f"[green]Done.[/] {speaker.describe()}")
    console.print(
        "[dim]The call word will now be checked against this voice. It is a "
        "convenience gate, not security — see the README.[/]"
    )
    return 0


def resolve_startup_theme(spec: str | None) -> "theme_mod.Theme":
    """Work out which colours to start in, honouring ``--theme`` when given.

    An unrecognisable colour is worth saying something about — the operator
    asked for it explicitly — but it is never worth refusing to start over.
    """
    remembered = theme_mod.load()
    if not spec:
        return remembered
    chosen = theme_mod.resolve(spec, remembered)
    if chosen is None:
        print(
            f"I do not know the colour {spec!r}. Try a hex code such as #ff8c42, "
            f"a name such as violet, or one of: {', '.join(sorted(theme_mod.PRESETS))}.",
            file=sys.stderr,
        )
        return remembered
    theme_mod.save(chosen)
    return chosen


def run_desktop(args: argparse.Namespace) -> int:
    """Open J.A.R.V.I.S. Desktop: the windowed front end, standing alone.

    Nothing about the terminal experience is involved here. The desktop app
    builds its own agent and owns the process until the window closes.
    """
    from jarvis import desktop

    ok, why = desktop.available()
    if not ok:
        print(f"The desktop app cannot start: {why}.", file=sys.stderr)
        return 1

    resolve_startup_theme(getattr(args, "theme", None))
    LOG.info("front end: desktop window")
    return desktop.run(
        port=getattr(args, "port", 0) or 0,
        open_window_on_start=not getattr(args, "no_window", False),
        turbo=not getattr(args, "no_turbo", False),
        monitor=not getattr(args, "no_monitor", False),
        model=getattr(args, "model", None),
    )


def run_daemon() -> int:
    """Background mode: wait for the call word, greet, and open a terminal."""
    from rich.console import Console

    from jarvis.daemon import WakeDaemon
    from jarvis.voice import VoiceSystem

    console = Console()
    console.print("[bold cyan]J.A.R.V.I.S. — background listener[/]")

    voice = VoiceSystem()
    if not voice.stt_available:
        console.print(
            f"[red]No microphone available: {'; '.join(voice.status.errors) or 'unknown'}[/]"
        )
        return 1

    console.print(f"[dim]{voice.status.describe()}[/]")
    console.print(f"[dim]{speaker.describe()}[/]")
    console.print(
        "[dim]Waiting for \"Hello J.A.R.V.I.S.\" or \"Namaste, J.A.R.V.I.S.\" — "
        "Ctrl+C to stop.[/]"
    )
    daemon = WakeDaemon(voice, console=console)
    try:
        return daemon.run_forever()
    finally:
        voice.shutdown()


def manage_password(clearing: bool = False) -> int:
    """Set or remove the desktop window's password.

    Typed here rather than written anywhere, and stored as a salted scrypt hash
    in ``.jarvis_credentials.json`` beside the profile. Putting a password in a
    source file would publish it to wherever the repository is pushed, along
    with every other place the same password happens to be used.
    """
    import getpass

    from jarvis import auth

    if clearing:
        if not auth.has_password():
            print("No password is set.")
            return 0
        auth.clear_password()
        print("The password has been removed.")
        if auth.mode() in ("password", "any"):
            print("DESKTOP_AUTH_MODE is still set, so the window will have no way to unlock.")
        return 0

    try:
        first = getpass.getpass("New password: ")
        second = getpass.getpass("Again: ")
    except (EOFError, KeyboardInterrupt):
        print("\nNothing was changed.")
        return 1

    if first != second:
        print("Those did not match. Nothing was changed.")
        return 1
    try:
        auth.set_password(first)
    except ValueError as error:
        print(f"{error}. Nothing was changed.")
        return 1

    print(f"Saved a hash of it to {auth.CREDENTIALS_PATH}.")
    if auth.mode() == "off":
        print("Set DESKTOP_AUTH_MODE=password in your .env to make the window ask for it.")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Parse the command line, configure logging, and run the application."""
    force_utf8_output()
    args = parse_args(argv)
    apply_overrides(args)
    log_path = configure_logging(args.debug)
    install_excepthooks()
    LOG.info(
        "%s %s starting: python=%s platform=%s log=%s",
        settings.AGENT_NAME,
        __version__,
        sys.version.split()[0],
        platform.platform(),
        log_path,
    )

    if args.list_mics:
        return list_microphones()

    if args.toolchains:
        print(languages.toolchain_report(refresh=True))
        return 0

    if args.forget_voice:
        removed = speaker.forget()
        print(
            "Voiceprint deleted." if removed else "There was no voiceprint to delete."
        )
        return 0

    if args.enroll_voice:
        return enroll_voice()

    if args.daemon:
        return run_daemon()

    if args.locales:
        print(locales.report())
        return 0

    if args.command in {"set-password", "clear-password"}:
        return manage_password(args.command == "clear-password")

    if args.desktop or args.command in {"desktop", "app", "gui", "window"}:
        return run_desktop(args)

    return JarvisApplication(args).run()


if __name__ == "__main__":
    sys.exit(main())
