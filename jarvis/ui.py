"""The Stark HUD: everything the operator actually sees.

The display is split in two on purpose.

* **The transcript** scrolls normally, printed above the live region. Conversation,
  tool traces, protocol reports and alerts all land here, so the terminal's own
  scrollback keeps working and the operator can page back through a session.
* **The status strip** is a small pinned panel at the bottom -- the animated waveform,
  the current state, and live telemetry gauges. Rich redraws it in place while printed
  output flows above it untouched.

A full-screen dashboard would look impressive in a screenshot and be miserable to type
into; this arrangement keeps the HUD glowing without fighting the shell for the cursor.

Everything here is thread-safe. The ambient monitor pushes alerts from its own thread and
the voice system updates state and amplitude from another, all while the main thread is
blocked on :meth:`StarkHUD.prompt_input`.

Imports :mod:`config` and :mod:`rich` only.
"""

from __future__ import annotations

import io
import logging
import math
import random
import shutil
import sys
import threading
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Sequence

from rich.align import Align
from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.rule import Rule
from rich.syntax import Syntax
from rich.table import Table
from rich.markup import escape
from rich.text import Text

# prompt_toolkit keeps the status strip pinned *while the operator types* and routes
# background output (monitor alerts, tool traces) above the prompt instead of through
# the middle of the line being edited. Without it the HUD vanishes at exactly the moment
# it is most useful, and an alert arriving mid-keystroke corrupts the input.
try:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.completion import WordCompleter
    from prompt_toolkit.formatted_text import ANSI
    from prompt_toolkit.history import InMemoryHistory
    from prompt_toolkit.patch_stdout import patch_stdout

    _PTK_AVAILABLE = True
except ImportError:  # pragma: no cover - optional dependency
    PromptSession = None  # type: ignore[assignment]
    _PTK_AVAILABLE = False


from config import (
    PALETTE_CLEAN_SLATE,
    PALETTE_HOUSE_PARTY,
    PALETTE_STANDARD,
    PALETTE_VERONICA,
    STATE_IDLE,
    STATE_LISTENING,
    STATE_SPEAKING,
    STATE_THINKING,
    STATE_WORKING,
    settings,
)

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════════════
# Palettes
# ══════════════════════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class Palette:
    """A HUD colour scheme. Protocols swap these to signal a change of posture."""

    name: str
    primary: str
    secondary: str
    accent: str
    warn: str
    danger: str
    dim: str
    text: str
    border: str


PALETTES: dict[str, Palette] = {
    # Workshop default: arc-reactor cyan with Stark gold.
    PALETTE_STANDARD: Palette(
        name="Standard",
        primary="bright_cyan",
        secondary="gold1",
        accent="cyan",
        warn="yellow",
        danger="red",
        dim="grey42",
        text="white",
        border="cyan",
    ),
    # House Party: everything on, gold and amber, considerably louder.
    PALETTE_HOUSE_PARTY: Palette(
        name="House Party",
        primary="gold1",
        secondary="dark_orange",
        accent="orange1",
        warn="yellow1",
        danger="red1",
        dim="grey50",
        text="white",
        border="gold1",
    ),
    # Veronica: lockdown. Hard crimson, nothing friendly about it.
    PALETTE_VERONICA: Palette(
        name="Veronica",
        primary="red1",
        secondary="bright_red",
        accent="deep_pink2",
        warn="orange1",
        danger="bright_red",
        dim="grey37",
        text="white",
        border="red1",
    ),
    # Clean Slate: cool steel, deliberately unexciting.
    PALETTE_CLEAN_SLATE: Palette(
        name="Clean Slate",
        primary="bright_white",
        secondary="steel_blue1",
        accent="light_steel_blue",
        warn="yellow",
        danger="red",
        dim="grey42",
        text="white",
        border="steel_blue",
    ),
}


ARC_REACTOR_BANNER = r"""
     ██╗ █████╗ ██████╗ ██╗   ██╗██╗███████╗
     ██║██╔══██╗██╔══██╗██║   ██║██║██╔════╝
     ██║███████║██████╔╝██║   ██║██║███████╗
██   ██║██╔══██║██╔══██╗╚██╗ ██╔╝██║╚════██║
╚█████╔╝██║  ██║██║  ██║ ╚████╔╝ ██║███████║
 ╚════╝ ╚═╝  ╚═╝╚═╝  ╚═╝  ╚═══╝  ╚═╝╚══════╝
"""

