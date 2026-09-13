"""The line the window opens on, written by the model and different every time.

A greeting has one job and a hard constraint. The job: say something that was
not said last time, in J.A.R.V.I.S.'s voice, about *this* moment — the hour, the
workspace, the machine. The constraint: the window must appear instantly, and a
local model asked a question is anywhere between two hundred milliseconds and
never.

Those cannot both be satisfied by generating on demand, so this does not.

It keeps a **bank**. Opening the window takes a line out of the bank and shows it
immediately — no waiting, no spinner, no empty space — and, in the background,
asks the model for a new one and puts it in. The line you read was written by the
model; it was written a launch or two ago. When the model happens to be quick,
the fresh one arrives while the welcome is still on screen and replaces what is
there, so it is genuinely of this moment. When the model is slow, absent, or
mid-download, you get a good line anyway and never know the difference.

The very first launch is the only one with an empty bank, and it falls back to
the three lines in ``prompts.BOOT_GREETINGS`` — after which the bank fills and
they are never seen again.
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from config import PROJECT_ROOT, settings
from jarvis import prompts

LOG = logging.getLogger("jarvis.greeting")

#: Where the bank lives. Beside the profile and the theme, and ignored by git for
#: the same reason they are: it is this machine's, not the project's.
BANK_PATH = PROJECT_ROOT / ".jarvis_greetings.json"

#: How many lines to keep. Enough that a repeat is a long way off, few enough
#: that the file stays something a person could read.
BANK_SIZE = 16

#: How long the window will hold the welcome open for a fresh line before
#: deciding the model is not going to answer in time and banking it instead.
SWAP_WINDOW = 6.0

#: A greeting is one or two sentences. Anything longer is a model that has
#: started explaining itself, and gets cut.
MAX_WORDS = 34
MAX_CHARS = 220

SYSTEM = (
    "You are {agent_full_name} — {agent_name} — the assistant from Iron Man, running "
    "locally on {user_title}'s own machine.\n"
    "Write exactly one greeting for {user_title}, to be shown the moment the window "
    "opens.\n"
    "\n"
    "Rules, all of them hard:\n"
    "- One sentence. Two at the very most. Under twenty-five words.\n"
    "- Address {user_title} as {user_title}.\n"
    "- Dry, composed, quietly witty. Competent rather than eager. Never breathless, "
    "never an exclamation mark.\n"
    "- No emoji, no markdown, no quotation marks around it, no preamble like "
    "\"Here is a greeting\". Return the line itself and nothing else.\n"
    "- Do not offer a list of what you can do. Do not ask what they need. "
    "They know.\n"
    "- It must not resemble any of the recent lines you are shown."
)

TASK = (
    "It is {part_of_day}, {weekday}, {clock}. "
    "The workspace is {workspace} {machine}\n"
    "{history}\n"
    "Write the greeting."
)


def _part_of_day(now: datetime) -> str:
    hour = now.hour
    if hour < 5:
        return "the small hours"
    if hour < 12:
        return "morning"
    if hour < 17:
        return "afternoon"
    if hour < 21:
        return "evening"
    return "late evening"


# ══════════════════════════════════════════════════════════════════════════════════════
# The bank
# ══════════════════════════════════════════════════════════════════════════════════════
@dataclass
class Bank:
    """Lines the model has written, and which one was shown last."""

    lines: list[str]
    last: str = ""

    @classmethod
    def load(cls) -> "Bank":
        try:
            if BANK_PATH.exists():
                data = json.loads(BANK_PATH.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    lines = [str(line) for line in data.get("lines", []) if str(line).strip()]
                    return cls(lines=lines[-BANK_SIZE:], last=str(data.get("last", "")))
        except (OSError, ValueError):
            LOG.debug("The greeting bank could not be read; starting a new one")
        return cls(lines=[])

    def save(self) -> None:
        try:
            BANK_PATH.parent.mkdir(parents=True, exist_ok=True)
            BANK_PATH.write_text(
                json.dumps({"lines": self.lines[-BANK_SIZE:], "last": self.last}, indent=2),
                encoding="utf-8",
            )
        except OSError:
            LOG.debug("The greeting bank could not be written")

    def add(self, line: str) -> bool:
        """Bank a new line. False when it is empty or already in there."""
        line = line.strip()
        if not line or line in self.lines:
            return False
        self.lines.append(line)
        del self.lines[:-BANK_SIZE]
        self.save()
        return True

    def take(self) -> str:
        """A line that is not the one shown last time. "" when the bank is empty.

        Chosen at random rather than in turn, so two launches a minute apart do
        not read like a rota.
        """
        choices = [line for line in self.lines if line != self.last] or list(self.lines)
        if not choices:
            return ""
        chosen = random.choice(choices)
        self.last = chosen
        self.save()
        return chosen


# ══════════════════════════════════════════════════════════════════════════════════════
# Writing one
# ══════════════════════════════════════════════════════════════════════════════════════
def _machine_note(telemetry: Any = None) -> str:
    """One clause about the machine, or nothing at all.

    Deliberately thin. A greeting that recites four gauges is a status report,
    and there is already a status bar.
    """
    if telemetry is None:
        return ""
    try:
        cpu = float(getattr(telemetry, "cpu_percent", 0) or 0)
        ram = float(getattr(telemetry, "ram_percent", 0) or 0)
        battery = getattr(telemetry, "battery_percent", None)
    except (TypeError, ValueError):
        return ""

    if battery is not None and float(battery) < 20:
        return f"The battery is at {int(float(battery))} per cent."
    if cpu > 80:
        return f"The processor is busy, at {int(cpu)} per cent."
    if ram > 85:
        return f"Memory is tight, at {int(ram)} per cent."
    return "Everything is quiet."


def tidy(raw: str) -> str:
    """Take what the model returned and keep only the greeting.

    Small local models preface things, wrap things in quotes and occasionally
    answer with three paragraphs. None of that is the caller's problem.
    """
    text = str(raw or "").strip()
    if not text:
        return ""

    # Any leading throat-clearing, and the fenced blocks some models insist on.
    text = re.sub(r"^```[a-z]*\s*|\s*```$", "", text).strip()
    text = re.sub(r"^(?:here(?:'s| is)[^:]{0,40}:|greeting:|response:)\s*", "", text,
                  flags=re.IGNORECASE).strip()
    # A single line, even when it arrived as several.
    text = text.split("\n")[0].strip()
    # Matched quotes around the whole thing.
    for opener, closer in (('"', '"'), ("'", "'"), ("“", "”"), ("‘", "’")):
        if text.startswith(opener) and text.endswith(closer) and len(text) > 2:
            text = text[1:-1].strip()
    # Markdown emphasis, which is meaningless in a plain welcome line.
    text = re.sub(r"[*_`#]+", "", text).strip()

    if not text or len(text) > MAX_CHARS or len(text.split()) > MAX_WORDS:
        # Two sentences at most, and only if that gets it under the limits.
        sentences = re.split(r"(?<=[.!?])\s+", text)
        text = " ".join(sentences[:2]).strip()
        if not text or len(text) > MAX_CHARS or len(text.split()) > MAX_WORDS:
            return ""

    # A greeting with no letters in it is not a greeting.
    if not re.search(r"[A-Za-zऀ-ॿ]", text):
        return ""
    return text


def compose(agent: Any, *, telemetry: Any = None, recent: list[str] | None = None) -> str:
    """Ask the model for one greeting. "" when it cannot or will not."""
    ask = getattr(agent, "aside", None)
    if not callable(ask):
        return ""

    now = datetime.now()
    history = ""
    if recent:
        listed = "\n".join("- " + line for line in recent[-6:])
        history = "Recent greetings, none of which you may echo:\n" + listed

    system = prompts.personalise(SYSTEM)
    task = prompts.personalise(
        TASK,
        part_of_day=_part_of_day(now),
        weekday=now.strftime("%A"),
        clock=now.strftime("%H:%M"),
        # A directory called "J.A.R.V.I.S." would otherwise end the clause with
        # two full stops, and a model shown sloppy prose writes sloppy prose.
        workspace=(Path(settings.WORKSPACE_ROOT).name or "this machine").rstrip(".") + ".",
        machine=_machine_note(telemetry),
        history=history,
    )
    # An empty machine note or history would otherwise leave a ragged line or a
    # blank one, and a model shown sloppy prose writes sloppy prose back.
    task = "\n".join(line for line in
                     (re.sub(r"[ \t]+", " ", part).strip() for part in task.split("\n"))
                     if line)
    try:
        return tidy(ask(task, system=system))
    except Exception:
        LOG.debug("The model would not write a greeting", exc_info=True)
        return ""


def fallback() -> str:
    """What to say on the very first launch, before the bank has anything in it."""
    return prompts.personalise(random.choice(prompts.BOOT_GREETINGS))


# ══════════════════════════════════════════════════════════════════════════════════════
# What the window uses
# ══════════════════════════════════════════════════════════════════════════════════════
class Greeter:
    """Hands out a greeting now, and writes the next one in the background."""

    def __init__(self, agent: Any = None, on_fresh: Any = None) -> None:
        self.agent = agent
        #: Called with a newly written line, if one arrives while it is still
        #: worth showing. The window uses this to replace what it put up.
        self.on_fresh = on_fresh
        self.bank = Bank.load()
        self._lock = threading.Lock()
        self._opened_at = 0.0
        self._shown = ""

    @property
    def enabled(self) -> bool:
        return bool(getattr(settings, "GREETING_FROM_MODEL", True))

    def current(self) -> str:
        """The line to show right now. Instant, always something, never a repeat."""
        with self._lock:
            self._opened_at = time.monotonic()
            line = self.bank.take() or fallback()
            self._shown = line
            return line

    def refresh(self, telemetry: Any = None) -> None:
        """Write the next one, off the hot path. Safe to call and forget."""
        if not self.enabled or self.agent is None:
            return
        threading.Thread(
            target=self._write, args=(telemetry,), name="greeting", daemon=True
        ).start()

    def _write(self, telemetry: Any) -> None:
        line = compose(self.agent, telemetry=telemetry, recent=self.bank.lines)
        if not line:
            return
        with self._lock:
            fresh = self.bank.add(line)
            # Close enough to the window opening that the operator is probably
            # still looking at the welcome: show it instead of banking it for
            # a launch that may be days away.
            in_time = (time.monotonic() - self._opened_at) < SWAP_WINDOW
            swap = fresh and in_time and line != self._shown
            if swap:
                self.bank.last = line
                self._shown = line
                self.bank.save()
        if swap and callable(self.on_fresh):
            try:
                self.on_fresh(line)
            except Exception:
                LOG.debug("The window would not take the fresh greeting", exc_info=True)
