"""Voice I/O for J.A.R.V.I.S. — wake-word listening and non-blocking speech.

Two halves, deliberately independent so either can fail without taking the other
down with it:

* **TTS** — a single worker thread drains a :class:`queue.Queue`, so
  :meth:`VoiceSystem.speak` returns immediately and the ReAct loop never stalls
  waiting for a sentence to finish. The synthesiser is built *inside* that
  worker thread: SAPI5 (the Windows backend behind pyttsx3) is apartment
  threaded, and an engine constructed on one thread but driven from another
  deadlocks the moment you call ``runAndWait``.
* **STT** — ``speech_recognition``'s background listener feeds transcripts
  through a wake-word filter. The recogniser is suppressed while the speaker is
  active so J.A.R.V.I.S. never transcribes his own voice and answers himself.

Every optional dependency is guarded. With no microphone, no PyAudio and no TTS
engine at all, :class:`VoiceSystem` still constructs, reports what is missing in
:attr:`VoiceStatus.errors`, and turns every method into a well-behaved no-op.
``__init__`` never raises — a silent J.A.R.V.I.S. is a nuisance, a crashing one
is a bug.
"""

from __future__ import annotations

import logging
import math
import os
import queue
import random
import re
import shutil
import subprocess
import tempfile
import threading
import unicodedata
import time
import wave as wave_module
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from config import STATE_IDLE, STATE_LISTENING, STATE_SPEAKING, settings
from jarvis import locales

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------------------
# Optional audio stack. Each import is independent: pyttsx3 without PyAudio still speaks,
# PyAudio without pyttsx3 still listens. A broken install (not merely a missing one) is
# caught too, because a half-installed driver raising OSError at import time would
# otherwise take the whole application down before main() ever runs.
# --------------------------------------------------------------------------------------
try:  # pragma: no cover - availability is environmental
    import speech_recognition as sr  # type: ignore
except ImportError:
    sr = None  # type: ignore[assignment]
except Exception as _exc:  # pragma: no cover
    sr = None  # type: ignore[assignment]
    logger.warning("speech_recognition failed to import: %s", _exc)

try:  # pragma: no cover
    import pyaudio  # type: ignore  # noqa: F401  (imported for presence detection only)
except ImportError:
    pyaudio = None  # type: ignore[assignment]
except Exception as _exc:  # pragma: no cover
    pyaudio = None  # type: ignore[assignment]
    logger.warning("pyaudio failed to import: %s", _exc)

try:  # pragma: no cover
    import pyttsx3  # type: ignore
except ImportError:
    pyttsx3 = None  # type: ignore[assignment]
except Exception as _exc:  # pragma: no cover
    pyttsx3 = None  # type: ignore[assignment]
    logger.warning("pyttsx3 failed to import: %s", _exc)

try:  # pragma: no cover
    import edge_tts  # type: ignore
except ImportError:
    edge_tts = None  # type: ignore[assignment]
except Exception as _exc:  # pragma: no cover
    edge_tts = None  # type: ignore[assignment]
    logger.warning("edge_tts failed to import: %s", _exc)


# --------------------------------------------------------------------------------------
# Engine identifiers. These are the exact strings that land in VoiceStatus.tts_engine.
# --------------------------------------------------------------------------------------
ENGINE_PYTTSX3 = "pyttsx3"
ENGINE_EDGE = "edge-tts"
ENGINE_NONE = "none"

# How long after the last syllable the recogniser stays deaf. Speakers ring, rooms echo,
# and a hot microphone will happily transcribe the tail of our own sentence as a command.
_SELF_HEARING_TAIL = 0.7

# After a bare wake word ("Jarvis"), the next phrase counts as the command even though it
# carries no wake word of its own.
_WAKE_FOLLOW_UP_WINDOW = 14.0

# Bounded wait for the worker thread to report which engine it actually managed to build,
# so main.py's preflight banner tells the truth. pyttsx3.init() is ~200 ms; anything past
# this is a wedged driver and we carry on with the optimistic guess rather than hang.
_ENGINE_INIT_TIMEOUT = 8.0

# Longest run of characters handed to the synthesiser in one go. Speaking in sentence-sized
# chunks is what makes stop_speaking() responsive without cross-thread COM calls.
_SPEECH_CHUNK_CHARS = 240

# edge-tts expresses rate and volume as a percentage offset from its own baseline.
_EDGE_BASELINE_WPM = 175.0

_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?…])\s+")
_URL_PATTERN = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
_MD_IMAGE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_MD_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_FENCE_BLOCK = re.compile(r"```.*?```", re.DOTALL)
_DANGLING_FENCE = re.compile(r"```.*", re.DOTALL)
_INLINE_CODE = re.compile(r"`([^`]*)`")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s*", re.MULTILINE)
_BLOCKQUOTE = re.compile(r"^\s{0,3}>+\s?", re.MULTILINE)
_BULLET = re.compile(r"^\s{0,6}(?:[-*+•‣▪]|\d{1,3}[.)])\s+", re.MULTILINE)
_HRULE = re.compile(r"^\s{0,3}(?:[-*_]\s*){3,}$", re.MULTILINE)
_TABLE_ROW = re.compile(r"^\s*\|.*$", re.MULTILINE)
_EMPHASIS = re.compile(r"(\*{1,3}|_{1,3}|~{2})(?=\S)(.+?)(?<=\S)\1", re.DOTALL)
_DECORATION = re.compile(
    "[│┃║┆┊─━═┄┈"
    "┌┐└┘├┤┬┴┼"
    "▁▂▃▄▅▆▇█▔▕"
    "•‣▪◦→←↑↓⟶]+"
)
_TRIM_CHARS = " \t,.!?;:—–-"

_WS_RE = re.compile(r"\s+")

#: Where the spoken summary ends and the on-screen detail begins. The system prompt has
#: him lead with a speech-grade paragraph and put tables, code and lists underneath, so
#: streaming speech stops at the first of these rather than reciting a Markdown table.
_DETAIL_MARKER_RE = re.compile(r"\n\s*\n|```|\n\s*[|#*\-\d]")

#: Sentence terminators, including the Devanagari danda and double danda -- a Hindi reply
#: has no full stops in it at all, so an ASCII-only split would never speak a word of it.
_SENTENCE_END_RE = re.compile(r"(?<=[.!?\u0964\u0965])\s+")


def _split_sentences(buffer: str) -> tuple[list[str], str]:
    """Split a streaming buffer into complete sentences plus the unfinished tail."""
    if not buffer:
        return [], ""
    parts = _SENTENCE_END_RE.split(buffer)
    if len(parts) < 2:
        return [], buffer
    # A trailing terminator means the last part is itself complete.
    tail = parts[-1]
    complete = [p for p in parts[:-1] if p.strip()]
    if buffer[-1:] in ".!?\u0964\u0965":
        if tail.strip():
            complete.append(tail)
        tail = ""
    return complete, tail

#: Unicode combining marks -- the matras and viramas that Indic scripts are built from.
_MARK_CATEGORIES = {"Mn", "Mc", "Me"}


def _is_wake_char(ch: str) -> bool:
    """Characters that carry meaning in a call phrase.

    ``str.isalnum`` alone is wrong for Indic scripts: the vowel signs in
    "\u0928\u092e\u0938\u094d\u0924\u0947" are combining marks (Unicode category
    ``Mn``/``Mc``), which are not alphanumeric, so a naive filter shreds the word into
    unrecognisable consonant soup. Marks are kept alongside letters and digits.
    """
    if ch.isalnum():
        return True
    return unicodedata.category(ch) in _MARK_CATEGORIES