_ASCII_BANNER = r"""
    _   _   ___ __     __ ___  ___
 | | / \ | _ \\ \   / /|_ _|/ __|
 | || _ ||   / \ \ / /  | | \__ \
 |_||_| |_|_|_\  \_/   |___||___/
"""


def _console_supports_unicode() -> bool:
    """Can this console print block glyphs without raising?"""
    encoding = getattr(sys.stdout, "encoding", None) or ""
    try:
        "▁█▓░│".encode(encoding or "ascii")
        return True
    except (LookupError, UnicodeEncodeError):
        return False


_UNICODE_OK = _console_supports_unicode()


# ══════════════════════════════════════════════════════════════════════════════════════
# Waveform
# ══════════════════════════════════════════════════════════════════════════════════════

_BLOCKS = "▁▂▃▄▅▆▇█"
_ASCII_BLOCKS = ".:-=+*#@"


class WaveformRenderer:
    """The animated soundwave strip.

    Each state gets its own motion so the operator can tell what J.A.R.V.I.S. is doing
    without reading a word: a flat ripple when idle, reactive spikes while listening, a
    rolling wave while speaking, a travelling pulse while thinking, a scanner sweep while
    a tool runs.

    :meth:`frame` advances an internal phase counter and returns a string. It never
    sleeps -- the caller's refresh rate sets the tempo.
    """

    def __init__(self, width: int | None = None) -> None:
        self.width = max(8, int(width or settings.HUD_WAVEFORM_WIDTH))
        self._phase = 0
        self._glyphs = _BLOCKS if _UNICODE_OK else _ASCII_BLOCKS
        self._levels = len(self._glyphs) - 1
        self._rng = random.Random(0xA12C)

    def _glyph(self, level: float) -> str:
        index = int(max(0.0, min(1.0, level)) * self._levels)
        return self._glyphs[index]

    def frame(self, state: str, amplitude: float = 1.0) -> str:
        """Render one frame of the waveform for ``state``."""
        self._phase = (self._phase + 1) % 100_000
        amp = max(0.0, min(1.0, float(amplitude)))
        width = self.width
        phase = self._phase
        out: list[str] = []

        if state == STATE_SPEAKING:
            # A rolling wave whose height follows the speech amplitude.
            for i in range(width):
                base = 0.5 + 0.5 * _sine(i * 0.45 + phase * 0.35)
                jitter = self._rng.uniform(-0.12, 0.12)
                out.append(self._glyph(base * (0.35 + 0.65 * amp) + jitter))

        elif state == STATE_LISTENING:
            # Reactive: mostly quiet, spiking on input energy.
            for i in range(width):
                base = 0.12 + 0.88 * amp * abs(_sine(i * 0.7 + phase * 0.5))
                out.append(self._glyph(base + self._rng.uniform(-0.05, 0.15)))

        elif state == STATE_THINKING:
            # A single pulse travelling left to right.
            head = phase % width
            for i in range(width):
                distance = min(abs(i - head), width - abs(i - head))
                out.append(self._glyph(max(0.05, 1.0 - distance / 5.0)))

        elif state == STATE_WORKING:
            # Scanner sweep: a bright band bouncing across the strip.
            span = width * 2 - 2
            pos = phase % span
            head = pos if pos < width else span - pos
            for i in range(width):
                distance = abs(i - head)
                out.append(self._glyph(max(0.08, 1.0 - distance / 4.0)))

        else:  # STATE_IDLE and anything unexpected
            for i in range(width):
                base = 0.08 + 0.10 * _sine(i * 0.30 + phase * 0.12)
                out.append(self._glyph(base))

        return "".join(out)


def _sine(x: float) -> float:
    """Sine, hoisted out of the render loop -- this runs ~600 times per frame."""
    return math.sin(x)


# ══════════════════════════════════════════════════════════════════════════════════════
# Status strip renderable
# ══════════════════════════════════════════════════════════════════════════════════════


class _StatusStrip:
    """The pinned live region. Re-renders itself on every Live refresh."""

    def __init__(self, hud: "StarkHUD") -> None:
        self._hud = hud

    def __rich_console__(self, console: Console, options) -> Iterable[RenderableType]:
        yield self._hud._render_status()


