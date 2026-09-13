"""The J.A.R.V.I.S. reasoning core: a ReAct agent loop over an Ollama model.

This module owns exactly three things — the shape of a reply, the conversation
history, and the think/act/observe loop that turns an utterance into an answer.
It deliberately knows nothing about how that answer is displayed or spoken: the
HUD, the voice system, the protocol engine and the ambient monitor all arrive as
constructor arguments, are duck-typed, and are always allowed to be ``None``.
That keeps the import graph acyclic and makes the agent testable with no I/O.

The deployment this was written against::

    Ollama 0.33, model ``gemma4:31b-cloud``
    capabilities: completion, thinking, tools, vision
    tool calls arrive *streamed*, and their ``arguments`` are a JSON **object**

Other builds hand back ``arguments`` as a JSON *string*, and older clients hand
back plain dicts instead of pydantic response objects, so every read of a
response goes through :func:`_field` and every argument blob through
:func:`_coerce_arguments`. Being liberal here costs a few lines and buys
immunity to the next client release.
"""

from __future__ import annotations

import json
import logging
import platform as platform_mod
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from config import (
    STATE_IDLE,
    STATE_SPEAKING,
    STATE_THINKING,
    STATE_WORKING,
    settings,
)
from jarvis.prompts import TOOL_ERROR_TEMPLATE, build_system_prompt, personalise
from jarvis import languages, locales
from jarvis.tools import ToolRegistry, ToolResult

try:  # ollama is a hard requirement, but importing this module must never explode
    import ollama
except ImportError:  # pragma: no cover - only on a broken install
    ollama = None  # type: ignore[assignment]

try:
    import httpx
except ImportError:  # pragma: no cover - httpx ships as an ollama dependency
    httpx = None  # type: ignore[assignment]

__all__ = ["ToolCallRecord", "AgentReply", "ConversationMemory", "JarvisAgent"]

_LOG = logging.getLogger(__name__)

_INSTALL_HINT = (
    "the Ollama Python client is not installed. Run: pip install -r requirements.txt"
)

# Exception classes are resolved once, defensively: which of these exist depends on
# the installed client version, and `except ()` simply never matches anything.
_RESPONSE_ERRORS: tuple[type[BaseException], ...] = tuple(
    exc
    for exc in (
        getattr(ollama, "ResponseError", None),
        getattr(ollama, "RequestError", None),
    )
    if isinstance(exc, type) and issubclass(exc, BaseException)
)

_CONNECTION_ERRORS: tuple[type[BaseException], ...] = tuple(
    dict.fromkeys(
        exc
        for exc in (
            getattr(httpx, "ConnectError", None),
            getattr(httpx, "ConnectTimeout", None),
            getattr(httpx, "ReadTimeout", None),
            getattr(httpx, "TimeoutException", None),
            getattr(httpx, "HTTPError", None),
            ConnectionError,
            TimeoutError,
            OSError,
        )
        if isinstance(exc, type) and issubclass(exc, BaseException)
    )
)

# --------------------------------------------------------------------------------------
# Speech shaping. jarvis.voice owns the full `strip_for_speech`, but core.py may not
# import it (voice arrives as a constructor argument), so AgentReply.spoken carries its
# own small, self-contained summariser. A little duplication is cheaper than a cycle.
# --------------------------------------------------------------------------------------
_FENCE_RE = re.compile(r"```.*?```", re.S)
_INLINE_CODE_RE = re.compile(r"`([^`]*)`")
_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_URL_RE = re.compile(r"https?://\S+")
_EMPHASIS_RE = re.compile(r"(\*\*|\*|__|~~)")
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s*")
_BULLET_RE = re.compile(r"^\s*(?:[-*+•]|\d{1,2}[.)])\s+")
_TABLE_RE = re.compile(r"^\s*\|")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")

_SUMMARY_SENTENCES = 3
_AMBIENT_NOTE_CHARS = 280
_TOOL_PREVIEW_CHARS = 400


def _field(obj: Any, name: str, default: Any = None) -> Any:
    """Read ``name`` off an ollama response object *or* a plain dict.

    The client returns pydantic models on 0.4+ and dicts on older releases, and
    several fields (``content``, ``thinking``, ``tool_calls``) are legitimately
    ``None`` mid-stream. One accessor covers all three cases.
    """
    if obj is None:
        return default
    if isinstance(obj, Mapping):
        value = obj.get(name, default)
    else:
        value = getattr(obj, name, default)
    return default if value is None else value


