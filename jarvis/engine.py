"""The turbo engine: the same ReAct loop as :mod:`jarvis.core`, made fast.

:class:`~jarvis.core.JarvisAgent` is correct and readable, and it is also
synchronous from end to end. Every millisecond it spends waiting on a socket is a
millisecond the HUD cannot redraw, the operator cannot interrupt, and a second
tool cannot start. :class:`TurboAgent` keeps that class's behaviour — identical
memory handling, identical prompts, identical replies — and changes only *how*
the waiting is done.

Four changes account for nearly all of the difference:

* **A private event loop.** One daemon thread runs an :mod:`asyncio` loop and an
  ``ollama.AsyncClient`` on top of a pooled, keep-alive HTTP connection. The
  socket is opened once per session rather than once per turn.
* **Warm-up at boot.** The model is loaded into the daemon while the operator is
  still reading the banner, so the first question does not pay the load cost.
  On this machine that is the difference between a four-second first answer and
  a sub-second one.
* **Parallel instruments.** Tool calls issued together in one turn are executed
  concurrently unless they are dangerous or order-sensitive. Two web searches
  now cost one web search.
* **Real cancellation.** ``interrupt()`` cancels the asyncio task, which tears
  down the HTTP read immediately instead of waiting politely for the next chunk
  to arrive before noticing the flag.

Everything else is inherited. If :mod:`ollama` is too old to offer
``AsyncClient``, or the loop cannot start, :meth:`TurboAgent.chat` transparently
falls back to the synchronous implementation and nothing above it notices.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict, dataclass
from typing import Any, Iterable, Sequence

from config import (
    STATE_IDLE,
    STATE_SPEAKING,
    STATE_THINKING,
    STATE_WORKING,
    settings,
)
from jarvis import core as _core
from jarvis.core import AgentReply, JarvisAgent, ToolCallRecord, _Turn
from jarvis.tools import ToolResult

# Private helpers borrowed from the module this one extends. TurboAgent is a
# subclass of JarvisAgent living next door, not an unrelated consumer, so sharing
# the response-shape readers is deliberate: two copies of "which vintage of the
# client is this" logic would drift within a release.
_field = _core._field
_normalise_tool_call = _core._normalise_tool_call
_truncate = _core._truncate
_TOOL_PREVIEW_CHARS = _core._TOOL_PREVIEW_CHARS
_RESPONSE_ERRORS = _core._RESPONSE_ERRORS
_CONNECTION_ERRORS = _core._CONNECTION_ERRORS

try:
    import ollama
except ImportError:  # pragma: no cover - only on a broken install
    ollama = None  # type: ignore[assignment]

__all__ = ["TurboAgent", "TurnMetrics", "build_agent"]

_LOG = logging.getLogger(__name__)

#: Instruments that must never run beside another. ``dangerous`` tools go through
#: the permission broker, which asks the operator a question and would interleave
#: two prompts; the rest mutate global state that a sibling call would race.
_SERIAL_TOOLS = frozenset({"execute_protocol", "set_language"})

#: Upper bound on instruments running at once. Beyond this the bottleneck stops
#: being latency and starts being the machine.
_MAX_PARALLEL_TOOLS = 4

#: How long to wait for the private loop thread to come up before giving up on it
#: and using the synchronous path.
_LOOP_START_TIMEOUT = 5.0


# ══════════════════════════════════════════════════════════════════════════════════════
# Metrics
# ══════════════════════════════════════════════════════════════════════════════════════
@dataclass
class TurnMetrics:
    """What a turn actually cost, measured rather than estimated.

    The HUD shows these live, which turns "it feels slow" into a number that says
    whether the time went to the model, to the network, or to a tool.
    """

    #: Seconds from sending the request to the first token appearing on screen.
    ttft: float = 0.0
    #: Streamed chunks received (a lower bound on tokens; chunks are usually 1:1).
    chunks: int = 0
    #: Characters of prose streamed.
    chars: int = 0
    #: Tokens the server reports having generated, when it reports any.
    eval_tokens: int = 0
    #: Server-side generation rate, straight from the daemon's own timings.
    server_tokens_per_second: float = 0.0
    #: Wall-clock seconds spent inside model round-trips.
    model_seconds: float = 0.0
    #: Wall-clock seconds spent inside instruments.
    tool_seconds: float = 0.0
    #: Seconds tool parallelism saved: serial cost minus wall-clock cost.
    tool_seconds_saved: float = 0.0
    #: Total turn duration.
    total: float = 0.0
    iterations: int = 0
    tool_calls: int = 0
    #: The widest fan-out reached while executing instruments.
    parallel_peak: int = 1

    @property
    def tokens_per_second(self) -> float:
        """Observed streaming rate, from the client's side of the socket."""
        if self.server_tokens_per_second > 0:
            return self.server_tokens_per_second
        elapsed = max(self.model_seconds - self.ttft, 1e-6)
        return self.chunks / elapsed if self.chunks else 0.0

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["tokens_per_second"] = self.tokens_per_second
        return payload

    def summary(self) -> str:
        """One line fit for a status bar."""
        parts = [f"{self.total:.2f}s"]
        if self.ttft:
            parts.append(f"first token {self.ttft * 1000:.0f}ms")
        rate = self.tokens_per_second
        if rate:
            parts.append(f"{rate:.0f} tok/s")
        if self.tool_calls:
            parts.append(f"{self.tool_calls} tool{'s' if self.tool_calls != 1 else ''}")
        if self.tool_seconds_saved > 0.05:
            parts.append(f"{self.tool_seconds_saved:.1f}s saved in parallel")
        return " · ".join(parts)