def normalise_wake(text: str) -> str:
    """Collapse a phrase to its bare comparable form.

    "Hello, J.A.R.V.I.S.!" and "hello j a r v i s" both reduce to "hello jarvis", and
    "\u0928\u092e\u0938\u094d\u0924\u0947, \u091c\u093e\u0930\u094d\u0935\u093f\u0938" survives intact rather than being stripped to its
    bare consonants.
    """
    if not text:
        return ""
    normalised = unicodedata.normalize("NFC", str(text))
    kept = [ch.lower() if _is_wake_char(ch) else " " for ch in normalised]
    flat = _WS_RE.sub(" ", "".join(kept)).strip()
    if not flat:
        return ""

    # Re-join runs of single ASCII letters, so a recogniser that spells the name out as
    # "j a r v i s" still matches. Deliberately ASCII-only: an Indic syllable is
    # legitimately one character long and must never be glued to its neighbour.
    out: list[str] = []
    run: list[str] = []
    for token in flat.split(" "):
        if len(token) == 1 and token.isascii() and token.isalpha():
            run.append(token)
            continue
        if run:
            out.append("".join(run) if len(run) > 1 else run[0])
            run = []
        out.append(token)
    if run:
        out.append("".join(run) if len(run) > 1 else run[0])
    return " ".join(out)


def _recover_tail(original: str, normalised_tail: str) -> str:
    """Return the command with its original punctuation where that is recoverable."""
    if not normalised_tail:
        return ""
    words = normalised_tail.split()
    if not words:
        return ""
    match = re.search(re.escape(words[0]), original, re.IGNORECASE)
    if match:
        candidate = original[match.start():].strip().strip(_TRIM_CHARS)
        if candidate:
            return candidate
    return normalised_tail


@dataclass
class VoiceStatus:
    """What the audio subsystem actually managed to bring up.

    ``errors`` accumulates human-readable reasons rather than exceptions, because
    this ends up on the HUD in front of the operator, not in a stack trace.
    """

    tts_available: bool = False
    stt_available: bool = False
    tts_engine: str = ENGINE_NONE
    microphone: str | None = None
    errors: list[str] = field(default_factory=list)

    def describe(self) -> str:
        """One terse line for the HUD's voice-status field."""
        speech = f"TTS {self.tts_engine}" if self.tts_available else "TTS offline"
        if self.stt_available:
            hearing = f"STT {self.microphone or 'default microphone'}"
        else:
            hearing = "STT offline"
        line = f"{speech} | {hearing}"
        if self.errors:
            # Two reasons is plenty for a status strip; the log file has the rest.
            line += " — " + "; ".join(self.errors[:2])
        return line


def strip_for_speech(text: str, max_chars: int | None = None) -> str:
    """Reduce Markdown to something worth listening to.

    Code fences, tables, headings, emphasis markers and bare URLs are all
    perfectly readable and completely unlistenable, so they come out. The result
    is collapsed to single-spaced prose and truncated on a sentence boundary at
    ``max_chars`` (default ``settings.TTS_MAX_CHARS``) — J.A.R.V.I.S. delivers a
    verdict aloud and leaves the data on screen.
    """
    if not text:
        return ""

    # 0 means an explicit 'do not truncate' -- streamed sentences are already short,
    # and clipping each one at the paragraph limit would swallow most of the reply.
    cleaned = str(text)
    cleaned = _FENCE_BLOCK.sub(" ", cleaned)
    cleaned = _DANGLING_FENCE.sub(" ", cleaned)  # an unterminated fence from a cut stream
    cleaned = _TABLE_ROW.sub(" ", cleaned)
    cleaned = _HRULE.sub(" ", cleaned)
    cleaned = _MD_IMAGE.sub(r"\1", cleaned)
    cleaned = _MD_LINK.sub(r"\1", cleaned)
    cleaned = _URL_PATTERN.sub("a link", cleaned)
    cleaned = _INLINE_CODE.sub(r"\1", cleaned)
    cleaned = _HEADING.sub("", cleaned)
    cleaned = _BLOCKQUOTE.sub("", cleaned)
    cleaned = _BULLET.sub("", cleaned)
    # Twice, so a nested marker like **_emphatic_** unwraps completely.
    cleaned = _EMPHASIS.sub(r"\2", cleaned)
    cleaned = _EMPHASIS.sub(r"\2", cleaned)
    cleaned = _DECORATION.sub(" ", cleaned)
    cleaned = cleaned.replace("\r", "\n")
    # A paragraph break becomes a sentence break, which gives the truncator somewhere
    # sensible to cut and stops two paragraphs running together into one breathless line.
    cleaned = re.sub(r"\n{2,}", ". ", cleaned)
    cleaned = cleaned.replace("\n", " ")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    cleaned = re.sub(r"\s+([,.;:!?])", r"\1", cleaned)
    cleaned = re.sub(r"(?:\.\s*){2,}", ". ", cleaned).strip()
    cleaned = cleaned.strip(_TRIM_CHARS + "*_")

    limit = settings.TTS_MAX_CHARS if max_chars is None else max_chars
    if limit and limit > 0 and len(cleaned) > limit:
        cleaned = _truncate_on_sentence(cleaned, limit)
    return cleaned


def _truncate_on_sentence(text: str, limit: int) -> str:
    """Cut ``text`` to ``limit`` characters, preferring a sentence boundary."""
    head = text[:limit]
    cut = max(head.rfind(". "), head.rfind("! "), head.rfind("? "))
    # Only honour a boundary that leaves a worthwhile amount of speech behind; otherwise
    # a stray full stop in the first few words would gut the whole summary.
    if cut >= limit // 3:
        return head[: cut + 1].strip()
    space = head.rfind(" ")
    trimmed = (head[:space] if space > 0 else head).strip().rstrip(_TRIM_CHARS)
    if trimmed and trimmed[-1] not in ".!?":
        trimmed += "."
    return trimmed


def _chunk_for_speech(text: str, limit: int = _SPEECH_CHUNK_CHARS) -> list[str]:
    """Split speech into sentence-sized chunks the worker can abandon between."""
    sentences = [s.strip() for s in _SENTENCE_BOUNDARY.split(text) if s.strip()]
    if not sentences:
        return []
    chunks: list[str] = []
    buffer = ""
    for sentence in sentences:
        while len(sentence) > limit:
            # An enormous unpunctuated sentence still has to be breakable, or the ability
            # to interrupt dies along with it.
            window = sentence[:limit]
            split = max(window.rfind(", "), window.rfind("; "), window.rfind(" "))
            if split <= 0:
                split = limit
            chunks.append(sentence[:split].strip())
            sentence = sentence[split:].strip()
        if not sentence:
            continue
        if not buffer:
            buffer = sentence
        elif len(buffer) + 1 + len(sentence) <= limit:
            buffer = f"{buffer} {sentence}"
        else:
            chunks.append(buffer)
            buffer = sentence
    if buffer:
        chunks.append(buffer)
    return chunks


@dataclass
class _Utterance:
    """One queued line of speech plus the event that reports it finished."""

    text: str
    done: threading.Event = field(default_factory=threading.Event)
    #: Locale to speak this line in. ``None`` follows the active conversation locale,
    #: which is what almost every caller wants; the wake daemon overrides it so a Hindi
    #: greeting is read by the Hindi voice even before any conversation has happened.
    locale: Any = None


