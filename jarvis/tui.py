"""The full-screen HUD: J.A.R.V.I.S. as a Textual application.

:mod:`jarvis.ui` pins a small status strip below a scrolling terminal. That is a
good, conservative design and it stays available under ``--classic``. This module
is the other answer to the same question: give the whole terminal over to the
display, and get a real application in return — a transcript you can scroll with
the mouse, instruments that redraw as they run, live vitals, a modal consent
dialog that cannot be dismissed by a stray keystroke, and a command palette.

It presents exactly the interface :class:`jarvis.ui.StarkHUD` presents, method for
method, so the agent, the ambient monitor, the voice system and the permission
broker bind to it without knowing which display they got.

Two design decisions carry most of the responsiveness:

**Nothing that renders happens on a caller's thread.** The agent streams tokens
from an asyncio loop, the monitor pushes telemetry from a poller, the voice
system pushes amplitude from an audio callback. All of them write to a queue and
return immediately. A single pump on the UI thread drains that queue on a frame
budget, so a model emitting three hundred tokens a second costs the display
thirty redraws a second, not three hundred.

**The transcript coalesces.** A streamed reply is one widget that is re-rendered
from a growing buffer, never one widget per token, and it is only re-rendered on
frames where the buffer actually changed.
"""

from __future__ import annotations

import logging
import queue
import random
import re
import shutil
import threading
import time
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Sequence

from rich.console import Group, RenderableType
from rich.highlighter import Highlighter
from rich.markdown import Markdown as RichMarkdown
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.suggester import SuggestFromList
from textual.theme import Theme
from textual.widgets import Button, Footer, Input, Label, Static