# ══════════════════════════════════════════════════════════════════════════════════════
# The private event loop
# ══════════════════════════════════════════════════════════════════════════════════════
class _LoopThread:
    """One asyncio loop, on one daemon thread, for the life of the process.

    The agent is called from the main thread (typed input), from the voice
    thread, and from the monitor thread. Rather than make every caller async,
    the loop lives here and callers hand it coroutines and wait on a
    :class:`~concurrent.futures.Future`.
    """

    def __init__(self, name: str = "jarvis-engine") -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._lock = threading.Lock()
        self._name = name
        self._failed = False

    @property
    def loop(self) -> asyncio.AbstractEventLoop | None:
        return self._loop

    def start(self) -> asyncio.AbstractEventLoop | None:
        """Bring the loop up if it is not already running. Idempotent."""
        with self._lock:
            if self._loop is not None or self._failed:
                return self._loop
            self._ready.clear()
            thread = threading.Thread(target=self._run, name=self._name, daemon=True)
            self._thread = thread
            thread.start()
            if not self._ready.wait(_LOOP_START_TIMEOUT):
                _LOG.warning("Engine loop did not start within %.1fs", _LOOP_START_TIMEOUT)
                self._failed = True
            return self._loop

    def _run(self) -> None:
        try:
            loop = asyncio.new_event_loop()
        except Exception:
            _LOG.exception("Could not create the engine event loop")
            self._failed = True
            self._ready.set()
            return
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._ready.set()
        try:
            loop.run_forever()
        finally:
            self._loop = None
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception:
                _LOG.debug("Async generator shutdown failed", exc_info=True)
            try:
                loop.close()
            except Exception:
                _LOG.debug("Loop close failed", exc_info=True)

    def submit(self, coro) -> Future:
        """Schedule a coroutine on the loop and hand back a blocking future."""
        loop = self.start()
        if loop is None:
            coro.close()
            raise RuntimeError("the engine event loop is unavailable")
        return asyncio.run_coroutine_threadsafe(coro, loop)

    def call_soon(self, func, *args) -> bool:
        """Run ``func`` on the loop thread. False when there is no loop."""
        loop = self._loop
        if loop is None or loop.is_closed():
            return False
        try:
            loop.call_soon_threadsafe(func, *args)
            return True
        except RuntimeError:
            return False

    def stop(self) -> None:
        """Stop the loop. Safe to call twice, safe to call during shutdown."""
        loop = self._loop
        if loop is None:
            return
        try:
            loop.call_soon_threadsafe(loop.stop)
        except RuntimeError:
            pass
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)