class VoiceSystem:
    """Wake-word capture and queued, interruptible speech.

    All five callbacks are optional and are invoked from background threads, so
    they must be cheap and thread-safe on the receiving end (``StarkHUD`` is).
    Any exception a callback raises is logged and swallowed — a display fault must
    never take the voice threads down with it.
    """

    def __init__(
        self,
        on_wake: Callable[[], None] | None = None,
        on_utterance: Callable[[str], None] | None = None,
        on_state: Callable[[str], None] | None = None,
        on_amplitude: Callable[[float], None] | None = None,
        on_log: Callable[[str, str], None] | None = None,
    ) -> None:
        self._on_wake = on_wake
        self._wake_cache: list[str] | None = None
        # Guards the streaming-speech buffer, the current-utterance echo check
        # and the chime cache -- all touched from both the TTS worker and the
        # listener thread.
        self._lock = threading.RLock()
        # Streaming speech: partial sentence, and whether the summary has ended.
        self._speech_buffer = ""
        self._speech_pending = ""
        self._speech_closed = False
        self._speech_spoken = 0
        # What is being said right now, so we can recognise our own echo.
        self._current_utterance = ""
        self._chime_cache: dict[str, str] = {}
        self._on_utterance = on_utterance
        self._on_state = on_state
        self._on_amplitude = on_amplitude
        self._on_log = on_log

        self.status = VoiceStatus()
        self._status_lock = threading.Lock()

        # -- TTS plumbing ---------------------------------------------------------
        self._tts_queue: "queue.Queue[_Utterance | None]" = queue.Queue()
        self._tts_thread: threading.Thread | None = None
        self._speaking = threading.Event()
        self._idle = threading.Event()
        self._idle.set()
        self._stop_current = threading.Event()
        self._shutting_down = threading.Event()
        self._engine_ready = threading.Event()
        self._muted = False
        self._suppress_until = 0.0
        self._word_tick = 0.0
        self._player: subprocess.Popen | None = None
        self._player_lock = threading.Lock()
        self._ffplay = shutil.which("ffplay")

        # -- STT plumbing ---------------------------------------------------------
        self._recognizer: Any = None
        self._bg_stopper: Callable[..., Any] | None = None
        self._listen_lock = threading.Lock()
        self._capture_queue: "queue.Queue[str] | None" = None
        self._capture_lock = threading.Lock()
        self._awaiting_command_until = 0.0
        self._mic_index: int | None = None

        # Neither of these may raise: every failure path inside records an error string
        # and leaves the corresponding half of the system switched off.
        self._setup_tts()
        self._setup_stt()

    # ==================================================================================
    # Introspection
    # ==================================================================================

    @property
    def tts_available(self) -> bool:
        """True when something on this machine can actually make a sound."""
        with self._status_lock:
            return self.status.tts_available

    @property
    def stt_available(self) -> bool:
        """True when a microphone was found and calibrated."""
        with self._status_lock:
            return self.status.stt_available

    @staticmethod
    def list_microphones() -> list[tuple[int, str]]:
        """Enumerate input devices as ``(index, name)``. Empty when there are none."""
        if sr is None:
            return []
        try:
            names = sr.Microphone.list_microphone_names()
        except Exception as exc:  # PyAudio raises freely when no host API is present
            logger.warning("Microphone enumeration failed: %s", exc)
            return []
        return [(index, str(name)) for index, name in enumerate(names)]

    # ==================================================================================
    # Setup
    # ==================================================================================

    def _record_error(self, message: str) -> None:
        """Append a degradation reason exactly once, for the HUD and the log."""
        with self._status_lock:
            if message not in self.status.errors:
                self.status.errors.append(message)
        logger.warning("voice: %s", message)

    def _setup_tts(self) -> None:
        """Choose a synthesiser and start the worker thread that will own it."""
        if not settings.VOICE_ENABLED or not settings.TTS_ENABLED:
            with self._status_lock:
                self.status.tts_engine = ENGINE_NONE
            return
        if settings.TTS_ENGINE == ENGINE_NONE:
            with self._status_lock:
                self.status.tts_engine = ENGINE_NONE
            return

        candidate = self._choose_engine()
        with self._status_lock:
            self.status.tts_engine = candidate
            self.status.tts_available = candidate != ENGINE_NONE
        if candidate == ENGINE_NONE:
            self._record_error(
                "no speech engine available (install pyttsx3, or edge-tts plus ffplay)"
            )

        # The worker runs even for the silent engine, so state callbacks keep firing and
        # no caller has to special-case a machine that cannot speak.
        self._tts_thread = threading.Thread(
            target=self._tts_worker, name="jarvis-tts", daemon=True
        )
        self._tts_thread.start()
        # Wait briefly for the *real* construction result so the boot banner is honest
        # about which engine came up rather than which one we hoped for.
        if not self._engine_ready.wait(_ENGINE_INIT_TIMEOUT):
            logger.warning(
                "TTS engine construction still pending after %.0fs", _ENGINE_INIT_TIMEOUT
            )

    def _choose_engine(self) -> str:
        """Resolve ``settings.TTS_ENGINE`` against what is importable right now.

        edge-tts renders to an mp3 and needs an external player; without an ``ffplay``
        binary on PATH it can synthesise perfectly and still be inaudible, so it only
        counts as available when both halves are present.
        """
        wanted = settings.TTS_ENGINE
        edge_ok = edge_tts is not None and self._ffplay is not None
        if wanted == ENGINE_PYTTSX3:
            if pyttsx3 is None:
                self._record_error("pyttsx3 is not installed")
                return ENGINE_NONE
            return ENGINE_PYTTSX3
        if wanted == ENGINE_EDGE:
            if edge_tts is None:
                self._record_error("edge-tts is not installed")
                return ENGINE_NONE
            if self._ffplay is None:
                self._record_error("edge-tts needs an ffplay binary on PATH for playback")
                return ENGINE_NONE
            return ENGINE_EDGE
        # "auto": neural first, and not for the sake of polish. The offline SAPI5 voice
        # set on a stock Windows install is US English only -- no British voice, and no
        # Indic voice at all -- so pyttsx3 physically cannot speak Hindi, Bengali,
        # Telugu, Marathi or Tamil. edge-tts can, and it carries the British timbre the
        # part calls for. pyttsx3 stays the fallback for no network or no player.
        if edge_ok:
            return ENGINE_EDGE
        if pyttsx3 is not None:
            if edge_tts is not None and self._ffplay is None:
                self._record_error(
                    "edge-tts is installed but no player was found; falling back to "
                    "pyttsx3, which cannot speak the Indian languages"
                )
            return ENGINE_PYTTSX3
        return ENGINE_NONE

    def _setup_stt(self) -> None:
        """Build the recogniser and calibrate against room noise, or degrade quietly."""
        if not settings.VOICE_ENABLED or not settings.STT_ENABLED:
            return
        if sr is None:
            self._record_error("SpeechRecognition is not installed; voice input disabled")
            return
        if pyaudio is None:
            self._record_error("PyAudio is not installed; microphone capture unavailable")
            return

        devices = self.list_microphones()
        if not devices:
            self._record_error("no input devices found; voice input disabled")
            return

        index = settings.MIC_INDEX
        if index is not None and all(i != index for i, _ in devices):
            self._record_error(
                f"microphone index {index} does not exist; using the system default"
            )
            index = None
        self._mic_index = index

        try:
            recognizer = sr.Recognizer()
            recognizer.energy_threshold = settings.STT_ENERGY_THRESHOLD
            recognizer.dynamic_energy_threshold = settings.STT_DYNAMIC_ENERGY
            recognizer.pause_threshold = settings.STT_PAUSE_THRESHOLD
            with self._open_microphone() as source:
                # Calibrate once, here. Doing it per-phrase would clip the first word of
                # every command while the recogniser listens to the room instead.
                if settings.STT_AMBIENT_CALIBRATION > 0:
                    recognizer.adjust_for_ambient_noise(
                        source, duration=settings.STT_AMBIENT_CALIBRATION
                    )
        except Exception as exc:
            self._record_error(
                f"microphone unavailable ({exc.__class__.__name__}); voice input disabled"
            )
            return

        name = self._microphone_name(index, devices)
        self._recognizer = recognizer
        with self._status_lock:
            self.status.stt_available = True
            self.status.microphone = name
        logger.info(
            "Microphone ready: %s (energy threshold %.0f)", name, recognizer.energy_threshold
        )

    @staticmethod
    def _microphone_name(index: int | None, devices: list[tuple[int, str]]) -> str:
        """Resolve a friendly device name for the status line."""
        if index is not None:
            for i, label in devices:
                if i == index:
                    return label
        return "system default"

    def _open_microphone(self) -> Any:
        """Construct a fresh ``sr.Microphone`` for the configured device."""
        if self._mic_index is None:
            return sr.Microphone()
        return sr.Microphone(device_index=self._mic_index)

    # ==================================================================================
    # Callback fan-out — all of these run on background threads
    # ==================================================================================

    def _emit_state(self, state: str) -> None:
        if self._on_state is None:
            return
        try:
            self._on_state(state)
        except Exception:
            logger.exception("on_state callback failed for state %r", state)

    def _emit_amplitude(self, value: float) -> None:
        if self._on_amplitude is None:
            return
        try:
            self._on_amplitude(max(0.0, min(1.0, float(value))))
        except Exception:
            logger.exception("on_amplitude callback failed")

    def _log(self, message: str, level: str = "info") -> None:
        logger.log(logging.ERROR if level == "error" else logging.INFO, "voice: %s", message)
        if self._on_log is None:
            return
        try:
            self._on_log(message, level)
        except Exception:
            logger.exception("on_log callback failed")

    # ==================================================================================
    # Text to speech
    # ==================================================================================

    def speak(
        self,
        text: str,
        blocking: bool = False,
        interrupt: bool = False,
        locale: Any = None,
    ) -> None:
        """Queue ``text`` for the speaker.

        Returns the moment the line is queued unless ``blocking`` is set.
        ``interrupt=True`` abandons whatever is being said and drops everything
        still queued — used when the operator talks over J.A.R.V.I.S. The text
        goes through :func:`strip_for_speech` first, so callers may hand over raw
        Markdown without thinking about it.
        """
        if self._shutting_down.is_set():
            return
        spoken = strip_for_speech(text)
        if not spoken:
            return
        if interrupt:
            self.stop_speaking()
        with self._status_lock:
            muted = self._muted
        if muted:
            # Mute silences the speaker, not the microphone: the transcript still carries
            # the reply, we simply do not say it out loud.
            logger.debug("Suppressed speech while muted: %s", spoken[:60])
            return
        if self._tts_thread is None or not self._tts_thread.is_alive():
            return

        item = _Utterance(spoken, locale=locale)
        self._idle.clear()
        self._tts_queue.put(item)
        if blocking:
            # A hard cap derived from the configured speech rate, so a wedged driver can
            # never hang the main thread outright.
            words = max(1, len(spoken.split()))
            budget = words / max(1.0, settings.TTS_RATE / 60.0) * 3.0 + 20.0
            item.done.wait(budget)

    def feed_speech(self, token: str) -> None:
        """Accept a streamed token and speak whole sentences as they complete.

        The difference between an assistant that answers in one second and one that
        answers in eight is entirely here: waiting for the full reply before opening your
        mouth is what makes a voice assistant feel slow.

        Only the opening summary is spoken. The system prompt has him lead with a
        speech-grade paragraph and put tables, code and enumerations below it, so the
        first blank line or Markdown marker ends the spoken portion -- reading a Markdown
        table aloud helps nobody.
        """
        if not settings.SPEAK_STREAMING or not token:
            return
        with self._lock:
            if self._speech_closed:
                return
            self._speech_buffer += str(token)
            buffer = self._speech_buffer

            # Detail has begun; stop feeding the speaker.
            if _DETAIL_MARKER_RE.search(buffer):
                head = _DETAIL_MARKER_RE.split(buffer, maxsplit=1)[0]
                self._speech_buffer = ""
                self._speech_closed = True
                remainder = " ".join(
                    part for part in (self._speech_pending, head.strip()) if part
                ).strip()
                self._speech_pending = ""
                self._speech_spoken = 0
                if remainder:
                    self._queue_sentence(remainder)
                return

            sentences, rest = _split_sentences(buffer)
            if not sentences:
                return
            self._speech_buffer = rest

            # A sentence below the minimum is held back and glued to the next one rather
            # than dropped. Discarding it loses real content -- "नमस्ते सर।" is ten
            # characters and a perfectly good thing to say -- and Hindi sentences are
            # short often enough that a length filter alone silently eats half a reply.
            ready: list[str] = []
            for sentence in sentences:
                candidate = " ".join(
                    part for part in (self._speech_pending, sentence.strip()) if part
                ).strip()
                if len(candidate) >= settings.SPEAK_MIN_SENTENCE:
                    ready.append(candidate)
                    self._speech_pending = ""
                else:
                    self._speech_pending = candidate

        for sentence in ready:
            self._queue_sentence(sentence)

    def flush_speech(self) -> None:
        """Speak whatever is left in the streaming buffer and reset it."""
        with self._lock:
            tail = " ".join(
                part
                for part in (self._speech_pending, self._speech_buffer.strip())
                if part
            ).strip()
            self._speech_buffer = ""
            self._speech_pending = ""
            self._speech_closed = False
            self._speech_spoken = 0
        # No length floor here: this is the last of the reply, and a short closing line
        # ("Done, Sir.") is exactly the sort of thing that must not be swallowed.
        if tail:
            self._queue_sentence(tail)

    def reset_speech_stream(self) -> None:
        """Drop any partial streamed sentence without speaking it."""
        with self._lock:
            self._speech_buffer = ""
            self._speech_pending = ""
            self._speech_closed = False
            self._speech_spoken = 0

    def _queue_sentence(self, sentence: str) -> None:
        """Send one already-complete sentence to the speaker."""
        cleaned = strip_for_speech(sentence, max_chars=0)
        if cleaned:
            self.speak(cleaned)

    def stop_speaking(self) -> None:
        """Abandon the current utterance and drain everything queued behind it."""
        self._stop_current.set()
        drained = 0
        while True:
            try:
                pending = self._tts_queue.get_nowait()
            except queue.Empty:
                break
            if pending is None:
                self._tts_queue.put(None)  # never swallow the shutdown sentinel
                break
            pending.done.set()
            drained += 1
        with self._player_lock:
            player = self._player
        if player is not None and player.poll() is None:
            try:
                player.kill()
            except Exception:
                logger.debug("Could not kill the audio player", exc_info=True)
        if drained:
            logger.debug("Dropped %d queued utterance(s)", drained)
        if not self._speaking.is_set():
            # Nothing in flight to notice the flag, so clear it here rather than leave it
            # armed to silence the next legitimate line.
            self._stop_current.clear()
            self._idle.set()

    def is_speaking(self) -> bool:
        """True while an utterance is actively being rendered."""
        return self._speaking.is_set()

    def wait_until_spoken(self, timeout: float | None = None) -> None:
        """Block until the speech queue is empty (or ``timeout`` elapses)."""
        self._idle.wait(timeout)

    def set_muted(self, muted: bool) -> None:
        """Silence or restore the speaker. The microphone is unaffected."""
        with self._status_lock:
            changed = self._muted != bool(muted)
            self._muted = bool(muted)
        if muted:
            self.stop_speaking()
        if changed:
            self._log("Audio output muted." if muted else "Audio output restored.", "info")

    def is_muted(self) -> bool:
        """True when speech output is suppressed."""
        with self._status_lock:
            return self._muted

    # -- worker ------------------------------------------------------------------------

    def _tts_worker(self) -> None:
        """Own the synthesiser for its whole life and drain the speech queue.

        The engine is built here, on this thread, and never touched from another:
        SAPI5 is apartment threaded, so an engine created on the main thread and
        driven from here deadlocks on the first ``runAndWait``.
        """
        engine: Any = None
        with self._status_lock:
            mode = self.status.tts_engine

        if mode == ENGINE_PYTTSX3:
            engine = self._build_pyttsx3()
            if engine is None:
                # pyttsx3 imported but would not start. Fall back to the neural engine if
                # one is usable, otherwise go silent — but stay running either way.
                mode = ENGINE_EDGE if (edge_tts is not None and self._ffplay) else ENGINE_NONE
                with self._status_lock:
                    self.status.tts_engine = mode
                    self.status.tts_available = mode != ENGINE_NONE
        self._engine_ready.set()

        while True:
            item = self._tts_queue.get()
            if item is None:
                break
            try:
                self._render(item.text, mode, engine, item.locale)
            except Exception:
                logger.exception("Speech rendering failed")
            finally:
                item.done.set()
                self._stop_current.clear()
                if self._tts_queue.empty():
                    self._idle.set()
                    self._emit_state(STATE_IDLE)

        if engine is not None:
            try:
                engine.stop()
            except Exception:
                logger.debug("Engine shutdown complained", exc_info=True)

    def _build_pyttsx3(self) -> Any:
        """Construct and tune the pyttsx3 engine. Returns ``None`` on failure."""
        if pyttsx3 is None:
            return None
        try:
            engine = pyttsx3.init()
        except Exception as exc:
            self._record_error(f"pyttsx3 would not start ({exc.__class__.__name__})")
            return None
        try:
            engine.setProperty("rate", int(settings.TTS_RATE))
            engine.setProperty("volume", float(settings.TTS_VOLUME))
        except Exception:
            logger.debug("Could not apply rate/volume", exc_info=True)
        try:
            chosen = self._select_pyttsx3_voice(engine)
            if chosen:
                logger.info("TTS voice: %s", chosen)
        except Exception:
            logger.debug("Voice selection failed; using the driver default", exc_info=True)
        try:
            # Word events give us genuine speech timing for the waveform. Not every driver
            # emits them, which is why the amplitude pump can stand on its own.
            engine.connect("started-word", self._on_word)
        except Exception:
            logger.debug("Driver does not support word events", exc_info=True)
        return engine

    def _select_pyttsx3_voice(self, engine: Any) -> str:
        """Prefer ``settings.TTS_VOICE_HINT``, then any English voice, then whatever exists."""
        voices = list(engine.getProperty("voices") or [])
        if not voices:
            return ""
        hint = (settings.TTS_VOICE_HINT or "").strip().lower()
        chosen = None
        if hint:
            for voice in voices:
                if hint in str(getattr(voice, "name", "") or "").lower():
                    chosen = voice
                    break
        if chosen is None:
            for voice in voices:
                if self._voice_is_english(voice):
                    chosen = voice
                    break
        if chosen is None:
            chosen = voices[0]
        engine.setProperty("voice", chosen.id)
        return str(getattr(chosen, "name", "") or chosen.id)

    @staticmethod
    def _voice_is_english(voice: Any) -> bool:
        """Best-effort English detection across SAPI5, NSSpeech and espeak metadata."""
        name = str(getattr(voice, "name", "") or "").lower()
        identifier = str(getattr(voice, "id", "") or "").lower()
        if "english" in name or "en-" in identifier or "en_" in identifier:
            return True
        languages: Iterable[Any] = getattr(voice, "languages", None) or ()
        for language in languages:
            if isinstance(language, bytes):
                language = language.decode("utf-8", "replace")
            # SAPI5 prefixes language tags with a length byte; strip it before matching.
            if str(language).lower().lstrip("\x05").startswith("en"):
                return True
        return False

    def _on_word(self, name: str = "", location: int = 0, length: int = 0) -> None:
        """pyttsx3 word event: mark the beat so the waveform kicks on real syllables."""
        self._word_tick = time.monotonic()

    def _render(
        self, text: str, mode: str, engine: Any, locale: Any = None
    ) -> None:
        """Speak one queued utterance, whatever the backend."""
        with self._lock:
            self._current_utterance = str(text)

        self._speaking.set()
        # Deaf for the whole utterance, not merely at the end of it.
        self._suppress_until = float("inf")
        self._emit_state(STATE_SPEAKING)
        pump_stop = threading.Event()
        pump: threading.Thread | None = None
        try:
            if mode in (ENGINE_PYTTSX3, ENGINE_EDGE) and self._on_amplitude is not None:
                pump = threading.Thread(
                    target=self._amplitude_pump,
                    args=(text, pump_stop),
                    name="jarvis-amplitude",
                    daemon=True,
                )
                pump.start()
            if mode == ENGINE_PYTTSX3 and engine is not None:
                self._render_pyttsx3(text, engine)
            elif mode == ENGINE_EDGE:
                self._render_edge(text, locale)
            # ENGINE_NONE renders nothing, but the state callbacks either side of this
            # still fire, so the HUD and every caller behave identically on a mute machine.
        finally:
            pump_stop.set()
            if pump is not None:
                pump.join(timeout=1.0)
            self._speaking.clear()
            self._suppress_until = time.monotonic() + _SELF_HEARING_TAIL
            self._emit_amplitude(0.0)

    def _render_pyttsx3(self, text: str, engine: Any) -> None:
        """Speak in sentence-sized chunks so an interrupt lands within a breath.

        The engine is only ever driven from this thread. Reaching into an
        apartment-threaded COM object from elsewhere to call ``stop()`` is how you
        hang a process, so interruption is cooperative: we check the flag between
        chunks instead.
        """
        for chunk in _chunk_for_speech(text):
            if self._stop_current.is_set() or self._shutting_down.is_set():
                return
            try:
                engine.say(chunk)
                engine.runAndWait()
            except RuntimeError:
                # "run loop already started": the driver is mid-utterance. Unwind its loop
                # and give up on this line rather than fight it.
                logger.debug("pyttsx3 loop was busy; recovering", exc_info=True)
                try:
                    engine.endLoop()
                except Exception:
                    logger.debug("endLoop failed", exc_info=True)
                return
            except Exception:
                logger.exception("pyttsx3 refused to speak a chunk")
                return

    def _render_edge(self, text: str, locale: Any = None) -> None:
        """Synthesise with edge-tts and play the result through ffplay."""
        if edge_tts is None or not self._ffplay:
            return
        workdir = tempfile.mkdtemp(prefix="jarvis-tts-")
        audio_path = os.path.join(workdir, "line.mp3")
        try:
            if not self._edge_render_file(text, audio_path):
                return
            if self._stop_current.is_set() or self._shutting_down.is_set():
                return
            self._play_file(audio_path)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    def _edge_render_file(self, text: str, path: str) -> bool:
        """Run the edge-tts coroutine to completion. True when playable audio exists."""
        import asyncio

        # edge-tts takes rate and volume as percentage offsets from its own baseline,
        # whereas our settings are words-per-minute and a 0..1 gain.
        rate_pct = int(round((settings.TTS_RATE - _EDGE_BASELINE_WPM) / _EDGE_BASELINE_WPM * 100))
        volume_pct = int(round((settings.TTS_VOLUME - 1.0) * 100))

        # The voice must follow the language: a Hindi sentence read by a British English
        # voice is unintelligible, not merely accented.
        try:
            voice_id = (
                locales.voice_for(locale)
                if settings.LOCALE_SPEAK_NATIVE
                else settings.TTS_VOICE
            )
        except Exception:
            voice_id = settings.TTS_VOICE

        async def render() -> None:
            communicate = edge_tts.Communicate(
                text,
                voice_id,
                rate=f"{rate_pct:+d}%",
                volume=f"{volume_pct:+d}%",
                pitch=settings.TTS_PITCH or "+0Hz",
            )
            await communicate.save(path)

        try:
            # A private event loop per utterance: this worker thread has none of its own,
            # and the network round-trip must not touch anyone else's loop.
            asyncio.run(render())
        except Exception as exc:
            self._record_error(f"edge-tts synthesis failed ({exc.__class__.__name__})")
            return False
        try:
            return os.path.getsize(path) > 0
        except OSError:
            return False

    def _play_file(self, path: str) -> None:
        """Play a rendered audio file, killable by :meth:`stop_speaking`."""
        command = [str(self._ffplay), "-nodisp", "-autoexit", "-loglevel", "quiet", path]
        # Windows would otherwise flash a console window on every single sentence.
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=creation_flags,
            )
        except Exception as exc:
            self._record_error(f"ffplay playback failed ({exc.__class__.__name__})")
            return
        with self._player_lock:
            self._player = process
        try:
            while process.poll() is None:
                if self._stop_current.is_set() or self._shutting_down.is_set():
                    try:
                        process.kill()
                    except Exception:
                        logger.debug("Could not kill ffplay", exc_info=True)
                    break
                time.sleep(0.05)
            process.wait(timeout=2.0)
        except Exception:
            logger.debug("Playback wait failed", exc_info=True)
        finally:
            with self._player_lock:
                self._player = None

    def _amplitude_pump(self, text: str, stop: threading.Event) -> None:
        """Drive ``on_amplitude`` with a plausible speech envelope.

        Neither pyttsx3 nor ffplay hands back sample levels, so the waveform is
        synthesised: a syllable-rate oscillation derived from the configured words
        per minute, kicked by each real ``started-word`` event and roughened with
        jitter. It is a fiction, but it is a fiction that moves in time with the voice.
        """
        words = max(1, len(text.split()))
        words_per_second = max(1.0, settings.TTS_RATE / 60.0)
        duration = words / words_per_second
        fps = max(4, min(30, int(settings.HUD_FPS) or 12))
        interval = 1.0 / fps
        syllable_hz = words_per_second * 1.45
        started = time.monotonic()
        level = 0.0
        while not stop.is_set():
            elapsed = time.monotonic() - started
            if elapsed > duration + 5.0:
                break  # the estimate was wrong; stop guessing rather than run forever
            carrier = 0.55 + 0.30 * math.sin(2.0 * math.pi * syllable_hz * elapsed)
            since_word = time.monotonic() - self._word_tick if self._word_tick else 9.9
            kick = 0.35 * math.exp(-since_word * 7.0) if since_word < 1.0 else 0.0
            target = carrier + kick + random.uniform(-0.10, 0.10)
            if elapsed < 0.15:
                target *= elapsed / 0.15  # fade in rather than snapping to full height
            level += (target - level) * 0.55  # one-pole smoothing keeps the bars fluid
            self._emit_amplitude(level)
            stop.wait(interval)
        self._emit_amplitude(0.0)

    # ==================================================================================
    # Speech to text
    # ==================================================================================

    def play_chime(self, kind: str = "wake") -> None:
        """A short rising two-tone, the way an assistant signals it is listening.

        Generated once with numpy and cached as a wav, so there is no asset to ship and
        no network round trip. Silently does nothing if numpy or a player is missing.
        """
        if not settings.WAKE_CHIME:
            return
        try:
            path = self._chime_path(kind)
            if path:
                threading.Thread(
                    target=self._play_file, args=(path,), daemon=True
                ).start()
        except Exception:
            logger.debug("Chime failed", exc_info=True)

    def _chime_path(self, kind: str) -> str | None:
        """Render the chime to a cached wav; returns its path."""
        with self._lock:
            cached = self._chime_cache.get(kind)
        if cached and os.path.exists(cached):
            return cached
        try:
            import numpy as np
        except ImportError:
            return None

        rate = 44100
        # Rising for "listening", falling for "done" -- the same grammar every voice
        # assistant uses, because it reads without being explained.
        tones = [(660.0, 0.09), (990.0, 0.13)] if kind == "wake" else [(880.0, 0.09), (587.0, 0.13)]
        chunks = []
        for frequency, duration in tones:
            samples = int(rate * duration)
            t = np.linspace(0.0, duration, samples, endpoint=False)
            wave = np.sin(2.0 * np.pi * frequency * t)
            # A short raised-cosine envelope; a bare sine start clicks audibly.
            envelope = np.ones(samples)
            edge = max(1, int(samples * 0.25))
            ramp = 0.5 * (1.0 - np.cos(np.linspace(0.0, np.pi, edge)))
            envelope[:edge] = ramp
            envelope[-edge:] = ramp[::-1]
            chunks.append(wave * envelope * 0.28)
        signal = np.concatenate(chunks)
        pcm = (signal * 32767.0).astype("<i2")

        try:
            directory = Path(tempfile.gettempdir()) / "jarvis_chimes"
            directory.mkdir(parents=True, exist_ok=True)
            path = str(directory / f"{kind}.wav")
            with wave_module.open(path, "wb") as handle:
                handle.setnchannels(1)
                handle.setsampwidth(2)
                handle.setframerate(rate)
                handle.writeframes(pcm.tobytes())
        except (OSError, ValueError):
            logger.debug("Could not write the chime", exc_info=True)
            return None

        with self._lock:
            self._chime_cache[kind] = path
        return path

    def start_listening(self) -> bool:
        """Start the background wake-word listener. False when there is no microphone."""
        if not self.stt_available or self._recognizer is None:
            return False
        with self._listen_lock:
            if self._bg_stopper is not None:
                return True
            try:
                microphone = self._open_microphone()
                self._bg_stopper = self._recognizer.listen_in_background(
                    microphone,
                    self._on_background_audio,
                    phrase_time_limit=settings.STT_PHRASE_TIME_LIMIT,
                )
            except Exception as exc:
                self._bg_stopper = None
                self._record_error(
                    f"could not open the microphone stream ({exc.__class__.__name__})"
                )
                with self._status_lock:
                    self.status.stt_available = False
                return False
        self._emit_state(STATE_LISTENING)
        if settings.WAKE_WORD_REQUIRED:
            words = ", ".join(f'"{w}"' for w in settings.WAKE_WORDS) or "(none configured)"
            self._log(f"Listening for {words}.", "info")
        else:
            self._log("Listening — wake word not required.", "info")
        return True

    @property
    def listening(self) -> bool:
        """Whether the background listener currently holds the microphone.

        The window draws its microphone control from this, and a control that
        shows the wrong state is worse than no control: you press it to start
        listening and it stops.
        """
        with self._listen_lock:
            return self._bg_stopper is not None

    def stop_listening(self) -> None:
        """Release the microphone. Idempotent."""
        with self._listen_lock:
            stopper, self._bg_stopper = self._bg_stopper, None
        if stopper is None:
            return
        try:
            # wait_for_stop=False on purpose: the listener is a daemon thread that exits
            # within one phrase, and blocking shutdown for up to STT_PHRASE_TIME_LIMIT
            # seconds because someone happens to be mid-sentence is the worse outcome.
            stopper(wait_for_stop=False)
        except Exception:
            logger.debug("Background listener stopper complained", exc_info=True)
        self._awaiting_command_until = 0.0
        self._emit_state(STATE_IDLE)

    def _is_listening(self) -> bool:
        with self._listen_lock:
            return self._bg_stopper is not None

    def _deaf(self) -> bool:
        """True while we must ignore the microphone — we are the one making the noise."""
        return self._speaking.is_set() or time.monotonic() < self._suppress_until

    def _on_background_audio(self, recognizer: Any, audio: Any) -> None:
        """Background-listener callback: transcribe one phrase and route it.

        Runs on the listener thread with the microphone stream open, so it must never
        block on anything that needs that same stream.
        """
        if self._shutting_down.is_set():
            return

        speaking = self.is_speaking()
        # While talking we are normally deaf, so we do not answer our own voice. Barge-in
        # deliberately lifts that, and _looks_like_self() filters the echo instead.
        if speaking and not settings.BARGE_IN_ENABLED:
            return
        if not speaking and self._deaf():
            return

        text = self._transcribe(recognizer, audio)
        if not text:
            return

        if speaking:
            if not self._maybe_barge_in(text):
                return  # it was our own voice coming back through the microphone

        # A wake word from a stranger is not a wake word.
        if settings.WAKE_WORD_REQUIRED and not self._awaiting_command_until:
            matched, _ = self._split_wake_word(text)
            if matched is not None and not self._verify_speaker(audio):
                return
        with self._capture_lock:
            sink = self._capture_queue
        if sink is not None:
            # listen_once() is waiting for exactly this phrase. Hand it over rather than
            # opening a second input stream on a device we already hold.
            try:
                sink.put_nowait(text)
            except queue.Full:
                logger.debug("Direct capture sink was full; dropping phrase")
            return
        self._route_phrase(text)

    def _transcribe(self, recognizer: Any, audio: Any, language: str | None = None) -> str:
        """Google Web Speech transcription. Returns "" for silence or a network fault."""
        if sr is None:
            return ""
        try:
            return str(
                recognizer.recognize_google(
                    audio, language=language or self._wake_stt_language()
                )
            ).strip()
        except sr.UnknownValueError:
            return ""  # unintelligible: almost always a cough, a chair, or the fan
        except sr.RequestError as exc:
            self._log(f"Speech recognition service unreachable: {exc}", "warn")
            return ""
        except Exception:
            logger.exception("Transcription failed")
            return ""

    def _verify_speaker(self, audio: Any) -> bool:
        """Is this the operator's own voice?

        Fails open in every uncertain case -- no voiceprint enrolled, module unavailable,
        verification error. Being locked out of your own assistant because a cold changed
        your voice would be far worse than the thing this guards against.
        """
        if not settings.SPEAKER_VERIFY_ENABLED:
            return True
        try:
            from jarvis import speaker
        except Exception:
            return True
        try:
            if not speaker.enrolled():
                return True
            result = speaker.verify(audio)
        except Exception:
            logger.debug("Speaker verification faulted; allowing", exc_info=True)
            return True
        if not result.accepted:
            self._log(
                f"Ignored a wake word from an unrecognised voice "
                f"(similarity {result.score:.2f} < {result.threshold:.2f}).",
                "warn",
            )
            logger.info("wake rejected by voiceprint: score=%.3f", result.score)
        return bool(result.accepted)

    def _looks_like_self(self, heard: str) -> bool:
        """Did we just hear ourselves through the speakers?

        Without acoustic echo cancellation the microphone picks up whatever the speakers
        are playing. Comparing the transcript against the words currently being spoken
        catches the overwhelming majority of that, which is what makes barge-in usable at
        all on a laptop with no headset.
        """
        with self._lock:
            current = self._current_utterance
        if not current or not heard:
            return False
        spoken = set(normalise_wake(current).split())
        picked = set(normalise_wake(heard).split())
        if not picked:
            return False
        overlap = len(spoken & picked) / len(picked)
        return overlap >= 0.6

    def _maybe_barge_in(self, heard: str) -> bool:
        """Stop talking because the operator started. True when we did."""
        if not settings.BARGE_IN_ENABLED or not self.is_speaking():
            return False
        if self._looks_like_self(heard):
            return False
        self.stop_speaking()
        self._log("Interrupted.", "info")
        return True

    def arm_follow_up(self, seconds: float | None = None) -> None:
        """Accept the next phrase as a command without a second call word.

        This is what makes a conversation feel like a conversation rather than a series
        of unrelated commands each prefixed by his name.
        """
        if not settings.CONTINUED_CONVERSATION:
            return
        window = settings.FOLLOW_UP_SECONDS if seconds is None else seconds
        if window > 0:
            self._awaiting_command_until = time.monotonic() + window
            self._emit_state(STATE_LISTENING)

    def _route_phrase(self, text: str) -> None:
        """Apply the wake-word policy to a freshly transcribed phrase."""
        now = time.monotonic()
        if self._awaiting_command_until and now <= self._awaiting_command_until:
            # We already answered the wake word; this phrase is the command itself.
            self._awaiting_command_until = 0.0
            _, remainder = self._split_wake_word(text)
            self._deliver(remainder or text)
            return
        self._awaiting_command_until = 0.0

        if not settings.WAKE_WORD_REQUIRED:
            self._deliver(text)
            return

        matched, remainder = self._split_wake_word(text)
        if matched is None:
            logger.debug("Ignored (no wake word): %s", text)
            return
        if remainder:
            self._deliver(remainder)
        else:
            self._acknowledge_wake()

    def _stt_language(self) -> str:
        """Recognition language for a *command*, following the conversation locale."""
        try:
            return locales.stt_code()
        except Exception:
            return settings.STT_LANGUAGE

    @staticmethod
    def _wake_stt_language() -> str:
        """Recognition language for *wake detection*, which is deliberately fixed.

        Measured on this machine by feeding synthesised speech back through the
        recogniser: ``en-IN`` transcribes both English and spoken Hindi (the latter as
        romanised Hinglish -- "namaste Jarvis Mera system kaisa"), and every call phrase
        matches. ``hi-IN`` does the reverse badly: it renders "Hello Jarvis" as
        "हेलो जार्विस" and the English call word stops working entirely.

        So wake detection does not follow the conversation locale. Commands still do.
        """
        return settings.STT_WAKE_LANGUAGE or "en-IN"

    def _wake_candidates(self) -> list[str]:
        """Every phrase that wakes him, expanded once and cached.

        The call words are "Hello J.A.R.V.I.S." and "Namaste, J.A.R.V.I.S.", but a
        recogniser never returns the dots -- and it mishears the name in a small,
        predictable set of ways. So each greeting is expanded across every known
        spelling of the name, and the whole set is normalised exactly the way the
        incoming phrase will be.
        """
        if self._wake_cache is not None:
            return self._wake_cache

        phrases: list[str] = []
        try:
            configured = locales.wake_vocabulary()
        except Exception:
            configured = [str(w) for w in settings.WAKE_WORDS]

        variants = [normalise_wake(v) for v in settings.WAKE_NAME_VARIANTS]
        variants = [v for v in variants if v]

        for raw in configured:
            phrase = normalise_wake(raw)
            if not phrase:
                continue
            if phrase not in phrases:
                phrases.append(phrase)
            # "hello jarvis" also becomes "hello jervis", "hello javis", and so on.
            parts = phrase.split()
            if len(parts) >= 2:
                greeting, _name = " ".join(parts[:-1]), parts[-1]
                for variant in variants:
                    candidate = greeting + " " + variant
                    if candidate not in phrases:
                        phrases.append(candidate)

        if settings.WAKE_ALLOW_BARE_NAME:
            for variant in variants:
                if variant not in phrases:
                    phrases.append(variant)

        # Longest first so a full greeting always beats a prefix of it.
        phrases.sort(key=len, reverse=True)
        self._wake_cache = phrases
        return phrases

    def _split_wake_word(self, text: str) -> tuple[str | None, str]:
        """Find a call phrase and return ``(matched, remainder)``.

        Matching runs against a normalised copy while the remainder is recovered from
        the original, so the operator's own punctuation survives into the command that
        reaches the agent.
        """
        flat = normalise_wake(text)
        if flat:
            for wake in self._wake_candidates():
                index = flat.find(wake)
                if index < 0:
                    continue
                # Whole-word hits only; "jarvisland" is not a summons.
                after = index + len(wake)
                if after < len(flat) and flat[after] != " ":
                    continue
                if index > 0 and flat[index - 1] != " ":
                    continue
                remainder = (flat[:index] + " " + flat[after:]).strip()
                remainder = _WS_RE.sub(" ", remainder).strip(_TRIM_CHARS)
                return wake, _recover_tail(text, remainder)

        for wake in sorted(settings.WAKE_WORDS, key=len, reverse=True):
            wake = wake.strip()
            if not wake:
                continue
            # The transcriber punctuates as it pleases, so allow commas between words.
            pattern = re.compile(
                r"\b" + r"[\s,]+".join(re.escape(part) for part in wake.split()) + r"\b",
                re.IGNORECASE,
            )
            match = pattern.search(text)
            if match is None:
                continue
            remainder = f"{text[: match.start()]} {text[match.end():]}"
            remainder = re.sub(r"\s+", " ", remainder).strip().strip(_TRIM_CHARS)
            return wake, remainder
        return None, text

    def _acknowledge_wake(self) -> None:
        """Bare wake word: signal the caller, then take the next phrase as the command."""
        self._emit_state(STATE_LISTENING)
        self.play_chime("wake")
        if self._on_wake is not None:
            try:
                self._on_wake()
            except Exception:
                logger.exception("on_wake callback failed")

        if self._is_listening():
            # The background listener already holds the microphone and will deliver the
            # very next phrase, so arming this window *is* the follow-up capture. Calling
            # listen_once() from this thread would deadlock the listener against itself,
            # since we are running inside its own callback.
            self._awaiting_command_until = time.monotonic() + _WAKE_FOLLOW_UP_WINDOW
            return
        # No background listener (something drove _route_phrase directly): take one
        # explicit capture off-thread so we never block whoever called us.
        threading.Thread(
            target=self._follow_up_capture, name="jarvis-followup", daemon=True
        ).start()

    def _follow_up_capture(self) -> None:
        """One-shot command capture, used when no background listener is running."""
        command = self.listen_once(timeout=_WAKE_FOLLOW_UP_WINDOW)
        if command:
            self._deliver(command)

    def _deliver(self, text: str) -> None:
        """Hand a finished command to the application."""
        text = (text or "").strip()
        if not text:
            return
        logger.info("Heard: %s", text)
        if self._on_utterance is None:
            return
        try:
            self._on_utterance(text)
        except Exception:
            logger.exception("on_utterance callback failed")

    def listen_once(
        self, timeout: float = 8.0, phrase_time_limit: float | None = None
    ) -> str | None:
        """Capture and transcribe exactly one phrase. ``None`` on silence or failure.

        Wake words are not required here — this is the explicit "I am asking you a
        question right now" path used by onboarding and by follow-up capture.
        """
        if not self.stt_available or self._recognizer is None:
            return None
        if self._shutting_down.is_set():
            return None
        # Never record over ourselves: let the speaker finish, then let the room settle.
        if self._speaking.is_set():
            self.wait_until_spoken(timeout=min(timeout, 30.0))
        remaining = self._suppress_until - time.monotonic()
        if 0.0 < remaining <= _SELF_HEARING_TAIL:
            time.sleep(remaining)

        self._emit_state(STATE_LISTENING)
        try:
            if self._is_listening():
                return self._listen_via_background(timeout)
            return self._listen_direct(timeout, phrase_time_limit)
        finally:
            self._emit_state(STATE_LISTENING if self._is_listening() else STATE_IDLE)

    def _listen_via_background(self, timeout: float) -> str | None:
        """Borrow the next phrase from the already-running listener.

        Two simultaneous input streams on one device is a coin toss across host APIs,
        so we reuse the stream we already have instead of opening a second one.
        """
        sink: "queue.Queue[str]" = queue.Queue(maxsize=1)
        with self._capture_lock:
            self._capture_queue = sink
        try:
            return sink.get(timeout=max(0.5, timeout))
        except queue.Empty:
            return None
        finally:
            with self._capture_lock:
                self._capture_queue = None

    def _listen_direct(self, timeout: float, phrase_time_limit: float | None) -> str | None:
        """Open the microphone ourselves for a single phrase."""
        limit = (
            phrase_time_limit
            if phrase_time_limit is not None
            else settings.STT_PHRASE_TIME_LIMIT
        )
        try:
            with self._open_microphone() as source:
                audio = self._recognizer.listen(
                    source, timeout=max(0.5, timeout), phrase_time_limit=limit
                )
        except Exception as exc:
            if sr is not None and isinstance(exc, sr.WaitTimeoutError):
                return None  # nobody said anything; entirely normal
            logger.warning("Single-phrase capture failed: %s", exc)
            return None
        return self._transcribe(self._recognizer, audio) or None

    # ==================================================================================
    # Teardown
    # ==================================================================================

    def shutdown(self) -> None:
        """Release the microphone, silence the speaker and retire the worker thread."""
        if self._shutting_down.is_set():
            return
        self._shutting_down.set()
        try:
            self.stop_listening()
        except Exception:
            logger.debug("stop_listening failed during shutdown", exc_info=True)
        try:
            self.stop_speaking()
        except Exception:
            logger.debug("stop_speaking failed during shutdown", exc_info=True)
        self._tts_queue.put(None)  # sentinel: retires the worker after the current line
        thread = self._tts_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=5.0)
            if thread.is_alive():
                logger.warning("TTS worker did not retire within the timeout")
        self._tts_thread = None
        with self._lock:
            self._current_utterance = ""
        self._emit_amplitude(0.0)
        self._emit_state(STATE_IDLE)