# ══════════════════════════════════════════════════════════════════════════════════════
# The HUD
# ══════════════════════════════════════════════════════════════════════════════════════

_STATE_LABELS = {
    STATE_IDLE: "STANDBY",
    STATE_LISTENING: "LISTENING",
    STATE_THINKING: "THINKING",
    STATE_SPEAKING: "SPEAKING",
    STATE_WORKING: "WORKING",
}

#: Completed on Tab at the prompt. Kept here rather than imported from main to avoid a
#: cycle; main.py owns the authoritative handler table.
SLASH_COMMANDS = [
    "/help", "/quit", "/exit", "/clear", "/protocol", "/protocols", "/voice",
    "/mute", "/unmute", "/diag", "/tools", "/model", "/history", "/mics",
    "/theme", "/title", "/lang", "/locales", "/toolchains", "/permissions",
    "/allow", "/revoke", "/apps",
]

_LEVEL_GLYPHS_UNICODE = {"info": "·", "warn": "!", "error": "×", "success": "✓"}
_LEVEL_GLYPHS_ASCII = {"info": "-", "warn": "!", "error": "x", "success": "+"}

_LEVEL_STYLES = {
    "info": "primary",
    "warn": "warn",
    "error": "danger",
    "success": "secondary",
}


class StarkHUD:
    """The heads-up display.

    All public methods are safe to call from any thread and are no-ops rather than
    errors when the display is disabled, so callers never have to guard them.
    """

    def __init__(
        self,
        console: Console | None = None,
        palette: str = PALETTE_STANDARD,
        enabled: bool | None = None,
    ) -> None:
        self.console = console or Console(
            highlight=False,
            soft_wrap=False,
            legacy_windows=False,
        )
        self._lock = threading.RLock()
        self._palette = PALETTES.get(palette, PALETTES[PALETTE_STANDARD])
        self._palette_key = palette if palette in PALETTES else PALETTE_STANDARD

        wanted = settings.HUD_ENABLED if enabled is None else bool(enabled)
        # A redirected stdout cannot host a live region; fall back to plain printing.
        self._enabled = bool(wanted and self.console.is_terminal)

        self._waveform = WaveformRenderer()
        self._state = STATE_IDLE
        self._state_detail = ""
        self._amplitude = 0.0
        self._telemetry = None
        self._protocol: str | None = None
        self._voice_status = "voice: initialising"
        self._model_status = f"model: {settings.MODEL_NAME}"
        self._alerts: deque = deque(maxlen=3)

        self._live: Live | None = None
        self._stream_buffer: list[str] = []
        self._streaming = False
        self._session = None          # lazily built PromptSession
        self._completer = None

        if not _UNICODE_OK:
            logger.info("Console is not UTF-8; using the ASCII glyph set")

    # -- palette helpers ---------------------------------------------------------------

    def _style(self, name: str) -> str:
        """Resolve a semantic colour name against the active palette."""
        return getattr(self._palette, name, name)

    @property
    def palette(self) -> Palette:
        return self._palette

    @property
    def live_active(self) -> bool:
        return self._live is not None

    # -- lifecycle ---------------------------------------------------------------------

    def start(self) -> None:
        """Begin the pinned status strip. Idempotent."""
        if not self._enabled:
            return
        with self._lock:
            if self._live is not None:
                return
            try:
                self._live = Live(
                    _StatusStrip(self),
                    console=self.console,
                    refresh_per_second=max(2, int(settings.HUD_FPS)),
                    transient=True,
                    auto_refresh=True,
                )
                self._live.start()
            except Exception:
                # A terminal that refuses the live region is not a reason to die.
                logger.debug("Could not start the live region", exc_info=True)
                self._live = None
                self._enabled = False

    def stop(self) -> None:
        """Tear the status strip down. Idempotent and safe during shutdown."""
        with self._lock:
            live, self._live = self._live, None
        if live is not None:
            try:
                live.stop()
            except Exception:
                logger.debug("Live region did not stop cleanly", exc_info=True)

    @contextmanager
    def paused(self):
        """Suspend the live region so raw stdin/stdout can use the terminal."""
        live = self._live
        if live is None:
            yield
            return
        try:
            live.stop()
        except Exception:
            logger.debug("Could not pause the live region", exc_info=True)
        try:
            yield
        finally:
            try:
                live.start()
                with self._lock:
                    self._live = live
            except Exception:
                logger.debug("Could not resume the live region", exc_info=True)
                with self._lock:
                    self._live = None

    # -- printing ----------------------------------------------------------------------

    def _emit(self, renderable: RenderableType) -> None:
        """Print above the live region, or plainly when there is none."""
        try:
            with self._lock:
                self.console.print(renderable)
        except UnicodeEncodeError:
            # Legacy console that cannot render what we asked for; degrade rather than die.
            try:
                self.console.print(Text(_ascii_fallback(renderable)))
            except Exception:
                logger.debug("Output dropped: console cannot encode it", exc_info=True)
        except Exception:
            logger.debug("HUD print failed", exc_info=True)

    def print_banner(self) -> None:
        """The arc-reactor splash shown once at boot."""
        banner = ARC_REACTOR_BANNER if _UNICODE_OK else _ASCII_BANNER
        subtitle = Text(
            f"{settings.AGENT_FULL_NAME}  ·  v{_version()}",
            style=self._style("dim"),
        )
        body = Group(
            Align.center(Text(banner, style=f"bold {self._style('primary')}")),
            Align.center(subtitle),
        )
        self._emit(
            Panel(
                body,
                border_style=self._style("border"),
                padding=(0, 2),
            )
        )

    # -- state -------------------------------------------------------------------------

    def set_palette(self, name: str) -> None:
        """Switch colour scheme, e.g. when a protocol engages."""
        with self._lock:
            if name in PALETTES:
                self._palette = PALETTES[name]
                self._palette_key = name

    def set_state(self, state: str, detail: str = "") -> None:
        with self._lock:
            self._state = state
            self._state_detail = detail

    def set_amplitude(self, value: float) -> None:
        with self._lock:
            try:
                self._amplitude = max(0.0, min(1.0, float(value)))
            except (TypeError, ValueError):
                self._amplitude = 0.0

    def set_telemetry(self, telemetry) -> None:
        with self._lock:
            self._telemetry = telemetry

    def set_protocol(self, name: str | None) -> None:
        with self._lock:
            self._protocol = name

    def set_voice_status(self, text: str) -> None:
        with self._lock:
            self._voice_status = str(text)

    def set_model_status(self, text: str) -> None:
        with self._lock:
            self._model_status = str(text)

    # -- transcript --------------------------------------------------------------------

    def log_user(self, text: str) -> None:
        """Echo what the operator said or typed."""
        title = settings.USER_TITLE
        self._emit(
            Panel(
                Text(str(text), style=self._style("text")),
                title=f"[{self._style('secondary')}]{title}[/]",
                title_align="left",
                border_style=self._style("dim"),
                padding=(0, 1),
            )
        )

    def log_agent(self, text: str, markdown: bool = True) -> None:
        """J.A.R.V.I.S.'s reply, rendered as Markdown so tables and code survive."""
        content = str(text).strip()
        if not content:
            return
        body: RenderableType
        if markdown:
            try:
                body = Markdown(content)
            except Exception:
                body = Text(content, style=self._style("text"))
        else:
            body = Text(content, style=self._style("text"))
        self._emit(
            Panel(
                body,
                title=f"[bold {self._style('primary')}]{settings.AGENT_NAME}[/]",
                title_align="left",
                border_style=self._style("border"),
                padding=(0, 1),
            )
        )

    def log_system(self, text: str, level: str = "info") -> None:
        """A one-line operational note: preflight results, mode changes, warnings."""
        style = self._style(_LEVEL_STYLES.get(level, "primary"))
        # Keyed by level in both tables: the previous version mixed level names and
        # glyphs as keys, so "success" fell through to a generic asterisk.
        glyphs = _LEVEL_GLYPHS_UNICODE if _UNICODE_OK else _LEVEL_GLYPHS_ASCII
        marker = glyphs.get(level, glyphs["info"])
        self._emit(Text(f"  {marker} {text}", style=style))

    def log_tool(
        self, name: str, arguments: dict, result: str = "", ok: bool = True
    ) -> None:
        """One line of the ReAct trace: which instrument ran, with what, and how it went."""
        status_style = self._style("secondary") if ok else self._style("danger")
        mark = ("✓" if ok else "×") if _UNICODE_OK else ("+" if ok else "x")
        arg_text = _compact_args(arguments)

        line = Text()
        line.append(f"  {mark} ", style=status_style)
        line.append(str(name), style=f"bold {self._style('accent')}")
        if arg_text:
            line.append(f"({arg_text})", style=self._style("dim"))
        preview = _one_line(result, 96)
        if preview:
            line.append(f"  → {preview}" if _UNICODE_OK else f"  -> {preview}",
                        style=self._style("dim"))
        self._emit(line)

    def log_interim(self, text: str) -> None:
        """Narration that came alongside a tool call, not the final answer.

        Rendering this as a full panel was the source of the duplicated reply: the model
        often says "Rust is not installed, I will use Python" *and* calls a tool in the
        same turn, then answers properly a turn later. The remark is worth showing; it is
        not worth showing twice, dressed as a conclusion.
        """
        content = _one_line(text, 200)
        if not content:
            return
        self._emit(Text(f"  {content}", style=f"italic {self._style('dim')}"))

    def log_tool_start(self, name: str, arguments: dict) -> None:
        """Announce a tool *before* it runs, so long calls do not look like a hang."""
        arrow = "▸" if _UNICODE_OK else ">"
        line = Text()
        line.append(f"  {arrow} ", style=self._style("accent"))
        line.append(str(name), style=f"bold {self._style('accent')}")
        arg_text = _compact_args(arguments)
        if arg_text:
            line.append(f"({arg_text})", style=self._style("dim"))
        self._emit(line)

    def log_thought(self, text: str) -> None:
        """The model's private reasoning, when thinking is switched on."""
        content = _one_line(text, 400)
        if not content:
            return
        self._emit(
            Panel(
                Text(content, style=f"italic {self._style('dim')}"),
                title=f"[{self._style('dim')}]reasoning[/]",
                title_align="left",
                border_style=self._style("dim"),
                padding=(0, 1),
            )
        )

    def push_alert(self, alert) -> None:
        """A proactive warning from the ambient monitor."""
        severity = str(getattr(alert, "severity", "warning")).lower()
        style = self._style("danger") if severity == "critical" else self._style("warn")
        title = str(getattr(alert, "title", "Alert"))
        message = str(getattr(alert, "message", alert))
        suggestion = str(getattr(alert, "suggestion", "") or "")

        with self._lock:
            self._alerts.append((title, severity))

        body = Text(message, style=self._style("text"))
        if suggestion:
            body.append(f"\n{suggestion}", style=self._style("dim"))
        self._emit(
            Panel(
                body,
                title=f"[bold {style}]{escape(title)}[/]",
                title_align="left",
                border_style=style,
                padding=(0, 1),
            )
        )

    def render_table(
        self, title: str, columns: Sequence[str], rows: Sequence[Sequence[str]]
    ) -> None:
        """A titled table -- diagnostics, tool listings, protocol reports."""
        table = Table(
            title=title or None,
            title_style=f"bold {self._style('secondary')}",
            border_style=self._style("dim"),
            header_style=f"bold {self._style('primary')}",
            expand=False,
            padding=(0, 1),
        )
        for column in columns:
            table.add_column(str(column), overflow="fold")
        for row in rows:
            table.add_row(*[Text(str(cell)) for cell in row])
        self._emit(table)

    def render_code(self, code: str, language: str = "python", title: str = "") -> None:
        """Syntax-highlighted source, in whatever language it happens to be."""
        try:
            body: RenderableType = Syntax(
                str(code), language or "text", theme="monokai",
                line_numbers=False, word_wrap=True,
            )
        except Exception:
            body = Text(str(code))
        self._emit(
            Panel(
                body,
                title=f"[{self._style('accent')}]{title or language}[/]",
                title_align="left",
                border_style=self._style("dim"),
                padding=(0, 1),
            )
        )

    def clear_transcript(self) -> None:
        """Wipe the screen. Clean Slate Protocol's visible half."""
        with self._lock:
            self._alerts.clear()
        try:
            self.console.clear()
        except Exception:
            logger.debug("Console clear failed", exc_info=True)

    # -- streaming ---------------------------------------------------------------------

    def stream_begin(self) -> None:
        """Start collecting a streamed reply."""
        with self._lock:
            self._stream_buffer = []
            self._streaming = True

    def stream_token(self, token: str) -> None:
        """Buffer one streamed chunk.

        Tokens are accumulated rather than printed as they arrive: partial Markdown
        renders badly, and a half-drawn table is worse than a brief wait. The waveform
        keeps moving in the meantime so the operator can see he is working.
        """
        if not token:
            return
        with self._lock:
            if self._streaming:
                self._stream_buffer.append(str(token))

    def stream_end(self, final_text: str | None = None, interim: bool = False) -> None:
        """Finish a streamed reply and render it.

        ``interim=True`` means this turn also carried tool calls, so the text is
        narration rather than the answer and gets a quiet line instead of a panel.
        """
        with self._lock:
            buffered = "".join(self._stream_buffer)
            self._stream_buffer = []
            self._streaming = False
        text = final_text if final_text is not None else buffered
        if not text or not text.strip():
            return
        if interim:
            self.log_interim(text)
        else:
            self.log_agent(text)

    # -- input -------------------------------------------------------------------------

    def _ensure_session(self):
        """Build the prompt_toolkit session once, on first use."""
        if self._session is not None or not _PTK_AVAILABLE:
            return self._session
        try:
            self._completer = WordCompleter(
                SLASH_COMMANDS, ignore_case=True, sentence=False
            )
            self._session = PromptSession(
                history=InMemoryHistory(),
                completer=self._completer,
                complete_while_typing=False,
                enable_history_search=True,
            )
        except Exception:
            logger.debug("prompt_toolkit session unavailable", exc_info=True)
            self._session = None
        return self._session

    def _toolbar(self):
        """The status strip, rendered for prompt_toolkit's pinned bottom toolbar."""
        try:
            width = shutil.get_terminal_size((100, 24)).columns
            buffer = io.StringIO()
            scratch = Console(
                file=buffer,
                force_terminal=True,
                color_system="truecolor",
                width=max(40, width - 1),
                highlight=False,
                soft_wrap=False,
                legacy_windows=False,
            )
            scratch.print(self._render_status(bordered=False))
            return ANSI(buffer.getvalue().rstrip("\n"))
        except Exception:
            logger.debug("Toolbar render failed", exc_info=True)
            return ""

    def _read_line(self, label: str, style: str = "") -> str:
        """The single input primitive every prompt in the HUD goes through.

        With prompt_toolkit the live region is handed over to it, which keeps the status
        strip visible as a bottom toolbar and -- via ``patch_stdout`` -- lets the monitor
        thread print an alert without shredding the line being typed. Without it, we fall
        back to suspending Rich's Live and using a plain read.
        """
        session = self._ensure_session()
        if session is not None and self._enabled:
            live = self._live
            if live is not None:
                try:
                    live.stop()
                except Exception:
                    logger.debug("Could not pause Live for input", exc_info=True)
            try:
                with patch_stdout(raw=True):
                    return session.prompt(
                        label,
                        bottom_toolbar=self._toolbar,
                        refresh_interval=1.0 / max(2, int(settings.HUD_FPS)),
                    )
            except (EOFError, KeyboardInterrupt):
                raise
            except Exception:
                logger.debug("prompt_toolkit input failed; falling back", exc_info=True)
            finally:
                if live is not None:
                    try:
                        live.start()
                        with self._lock:
                            self._live = live
                    except Exception:
                        logger.debug("Could not resume Live", exc_info=True)
                        with self._lock:
                            self._live = None

        markup = f"[bold {style or self._style('secondary')}]{escape(label)}[/]"
        with self.paused():
            try:
                answer = self.console.input(markup)
            except (EOFError, KeyboardInterrupt):
                raise
            except Exception:
                logger.debug("Console input failed; using raw stdin", exc_info=True)
                answer = input(label)
        # Piped stdin echoes nothing and supplies no newline, so without this the next
        # line of output lands on top of the prompt label.
        if not self.console.is_terminal:
            self.console.print()
        return answer

    def prompt_input(self, prompt_text: str = "") -> str:
        """Read a line from the operator with the HUD still on screen."""
        # Parenthesised deliberately: the previous form bound as
        # `(prompt_text or default) if unicode else "> "`, which silently discarded a
        # caller-supplied prompt on any non-UTF-8 console.
        default = f"{settings.USER_TITLE} > " if not _UNICODE_OK else f"{settings.USER_TITLE} \u203a "
        label = prompt_text or default
        return self._read_line(label)

    def confirm(self, question: str) -> bool:
        """A yes/no gate for anything destructive."""
        self._emit(Text(f"  {question}", style=f"bold {self._style('warn')}"))
        try:
            answer = self._read_line("  (y/N) ", style=self._style("warn"))
        except (EOFError, KeyboardInterrupt):
            return False
        return str(answer).strip().lower() in {"y", "yes"}

    def ask_permission(self, request) -> str:
        """Put a permission request to the operator and return their raw answer.

        Deliberately verbose about *what* is being asked: a prompt that says only
        "allow?" trains people to type y without reading, which defeats the point of
        having a prompt at all.
        """
        reversible = bool(getattr(request, "reversible", True))
        accent = self._style("warn" if reversible else "danger")

        detail = Table.grid(padding=(0, 2))
        detail.add_column(style=self._style("dim"), justify="right")
        detail.add_column(style=self._style("text"), overflow="fold")
        detail.add_row("Scope", str(getattr(request, "label", "")))
        detail.add_row("Action", str(getattr(request, "action", "")))
        detail.add_row("Target", str(getattr(request, "target", "")))
        extra = str(getattr(request, "detail", "") or "")
        if extra:
            detail.add_row("Context", extra)
        if not reversible:
            detail.add_row("Note", "This cannot be undone.")

        options = Text()
        for key, meaning in (
            ("y", "allow once"),
            ("a", "always allow"),
            ("n", "deny"),
            ("!", "deny and stop asking"),
        ):
            options.append(f" [{key}] ", style=f"bold {accent}")
            options.append(f"{meaning}  ", style=self._style("dim"))

        question = str(getattr(request, "question", lambda: "May I proceed?")())
        self._emit(
            Panel(
                Group(
                    Text(question, style=f"bold {self._style('text')}"),
                    Text(),
                    detail,
                    Text(),
                    options,
                ),
                title=f"[bold {accent}]Permission required[/]",
                title_align="left",
                border_style=accent,
                padding=(0, 1),
            )
        )
        try:
            return self._read_line("  permit? ", style=accent).strip()
        except (EOFError, KeyboardInterrupt):
            return "n"

    def choose(
        self, question: str, options: list[tuple[str, str]], hint: str = ""
    ) -> str:
        """Offer a small menu and return the operator's raw answer.

        Used by first-run onboarding, where the answer may be a listed key *or* a
        free-text title the operator typed instead, so the raw string is returned rather
        than a validated choice.
        """
        table = Table.grid(padding=(0, 2))
        table.add_column(style=f"bold {self._style('secondary')}", justify="right")
        table.add_column(style=self._style("text"))
        for key, label in options:
            table.add_row(str(key), str(label))

        body: list[RenderableType] = [Text(question, style=self._style("text")), Text()]
        body.append(table)
        if hint:
            body.extend([Text(), Text(hint, style=self._style("dim"))])

        self._emit(
            Panel(
                Group(*body),
                border_style=self._style("border"),
                padding=(1, 2),
            )
        )
        try:
            return self._read_line("  > ", style=self._style("primary")).strip()
        except (EOFError, KeyboardInterrupt):
            return ""
        except Exception:
            return ""

    # -- the pinned strip --------------------------------------------------------------

    def _render_status(self, bordered: bool = True) -> RenderableType:
        """Build the status panel. Called on every refresh, so it stays cheap.

        ``bordered=False`` is used for prompt_toolkit's bottom toolbar, where a boxed
        panel wastes two of the very few lines the toolbar gets.
        """
        with self._lock:
            state = self._state
            detail = self._state_detail
            amplitude = self._amplitude
            telemetry = self._telemetry
            protocol = self._protocol
            voice = self._voice_status
            model = self._model_status
            palette = self._palette

        wave = self._waveform.frame(state, amplitude)
        state_style = {
            STATE_LISTENING: palette.secondary,
            STATE_SPEAKING: palette.primary,
            STATE_THINKING: palette.accent,
            STATE_WORKING: palette.warn,
        }.get(state, palette.dim)

        top = Text()
        top.append(wave, style=f"bold {state_style}")
        top.append("  ")
        top.append(_STATE_LABELS.get(state, state.upper()), style=f"bold {state_style}")
        if detail:
            top.append(f"  {detail}", style=palette.dim)

        gauges = _telemetry_line(telemetry, palette)

        footer = Text()
        footer.append(model, style=palette.dim)
        footer.append("  │  " if _UNICODE_OK else "  |  ", style=palette.dim)
        footer.append(voice, style=palette.dim)
        if protocol:
            footer.append("  │  " if _UNICODE_OK else "  |  ", style=palette.dim)
            footer.append(f"PROTOCOL {protocol.replace('_', ' ').upper()}",
                          style=f"bold {palette.secondary}")
        footer.append("  │  " if _UNICODE_OK else "  |  ", style=palette.dim)
        footer.append(datetime.now().strftime("%H:%M:%S"), style=palette.dim)

        parts: list[RenderableType] = [top]
        if gauges is not None:
            parts.append(gauges)
        parts.append(footer)

        if not bordered:
            return Group(*parts)
        return Panel(
            Group(*parts),
            border_style=palette.border,
            padding=(0, 1),
        )