# ══════════════════════════════════════════════════════════════════════════════════════
# The agent
# ══════════════════════════════════════════════════════════════════════════════════════
class TurboAgent(JarvisAgent):
    """A :class:`~jarvis.core.JarvisAgent` whose waiting is done asynchronously."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._loop_thread = _LoopThread()
        self._aclient: Any = None
        self._aclient_lock = threading.Lock()
        self._pool = ThreadPoolExecutor(
            max_workers=_MAX_PARALLEL_TOOLS, thread_name_prefix="jarvis-tool"
        )
        self._task: asyncio.Task | None = None
        self._metrics = TurnMetrics()
        self._warm = threading.Event()
        self._warm_future: Future | None = None
        self._async_disabled = False
        # Set while a turn is being driven asynchronously, so interrupt() knows
        # whether to cancel a task or fall back to the inherited flag.
        self._async_turn = False

    # -- capability ------------------------------------------------------------------
    @property
    def async_available(self) -> bool:
        """True when the fast path can actually be used."""
        return (
            not self._async_disabled
            and ollama is not None
            and getattr(ollama, "AsyncClient", None) is not None
        )

    @property
    def metrics(self) -> TurnMetrics:
        """Measurements from the most recently completed turn."""
        return self._metrics

    @property
    def warm(self) -> bool:
        """True once the model has been loaded into the daemon."""
        return self._warm.is_set()

    # -- client ----------------------------------------------------------------------
    def _build_async_client(self) -> Any:
        """Construct the pooled async client. Called once, on the loop thread."""
        assert ollama is not None
        kwargs: dict[str, Any] = {"host": settings.OLLAMA_HOST}
        try:
            import httpx

            # One long-lived connection per host, held open between turns. The
            # default pool would close it and pay a fresh TCP (and TLS, for a
            # cloud model) handshake on every request.
            kwargs["timeout"] = httpx.Timeout(
                settings.OLLAMA_TIMEOUT, connect=min(10.0, settings.OLLAMA_TIMEOUT)
            )
            kwargs["limits"] = httpx.Limits(
                max_keepalive_connections=8,
                max_connections=16,
                keepalive_expiry=300.0,
            )
        except Exception:
            _LOG.debug("httpx tuning unavailable; using client defaults", exc_info=True)
            kwargs["timeout"] = settings.OLLAMA_TIMEOUT
        try:
            return ollama.AsyncClient(**kwargs)
        except TypeError:
            # A client too old for the httpx keywords still works, just untuned.
            return ollama.AsyncClient(host=settings.OLLAMA_HOST)

    async def _get_async_client(self) -> Any:
        with self._aclient_lock:
            if self._aclient is None:
                self._aclient = self._build_async_client()
            return self._aclient

    # -- warm-up ---------------------------------------------------------------------
    def warm_up(self, blocking: bool = False, timeout: float | None = None) -> bool:
        """Load the model into the daemon before it is first needed.

        Returns True when the model is (or has just become) resident. Called at
        boot with ``blocking=False`` so the operator's terminal is usable while
        the daemon does the expensive part.
        """
        if not self.async_available:
            return False
        if self._warm.is_set():
            return True
        if self._warm_future is None or self._warm_future.done():
            try:
                self._warm_future = self._loop_thread.submit(self._warm_up_async())
            except RuntimeError:
                self._async_disabled = True
                return False
        if not blocking:
            return False
        try:
            return bool(self._warm_future.result(timeout=timeout))
        except Exception:
            return False

    async def _warm_up_async(self) -> bool:
        """Ask the daemon to resident-load the model and hold it there."""
        started = time.monotonic()
        try:
            client = await self._get_async_client()
        except Exception:
            _LOG.debug("Async client construction failed during warm-up", exc_info=True)
            return False

        # An empty prompt is the documented way to load a model without generating
        # anything: the daemon pulls the weights in and keep_alive pins them.
        try:
            await client.generate(
                model=settings.MODEL_NAME,
                prompt="",
                keep_alive=settings.OLLAMA_KEEP_ALIVE,
            )
        except Exception as exc:
            _LOG.debug("Empty-prompt warm-up rejected (%s); trying a one-token chat", exc)
            try:
                await client.chat(
                    model=settings.MODEL_NAME,
                    messages=[{"role": "user", "content": "ready"}],
                    stream=False,
                    keep_alive=settings.OLLAMA_KEEP_ALIVE,
                    options={"num_predict": 1},
                )
            except Exception:
                _LOG.info("Model warm-up did not succeed; the first turn will pay for it")
                return False

        self._warm.set()
        elapsed = time.monotonic() - started
        _LOG.info("Model %s warm in %.2fs", settings.MODEL_NAME, elapsed)
        self._hud_call("set_model_status", f"{settings.MODEL_NAME} · warm")
        return True

    # -- the loop --------------------------------------------------------------------
    def chat(self, user_text: str, speak: bool = True) -> AgentReply:
        """Run one turn on the fast path, or the inherited one if it is unavailable."""
        if not self.async_available:
            return super().chat(user_text, speak=speak)
        text = (user_text or "").strip()
        if not text:
            return AgentReply(text="")
        try:
            future = self._loop_thread.submit(self._chat_async(text, speak))
        except RuntimeError:
            _LOG.warning("Engine loop unavailable; using the synchronous path")
            self._async_disabled = True
            return super().chat(user_text, speak=speak)
        try:
            return future.result()
        except Exception:
            _LOG.exception("The async turn faulted outside the loop")
            line = (
                f"My apologies, {settings.USER_TITLE}. The reasoning core faulted "
                "before it could answer."
            )
            return AgentReply(text=line, error=line)

    async def _chat_async(self, text: str, speak: bool) -> AgentReply:
        """The ReAct loop, awaited rather than blocked on.

        Mirrors :meth:`JarvisAgent.chat` step for step so the two paths cannot
        drift in behaviour — only in latency.
        """
        self._sync_locale(text)

        if not self._turn_lock.acquire(blocking=False):
            busy_line = (
                f"One moment, {settings.USER_TITLE}. I am still working on the "
                "previous request."
            )
            return AgentReply(text=busy_line, error=busy_line)

        started = time.monotonic()
        metrics = TurnMetrics()
        self._metrics = metrics
        self._busy.set()
        self._cancel.clear()
        self._async_turn = True
        self._task = asyncio.current_task()
        self._speak_streaming = bool(speak and settings.SPEAK_STREAMING)
        self._streamed_speech = False
        self._voice_call("reset_speech_stream")

        records: list[ToolCallRecord] = []
        thoughts: list[str] = []
        last_interim = ""
        final_text = ""
        error: str | None = None
        cancelled = False

        try:
            self.memory.add("user", text)
            self._hud_call("set_state", STATE_THINKING)
            prompt = self.system_prompt()
            schemas = self._tool_schemas()

            for iteration in range(1, max(1, settings.MAX_TOOL_ITERATIONS) + 1):
                metrics.iterations = iteration
                if self._cancel.is_set():
                    error = f"Stopped at your word, {settings.USER_TITLE}."
                    break

                self._hud_call("set_state", STATE_THINKING)
                round_started = time.monotonic()
                try:
                    turn = await self._run_turn_async(
                        self.memory.messages(prompt), schemas, metrics
                    )
                except asyncio.CancelledError:
                    cancelled = True
                    error = f"Stopped at your word, {settings.USER_TITLE}."
                    break
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
                finally:
                    metrics.model_seconds += time.monotonic() - round_started

                if turn.thinking:
                    thoughts.append(turn.thinking)
                    self._hud_call("log_thought", turn.thinking)

                if not turn.tool_calls:
                    final_text = turn.content.strip()
                    if final_text:
                        self.memory.add("assistant", final_text)
                    break

                if turn.content.strip():
                    last_interim = turn.content.strip()

                self.memory.add_raw(self._assistant_message(turn))
                self._hud_call("set_state", STATE_WORKING)
                try:
                    round_records = await self._execute_tool_calls_async(
                        turn.tool_calls, iteration, metrics
                    )
                except asyncio.CancelledError:
                    cancelled = True
                    error = f"Stopped at your word, {settings.USER_TITLE}."
                    break
                records.extend(round_records)
                metrics.tool_calls = len(records)

                if self._cancel.is_set():
                    error = f"Stopped at your word, {settings.USER_TITLE}."
                    break
            else:
                final_text, error = await self._force_conclusion_async(prompt, metrics)
                if final_text:
                    self.memory.add("assistant", final_text)

            if not final_text and not error:
                if last_interim:
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

            metrics.total = time.monotonic() - started
            self._hud_call("set_metrics", metrics)

            reply = AgentReply(
                text=final_text or (error or ""),
                thinking="\n\n".join(t for t in thoughts if t).strip(),
                tool_calls=records,
                iterations=metrics.iterations,
                error=error,
                duration=metrics.total,
            )
            if error and not final_text:
                self._hud_call("log_system", error, "error")
                reply.surfaced = True

            if speak and not cancelled:
                if self._streamed_speech:
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
                self._voice_call("reset_speech_stream")
            self._speak_streaming = False
            self._async_turn = False
            self._task = None
            self._busy.clear()
            self._cancel.clear()
            self._hud_call("set_state", STATE_IDLE)
            self._turn_lock.release()

    # -- one round-trip --------------------------------------------------------------
    async def _send_async(
        self, messages: Sequence[dict], tools: Sequence[dict] | None, stream: bool
    ) -> Any:
        """Issue one ``AsyncClient.chat`` with the configured generation options."""
        client = await self._get_async_client()
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
                return await client.chat(think=settings.MODEL_THINKING, **kwargs)
            except TypeError:
                self._think_supported = False
        return await client.chat(**kwargs)

    async def _run_turn_async(
        self,
        messages: Sequence[dict],
        tools: Sequence[dict] | None,
        metrics: TurnMetrics,
        allow_retry: bool = True,
    ) -> _Turn:
        try:
            if settings.STREAM_RESPONSES:
                stream = await self._send_async(messages, tools, stream=True)
                return await self._consume_stream_async(stream, metrics)
            response = await self._send_async(messages, tools, stream=False)
            return self._consume_response(response)
        except _RESPONSE_ERRORS as exc:
            detail = self._describe_response_error(exc).lower()
            if allow_retry and self._think_supported and "think" in detail:
                self._think_supported = False
                _LOG.info("Disabling `think` after server rejection: %s", detail)
                return await self._run_turn_async(
                    messages, tools, metrics, allow_retry=False
                )
            raise

    async def _consume_stream_async(self, stream: Any, metrics: TurnMetrics) -> _Turn:
        """Fold streamed chunks into a turn, pushing every token straight at the HUD.

        The synchronous core buffers tokens and renders once at the end. Here they
        go on screen as they land: the perceived latency of an answer is when its
        first word appears, not when its last one does.
        """
        turn = _Turn()
        content_parts: list[str] = []
        thinking_parts: list[str] = []
        streaming = False
        first_token_at = 0.0
        request_started = time.monotonic()

        try:
            async for chunk in stream:
                if self._cancel.is_set():
                    break
                message = _field(chunk, "message", None)

                # The terminal chunk carries the daemon's own timings; they are a
                # more honest tokens-per-second than anything measured out here.
                eval_count = _field(chunk, "eval_count", None)
                if eval_count:
                    metrics.eval_tokens = int(eval_count)
                    duration = _field(chunk, "eval_duration", 0) or 0
                    if duration:
                        metrics.server_tokens_per_second = (
                            int(eval_count) / (int(duration) / 1e9)
                        )

                if message is None:
                    continue

                piece = _field(message, "content", "")
                if piece:
                    piece = str(piece)
                    if not streaming:
                        first_token_at = time.monotonic()
                        metrics.ttft = first_token_at - request_started
                        self._hud_call("stream_begin")
                        streaming = True
                    content_parts.append(piece)
                    metrics.chunks += 1
                    metrics.chars += len(piece)
                    self._hud_call("stream_token", piece)
                    if self._speak_streaming and self._voice_call("feed_speech", piece):
                        self._streamed_speech = True

                thought = _field(message, "thinking", "")
                if thought:
                    thinking_parts.append(str(thought))

                calls = _field(message, "tool_calls", None)
                if calls:
                    turn.tool_calls.extend(list(calls))
        finally:
            await self._close_stream(stream)
            turn.content = "".join(content_parts)
            turn.thinking = "".join(thinking_parts).strip()
            if streaming:
                self._hud_call("stream_end", turn.content, bool(turn.tool_calls))
        return turn

    @staticmethod
    async def _close_stream(stream: Any) -> None:
        """Release the response, whichever shape of closer this client offers."""
        for attribute in ("aclose", "close"):
            closer = getattr(stream, attribute, None)
            if not callable(closer):
                continue
            try:
                result = closer()
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                _LOG.debug("Closing the response stream failed", exc_info=True)
            return

    async def _force_conclusion_async(
        self, prompt: str, metrics: TurnMetrics
    ) -> tuple[str, str | None]:
        """The iteration budget is spent: ask once more, offering no tools."""
        limit = max(1, settings.MAX_TOOL_ITERATIONS)
        exhausted = (
            f"{settings.USER_TITLE}, I exhausted my {limit} tool iterations without "
            "reaching a conclusion. Narrow the request and I will try again."
        )
        try:
            turn = await self._run_turn_async(self.memory.messages(prompt), None, metrics)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _LOG.warning("Closing request failed after the tool budget: %s", exc)
            return "", exhausted
        text = turn.content.strip()
        return (text, None) if text else ("", exhausted)

    # -- instruments -----------------------------------------------------------------
    def _parallel_safe(self, name: str) -> bool:
        """True when this instrument may run beside another.

        Dangerous tools are excluded because they route through the permission
        broker, and two consent prompts arriving at one terminal is not a race the
        operator should have to win.
        """
        if not name or name in _SERIAL_TOOLS:
            return False
        try:
            spec = self.registry._specs.get(name)  # noqa: SLF001 - sibling module
        except Exception:
            return False
        if spec is None:
            return False
        return not bool(getattr(spec, "dangerous", False))

    async def _execute_tool_calls_async(
        self, raw_calls: Iterable[Any], iteration: int, metrics: TurnMetrics
    ) -> list[ToolCallRecord]:
        """Run the requested instruments, concurrently where that is safe.

        Results are written back in the order the model asked for them regardless
        of the order they finish in: an assistant ``tool_calls`` block and its
        replies have to stay aligned or the model loses the thread.
        """
        parsed = [_normalise_tool_call(raw) for raw in raw_calls]
        if not parsed:
            return []

        results: list[ToolResult | None] = [None] * len(parsed)
        batch_started = time.monotonic()
        serial_cost = 0.0

        if self._cancel.is_set():
            for index, (name, _display, _payload, _call_id) in enumerate(parsed):
                results[index] = ToolResult(
                    name=name or "tool",
                    ok=False,
                    content="Cancelled by the operator before execution.",
                )
        else:
            concurrent_indices = [
                index
                for index, (name, _d, _p, _c) in enumerate(parsed)
                if self._parallel_safe(name)
            ]
            concurrent_set = set(concurrent_indices)
            serial_indices = [
                index for index in range(len(parsed)) if index not in concurrent_set
            ]

            if len(concurrent_indices) > 1:
                metrics.parallel_peak = max(
                    metrics.parallel_peak, min(len(concurrent_indices), _MAX_PARALLEL_TOOLS)
                )

            # Fan out the safe ones first so the slow network calls overlap with
            # whatever the serial ones are about to do.
            loop = asyncio.get_running_loop()
            pending = {}
            for index in concurrent_indices:
                name, display, payload, _call_id = parsed[index]
                self._hud_call("log_tool_start", name or "unknown", display)
                pending[index] = loop.run_in_executor(
                    self._pool, self._timed_invoke, name, payload
                )

            for index in serial_indices:
                name, display, payload, _call_id = parsed[index]
                if self._cancel.is_set():
                    results[index] = ToolResult(
                        name=name or "tool",
                        ok=False,
                        content="Cancelled by the operator before execution.",
                    )
                    continue
                self._hud_call("log_tool_start", name or "unknown", display)
                # Serial tools may block on an operator prompt, so they still get a
                # thread — the loop must stay responsive to a cancellation.
                result, cost = await loop.run_in_executor(
                    self._pool, self._timed_invoke, name, payload
                )
                serial_cost += cost
                results[index] = result

            if pending:
                try:
                    gathered = await asyncio.gather(*pending.values())
                except asyncio.CancelledError:
                    for future in pending.values():
                        future.cancel()
                    raise
                for index, (result, cost) in zip(pending.keys(), gathered):
                    serial_cost += cost
                    results[index] = result

        wall_clock = time.monotonic() - batch_started
        metrics.tool_seconds += wall_clock
        metrics.tool_seconds_saved += max(0.0, serial_cost - wall_clock)

        records: list[ToolCallRecord] = []
        for index, (name, display, _payload, call_id) in enumerate(parsed):
            result = results[index] or ToolResult(
                name=name or "tool", ok=False, content="The instrument produced nothing."
            )
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

    def _timed_invoke(self, name: str, payload: Any) -> tuple[ToolResult, float]:
        """Run one instrument on a worker thread, reporting what it cost."""
        started = time.perf_counter()
        result = self._invoke_tool(name, payload)
        return result, time.perf_counter() - started

    # -- control ---------------------------------------------------------------------
    def interrupt(self) -> None:
        """Stop the turn in progress, now rather than at the next chunk boundary."""
        super().interrupt()
        task = self._task
        if task is not None and self._async_turn:
            self._loop_thread.call_soon(task.cancel)

    def close(self) -> None:
        """Release the loop, the pool and the socket. Safe during shutdown."""
        client = self._aclient
        self._aclient = None
        if client is not None:
            closer = getattr(client, "_client", None)
            aclose = getattr(closer, "aclose", None)
            if callable(aclose):
                try:
                    self._loop_thread.submit(aclose()).result(timeout=2.0)
                except Exception:
                    _LOG.debug("Async HTTP client did not close cleanly", exc_info=True)
        try:
            self._pool.shutdown(wait=False, cancel_futures=True)
        except TypeError:  # pragma: no cover - Python < 3.9
            self._pool.shutdown(wait=False)
        self._loop_thread.stop()


# ══════════════════════════════════════════════════════════════════════════════════════
# Factory
# ══════════════════════════════════════════════════════════════════════════════════════
def build_agent(registry, turbo: bool = True, **collaborators: Any) -> JarvisAgent:
    """Return the fastest agent this installation can actually run.

    ``turbo=False`` — or an ``ollama`` too old for ``AsyncClient`` — yields the
    synchronous core, which behaves identically and simply waits more.
    """
    if turbo and ollama is not None and getattr(ollama, "AsyncClient", None) is not None:
        agent = TurboAgent(registry, **collaborators)
        if agent.async_available:
            return agent
    return JarvisAgent(registry, **collaborators)