def _coerce_arguments(value: Any) -> tuple[dict[str, Any], Any]:
    """Normalise a tool-call ``arguments`` blob.

    Returns ``(display_dict, call_payload)``. gemma4:31b-cloud hands back a JSON
    object; other models hand back a JSON string. When a string will not parse we
    keep the original for ``ToolRegistry.call`` — its contract accepts either form
    and it may well salvage what we could not.
    """
    if value is None:
        return {}, {}
    if isinstance(value, Mapping):
        parsed = {str(k): v for k, v in value.items()}
        return parsed, parsed
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return {}, {}
        try:
            decoded = json.loads(text)
        except (ValueError, TypeError):
            return {}, value  # let the registry try; it never raises
        if isinstance(decoded, Mapping):
            parsed = {str(k): v for k, v in decoded.items()}
            return parsed, parsed
        return {}, value
    return {}, value


def _normalise_tool_call(raw: Any) -> tuple[str, dict[str, Any], Any, str | None]:
    """Flatten one tool call into ``(name, display_args, call_payload, call_id)``."""
    function = _field(raw, "function", {})
    name = str(_field(function, "name", "") or _field(raw, "name", "") or "").strip()
    raw_args = _field(function, "arguments", None)
    if raw_args is None:
        raw_args = _field(raw, "arguments", None)
    display, payload = _coerce_arguments(raw_args)
    call_id = _field(raw, "id", None) or _field(function, "id", None)
    return name, display, payload, (str(call_id) if call_id else None)


def _clean_inline(text: str) -> str:
    """Strip inline markup so a sentence reads well out loud."""
    text = _LINK_RE.sub(r"\1", text)
    text = _URL_RE.sub("a link", text)
    text = _INLINE_CODE_RE.sub(r"\1", text)
    text = _EMPHASIS_RE.sub("", text)
    return " ".join(text.split())


def _summarise_for_speech(text: str, max_chars: int, max_sentences: int) -> str:
    """Return the leading prose of ``text``, trimmed to whole sentences.

    The system prompt asks the model to lead with a spoken-grade summary and put
    the tables and code below it, so "everything before the first block element"
    is almost always exactly the right thing to say aloud.
    """
    body = _FENCE_RE.sub(" ", text or "")
    lead_lines: list[str] = []
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line:
            if lead_lines:
                break  # a blank line closes the lead paragraph
            continue
        if line.startswith("```") or _TABLE_RE.match(line) or line.startswith(">"):
            if lead_lines:
                break
            line = line.lstrip("`>| ").strip()
        if _BULLET_RE.match(line):
            if lead_lines:
                break
            line = _BULLET_RE.sub("", line)  # a lone leading bullet: keep the words
        if _HEADING_RE.match(line):
            if lead_lines:
                break
            line = _HEADING_RE.sub("", line)
        if line:
            lead_lines.append(line)

    lead = _clean_inline(" ".join(lead_lines)) or _clean_inline(body)
    if not lead:
        return ""

    kept: list[str] = []
    used = 0
    for sentence in _SENTENCE_SPLIT_RE.split(lead)[:max_sentences]:
        if kept and used + len(sentence) > max_chars:
            break
        kept.append(sentence)
        used += len(sentence) + 1
    spoken = " ".join(kept).strip() or lead
    if len(spoken) > max_chars:
        spoken = spoken[:max_chars].rsplit(" ", 1)[0].rstrip(",;:") + "..."
    return spoken


def _truncate(text: str, limit: int) -> str:
    """Shorten ``text`` for a log line without losing the shape of it."""
    text = text or ""
    return text if len(text) <= limit else text[: max(0, limit - 3)].rstrip() + "..."


def _same_model(wanted: str, available: str) -> bool:
    """Compare two model references, treating a bare name as ``name:latest``."""
    a, b = wanted.strip().lower(), available.strip().lower()
    if a == b:
        return True
    a = a[: -len(":latest")] if a.endswith(":latest") else a
    b = b[: -len(":latest")] if b.endswith(":latest") else b
    return a == b


# --------------------------------------------------------------------------------------
# Data carriers
# --------------------------------------------------------------------------------------
@dataclass
class ToolCallRecord:
    """One instrument reading: what was called, with what, and what came back."""

    name: str
    arguments: dict
    result: ToolResult
    iteration: int


@dataclass
class AgentReply:
    """The complete outcome of a single turn — display copy and speech both."""

    text: str
    thinking: str = ""
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    iterations: int = 0
    error: str | None = None
    #: True when the error has already been rendered to the HUD by the agent itself.
    surfaced: bool = False
    duration: float = 0.0

    @property
    def spoken(self) -> str:
        """The leading summary sentences, stripped of markup and safe to speak."""
        source = self.text.strip() or (self.error or "")
        return _summarise_for_speech(source, settings.TTS_MAX_CHARS, _SUMMARY_SENTENCES)

    @property
    def used_tools(self) -> bool:
        """True when at least one instrument was consulted during the turn."""
        return bool(self.tool_calls)