from config import (
    PALETTE_CLEAN_SLATE,
    QUIET_WORDS,
    TALK_WORDS,
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

__all__ = ["JarvisTUI", "available"]


def available() -> bool:
    """True when this terminal can host the full-screen HUD."""
    try:
        import textual  # noqa: F401
    except ImportError:
        return False
    return True


# ══════════════════════════════════════════════════════════════════════════════════════
# Themes — one per protocol posture, so a mode change is visible before it is read
# ══════════════════════════════════════════════════════════════════════════════════════
THEMES: dict[str, Theme] = {
    # Workshop default: arc-reactor cyan on near-black, with Stark gold for accents.
    PALETTE_STANDARD: Theme(
        name="jarvis-standard",
        primary="#d97757",
        secondary="#c2b8ab",
        accent="#8fb8d8",
        warning="#e0a458",
        error="#e06c62",
        success="#7fb069",
        foreground="#e8e6e3",
        background="#161513",
        surface="#1c1b19",
        panel="#232220",
        dark=True,
    ),
    # House Party: everything on, gold and amber, considerably louder.
    PALETTE_HOUSE_PARTY: Theme(
        name="jarvis-house-party",
        primary="#fbbf24",
        secondary="#fb923c",
        accent="#f97316",
        warning="#fde047",
        error="#ef4444",
        success="#a3e635",
        foreground="#fef3c7",
        background="#0d0800",
        surface="#1a1004",
        panel="#241708",
        dark=True,
    ),
    # Veronica: armour-plate steel. Deliberately cold.
    PALETTE_VERONICA: Theme(
        name="jarvis-veronica",
        primary="#94a3b8",
        secondary="#e2e8f0",
        accent="#cbd5e1",
        warning="#fbbf24",
        error="#f87171",
        success="#86efac",
        foreground="#e2e8f0",
        background="#07090c",
        surface="#111418",
        panel="#181d24",
        dark=True,
    ),
    # Clean Slate: green, quiet, everything forgotten.
    PALETTE_CLEAN_SLATE: Theme(
        name="jarvis-clean-slate",
        primary="#4ade80",
        secondary="#22d3ee",
        accent="#34d399",
        warning="#facc15",
        error="#f87171",
        success="#4ade80",
        foreground="#dcfce7",
        background="#030805",
        surface="#0a1410",
        panel="#0f1d16",
        dark=True,
    ),
}

# ══════════════════════════════════════════════════════════════════════════════════════
# Ink — concrete colours for the parts Rich draws
# ══════════════════════════════════════════════════════════════════════════════════════
# Textual resolves ``$primary`` inside its own CSS. Rich, which draws everything a
# widget returns from ``render()``, does not: it parses the style string itself and
# rejects the variable. So the palette exists twice — as a Textual theme for the
# chrome, and as this table of literal colours for the renderables. They are defined
# side by side to keep them honest.
@dataclass(frozen=True)
class Ink:
    """Literal colours for one posture, in the form Rich understands."""

    primary: str
    secondary: str
    accent: str
    warning: str
    error: str
    success: str
    text: str
    soft: str
    muted: str
    faint: str


INKS: dict[str, Ink] = {
    PALETTE_STANDARD: Ink(
        primary="#d97757", secondary="#c2b8ab", accent="#8fb8d8",
        warning="#e0a458", error="#e06c62", success="#7fb069",
        text="#e8e6e3", soft="#b4b0aa", muted="#8a857e", faint="#54504a",
    ),
    PALETTE_HOUSE_PARTY: Ink(
        primary="#fbbf24", secondary="#fb923c", accent="#f97316",
        warning="#fde047", error="#ef4444", success="#a3e635",
        text="#fef3c7", soft="#e3cfa0", muted="#b39a66", faint="#7a6535",
    ),
    PALETTE_VERONICA: Ink(
        primary="#94a3b8", secondary="#e2e8f0", accent="#cbd5e1",
        warning="#fbbf24", error="#f87171", success="#86efac",
        text="#e2e8f0", soft="#b6c0cc", muted="#8791a0", faint="#525c69",
    ),
    PALETTE_CLEAN_SLATE: Ink(
        primary="#4ade80", secondary="#22d3ee", accent="#34d399",
        warning="#facc15", error="#f87171", success="#4ade80",
        text="#dcfce7", soft="#a7d7b8", muted="#79a888", faint="#456b52",
    ),
}

#: The colours currently in force. Swapped whole when a protocol changes posture,
#: so a widget mid-render always sees one consistent scheme rather than a mix.
INK: Ink = INKS[PALETTE_STANDARD]


def ink(name: str) -> str:
    """Resolve a semantic colour name against the palette in force."""
    return getattr(INK, name, name)


#: How each activity state reads in the status chip, and which colour it takes.
STATE_STYLE: dict[str, tuple[str, str]] = {
    STATE_IDLE: ("READY", "primary"),
    STATE_LISTENING: ("LISTENING", "success"),
    STATE_THINKING: ("THINKING", "secondary"),
    STATE_WORKING: ("WORKING", "warning"),
    STATE_SPEAKING: ("SPEAKING", "accent"),
}

#: The transcript's whole punctuation. ``BULLET`` opens anything J.A.R.V.I.S. did
#: or said, ``BRANCH`` hangs a result underneath it, ``CARET`` marks what the
#: operator typed, and ``SPARK`` is the system speaking as itself.
BULLET = "\u23fa"      # ⏺
BRANCH = "\u23bf"      # ⎿
CARET = "\u203a"       # ›
SPARK = "\u273b"       # ✻

_BLOCKS = "▁▂▃▄▅▆▇█"
_BAR_FULL = "█"
_BAR_EMPTY = "░"
#: Segments in a sidebar gauge. Sized so label, bar, reading and history all fit
#: the panel without truncation.
_BAR_SEGMENTS = 8

#: Redraw budget for the pump. Thirty frames a second is past the point where a
#: terminal reads as instant, and it leaves the CPU to the model.
_FPS = 30
_FRAME = 1.0 / _FPS

#: Transcript entries kept mounted. Textual is fast, but not free per widget, and
#: nobody scrolls back four hundred exchanges.
_MAX_ENTRIES = 400

_SLASH_COMMANDS = [
    "/help", "/quit", "/exit", "/clear", "/protocol ", "/protocols", "/voice",
    "/mute", "/unmute", "/diag", "/tools", "/model", "/history", "/mics",
    "/theme ", "/title ", "/lang ", "/locales", "/toolchains", "/permissions",
    "/allow ", "/revoke ", "/apps", "/enroll", "/talk", "/quiet", "/voiceprint",
    "/metrics",
]


# ══════════════════════════════════════════════════════════════════════════════════════
# Events queued from other threads
# ══════════════════════════════════════════════════════════════════════════════════════
@dataclass(slots=True)
class _Event:
    """One thing to do on the UI thread, recorded by whichever thread wanted it."""

    kind: str
    args: tuple = ()


# ══════════════════════════════════════════════════════════════════════════════════════
# Sidebar instruments
# ══════════════════════════════════════════════════════════════════════════════════════
class Reactor(Static):
    """The arc reactor: an amplitude-driven waveform and the current posture.

    Redrawn by the pump rather than by a timer of its own, and only while there is
    something to animate — an idle HUD costs nothing.
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.state = STATE_IDLE
        self.amplitude = 0.0
        self._phase = 0.0
        self._width = max(12, int(settings.HUD_WAVEFORM_WIDTH) // 2)

    def advance(self) -> None:
        """One animation step. Called from the pump."""
        self._phase += 0.45 if self.state != STATE_IDLE else 0.08
        self.refresh()

    def render(self) -> RenderableType:
        import math

        label, colour = STATE_STYLE.get(self.state, ("READY", "primary"))
        colour = ink(colour)
        bars = Text()
        for index in range(self._width):
            if self.state == STATE_IDLE:
                # A slow, shallow breath: present, not demanding attention.
                level = 0.12 + 0.10 * (1 + math.sin(self._phase + index * 0.5)) / 2
            elif self.state == STATE_LISTENING:
                level = max(0.06, self.amplitude) * (
                    0.55 + 0.45 * abs(math.sin(self._phase + index * 0.7))
                )
            elif self.state == STATE_SPEAKING:
                level = 0.35 + 0.65 * abs(math.sin(self._phase * 1.3 + index * 0.55))
            else:  # thinking, working
                level = 0.25 + 0.55 * abs(math.sin(self._phase + index * 0.9))
            level = max(0.0, min(1.0, level))
            glyph = _BLOCKS[min(len(_BLOCKS) - 1, int(level * (len(_BLOCKS) - 1) + 0.5))]
            bars.append(glyph, style=colour)
        chip = Text(f"\n{label}", style=f"bold {colour}")
        return Group(bars, chip)


class Vitals(Static):
    """CPU, memory, disk and battery, with a short history behind each."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.telemetry: Any = None
        self._history: dict[str, deque[float]] = {
            key: deque(maxlen=14)
            for key in ("cpu", "ram", "disk", "gpu", "battery")
        }

    def update_telemetry(self, telemetry: Any) -> None:
        self.telemetry = telemetry
        for key, value in (
            ("cpu", getattr(telemetry, "cpu_percent", None)),
            ("ram", getattr(telemetry, "ram_percent", None)),
            ("disk", self._busiest_disk(telemetry)),
            ("gpu", self._gpu_load(telemetry)),
            ("battery", getattr(telemetry, "battery_percent", None)),
        ):
            if value is not None:
                self._history[key].append(float(value))
        self.refresh(layout=True)

    @staticmethod
    def _busiest_disk(telemetry: Any) -> float | None:
        disks = getattr(telemetry, "disks", None) or []
        percents = [getattr(disk, "percent", None) for disk in disks]
        usable = [p for p in percents if isinstance(p, (int, float))]
        return max(usable) if usable else None

    @staticmethod
    def _gpu_load(telemetry: Any) -> float | None:
        for gpu in getattr(telemetry, "gpus", None) or []:
            load = getattr(gpu, "utilization_percent", None)
            if isinstance(load, (int, float)):
                return float(load)
        return None

    def _sparkline(self, key: str) -> Text:
        series = self._history[key]
        if not series:
            return Text(" " * 4, style=INK.faint)
        recent = list(series)[-4:]
        text = Text()
        for value in recent:
            index = min(len(_BLOCKS) - 1, int(value / 100 * (len(_BLOCKS) - 1) + 0.5))
            text.append(_BLOCKS[index], style=self._tone(value))
        return text

    @staticmethod
    def _tone(percent: float, inverted: bool = False) -> str:
        """Colour a reading. ``inverted`` for battery, where empty is the bad end."""
        if inverted:
            percent = 100.0 - percent
        if percent >= 90:
            return INK.error
        if percent >= 75:
            return INK.warning
        return INK.success

    def _row(self, table: Table, label: str, percent: float | None, key: str,
             suffix: str = "", inverted: bool = False) -> None:
        if percent is None:
            table.add_row(label, Text("—", style=INK.faint), Text(""), Text(""))
            return
        tone = self._tone(percent, inverted)
        filled = int(round(max(0.0, min(100.0, percent)) / 100 * _BAR_SEGMENTS))
        bar = Text(_BAR_FULL * filled, style=tone)
        bar.append(_BAR_EMPTY * (_BAR_SEGMENTS - filled), style=INK.faint)
        value = Text(f"{percent:3.0f}%{suffix}", style=tone)
        table.add_row(label, bar, value, self._sparkline(key))

    def render(self) -> RenderableType:
        telemetry = self.telemetry
        table = Table.grid(padding=(0, 1))
        table.add_column(style=INK.muted, justify="left", width=3)
        table.add_column(width=_BAR_SEGMENTS)
        table.add_column(justify="right", width=7)
        table.add_column(width=4)

        if telemetry is None:
            return Text("awaiting telemetry…", style=INK.muted)

        self._row(table, "CPU", getattr(telemetry, "cpu_percent", None), "cpu")
        self._row(table, "RAM", getattr(telemetry, "ram_percent", None), "ram")
        self._row(table, "DSK", self._busiest_disk(telemetry), "disk")
        gpu = self._gpu_load(telemetry)
        if gpu is not None:
            self._row(table, "GPU", gpu, "gpu")

        battery = getattr(telemetry, "battery_percent", None)
        if battery is not None:
            plugged = getattr(telemetry, "battery_plugged", None)
            self._row(table, "BAT", float(battery), "battery",
                      suffix=" ⚡" if plugged else "  ", inverted=True)

        footer = Text()
        ram_used = getattr(telemetry, "ram_used_gb", None)
        ram_total = getattr(telemetry, "ram_total_gb", None)
        if ram_used is not None and ram_total:
            footer.append(f"{ram_used:.1f}/{ram_total:.0f} GB", style=INK.muted)
        uptime = getattr(telemetry, "uptime_seconds", None)
        if uptime:
            hours = int(uptime // 3600)
            footer.append(f"  ·  up {hours}h", style=INK.muted)
        return Group(table, footer) if footer.plain else table


class TurnMeter(Static):
    """What the last turn cost, and what the session has cost so far.

    This panel is the reason the engine measures anything: "it feels slow" is not
    actionable, "first token in 2.4 seconds and the model is cold" is.
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.metrics: Any = None
        self.turns = 0
        self.total_seconds = 0.0
        self.saved_seconds = 0.0

    def update_metrics(self, metrics: Any) -> None:
        self.metrics = metrics
        self.turns += 1
        self.total_seconds += float(getattr(metrics, "total", 0.0) or 0.0)
        self.saved_seconds += float(getattr(metrics, "tool_seconds_saved", 0.0) or 0.0)
        self.refresh(layout=True)

    def render(self) -> RenderableType:
        table = Table.grid(padding=(0, 1))
        table.add_column(style=INK.muted, width=12)
        table.add_column(justify="right")

        metrics = self.metrics
        if metrics is None:
            table.add_row("first token", Text("—", style=INK.faint))
            table.add_row("throughput", Text("—", style=INK.faint))
            table.add_row("turn", Text("—", style=INK.faint))
            return table

        ttft = float(getattr(metrics, "ttft", 0.0) or 0.0)
        rate = float(getattr(metrics, "tokens_per_second", 0.0) or 0.0)
        total = float(getattr(metrics, "total", 0.0) or 0.0)
        tools = int(getattr(metrics, "tool_calls", 0) or 0)
        saved = float(getattr(metrics, "tool_seconds_saved", 0.0) or 0.0)

        table.add_row(
            "first token",
            Text(f"{ttft * 1000:.0f} ms" if ttft else "—", style=self._tone_ttft(ttft)),
        )
        table.add_row("throughput", Text(f"{rate:.0f} tok/s" if rate else "—",
                                         style=INK.secondary))
        table.add_row("turn", Text(f"{total:.2f} s", style=INK.primary))
        if tools:
            table.add_row("instruments", Text(str(tools), style=INK.accent))
        if saved > 0.05:
            table.add_row("parallel", Text(f"−{saved:.1f} s", style=INK.success))

        if self.turns:
            average = self.total_seconds / self.turns
            summary = Text(f"{self.turns}× · {average:.1f}s avg", style=INK.muted)
            if self.saved_seconds > 0.2:
                summary.append(f" · −{self.saved_seconds:.1f}s", style=INK.success)
            return Group(table, Text(""), summary)
        return table

    @staticmethod
    def _tone_ttft(ttft: float) -> str:
        if not ttft:
            return INK.faint
        if ttft < 0.6:
            return INK.success
        if ttft < 2.0:
            return INK.warning
        return INK.error


class Systems(Static):
    """Model, voice, protocol and locale: the four things worth knowing at a glance."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.model_status = f"{settings.MODEL_NAME}"
        self.voice_status = "voice: off"
        self.protocol: str | None = None
        self.tools: Sequence[str] = ()

    def render(self) -> RenderableType:
        table = Table.grid(padding=(0, 1))
        table.add_column(style=INK.muted, width=8)
        table.add_column(overflow="ellipsis")
        table.add_row("model", Text(self.model_status, style=INK.primary))
        table.add_row("voice", Text(self.voice_status, style=INK.soft))
        if self.protocol:
            table.add_row("protocol", Text(self.protocol.upper(), style=f"bold {INK.secondary}"))
        if self.tools:
            table.add_row("tools", Text(str(len(self.tools)), style=INK.accent))
        return table


# ══════════════════════════════════════════════════════════════════════════════════════
# Live keywords
# ══════════════════════════════════════════════════════════════════════════════════════
# A handful of bare words act the moment they are sent rather than going to the
# model: `talk` opens the speaker, `quiet` shuts it. Nothing in the interface
# said so, which made them folklore. Colouring them as they are typed turns the
# feature into something you discover by using it -- and, just as usefully, tells
# you *before* you press enter that this line is a switch and not a question.
#
# Longest phrases first, so "shut up" wins over a "shut" that is not there, and
# whole words only, so "talkative" and "basement" stay ordinary prose.
_KEYWORD_KIND: dict[str, str] = {
    **{word: "talk" for word in TALK_WORDS},
    **{word: "quiet" for word in QUIET_WORDS},
}

_KEYWORD_RE = re.compile(
    r"(?<![\w'-])("
    + "|".join(
        re.escape(word)
        for word in sorted(_KEYWORD_KIND, key=len, reverse=True)
    )
    + r")(?![\w'-])",
    re.IGNORECASE,
)


def keyword_style(kind: str) -> str:
    """One colour for "speak", another for "stop speaking". Never the same one."""
    return f"bold {INK.success}" if kind == "talk" else f"bold {INK.accent}"


class KeywordHighlighter(Highlighter):
    """Colours the words that do something, wherever they appear.

    Runs on every keystroke in the composer, so it is one compiled regex over a
    line of text and nothing else.
    """

    def highlight(self, text: Text) -> None:
        plain = text.plain
        if not plain:
            return
        for match in _KEYWORD_RE.finditer(plain):
            kind = _KEYWORD_KIND.get(match.group(1).lower())
            if kind is None:
                continue
            text.stylize(keyword_style(kind), match.start(1), match.end(1))


#: One instance is enough; it holds no state.
KEYWORDS = KeywordHighlighter()


# ══════════════════════════════════════════════════════════════════════════════════════
# The greeting, and the line that replaces it while he is working
# ══════════════════════════════════════════════════════════════════════════════════════
#: Openers for a session with nothing yet to say about the machine. Chosen by the
#: hour rather than at random, so the greeting reads as observation rather than
#: decoration.
_OPENERS: dict[str, str] = {
    "small_hours": "You are up late, {address}.",
    "morning": "Good morning, {address}.",
    "afternoon": "Good afternoon, {address}.",
    "evening": "Good evening, {address}.",
}

#: Said underneath the greeting when the machine has nothing worth reporting.
_PLEASANTRIES: tuple[str, ...] = (
    "Everything is nominal. What shall we build?",
    "All systems are yours. Where would you like to start?",
    "The workshop is quiet. Say the word.",
    "Standing by, and rather well rested.",
    "Nothing is on fire. A promising beginning.",
)

#: What he calls the wait. Claude Code rotates a verb here and it turns a hang
#: into a status; this set is his rather than theirs.
_WORKING_VERBS: tuple[str, ...] = (
    "Cogitating", "Calibrating", "Deliberating", "Triangulating", "Synthesising",
    "Reasoning", "Computing", "Considering", "Correlating", "Compiling thought",
)


def _time_of_day(now: datetime | None = None) -> str:
    hour = (now or datetime.now()).hour
    if hour < 5:
        return "small_hours"
    if hour < 12:
        return "morning"
    if hour < 18:
        return "afternoon"
    return "evening"


def _address() -> str:
    """How to open. A name if he has been given one, the honorific otherwise."""
    return (settings.USER_NAME or settings.USER_TITLE).strip() or "Sir"


class Greeting(Static):
    """The centred opening, in the manner of the Claude app.

    It reads the machine before it speaks. A greeting that says "everything is
    nominal" while the battery is at eleven percent is worse than no greeting at
    all, so the second line is drawn from whatever is actually true — a dying
    battery, a full disk, an unreachable daemon — and falls back to pleasantry
    only when there is genuinely nothing to report.
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__("", **kwargs)
        self.telemetry: Any = None
        self.model_ready: bool | None = None
        self.warm = False
        self._pleasantry = random.choice(_PLEASANTRIES)

    def observe(
        self,
        telemetry: Any = None,
        model_ready: bool | None = None,
        warm: bool | None = None,
    ) -> None:
        """Fold in whatever the boot sequence has learned so far."""
        if telemetry is not None:
            self.telemetry = telemetry
        if model_ready is not None:
            self.model_ready = model_ready
        if warm is not None:
            self.warm = warm
        self.refresh(layout=True)

    # -- the second line ---------------------------------------------------------------
    def _observation(self) -> tuple[str, str] | None:
        """The most worth saying, or None when nothing is."""
        if self.model_ready is False:
            return (
                f"I cannot reach the reasoning core. `ollama serve` will fix it.",
                INK.warning,
            )

        telemetry = self.telemetry
        if telemetry is not None:
            battery = getattr(telemetry, "battery_percent", None)
            plugged = getattr(telemetry, "battery_plugged", None)
            if isinstance(battery, (int, float)) and battery <= 25 and plugged is False:
                return (f"Battery is at {battery:.0f}%. Worth finding a cable.", INK.warning)

            disks = getattr(telemetry, "disks", None) or []
            worst = max(
                (getattr(d, "percent", 0) or 0 for d in disks), default=0
            )
            if worst >= 90:
                return (
                    f"A filesystem is at {worst:.0f}%. Say the word and I will clear the caches.",
                    INK.warning,
                )

            ram = getattr(telemetry, "ram_percent", None)
            if isinstance(ram, (int, float)) and ram >= 88:
                return (f"Memory is at {ram:.0f}%. Something is being greedy.", INK.warning)

            cpu = getattr(telemetry, "cpu_percent", None)
            if isinstance(cpu, (int, float)) and cpu >= 85:
                return (f"Something is working the processor hard — {cpu:.0f}%.", INK.muted)

        if self.warm:
            return (f"{settings.MODEL_NAME} is warm and standing by.", INK.muted)
        return None

    def render(self) -> RenderableType:
        opener = _OPENERS[_time_of_day()].format(address=_address())
        observation = self._observation()
        line, tone = observation if observation else (self._pleasantry, INK.muted)

        body = Text(justify="center")
        body.append(f"{SPARK}\n\n", style=INK.primary)
        body.append(f"{opener}\n", style=f"bold {INK.text}")
        body.append(f"{line}\n\n", style=tone)
        body.append("/help for commands", style=INK.faint)
        body.append("   ·   ", style=INK.faint)
        body.append("? for shortcuts", style=INK.faint)
        return body


class StatusLine(Static):
    """What he is doing, while he is doing it.

    A terminal that shows nothing for eight seconds looks broken. This shows the
    verb, the elapsed time, how much has come back so far, and how to stop it.
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__("", **kwargs)
        self.state = STATE_IDLE
        self.detail = ""
        self.started: float | None = None
        self.tokens = 0
        self.verb = _WORKING_VERBS[0]
        self._tick = 0

    def begin(self) -> None:
        """A turn has started: new verb, new clock."""
        self.started = time.monotonic()
        self.tokens = 0
        self.verb = random.choice(_WORKING_VERBS)

    def end(self) -> None:
        self.started = None
        self.tokens = 0
        self.update("")

    def advance(self) -> None:
        self._tick += 1
        if self.started is not None:
            self.update(self._build())

    def _build(self) -> RenderableType:
        elapsed = time.monotonic() - (self.started or time.monotonic())
        spark = SPARK if self._tick % 2 else "\u2739"
        label = self.detail or self.verb

        line = Text()
        line.append(f"{spark} ", style=INK.primary)
        line.append(f"{label}… ", style=INK.soft)
        line.append("(", style=INK.faint)
        line.append(f"{elapsed:.0f}s", style=INK.muted)
        if self.tokens:
            line.append(" · ", style=INK.faint)
            line.append(f"↓ {_compact_count(self.tokens)} tokens", style=INK.muted)
        line.append(" · ", style=INK.faint)
        line.append("esc to interrupt", style=INK.faint)
        line.append(")", style=INK.faint)
        return line


# ══════════════════════════════════════════════════════════════════════════════════════
# Transcript entries
# ══════════════════════════════════════════════════════════════════════════════════════
class Entry(Static):
    """Base class for anything that lands in the transcript.

    Keeps a handle on whatever it was last drawn from. Textual name-mangles its
    own copy, and a transcript you cannot read back is a transcript you cannot
    test, copy out, or export.
    """

    def __init__(self, renderable: RenderableType = "", **kwargs: Any) -> None:
        super().__init__(renderable, **kwargs)
        self.source: RenderableType = renderable

    def update(self, content: RenderableType = "", **kwargs: Any) -> None:
        self.source = content
        super().update(content, **kwargs)


class UserEntry(Entry):
    """What the operator said, echoed back the way they typed it."""

    def __init__(self, text: str, spoken: bool = False) -> None:
        body = Text()
        body.append(f"{CARET} ", style=INK.muted)
        if spoken:
            body.append("🎙 ", style=INK.success)
        # Highlighted here too: seeing the word still lit in the transcript is
        # what explains why he stopped talking three lines later.
        said = Text(text, style=INK.soft)
        KEYWORDS.highlight(said)
        body.append_text(said)
        super().__init__(body, classes="entry user")


class AgentEntry(Entry):
    """A reply from J.A.R.V.I.S., re-rendered in place as it streams.

    Held as Markdown source rather than a widget tree: growing text means the
    whole document is re-rendered every frame it changes, and Rich parses a few
    kilobytes of Markdown far more cheaply than Textual can rebuild and re-layout
    a document's worth of widgets.
    """

    def __init__(self, text: str = "", streaming: bool = False) -> None:
        super().__init__("", classes="entry agent")
        self._text = text
        self._streaming = streaming
        self.rerender()

    @property
    def text(self) -> str:
        return self._text

    def set_text(self, text: str, streaming: bool = False) -> None:
        self._text = text
        self._streaming = streaming
        self.rerender()

    def rerender(self) -> None:
        """Bullet, then the reply, in the transcript's own punctuation.

        The bullet sits in its own column and the prose is indented to clear it,
        which is what makes a long answer scannable next to the tool calls that
        produced it.
        """
        body = self._text
        if not body.strip():
            bullet = Text(f"{BULLET} ", style=INK.primary)
            bullet.append("▍", style=INK.primary)
            return self.update(bullet)
        try:
            rendered: RenderableType = RichMarkdown(
                body + ("▍" if self._streaming else ""),
                code_theme="monokai",
                inline_code_theme="monokai",
            )
        except Exception:
            rendered = Text(body)
        # A two-column grid rather than a stack: the bullet sits on the reply's
        # first line and every line after it is indented clear of the gutter.
        layout = Table.grid(padding=(0, 0))
        layout.add_column(width=2, vertical="top")
        layout.add_column(ratio=1, overflow="fold")
        layout.add_row(Text(BULLET, style=INK.primary), rendered)
        self.update(layout)


class SystemEntry(Entry):
    """A line from the application rather than from either party."""

    _MARKS = {
        "info": (SPARK, "muted"),
        "success": (SPARK, "success"),
        "warn": (SPARK, "warning"),
        "warning": (SPARK, "warning"),
        "error": (SPARK, "error"),
    }

    def __init__(self, text: str, level: str = "info") -> None:
        mark, colour = self._MARKS.get(str(level).lower(), (SPARK, "muted"))
        colour = ink(colour)
        body = Text()
        body.append(f"{mark} ", style=colour)
        body.append(
            text,
            style=colour if level in {"error", "warn", "warning"} else INK.muted,
        )
        super().__init__(body, classes="entry system")


class InterimEntry(Entry):
    """Narration that arrived alongside a tool call — a remark, not an answer."""

    def __init__(self, text: str) -> None:
        body = Text()
        body.append(f"{BULLET} ", style=INK.faint)
        body.append(text, style=f"italic {INK.muted}")
        super().__init__(body, classes="entry interim")


class ThoughtEntry(Entry):
    """The model's private reasoning, when the thinking channel is switched on."""

    def __init__(self, text: str) -> None:
        body = Text()
        body.append(f"{BRANCH}  ", style=INK.faint)
        body.append(text, style=f"italic {INK.faint}")
        super().__init__(body, classes="entry thought")


class ToolEntry(Entry):
    """One instrument, from the moment it starts to the moment it reports.

    Created the instant the call is issued and updated in place when the result
    lands, so a long build or a slow search shows as running rather than as a gap.
    """

    SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def __init__(self, name: str, arguments: Any) -> None:
        super().__init__("", classes="entry tool running")
        self.tool_name = name
        self.arguments = _compact_args(arguments)
        self.result: str = ""
        self.ok: bool | None = None
        self.started = time.monotonic()
        self.duration = 0.0
        self._tick = 0
        self.rerender()

    def advance(self) -> None:
        """Spin, while this instrument is still running."""
        if self.ok is None:
            self._tick += 1
            self.rerender()

    def complete(self, result: str, ok: bool) -> None:
        self.result = result
        self.ok = ok
        self.duration = time.monotonic() - self.started
        self.remove_class("running")
        self.add_class("ok" if ok else "failed")
        self.rerender()

    def render(self) -> RenderableType:
        return self._build()

    def rerender(self) -> None:
        self.update(self._build())

    def _build(self) -> RenderableType:
        """``⏺ tool(args)`` with its reading hanging underneath on a ``⎿``."""
        if self.ok is None:
            mark = self.SPINNER[self._tick % len(self.SPINNER)]
            colour = INK.accent
        else:
            mark = BULLET
            colour = INK.success if self.ok else INK.error

        head = Text()
        head.append(f"{mark} ", style=colour)
        head.append(self.tool_name, style=f"bold {INK.text}")
        head.append(f"({self.arguments})", style=INK.muted)
        if self.ok is None:
            head.append(f"  {time.monotonic() - self.started:.0f}s", style=INK.faint)
        else:
            head.append(f"  {self.duration:.2f}s", style=INK.faint)

        if not self.result:
            return head
        reading = Text()
        reading.append(f"  {BRANCH}  ", style=INK.faint)
        reading.append(
            _one_line(self.result, 400),
            style=INK.error if self.ok is False else INK.muted,
        )
        return Group(head, reading)


class AlertEntry(Entry):
    """A proactive warning from the ambient monitor."""

    def __init__(self, alert: Any) -> None:
        severity = str(getattr(alert, "severity", "warning")).lower()
        colour = INK.error if severity == "critical" else INK.warning
        title = str(getattr(alert, "title", "Alert"))
        message = str(getattr(alert, "message", alert))
        suggestion = str(getattr(alert, "suggestion", "") or "")

        body = Text()
        body.append(f"{BULLET} ", style=colour)
        body.append(f"{title}  ", style=f"bold {colour}")
        body.append(message, style=INK.text)
        if suggestion:
            body.append(f"\n  {BRANCH}  ", style=INK.faint)
            body.append(suggestion, style=INK.muted)
        super().__init__(body, classes=f"entry alert {severity}")


class TableEntry(Entry):
    """A titled table — diagnostics, tool listings, protocol reports."""

    def __init__(self, title: str, columns: Sequence[str], rows: Sequence[Sequence[str]]) -> None:
        table = Table(
            title=title or None,
            title_style=f"bold {INK.secondary}",
            border_style=INK.faint,
            header_style=f"bold {INK.primary}",
            expand=False,
            padding=(0, 1),
        )
        for column in columns:
            table.add_column(str(column), overflow="fold")
        for row in rows:
            table.add_row(*[Text(str(cell)) for cell in row])
        super().__init__(Group(Text(f"{BULLET} ", style=INK.primary), table),
                         classes="entry table")


class CodeEntry(Entry):
    """Syntax-highlighted source in whatever language it happens to be."""

    def __init__(self, code: str, language: str = "python", title: str = "") -> None:
        try:
            body: RenderableType = Syntax(
                str(code), language or "text", theme="monokai",
                line_numbers=False, word_wrap=True,
            )
        except Exception:
            body = Text(str(code))
        super().__init__(body, classes="entry code")
        self.border_title = title or language or "source"


# ══════════════════════════════════════════════════════════════════════════════════════
# Modal dialogs
# ══════════════════════════════════════════════════════════════════════════════════════
class ConfirmScreen(ModalScreen[bool]):
    """A yes/no gate for anything destructive."""

    BINDINGS = [
        Binding("y", "answer(True)", "Yes"),
        Binding("n,escape", "answer(False)", "No"),
    ]

    def __init__(self, question: str) -> None:
        super().__init__()
        self.question = question

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog", classes="modal confirm"):
            yield Label(self.question, classes="modal-question")
            with Horizontal(classes="modal-actions"):
                yield Button("Yes  (y)", variant="primary", id="yes")
                yield Button("No  (n)", variant="default", id="no")

    def on_mount(self) -> None:
        self.query_one("#dialog").border_title = "Confirm"

    @on(Button.Pressed)
    def _pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")

    def action_answer(self, value: bool) -> None:
        self.dismiss(value)


class PermissionScreen(ModalScreen[str]):
    """Consent for anything that reaches beyond the workspace.

    Deliberately verbose about *what* is being asked: a prompt that says only
    "allow?" trains people to press y without reading, which defeats the point of
    having a prompt at all.
    """

    BINDINGS = [
        Binding("y", "answer('y')", "Allow once"),
        Binding("a", "answer('a')", "Always"),
        Binding("n,escape", "answer('n')", "Deny"),
        Binding("exclamation_mark", "answer('!')", "Deny all"),
    ]

    def __init__(self, request: Any) -> None:
        super().__init__()
        self.request = request

    def compose(self) -> ComposeResult:
        request = self.request
        reversible = bool(getattr(request, "reversible", True))
        try:
            question = str(getattr(request, "question")())
        except Exception:
            question = "May I proceed?"

        detail = Table.grid(padding=(0, 2))
        detail.add_column(style=INK.muted, justify="right", width=8)
        detail.add_column(overflow="fold")
        detail.add_row("Scope", str(getattr(request, "label", "")))
        detail.add_row("Action", str(getattr(request, "action", "")))
        detail.add_row("Target", str(getattr(request, "target", "")))
        extra = str(getattr(request, "detail", "") or "")
        if extra:
            detail.add_row("Context", extra)
        if not reversible:
            detail.add_row("Note", Text("This cannot be undone.", style=INK.error))

        with Vertical(id="dialog", classes="modal permission" + ("" if reversible else " irreversible")):
            yield Label(question, classes="modal-question")
            yield Static(detail, classes="modal-detail")
            with Horizontal(classes="modal-actions"):
                yield Button("Allow once  (y)", variant="primary", id="y")
                yield Button("Always  (a)", variant="success", id="a")
                yield Button("Deny  (n)", variant="default", id="n")
                yield Button("Deny all  (!)", variant="error", id="bang")

    def on_mount(self) -> None:
        self.query_one("#dialog").border_title = "Permission required"

    @on(Button.Pressed)
    def _pressed(self, event: Button.Pressed) -> None:
        self.dismiss("!" if event.button.id == "bang" else str(event.button.id))

    def action_answer(self, value: str) -> None:
        self.dismiss(value)


class ChooseScreen(ModalScreen[str]):
    """A small menu that also accepts free text, for onboarding."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, question: str, options: list[tuple[str, str]], hint: str = "") -> None:
        super().__init__()
        self.question = question
        self.options = options
        self.hint = hint

    def compose(self) -> ComposeResult:
        table = Table.grid(padding=(0, 2))
        table.add_column(style=f"bold {INK.secondary}", justify="right", width=4)
        table.add_column()
        for key, label in self.options:
            table.add_row(str(key), str(label))

        with Vertical(id="dialog", classes="modal choose"):
            yield Label(self.question, classes="modal-question")
            yield Static(table, classes="modal-detail")
            if self.hint:
                yield Label(self.hint, classes="modal-hint")
            yield Input(placeholder="type a number, or your own answer", id="choice")

    def on_mount(self) -> None:
        self.query_one("#dialog").border_title = settings.AGENT_NAME
        self.query_one("#choice", Input).focus()

    @on(Input.Submitted)
    def _submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip())

    def action_cancel(self) -> None:
        self.dismiss("")


class HelpScreen(ModalScreen[None]):
    """Keys and commands, on demand."""

    BINDINGS = [Binding("escape,q,question_mark,f1", "dismiss", "Close")]

    def compose(self) -> ComposeResult:
        keys = Table.grid(padding=(0, 3))
        keys.add_column(style=f"bold {INK.secondary}", justify="right", width=12)
        keys.add_column()
        for key, meaning in (
            ("enter", "send"),
            ("esc", "interrupt the turn in progress"),
            ("ctrl+c", "interrupt; at an idle prompt, leave"),
            ("ctrl+d", "leave"),
            ("ctrl+l", "clear the transcript"),
            ("ctrl+s", "speech on / off"),
            ("ctrl+b", "the instrument panel: vitals, latency, systems"),
            ("ctrl+r", "show or hide reasoning"),
            ("ctrl+p", "command palette"),
            ("↑ / ↓", "walk back through what you have asked"),
            ("f1 / ?", "this"),
        ):
            keys.add_row(key, meaning)

        words = Text()
        words.append("talk", style=keyword_style("talk"))
        words.append(" and ", style=INK.muted)
        words.append("quiet", style=keyword_style("quiet"))
        words.append(
            " act the moment you send them, with no slash. Every alias is lit as\n"
            "you type it — shut up, chup kar, bolo — so you can see it before you\n"
            "press enter.",
            style=INK.muted,
        )

        commands = Table.grid(padding=(0, 3))
        commands.add_column(style=f"bold {INK.accent}", justify="right", width=12)
        commands.add_column()
        for command, meaning in (
            ("/diag", "full system report"),
            ("/tools", "the instruments and what they do"),
            ("/metrics", "latency of the last turn"),
            ("/protocol", "run a Stark protocol"),
            ("/theme", "standard · house_party · veronica · clean_slate"),
            ("/lang", "answer in another language"),
            ("/talk, /quiet", "speech on and off"),
            ("/help", "every command"),
        ):
            commands.add_row(command, meaning)

        with Vertical(id="dialog", classes="modal help"):
            yield Static(Text("Keys", style=f"bold {INK.primary}"))
            yield Static(keys, classes="modal-detail")
            yield Static(Text("Words", style=f"bold {INK.primary}"))
            yield Static(words, classes="modal-detail")
            yield Static(Text("Commands", style=f"bold {INK.primary}"))
            yield Static(commands, classes="modal-detail")
            yield Label("esc to close", classes="modal-hint")

    def on_mount(self) -> None:
        self.query_one("#dialog").border_title = f"{settings.AGENT_NAME} · help"

    def action_dismiss(self) -> None:  # type: ignore[override]
        self.dismiss(None)


# ══════════════════════════════════════════════════════════════════════════════════════
# The application
# ══════════════════════════════════════════════════════════════════════════════════════
class JarvisTUI(App):
    """The full-screen HUD.

    Construct it with the callbacks it needs, then call :meth:`run`. Everything
    else — the agent, the monitor, the voice system — talks to it through the
    :class:`jarvis.ui.StarkHUD` interface from whatever thread it happens to be
    on, and never blocks doing so.
    """

    CSS = """
    Screen { background: $background; }

    /* The name, and the model behind it, on one line at the top. */
    #masthead {
        height: 2;
        padding: 0 2;
        color: $primary;
        border-bottom: hkey $primary 25%;
    }

    #shell { height: 1fr; }

    /* Nothing said yet: the greeting has the window to itself. */
    #greeting {
        width: 1fr;
        height: 1fr;
        content-align: center middle;
        text-align: center;
    }
    #greeting.hidden { display: none; }

    #transcript {
        width: 1fr;
        height: 1fr;
        padding: 1 2 0 2;
        scrollbar-size-vertical: 1;
    }
    #transcript.hidden { display: none; }

    /* Kept, but out of the way: Claude Code has no sidebar, so neither has this
       until ctrl+b asks for one. */
    #sidebar {
        width: 34;
        background: $surface;
        border-left: vkey $primary 30%;
        padding: 0 1;
        display: none;
    }
    #sidebar.shown { display: block; }
    #sidebar > Static {
        border: round $primary 30%;
        border-title-color: $secondary;
        border-title-style: bold;
        padding: 0 1;
        margin-bottom: 1;
        height: auto;
    }
    #reactor { content-align: center middle; }

    .entry { height: auto; margin: 0 0 1 0; }
    .entry.agent { padding: 0 0 0 0; }
    .entry.user { color: $foreground 70%; }
    .entry.interim { padding: 0 0 0 2; }
    .entry.thought { padding: 0 0 0 2; }
    .entry.tool { padding: 0 0 0 0; }
    .entry.alert { padding: 0 0 0 0; }
    .entry.table { padding: 0 0 0 0; }
    .entry.code {
        border: round $foreground 25%;
        border-title-color: $accent;
        padding: 0 1;
        margin: 0 0 1 2;
    }

    /* The composer: a rounded box the width of the terminal, exactly where the
       eye already is. */
    #footer { height: auto; }
    #status {
        height: auto;
        min-height: 0;
        padding: 0 3;
    }
    #composer {
        height: 3;
        border: round $primary 45%;
        margin: 0 1;
        background: $surface;
    }
    #composer.busy { border: round $primary 80%; }
    #prompt-caret {
        width: 3;
        height: 1;
        padding: 0 0 0 1;
        color: $primary;
        text-style: bold;
    }
    #prompt {
        height: 1;
        border: none;
        background: $surface;
        padding: 0 1 0 0;
    }
    #prompt:focus { border: none; background: $surface; }
    #hints {
        height: 1;
        padding: 0 3;
        color: $foreground 40%;
    }

    .modal {
        width: 78;
        max-width: 90%;
        height: auto;
        max-height: 80%;
        border: round $primary;
        border-title-color: $primary;
        border-title-style: bold;
        background: $surface;
        padding: 1 2;
    }
    .modal.permission { border: round $warning; border-title-color: $warning; }
    .modal.permission.irreversible { border: round $error; border-title-color: $error; }
    .modal-question { width: 100%; text-style: bold; padding-bottom: 1; }
    .modal-detail { width: 100%; height: auto; padding-bottom: 1; }
    .modal-hint { color: $foreground 50%; padding-top: 1; }
    .modal-actions { height: auto; align-horizontal: right; }
    .modal-actions Button { margin-left: 1; }
    ModalScreen { align: center middle; background: $background 70%; }
    """

    BINDINGS = [
        # Deliberately not a priority binding: a modal's own escape has to win,
        # or a permission dialog cannot be dismissed without cancelling the turn.
        Binding("escape", "interrupt", "Interrupt", show=True),
        Binding("ctrl+c", "interrupt", "Interrupt", priority=True, show=False),
        Binding("ctrl+d", "leave", "Quit", priority=True),
        Binding("ctrl+l", "clear", "Clear"),
        Binding("ctrl+s", "toggle_speech", "Speech"),
        Binding("ctrl+b", "toggle_sidebar", "Instruments"),
        Binding("ctrl+r", "toggle_reasoning", "Reasoning"),
        Binding("f1", "help", "Help"),
        Binding("question_mark", "help", "Help", show=False),
        Binding("up", "history_back", "History", show=False),
        Binding("down", "history_forward", "History", show=False),
    ]

    state: reactive[str] = reactive(STATE_IDLE)

    def __init__(
        self,
        on_submit: Callable[[str], None] | None = None,
        on_ready: Callable[[], None] | None = None,
        on_quit: Callable[[], None] | None = None,
        on_interrupt: Callable[[], bool] | None = None,
        on_toggle_speech: Callable[[], None] | None = None,
        tools: Sequence[str] = (),
    ) -> None:
        super().__init__()
        self._on_submit = on_submit
        self._on_ready = on_ready
        self._on_quit = on_quit
        self._on_interrupt = on_interrupt
        self._on_toggle_speech = on_toggle_speech
        self._tool_names = list(tools)

        # Cross-thread plumbing. Producers append; the pump on the UI thread drains.
        self._events: queue.SimpleQueue[_Event] = queue.SimpleQueue()
        self._stream_lock = threading.Lock()
        self._stream_parts: list[str] = []
        self._stream_dirty = False
        self._streaming = False
        self._live_entry: AgentEntry | None = None
        self._running_tools: dict[str, ToolEntry] = {}
        self._pending_tools: deque[ToolEntry] = deque()

        self._amplitude = 0.0
        self._palette_key = PALETTE_STANDARD
        self._history: list[str] = []
        self._history_index = 0
        self._draft = ""
        self._show_reasoning = bool(settings.MODEL_THINKING)
        self._mounted = threading.Event()
        # Not `_closing`: Textual's own message pump owns that name, and setting
        # it stops the pump dead.
        self._leaving = False
        self._frame = 0
        self._agent_busy: Callable[[], bool] | None = None
        # True once the operator has expressed an opinion about the sidebar, after
        # which resizing the window stops having one.
        self._sidebar_pinned = False
        self._protocol: str | None = None
        self._model_status = settings.MODEL_NAME
        self._voice_status = ""
        self._stream_tokens = 0
        self._turn_active = False

    # -- composition -----------------------------------------------------------------
    def compose(self) -> ComposeResult:
        yield Static(self._masthead_text(), id="masthead")
        with Horizontal(id="shell"):
            with Vertical(id="body"):
                yield Greeting(id="greeting")
                yield VerticalScroll(id="transcript", classes="hidden")
            with Vertical(id="sidebar"):
                yield Reactor(id="reactor")
                yield Vitals(id="vitals")
                yield TurnMeter(id="meter")
                yield Systems(id="systems")
        with Vertical(id="footer"):
            yield StatusLine(id="status")
            with Horizontal(id="composer"):
                yield Label(CARET, id="prompt-caret")
                yield Input(
                    placeholder="ask me anything",
                    id="prompt",
                    highlighter=KEYWORDS,
                    suggester=SuggestFromList(_SLASH_COMMANDS, case_sensitive=False),
                )
            yield Static(self._hint_text(), id="hints")

    def on_mount(self) -> None:
        for theme in THEMES.values():
            self.register_theme(theme)
        self.theme = THEMES[PALETTE_STANDARD].name

        self.query_one("#reactor", Reactor).border_title = "reactor"
        self.query_one("#vitals", Vitals).border_title = "vitals"
        self.query_one("#meter", TurnMeter).border_title = "last turn"
        systems = self.query_one("#systems", Systems)
        systems.border_title = "systems"
        systems.tools = self._tool_names

        self.query_one("#prompt", Input).focus()

        # The pump. One timer, one drain, every frame — rather than one callback
        # per token arriving from the model's thread.
        self.set_interval(_FRAME, self._pump)
        self._mounted.set()
        if self._on_ready is not None:
            self._boot()

    @work(thread=True, name="boot", group="lifecycle")
    def _boot(self) -> None:
        """Run the caller's boot sequence off the UI thread."""
        try:
            if self._on_ready is not None:
                self._on_ready()
        except Exception:
            logger.exception("boot sequence failed")
            self.log_system("The boot sequence faulted; the log has the details.", "error")

    # -- the pump --------------------------------------------------------------------
    def _pump(self) -> None:
        """Drain everything the other threads queued, once per frame.

        Ordering is preserved: discrete events go through the queue in the order
        they were produced, and streamed text is flushed before each of them, so a
        tool card never overtakes the sentence that introduced it.
        """
        self._frame += 1
        drained = 0
        try:
            while drained < 64:
                event = self._events.get_nowait()
                self._flush_stream()
                self._apply(event)
                drained += 1
        except queue.Empty:
            pass
        except Exception:
            logger.exception("HUD event pump faulted")

        self._flush_stream()

        # Animate at a third of the frame rate: fast enough to read as motion,
        # cheap enough to be free.
        if self._frame % 3 == 0:
            try:
                status = self.query_one("#status", StatusLine)
                status.tokens = self._stream_tokens
                status.advance()
            except Exception:
                pass
            try:
                self.query_one("#reactor", Reactor).advance()
            except Exception:
                pass
            for entry in list(self._running_tools.values()):
                try:
                    entry.advance()
                except Exception:
                    pass

    def _flush_stream(self) -> None:
        """Re-render the live reply, but only on frames where it actually grew."""
        entry = self._live_entry
        if entry is None:
            # Tokens can arrive before the widget that shows them is mounted, so
            # the buffer is left dirty rather than consumed: the next frame, with
            # the widget in place, draws everything that has landed by then.
            return
        with self._stream_lock:
            if not self._stream_dirty:
                return
            text = "".join(self._stream_parts)
            self._stream_dirty = False
        try:
            entry.set_text(text, streaming=True)
            self._scroll_to_end()
        except Exception:
            logger.debug("Stream render failed", exc_info=True)

    def _apply(self, event: _Event) -> None:
        """Execute one queued event on the UI thread."""
        handler = getattr(self, f"_do_{event.kind}", None)
        if handler is None:
            logger.debug("Unknown HUD event %r", event.kind)
            return
        try:
            handler(*event.args)
        except Exception:
            logger.exception("HUD event %s failed", event.kind)

    def _post(self, kind: str, *args: Any) -> None:
        """Queue one event from any thread. Never blocks, never raises."""
        if self._leaving:
            return
        try:
            self._events.put(_Event(kind, args))
        except Exception:
            logger.debug("Could not queue HUD event %s", kind, exc_info=True)

    # -- transcript plumbing ---------------------------------------------------------
    def _reveal_transcript(self) -> None:
        """Trade the greeting for the transcript, once there is one."""
        greeting = self.query_one("#greeting", Greeting)
        if greeting.has_class("hidden"):
            return
        greeting.add_class("hidden")
        self.query_one("#transcript", VerticalScroll).remove_class("hidden")

    def _mount_entry(self, widget: Entry) -> None:
        self._reveal_transcript()
        transcript = self.query_one("#transcript", VerticalScroll)
        at_end = transcript.is_vertical_scroll_end
        transcript.mount(widget)
        self._trim_transcript(transcript)
        if at_end:
            self.call_after_refresh(self._scroll_to_end)

    def _trim_transcript(self, transcript: VerticalScroll) -> None:
        """Keep the mounted transcript bounded; the log keeps the full record."""
        children = list(transcript.children)
        excess = len(children) - _MAX_ENTRIES
        for widget in children[:excess]:
            try:
                widget.remove()
            except Exception:
                pass

    def _scroll_to_end(self) -> None:
        try:
            self.query_one("#transcript", VerticalScroll).scroll_end(animate=False)
        except Exception:
            pass

    def _masthead_text(self) -> Text:
        """``✻ J.A.R.V.I.S.`` on the left, whatever is answering on the right."""
        text = Text()
        text.append(f"{SPARK} ", style=INK.primary)
        text.append(settings.AGENT_NAME, style=f"bold {INK.text}")
        if self._protocol:
            text.append(f"   {self._protocol.upper()}", style=f"bold {INK.secondary}")
        text.append("   ", style=INK.faint)
        text.append(self._model_status, style=INK.faint)
        return text

    def _hint_text(self) -> Text:
        text = Text()
        text.append("? for shortcuts", style=INK.faint)
        text.append("   ·   ", style=INK.faint)
        text.append("/ for commands", style=INK.faint)
        text.append("   ·   ", style=INK.faint)
        text.append("talk", style=keyword_style("talk"))
        text.append(" / ", style=INK.faint)
        text.append("quiet", style=keyword_style("quiet"))
        text.append(" for speech", style=INK.faint)
        if self._voice_status and "off" not in self._voice_status:
            text.append("   ·   ", style=INK.faint)
            text.append(self._voice_status, style=INK.faint)
        return text

    def _refresh_chrome(self) -> None:
        """Redraw the two one-line strips that frame everything else."""
        try:
            self.query_one("#masthead", Static).update(self._masthead_text())
            self.query_one("#hints", Static).update(self._hint_text())
        except Exception:
            logger.debug("chrome refresh failed", exc_info=True)

    # ══════════════════════════════════════════════════════════════════════════════════
    # The StarkHUD interface. Every method below is safe to call from any thread.
    # ══════════════════════════════════════════════════════════════════════════════════

    # -- lifecycle -------------------------------------------------------------------
    def start(self) -> None:
        """Present for interface parity: the app is its own live region."""

    def stop(self) -> None:
        """Ask the application to exit. Idempotent."""
        self._leaving = True
        try:
            self.call_from_thread(self.exit)
        except Exception:
            logger.debug("Could not stop the TUI cleanly", exc_info=True)

    @contextmanager
    def paused(self):
        """No-op: a Textual screen has no live region to suspend."""
        yield

    def print_banner(self) -> None:
        """The banner is drawn at mount; this exists so callers need not care."""

    @property
    def live_active(self) -> bool:
        return bool(self.is_running)

    # -- state -----------------------------------------------------------------------
    def set_palette(self, name: str) -> None:
        self._post("palette", name)

    def _do_palette(self, name: str) -> None:
        global INK
        theme = THEMES.get(name)
        if theme is None:
            return
        self._palette_key = name
        INK = INKS.get(name, INK)
        self.theme = theme.name
        # Every Rich-rendered widget caches nothing, but it has already drawn
        # itself in the old colours; ask the screen for a full repaint.
        self.refresh(recompose=False, layout=True)
        for widget in self.query(Static):
            widget.refresh()

    def set_state(self, state: str, detail: str = "") -> None:
        # Ordered against stream_token, which also runs on the caller's thread.
        if state == STATE_IDLE:
            self._turn_active = False
        elif not self._turn_active:
            self._turn_active = True
            self._stream_tokens = 0
        self._post("state", state, detail)

    def _do_state(self, state: str, detail: str) -> None:
        self.state = state
        self.query_one("#reactor", Reactor).state = state

        status = self.query_one("#status", StatusLine)
        status.state = state
        status.detail = detail
        composer = self.query_one("#composer")
        if state == STATE_IDLE:
            status.end()
            composer.remove_class("busy")
        else:
            if status.started is None:
                status.begin()
            composer.add_class("busy")

    def set_amplitude(self, value: float) -> None:
        # Written directly rather than queued: this arrives from an audio callback
        # at tens of hertz and only the most recent value has ever mattered.
        try:
            self._amplitude = max(0.0, min(1.0, float(value)))
        except (TypeError, ValueError):
            self._amplitude = 0.0
        reactor = self._reactor_if_mounted()
        if reactor is not None:
            reactor.amplitude = self._amplitude

    def _reactor_if_mounted(self) -> Reactor | None:
        try:
            return self.query_one("#reactor", Reactor)
        except Exception:
            return None

    def set_telemetry(self, telemetry: Any) -> None:
        self._post("telemetry", telemetry)

    def _do_telemetry(self, telemetry: Any) -> None:
        self.query_one("#vitals", Vitals).update_telemetry(telemetry)
        # The greeting reads the machine before it speaks, so it wants this too --
        # but only while it is still the thing on screen.
        greeting = self.query_one("#greeting", Greeting)
        if not greeting.has_class("hidden"):
            greeting.observe(telemetry=telemetry)

    def set_metrics(self, metrics: Any) -> None:
        """Latency measurements from the engine, shown in the sidebar."""
        self._post("metrics", metrics)

    def _do_metrics(self, metrics: Any) -> None:
        self.query_one("#meter", TurnMeter).update_metrics(metrics)

    def set_protocol(self, name: str | None) -> None:
        self._post("protocol", name)

    def _do_protocol(self, name: str | None) -> None:
        self._protocol = name
        systems = self.query_one("#systems", Systems)
        systems.protocol = name
        systems.refresh(layout=True)
        self._refresh_chrome()

    def set_voice_status(self, text: str) -> None:
        self._post("voice_status", str(text))

    def _do_voice_status(self, text: str) -> None:
        self._voice_status = text
        systems = self.query_one("#systems", Systems)
        systems.voice_status = text
        systems.refresh(layout=True)
        self._refresh_chrome()

    def set_model_status(self, text: str) -> None:
        self._post("model_status", str(text))

    def _do_model_status(self, text: str) -> None:
        self._model_status = text
        systems = self.query_one("#systems", Systems)
        systems.model_status = text
        systems.refresh(layout=True)
        self._refresh_chrome()

    def set_model_ready(self, ready: bool) -> None:
        """Preflight's verdict. The greeting says so rather than pretending."""
        self._post("model_ready", bool(ready))

    def _do_model_ready(self, ready: bool) -> None:
        self.query_one("#greeting", Greeting).observe(model_ready=ready)

    def set_warm(self, warm: bool = True) -> None:
        """The model is loaded and pinned; the first question will be quick."""
        self._post("warm", bool(warm))

    def _do_warm(self, warm: bool) -> None:
        self.query_one("#greeting", Greeting).observe(warm=warm)

    def set_tools(self, names: Sequence[str]) -> None:
        self._post("tools", list(names))

    def _do_tools(self, names: list[str]) -> None:
        self._tool_names = names
        systems = self.query_one("#systems", Systems)
        systems.tools = names
        systems.refresh(layout=True)

    def bind_busy(self, predicate: Callable[[], bool]) -> None:
        """Tell the HUD how to ask whether the agent is mid-turn."""
        self._agent_busy = predicate

    # -- transcript ------------------------------------------------------------------
    def log_user(self, text: str) -> None:
        self._post("user", str(text))

    def _do_user(self, text: str) -> None:
        spoken = text.startswith("(voice) ")
        self._mount_entry(UserEntry(text[8:] if spoken else text, spoken=spoken))

    def log_agent(self, text: str, markdown: bool = True) -> None:
        self._post("agent", str(text))

    def _do_agent(self, text: str) -> None:
        if not text.strip():
            return
        self._mount_entry(AgentEntry(text))

    def log_system(self, text: str, level: str = "info") -> None:
        self._post("system", str(text), str(level))

    def _do_system(self, text: str, level: str) -> None:
        self._mount_entry(SystemEntry(text, level))

    def log_interim(self, text: str) -> None:
        self._post("interim", str(text))

    def _do_interim(self, text: str) -> None:
        content = _one_line(text, 240)
        if content:
            self._mount_entry(InterimEntry(content))

    def log_thought(self, text: str) -> None:
        self._post("thought", str(text))

    def _do_thought(self, text: str) -> None:
        if not self._show_reasoning:
            return
        content = _one_line(text, 600)
        if content:
            self._mount_entry(ThoughtEntry(content))

    def log_tool_start(self, name: str, arguments: Any) -> None:
        self._post("tool_start", str(name), arguments)

    def _do_tool_start(self, name: str, arguments: Any) -> None:
        entry = ToolEntry(name, arguments)
        self._mount_entry(entry)
        self._running_tools[self._tool_key(name, arguments)] = entry
        self._pending_tools.append(entry)

    def log_tool(self, name: str, arguments: Any, result: str = "", ok: bool = True) -> None:
        self._post("tool", str(name), arguments, str(result), bool(ok))

    def _do_tool(self, name: str, arguments: Any, result: str, ok: bool) -> None:
        key = self._tool_key(name, arguments)
        entry = self._running_tools.pop(key, None)
        if entry is None:
            # A result with no announcement: mount a finished card rather than
            # dropping the reading.
            entry = ToolEntry(name, arguments)
            self._mount_entry(entry)
        else:
            try:
                self._pending_tools.remove(entry)
            except ValueError:
                pass
        entry.complete(result, ok)

    @staticmethod
    def _tool_key(name: str, arguments: Any) -> str:
        return f"{name}:{_compact_args(arguments)}"

    def push_alert(self, alert: Any) -> None:
        self._post("alert", alert)

    def _do_alert(self, alert: Any) -> None:
        self._mount_entry(AlertEntry(alert))
        try:
            self.bell()
        except Exception:
            pass

    def render_table(self, title: str, columns: Sequence[str], rows: Sequence[Sequence[str]]) -> None:
        self._post("table", str(title), list(columns), [list(r) for r in rows])

    def _do_table(self, title: str, columns: list, rows: list) -> None:
        self._mount_entry(TableEntry(title, columns, rows))

    def render_code(self, code: str, language: str = "python", title: str = "") -> None:
        self._post("code", str(code), str(language), str(title))

    def _do_code(self, code: str, language: str, title: str) -> None:
        self._mount_entry(CodeEntry(code, language, title))

    def clear_transcript(self) -> None:
        self._post("clear")

    def _do_clear(self) -> None:
        transcript = self.query_one("#transcript", VerticalScroll)
        for child in list(transcript.children):
            try:
                child.remove()
            except Exception:
                pass
        self._running_tools.clear()
        self._pending_tools.clear()
        self._live_entry = None
        self.query_one("#transcript", VerticalScroll).add_class("hidden")
        self.query_one("#greeting", Greeting).remove_class("hidden")

    # -- streaming -------------------------------------------------------------------
    def stream_begin(self) -> None:
        with self._stream_lock:
            self._stream_parts = []
            self._stream_dirty = False
            self._streaming = True
        self._post("stream_begin")

    def _do_stream_begin(self) -> None:
        entry = AgentEntry("", streaming=True)
        self._live_entry = entry
        self._mount_entry(entry)

    def stream_token(self, token: str) -> None:
        """Take one chunk from the model. Deliberately the cheapest call here.

        This runs on the engine's event loop for every chunk the daemon emits, so
        it does no rendering, acquires one uncontended lock, and returns.
        """
        if not token:
            return
        with self._stream_lock:
            if not self._streaming:
                return
            self._stream_parts.append(token)
            self._stream_dirty = True
            self._stream_tokens += 1

    def stream_end(self, final_text: str | None = None, interim: bool = False) -> None:
        with self._stream_lock:
            buffered = "".join(self._stream_parts)
            self._stream_parts = []
            self._stream_dirty = False
            self._streaming = False
        text = final_text if final_text is not None else buffered
        self._post("stream_end", text or "", bool(interim))

    def _do_stream_end(self, text: str, interim: bool) -> None:
        entry, self._live_entry = self._live_entry, None
        if entry is None:
            if text.strip():
                self._mount_entry(InterimEntry(_one_line(text, 240)) if interim
                                  else AgentEntry(text))
            return
        if not text.strip():
            entry.remove()
            return
        if interim:
            # Narration on the way to an answer, not the answer: demote it rather
            # than leaving a full reply panel that the real answer will duplicate.
            entry.remove()
            self._mount_entry(InterimEntry(_one_line(text, 240)))
            return
        entry.set_text(text, streaming=False)
        self._scroll_to_end()

    # -- input -----------------------------------------------------------------------
    def prompt_input(self, prompt_text: str = "") -> str:
        """Not used in this front end: the composer is always on screen.

        Present so a caller written against :class:`jarvis.ui.StarkHUD` does not
        crash; it blocks forever rather than returning junk, and the application
        does not call it.
        """
        raise RuntimeError("the full-screen HUD reads input from its composer")

    def confirm(self, question: str) -> bool:
        result = self._ask(ConfirmScreen(question))
        return bool(result)

    def ask_permission(self, request: Any) -> str:
        result = self._ask(PermissionScreen(request))
        return str(result or "n")

    def choose(self, question: str, options: list[tuple[str, str]], hint: str = "") -> str:
        result = self._ask(ChooseScreen(question, options, hint))
        return str(result or "")

    def _ask(self, screen: ModalScreen) -> Any:
        """Put a modal up and block the *calling* thread until it is answered.

        The UI thread keeps running throughout — this is the one place a caller
        genuinely wants to wait, because it has asked the operator a question and
        has nothing to do until it is answered.

        Deliberately not ``push_screen_wait``: that requires the Textual worker
        context, which does not survive the hop across ``call_from_thread`` onto
        the app's own loop. Pushing with a completion callback and waiting on an
        event is equivalent, and works no matter which thread asked.
        """
        if not self.is_running:
            logger.warning("Modal requested before the HUD was running")
            return None

        answered = threading.Event()
        box: dict[str, Any] = {}

        def _answered(result: Any) -> None:
            box["result"] = result
            answered.set()

        try:
            self.call_from_thread(self.push_screen, screen, _answered)
        except Exception:
            logger.exception("Could not present the dialog")
            return None

        # Polled rather than waited on outright so a shutdown mid-question
        # releases the caller instead of stranding it on a dead screen.
        while not answered.wait(0.2):
            if self._leaving or not self.is_running:
                logger.info("Dialog abandoned: the HUD is closing")
                return None
        return box.get("result")

    # -- submissions -----------------------------------------------------------------
    @on(Input.Submitted, "#prompt")
    def _submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        event.input.value = ""
        if not text:
            return
        if not self._history or self._history[-1] != text:
            self._history.append(text)
        self._history_index = len(self._history)
        self._draft = ""
        self._dispatch(text)

    @work(thread=True, name="turn", group="agent")
    def _dispatch(self, text: str) -> None:
        """Hand one line to the application, off the UI thread."""
        if self._on_submit is None:
            return
        try:
            self._on_submit(text)
        except Exception:
            logger.exception("submission handler failed")
            self.log_system("That request faulted. The details are in the log.", "error")

    # -- actions ---------------------------------------------------------------------
    def action_interrupt(self) -> None:
        """Ctrl-C stops work in progress; at an idle prompt it means goodbye."""
        handled = False
        if self._on_interrupt is not None:
            try:
                handled = bool(self._on_interrupt())
            except Exception:
                logger.exception("interrupt handler failed")
        if not handled:
            self.action_leave()

    def action_leave(self) -> None:
        self._leaving = True
        if self._on_quit is not None:
            try:
                self._on_quit()
            except Exception:
                logger.exception("quit handler failed")
        self.exit()

    def action_clear(self) -> None:
        self._do_clear()

    def action_toggle_sidebar(self) -> None:
        """Bring the instrument panel in, or send it away again.

        Claude Code has no sidebar, so neither has this until it is asked for.
        The vitals and the latency meter are still live behind it.
        """
        self._sidebar_pinned = True
        self.query_one("#sidebar").toggle_class("shown")

    def action_toggle_reasoning(self) -> None:
        self._show_reasoning = not self._show_reasoning
        self._do_system(
            f"Reasoning {'shown' if self._show_reasoning else 'hidden'}.", "info"
        )

    def action_toggle_speech(self) -> None:
        if self._on_toggle_speech is None:
            return
        self._toggle_speech_worker()

    @work(thread=True, name="speech", group="voice")
    def _toggle_speech_worker(self) -> None:
        try:
            if self._on_toggle_speech is not None:
                self._on_toggle_speech()
        except Exception:
            logger.exception("speech toggle failed")

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    def action_history_back(self) -> None:
        if not self._history:
            return
        prompt = self.query_one("#prompt", Input)
        if self._history_index == len(self._history):
            self._draft = prompt.value
        self._history_index = max(0, self._history_index - 1)
        prompt.value = self._history[self._history_index]
        prompt.cursor_position = len(prompt.value)

    def action_history_forward(self) -> None:
        if not self._history:
            return
        prompt = self.query_one("#prompt", Input)
        self._history_index = min(len(self._history), self._history_index + 1)
        if self._history_index == len(self._history):
            prompt.value = self._draft
        else:
            prompt.value = self._history[self._history_index]
        prompt.cursor_position = len(prompt.value)

    # -- misc ------------------------------------------------------------------------
    def wait_ready(self, timeout: float = 10.0) -> bool:
        """Block until the screen is mounted. For callers that boot in a thread."""
        return self._mounted.wait(timeout)

    def refresh_prompt_label(self) -> None:
        """Pick up a change of honorific after onboarding."""
        self._post("prompt_label")

    def _do_prompt_label(self) -> None:
        # The composer keeps Claude Code's bare caret; it is the greeting that
        # learns the operator's name.
        self.query_one("#greeting", Greeting).refresh(layout=True)
        self._refresh_chrome()


# ══════════════════════════════════════════════════════════════════════════════════════
# Small helpers
# ══════════════════════════════════════════════════════════════════════════════════════
def _compact_count(value: int) -> str:
    """1400 becomes 1.4k; the exact figure is never the interesting part."""
    if value < 1000:
        return str(value)
    return f"{value / 1000:.1f}k"


def _one_line(text: Any, limit: int) -> str:
    """Collapse a blob to a single line no longer than ``limit``."""
    flat = " ".join(str(text or "").split())
    if len(flat) <= limit:
        return flat
    return flat[: max(0, limit - 1)].rstrip() + "…"


def _compact_args(arguments: Any) -> str:
    """A short, readable rendering of a tool's arguments."""
    if not arguments:
        return ""
    if isinstance(arguments, str):
        return _one_line(arguments, 60)
    if not isinstance(arguments, dict):
        return _one_line(arguments, 60)
    parts = []
    for key, value in list(arguments.items())[:3]:
        rendered = _one_line(value, 28)
        parts.append(f"{key}={rendered}" if rendered else str(key))
    if len(arguments) > 3:
        parts.append("…")
    return ", ".join(parts)