# ══════════════════════════════════════════════════════════════════════════════════════
# Rendering helpers
# ══════════════════════════════════════════════════════════════════════════════════════

_GAUGE_FULL = "█" if _UNICODE_OK else "#"
_GAUGE_EMPTY = "░" if _UNICODE_OK else "."


def _gauge(percent: float, width: int = 10) -> str:
    """A small bar gauge, e.g. ``████░░░░░░``."""
    try:
        pct = max(0.0, min(100.0, float(percent)))
    except (TypeError, ValueError):
        pct = 0.0
    filled = int(round(pct / 100.0 * width))
    return _GAUGE_FULL * filled + _GAUGE_EMPTY * (width - filled)


def _gauge_style(percent: float, palette: Palette) -> str:
    """Green-ish below 70, amber to 90, red above."""
    try:
        pct = float(percent)
    except (TypeError, ValueError):
        return palette.dim
    if pct >= 90:
        return palette.danger
    if pct >= 70:
        return palette.warn
    return palette.primary


def _telemetry_line(telemetry, palette: Palette) -> Text | None:
    """One row of live gauges, or ``None`` before the first sample arrives."""
    if telemetry is None or not settings.HUD_SHOW_TELEMETRY:
        return None

    line = Text()

    def add(label: str, percent, suffix: str = "") -> None:
        if percent is None:
            return
        if len(line) > 0:
            line.append("  ")
        style = _gauge_style(percent, palette)
        line.append(f"{label} ", style=palette.dim)
        line.append(_gauge(percent, 8), style=style)
        line.append(f" {float(percent):.0f}%{suffix}", style=style)

    add("CPU", getattr(telemetry, "cpu_percent", None))
    add("RAM", getattr(telemetry, "ram_percent", None))

    disks = getattr(telemetry, "disks", None) or []
    if disks:
        busiest = max(disks, key=lambda d: getattr(d, "percent", 0) or 0)
        add("DSK", getattr(busiest, "percent", None))

    battery = getattr(telemetry, "battery_percent", None)
    if battery is not None:
        plugged = getattr(telemetry, "battery_plugged", None)
        mark = ("⚡" if plugged else "") if _UNICODE_OK else ("^" if plugged else "")
        add("BAT", battery, mark)

    gpus = getattr(telemetry, "gpus", None) or []
    for gpu in gpus[:1]:
        util = getattr(gpu, "utilization_percent", None)
        if util is not None:
            add("GPU", util)

    return line if len(line) else None


def _compact_args(arguments) -> str:
    """Squash a tool's arguments onto one short line for the trace."""
    if not isinstance(arguments, dict) or not arguments:
        return ""
    parts = []
    for key, value in list(arguments.items())[:3]:
        text = _one_line(str(value), 40)
        parts.append(f"{key}={text}")
    return ", ".join(parts)


def _one_line(text, limit: int = 100) -> str:
    """Collapse whitespace and truncate, for single-line trace output."""
    if text is None:
        return ""
    flat = " ".join(str(text).split())
    if len(flat) <= limit:
        return flat
    return flat[: limit - 1].rstrip() + ("…" if _UNICODE_OK else "...")


def _ascii_fallback(renderable: RenderableType) -> str:
    """Last-ditch plain text for a console that cannot encode the real output."""
    try:
        text = str(renderable)
    except Exception:
        return "[output omitted: console encoding]"
    return text.encode("ascii", "replace").decode("ascii")


def _version() -> str:
    """Package version without importing the package (which would be circular)."""
    try:
        from jarvis import __version__

        return __version__
    except Exception:
        return "1.0.0"