# --------------------------------------------------------------------------------------
# Conversation history
# --------------------------------------------------------------------------------------
class ConversationMemory:
    """Rolling message history that never splits an assistant/tool pair.

    Ollama rejects — or worse, silently misreads — a ``tool`` message with no
    preceding assistant ``tool_calls``. So trimming works in whole exchanges: we cut
    from the front at ``user`` boundaries only, which keeps every call paired with
    its result no matter how aggressive the cap is.
    """

    def __init__(self, max_messages: int | None = None) -> None:
        limit = max_messages if max_messages else settings.HISTORY_MAX_MESSAGES
        self._max = max(2, int(limit))
        self._messages: list[dict] = []
        self._lock = threading.RLock()

    def add(self, role: str, content: str, **extra: Any) -> None:
        """Append a simple message; ``extra`` is merged into the payload."""
        message: dict[str, Any] = {"role": role, "content": content}
        message.update(extra)
        self.add_raw(message)

    def add_raw(self, message: dict) -> None:
        """Append a pre-built message dict (assistant tool_calls, tool results...)."""
        with self._lock:
            self._messages.append(dict(message))
            self._trim()

    def messages(self, system_prompt: str | None = None) -> list[dict]:
        """Return the wire-ready message list, optionally led by a system prompt."""
        with self._lock:
            history = [dict(m) for m in self._messages]
        if system_prompt:
            return [{"role": "system", "content": system_prompt}, *history]
        return history

    def clear(self) -> None:
        """Forget everything. CLEAN SLATE and ``/clear`` both land here."""
        with self._lock:
            self._messages.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._messages)

    # -- internals ---------------------------------------------------------------
    def _trim(self) -> None:
        """Drop whole oldest exchanges until the history fits the cap."""
        while len(self._messages) > self._max:
            cut = self._next_user_index(1)
            if cut is None:
                # A single exchange alone exceeds the cap (a long tool chain). Keep
                # the newest slice, then discard any tool message left without a call.
                del self._messages[: len(self._messages) - self._max]
                self._drop_orphan_head()
                return
            del self._messages[:cut]
        self._drop_orphan_head()

    def _next_user_index(self, start: int) -> int | None:
        """Index of the next ``user`` turn at or after ``start``, if there is one."""
        for index in range(start, len(self._messages)):
            if self._messages[index].get("role") == "user":
                return index
        return None

    def _drop_orphan_head(self) -> None:
        """Never let the history begin with a tool result whose call is gone."""
        while self._messages and self._messages[0].get("role") == "tool":
            self._messages.pop(0)


@dataclass
class _Turn:
    """Internal result of one round-trip to the model."""

    content: str = ""
    thinking: str = ""
    tool_calls: list[Any] = field(default_factory=list)


# --------------------------------------------------------------------------------------
# The agent
# --------------------------------------------------------------------------------------
class JarvisAgent:
    """Think, act, observe — the loop that makes the model useful.

    Every collaborator is optional and duck-typed. With none of them bound this is
    a perfectly serviceable headless agent; with all of them bound it drives the
    HUD, narrates itself, and can fire Stark protocols.
    """

    def __init__(
        self,
        registry: ToolRegistry,
        hud: Any = None,
        voice: Any = None,
        protocol_engine: Any = None,
        monitor: Any = None,
    ) -> None:
        self.registry = registry
        self.hud = hud
        self.voice = voice
        self.protocol_engine = protocol_engine
        self.monitor = monitor
        self.memory = ConversationMemory()

        self._client: Any = None
        self._client_lock = threading.Lock()
        self._turn_lock = threading.Lock()
        # System-prompt cache, keyed by (locale, toolchain summary).
        # Streaming speech bookkeeping for the current turn.
        self._streamed_speech = False
        self._speak_streaming = False
        self._prompt_cache: str | None = None
        self._prompt_cache_key: tuple[str, str] | None = None
        self._toolchain_summary: str | None = None
        self._busy = threading.Event()
        self._cancel = threading.Event()
        # Flipped off permanently if the server rejects the `think` parameter, so a
        # non-thinking build costs exactly one wasted request per process.
        self._think_supported = True
        self._ambient_seen: deque[str] = deque(maxlen=8)
        self._model_ready = False

    # -- collaborator plumbing ---------------------------------------------------
    def _hud_call(self, method: str, *args: Any, **kwargs: Any) -> None:
        """Invoke a HUD method if one is bound. A broken display never kills a turn."""
        hud = self.hud
        if hud is None:
            return
        func = getattr(hud, method, None)
        if not callable(func):
            return
        try:
            func(*args, **kwargs)
        except Exception:
            _LOG.debug("HUD.%s failed", method, exc_info=True)

    def _voice_call(self, method: str, *args) -> bool:
        """Invoke an optional voice-system method. True when it actually ran."""
        voice = self.voice
        if voice is None or not getattr(voice, "tts_available", False):
            return False
        func = getattr(voice, method, None)
        if not callable(func):
            return False
        try:
            func(*args)
            return True
        except Exception:
            _LOG.debug("voice.%s failed", method, exc_info=True)
            return False

    def _speak(self, text: str, interrupt: bool = False) -> None:
        """Hand a line to the voice system, if one is bound and able to speak."""
        voice = self.voice
        if voice is None or not text:
            return
        speak = getattr(voice, "speak", None)
        if not callable(speak):
            return
        if not getattr(voice, "tts_available", True):
            return
        try:
            speak(text, blocking=False, interrupt=interrupt)
        except Exception:
            _LOG.debug("voice.speak failed", exc_info=True)

    def _protocol_names(self) -> list[str]:
        """Protocol keys for the system prompt; an unbound engine means none."""
        engine = self.protocol_engine
        if engine is None:
            return []
        try:
            return list(engine.names())
        except Exception:
            _LOG.debug("protocol_engine.names failed", exc_info=True)
            return []

    def _get_client(self) -> Any:
        """Lazily construct the Ollama client (no network work at import time)."""
        with self._client_lock:
            if self._client is None:
                if ollama is None:
                    raise RuntimeError(_INSTALL_HINT)
                try:
                    self._client = ollama.Client(
                        host=settings.OLLAMA_HOST, timeout=settings.OLLAMA_TIMEOUT
                    )
                except TypeError:  # very old clients accept no httpx keyword arguments
                    self._client = ollama.Client(host=settings.OLLAMA_HOST)
            return self._client

    def aside(self, instruction: str, *, system: str = "", temperature: float = 0.95,
              limit: int = 120, timeout: float | None = None) -> str:
        """Ask the model one question, outside the conversation.

        No tools, no streaming, and nothing written to memory: the reply is not
        part of what the operator is talking about and must not turn up in the
        next turn's context. Used for the things J.A.R.V.I.S. says rather than
        answers \u2014 the greeting the window opens on, to begin with.

        Returns "" for every failure. A model that is not running is a perfectly
        ordinary state of affairs, and the callers all have something to say
        without it.
        """
        messages: list[dict[str, Any]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": instruction})

        try:
            client = self._get_client()
            response = client.chat(
                model=settings.MODEL_NAME,
                messages=messages,
                stream=False,
                options={
                    # Warmer than a normal turn on purpose: this is asked for a
                    # line that has not been said before, and the default
                    # temperature returns the same three sentences forever.
                    "temperature": temperature,
                    "num_predict": limit,
                },
            )
        except Exception:
            _LOG.debug("aside failed", exc_info=True)
            return ""

        message = _field(response, "message") or {}
        return str(_field(message, "content") or "").strip()

    # -- preflight ---------------------------------------------------------------
    def ensure_model(self) -> tuple[bool, str]:
        """Check the daemon is up and the configured model is installed.

        Returns ``(ok, message)`` where the message is always fit to show the
        operator — including the exact command that fixes it when it is not ok.
        """
        title = settings.USER_TITLE
        if ollama is None:
            self._model_ready = False
            return False, f"{title}, {_INSTALL_HINT}"
        try:
            listing = self._get_client().list()
        except _RESPONSE_ERRORS as exc:
            self._model_ready = False
            return False, (
                f"{title}, the Ollama daemon at {settings.OLLAMA_HOST} refused the "
                f"request: {self._describe_response_error(exc)}. "
                "Start it with: ollama serve"
            )
        except _CONNECTION_ERRORS as exc:
            self._model_ready = False
            return False, (
                f"{title}, I cannot reach the Ollama daemon at {settings.OLLAMA_HOST} "
                f"({exc.__class__.__name__}). Start it with: ollama serve"
            )
        except Exception as exc:
            self._model_ready = False
            _LOG.exception("ensure_model failed unexpectedly")
            return False, f"{title}, the model check failed unexpectedly: {exc}"

        available = self._model_names(listing)
        wanted = settings.MODEL_NAME
        if any(_same_model(wanted, name) for name in available):
            self._model_ready = True
            return True, (
                f"{wanted} is loaded on {settings.OLLAMA_HOST} and standing by, {title}."
            )

        self._model_ready = False
        shortlist = ", ".join(available[:8]) if available else "none"
        return False, (
            f"{title}, the daemon is running but {wanted} is not installed. "
            f"Pull it with: ollama pull {wanted}. Models present: {shortlist}."
        )

    @staticmethod
    def _model_names(listing: Any) -> list[str]:
        """Extract model names from a ``client.list()`` result of any vintage."""
        names: list[str] = []
        for entry in _field(listing, "models", []) or []:
            name = _field(entry, "model", "") or _field(entry, "name", "")
            if name:
                names.append(str(name))
        return names

    @staticmethod
    def _describe_response_error(exc: BaseException) -> str:
        """Pull the human half out of an ``ollama.ResponseError``."""
        detail = str(_field(exc, "error", "") or exc).strip()
        status = _field(exc, "status_code", None)
        if status:
            return f"{detail} (HTTP {status})"
        return detail or exc.__class__.__name__

    def system_prompt(self) -> str:
        """Render the system prompt for the current locale and toolchain set.

        Cached per ``(locale, toolchain summary)``: probing three dozen compilers on
        every turn would be absurd, but a stale English instruction after the operator
        switches to Hindi defeats the whole feature -- so the cache key carries both.
        """
        try:
            locale = locales.active()
            locale_code = locale.code
            instruction = locales.instruction_for(locale)
        except Exception:
            _LOG.debug("Locale lookup failed; using the default instruction", exc_info=True)
            locale_code, instruction = "en", ""

        if self._toolchain_summary is None:
            try:
                self._toolchain_summary = languages.toolchain_summary()
            except Exception:
                _LOG.debug("Toolchain probe failed", exc_info=True)
                self._toolchain_summary = ""

        key = (locale_code, self._toolchain_summary)
        if self._prompt_cache_key != key or self._prompt_cache is None:
            self._prompt_cache = build_system_prompt(
                self._protocol_names(),
                platform_mod.platform(),
                toolchains=self._toolchain_summary,
                locale_instruction=instruction,
            )
            self._prompt_cache_key = key
        return self._prompt_cache

    def _sync_locale(self, text: str) -> None:
        """Follow the operator into whatever language they just used."""
        if not settings.LOCALE_AUTO_DETECT:
            return
        if (settings.RESPONSE_LOCALE or "auto").strip().lower() != "auto":
            return
        try:
            detected = locales.detect_locale(text)
            if detected != locales.active().code:
                locales.set_active(detected)
        except Exception:
            _LOG.debug("Locale sync failed", exc_info=True)

    # -- the loop ----------------------------------------------------------------
    def chat(self, user_text: str, speak: bool = True) -> AgentReply:
        """Run one full ReAct turn: reason, call tools, reason again, answer.

        Tool calls are executed and fed back until the model stops asking for them
        or ``settings.MAX_TOOL_ITERATIONS`` is reached, at which point one final
        tool-free request forces a prose conclusion rather than a dangling loop.
        Every failure path returns an ``AgentReply`` with ``error`` set to a
        spoken-safe apology; nothing here raises at the caller.
        """
        text = (user_text or "").strip()
        if not text:
            return AgentReply(text="")

        # Do this before the system prompt is built for this turn, so a switch into
        # Hindi takes effect on THIS reply rather than the next one.
        self._sync_locale(text)

        if not self._turn_lock.acquire(blocking=False):
            # One thread drives the agent by design; a second caller is told so
            # rather than being allowed to interleave two tool loops.
            busy_line = (
                f"One moment, {settings.USER_TITLE}. I am still working on the "
                "previous request."
            )
            return AgentReply(text=busy_line, error=busy_line)

        started = time.monotonic()
        self._busy.set()
        self._cancel.clear()
        # Streaming speech is only worth it when we are actually going to speak.
        self._speak_streaming = bool(speak and settings.SPEAK_STREAMING)
        self._streamed_speech = False
        self._voice_call("reset_speech_stream")
        records: list[ToolCallRecord] = []
        thoughts: list[str] = []
        last_interim = ""
        final_text = ""
        error: str | None = None
        iterations = 0

        try:
            self.memory.add("user", text)
            self._hud_call("set_state", STATE_THINKING)
            prompt = self.system_prompt()
            schemas = self._tool_schemas()

            for iteration in range(1, max(1, settings.MAX_TOOL_ITERATIONS) + 1):
                iterations = iteration
                if self._cancel.is_set():
                    error = f"Stopped at your word, {settings.USER_TITLE}."
                    break

                self._hud_call("set_state", STATE_THINKING)
                try:
                    turn = self._run_turn(self.memory.messages(prompt), schemas)
                except _RESPONSE_ERRORS as exc:
                    error = self._model_error_line(exc)
                    break
                except _CONNECTION_ERRORS as exc:
                    error = self._connection_error_line(exc)
                    break
                except RuntimeError as exc:
                    error = f"{settings.USER_TITLE}, {exc}"
                    break
                except Exception as exc:
                    _LOG.exception("Model turn failed")
                    error = (
                        f"My apologies, {settings.USER_TITLE}. The reasoning core "
                        f"faulted: {exc}"
                    )
                    break

                if turn.thinking:
                    thoughts.append(turn.thinking)
                    self._hud_call("log_thought", turn.thinking)

                if not turn.tool_calls:
                    final_text = turn.content.strip()
                    if final_text:
                        self.memory.add("assistant", final_text)
                    break

                # Keep the narration that came with the tool call. Some models say
                # everything worth saying here ("I'll open Notepad for you") and then
                # return an empty final turn; without this the only prose we got is
                # discarded and a successful action is reported as a failure.
                if turn.content.strip():
                    last_interim = turn.content.strip()

                # The assistant message goes back verbatim in shape, tool calls and
                # all, or the model loses track of what it just asked for.
                self.memory.add_raw(self._assistant_message(turn))
                self._hud_call("set_state", STATE_WORKING)
                records.extend(self._execute_tool_calls(turn.tool_calls, iteration))

                if self._cancel.is_set():
                    error = f"Stopped at your word, {settings.USER_TITLE}."
                    break
            else:
                # Iteration budget spent with the model still reaching for tools.
                final_text, error = self._force_conclusion(prompt)
                if final_text:
                    self.memory.add("assistant", final_text)

            if not final_text and not error:
                # The work may well have been done even though the model had nothing
                # further to say. Report what actually happened, in order of preference:
                # its own narration, then the tools that succeeded, then an honest
                # admission that it produced nothing.
                if last_interim:
                    # Already on screen as the interim line, so it is not re-rendered --
                    # it simply becomes the answer and the spoken summary.
                    final_text = last_interim
                else:
                    succeeded = [
                        record.name
                        for record in records
                        if getattr(record.result, "ok", False)
                    ]
                    if succeeded:
                        done = ", ".join(sorted(set(succeeded)))
                        final_text = f"Done, {settings.USER_TITLE}. ({done} completed.)"
                        self._hud_call("log_agent", final_text)
                    else:
                        error = (
                            f"{settings.USER_TITLE}, the model returned nothing at all. "
                            "Worth another attempt."
                        )

            reply = AgentReply(
                text=final_text or (error or ""),
                thinking="\n\n".join(t for t in thoughts if t).strip(),
                tool_calls=records,
                iterations=iterations,
                error=error,
                duration=time.monotonic() - started,
            )
            if error and not final_text:
                # Nothing was streamed to the transcript, so surface the fault there --
                # and record that we did, so the caller does not print it again.
                self._hud_call("log_system", error, "error")
                reply.surfaced = True

            if speak:
                if self._streamed_speech:
                    # The summary has been coming out of the speaker sentence by sentence
                    # all along; all that remains is the unfinished tail.
                    self._hud_call("set_state", STATE_SPEAKING)
                    self._voice_call("flush_speech")
                else:
                    line = reply.spoken
                    if line:
                        self._hud_call("set_state", STATE_SPEAKING)
                        self._speak(line)
            return reply
        finally:
            if self._streamed_speech and error:
                # A fault mid-stream leaves a fragment in the buffer; drop it rather than
                # speaking half a sentence and stopping.
                self._voice_call("reset_speech_stream")
            self._speak_streaming = False
            self._busy.clear()
            self._cancel.clear()
            self._hud_call("set_state", STATE_IDLE)
            self._turn_lock.release()

    def _tool_schemas(self) -> list[dict]:
        """Fetch the tool schemas; an unusable registry degrades to plain chat."""
        try:
            return list(self.registry.schemas())
        except Exception:
            _LOG.exception("Tool registry produced no schemas")
            return []

    def _send(
        self, messages: Sequence[dict], tools: Sequence[dict] | None, stream: bool
    ) -> Any:
        """Issue one ``client.chat`` call with the configured generation options."""
        kwargs: dict[str, Any] = {
            "model": settings.MODEL_NAME,
            "messages": list(messages),
            "stream": stream,
            "options": {
                "temperature": settings.MODEL_TEMPERATURE,
                "top_p": settings.MODEL_TOP_P,
                "num_ctx": settings.MODEL_NUM_CTX,
            },
            "keep_alive": settings.OLLAMA_KEEP_ALIVE,
        }
        if tools:
            kwargs["tools"] = list(tools)
        if self._think_supported:
            try:
                return self._get_client().chat(think=settings.MODEL_THINKING, **kwargs)
            except TypeError:
                # Pre-0.5 clients have no `think` parameter; the model still answers,
                # simply without a separate reasoning channel.
                self._think_supported = False
        return self._get_client().chat(**kwargs)

    def _run_turn(
        self,
        messages: Sequence[dict],
        tools: Sequence[dict] | None,
        allow_retry: bool = True,
    ) -> _Turn:
        """One round-trip to the model, streamed or not, folded into a ``_Turn``."""
        try:
            if settings.STREAM_RESPONSES:
                return self._consume_stream(self._send(messages, tools, stream=True))
            return self._consume_response(self._send(messages, tools, stream=False))
        except _RESPONSE_ERRORS as exc:
            detail = self._describe_response_error(exc).lower()
            if allow_retry and self._think_supported and "think" in detail:
                # This model or server build does not accept the thinking channel.
                self._think_supported = False
                _LOG.info("Disabling `think` after server rejection: %s", detail)
                return self._run_turn(messages, tools, allow_retry=False)
            raise

    def _consume_response(self, response: Any) -> _Turn:
        """Read a non-streamed reply and render it to the HUD in one go."""
        message = _field(response, "message", {})
        turn = _Turn(
            content=str(_field(message, "content", "") or ""),
            thinking=str(_field(message, "thinking", "") or "").strip(),
            tool_calls=list(_field(message, "tool_calls", []) or []),
        )
        if turn.content.strip():
            # Content arriving *with* tool calls is narration on the way to an answer,
            # not the answer. Rendering it as a full reply panel is what made every
            # tool-using turn appear to answer twice.
            if turn.tool_calls:
                self._hud_call("log_interim", turn.content)
            else:
                self._hud_call("log_agent", turn.content)
        return turn

    def _consume_stream(self, stream: Any) -> _Turn:
        """Fold streamed chunks into a ``_Turn``, pushing tokens at the HUD.

        gemma4:31b-cloud streams its tool calls, so they are harvested from every
        chunk rather than only the last. The stream is closed on interrupt so a
        cancelled turn does not leave a socket bleeding in the background.
        """
        turn = _Turn()
        content_parts: list[str] = []
        thinking_parts: list[str] = []
        streaming = False
        try:
            for chunk in stream:
                if self._cancel.is_set():
                    break
                message = _field(chunk, "message", None)
                if message is None:
                    continue  # the terminal chunk carries only timings

                piece = _field(message, "content", "")
                if piece:
                    if not streaming:
                        self._hud_call("stream_begin")
                        streaming = True
                    content_parts.append(str(piece))
                    self._hud_call("stream_token", str(piece))
                    # Speak whole sentences the moment they complete. Waiting for the
                    # full reply is the difference between a one-second answer and an
                    # eight-second one, and it is the single biggest reason a voice
                    # assistant feels slow.
                    if self._speak_streaming and self._voice_call("feed_speech", str(piece)):
                        self._streamed_speech = True

                thought = _field(message, "thinking", "")
                if thought:
                    thinking_parts.append(str(thought))

                calls = _field(message, "tool_calls", None)
                if calls:
                    turn.tool_calls.extend(list(calls))
        finally:
            closer = getattr(stream, "close", None)
            if callable(closer):
                try:
                    closer()
                except Exception:
                    _LOG.debug("Closing the response stream failed", exc_info=True)
            turn.content = "".join(content_parts)
            turn.thinking = "".join(thinking_parts).strip()
            if streaming:
                # tool_calls are fully harvested by now, so we know whether this was the
                # answer or merely the preamble to one.
                self._hud_call("stream_end", turn.content, bool(turn.tool_calls))
        return turn

    def _assistant_message(self, turn: _Turn) -> dict:
        """Rebuild the assistant message, keeping call ids where the server sent them."""
        calls: list[dict] = []
        for raw in turn.tool_calls:
            name, display, _payload, call_id = _normalise_tool_call(raw)
            entry: dict[str, Any] = {
                "type": "function",
                "function": {"name": name, "arguments": display},
            }
            if call_id:
                entry["id"] = call_id
            calls.append(entry)
        message: dict[str, Any] = {"role": "assistant", "content": turn.content or ""}
        if calls:
            message["tool_calls"] = calls
        return message

    def _execute_tool_calls(
        self, raw_calls: Iterable[Any], iteration: int
    ) -> list[ToolCallRecord]:
        """Run every requested tool and append one ``tool`` message per call.

        Every call gets a result message even when the operator interrupts midway:
        an assistant ``tool_calls`` block with missing replies is exactly the split
        pair the memory is built to avoid.
        """
        records: list[ToolCallRecord] = []
        for raw in raw_calls:
            name, display, payload, call_id = _normalise_tool_call(raw)
            if self._cancel.is_set():
                result = ToolResult(
                    name=name or "tool",
                    ok=False,
                    content="Cancelled by the operator before execution.",
                )
            else:
                # Printed before the call so a slow build or a network search does not
                # look like a hang.
                self._hud_call("log_tool_start", name or "unknown", display)
                result = self._invoke_tool(name, payload)

            records.append(
                ToolCallRecord(
                    name=name, arguments=display, result=result, iteration=iteration
                )
            )
            self._hud_call(
                "log_tool",
                name or "unknown",
                display,
                _truncate(str(getattr(result, "content", "")), _TOOL_PREVIEW_CHARS),
                bool(getattr(result, "ok", False)),
            )
            self.memory.add_raw(self._tool_message(name, result, call_id))
        return records

    def _invoke_tool(self, name: str, payload: Any) -> ToolResult:
        """Call the registry. It promises never to raise; we assume nothing."""
        try:
            return self.registry.call(name, payload)
        except Exception as exc:
            _LOG.exception("Tool %s raised out of the registry", name)
            return ToolResult(
                name=name or "tool", ok=False, content=f"The instrument faulted: {exc}"
            )

    @staticmethod
    def _tool_message(name: str, result: ToolResult, call_id: str | None) -> dict:
        """Build the observation message fed back to the model."""
        try:
            content = result.as_message()
        except Exception:
            _LOG.debug("ToolResult.as_message failed", exc_info=True)
            content = str(getattr(result, "content", ""))
        if not getattr(result, "ok", True):
            # Tell the model how to behave about the failure, in its own voice,
            # rather than leaving it to improvise an apology.
            content = f"{content}\n\n" + personalise(
                TOOL_ERROR_TEMPLATE,
                tool=name or "unknown",
                error=str(getattr(result, "content", "")),
            )
        # Both keys are sent: `tool_name` is what recent Ollama builds read, `name`
        # is what older clients and OpenAI-shaped endpoints expect.
        message: dict[str, Any] = {
            "role": "tool",
            "content": content,
            "tool_name": name,
            "name": name,
        }
        if call_id:
            message["tool_call_id"] = call_id
        return message

    def _force_conclusion(self, prompt: str) -> tuple[str, str | None]:
        """After the iteration budget is spent, ask once more with no tools offered."""
        limit = max(1, settings.MAX_TOOL_ITERATIONS)
        exhausted = (
            f"{settings.USER_TITLE}, I exhausted my {limit} tool iterations without "
            "reaching a conclusion. Narrow the request and I will try again."
        )
        try:
            turn = self._run_turn(self.memory.messages(prompt), None)
        except Exception as exc:
            _LOG.warning("Closing request failed after the tool budget: %s", exc)
            return "", exhausted
        text = turn.content.strip()
        if text:
            return text, None
        return "", exhausted

    @staticmethod
    def _model_error_line(exc: BaseException) -> str:
        """A spoken-safe apology for a daemon that answered with an error."""
        return (
            f"My apologies, {settings.USER_TITLE}. {settings.MODEL_NAME} returned an "
            f"error: {JarvisAgent._describe_response_error(exc)}."
        )

    @staticmethod
    def _connection_error_line(exc: BaseException) -> str:
        """A spoken-safe apology for a daemon that did not answer at all."""
        return (
            f"My apologies, {settings.USER_TITLE}. I cannot reach the Ollama daemon at "
            f"{settings.OLLAMA_HOST}. Start it with the command ollama serve, then ask "
            f"me again. Reported fault: {exc.__class__.__name__}."
        )

    # -- ambient, control --------------------------------------------------------
    def ambient_report(self, alert: Any) -> None:
        """Speak one monitor alert. No model call, no full loop, no history churn.

        The alert already carries text written in J.A.R.V.I.S.'s voice with the
        operator's chosen title baked in at generation time, so paying for an
        inference round-trip to restate it would be pure theatre.
        """
        if alert is None:
            return
        message = str(_field(alert, "message", "") or "").strip()
        suggestion = str(_field(alert, "suggestion", "") or "").strip()
        headline = str(_field(alert, "title", "") or "").strip()
        key = str(_field(alert, "key", "") or headline or message[:40])
        if not message and not headline:
            return

        spoken = message or headline
        if suggestion:
            spoken = f"{spoken} {suggestion}"
        spoken = _summarise_for_speech(
            spoken, settings.TTS_MAX_CHARS, _SUMMARY_SENTENCES
        )

        if spoken:
            # Interrupting mid-answer would step on the reply, so ambient warnings
            # always queue politely behind whatever is currently being said.
            if not self.busy:
                self._hud_call("set_state", STATE_SPEAKING)
            self._speak(spoken)

        # A flapping threshold must not fill the context window with its own noise:
        # one short note per alert key, and nothing more.
        if key and key not in self._ambient_seen:
            self._ambient_seen.append(key)
            severity = str(_field(alert, "severity", "info") or "info")
            self.memory.add(
                "system",
                _truncate(
                    f"Ambient telemetry note ({severity}): {spoken}", _AMBIENT_NOTE_CHARS
                ),
            )

    def reset(self) -> None:
        """Wipe the conversation. CLEAN SLATE and ``/clear`` both land here."""
        self.memory.clear()
        self._ambient_seen.clear()
        self._cancel.clear()
        self._hud_call(
            "log_system", f"Context cleared, {settings.USER_TITLE}.", "success"
        )
        self._prompt_cache = None
        self._prompt_cache_key = None

    def interrupt(self) -> None:
        """Ask the current turn to stop at the next chunk or tool boundary."""
        self._cancel.set()
        voice = self.voice
        stopper = getattr(voice, "stop_speaking", None) if voice is not None else None
        if callable(stopper):
            try:
                stopper()
            except Exception:
                _LOG.debug("voice.stop_speaking failed", exc_info=True)

    @property
    def busy(self) -> bool:
        """True while a turn is in flight."""
        return self._busy.is_set()

    @property
    def model_ready(self) -> bool:
        """Result of the most recent :meth:`ensure_model` check."""
        return self._model_ready
