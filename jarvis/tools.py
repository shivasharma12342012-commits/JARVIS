"""The instrument rack: every tool J.A.R.V.I.S. can actually reach for.

A tool is a plain Python function plus a JSON schema. :class:`ToolRegistry` owns both,
hands Ollama the schemas for native function calling, and dispatches the calls that come
back. The registry makes two promises the agent loop depends on absolutely:

* :meth:`ToolRegistry.call` **never raises.** A broken tool, a bad argument, a
  hallucinated parameter, a missing binary -- all of it comes back as
  ``ToolResult(ok=False)`` with something the model can read and recover from.
* Arguments arrive as either a ``dict`` or a JSON string depending on the model and the
  client version, and both are accepted without the caller having to care.

The instruments themselves:

===========================  ==================================================
``system_diagnostics``       CPU, memory, disk, battery, GPU, process telemetry
``web_search``               DuckDuckGo, parsed with the standard library
``code_executor``            run source in any installed language
``run_command``              builds, test suites, package managers, git
``code_toolchains``          what this machine can compile and run
``file_ops``                 read, write, list, inspect, delete
``execute_protocol``         fire a Stark Protocol
``open_app``                 open an application, file or URL (asks first)
``app_control``              list, close or focus running applications
``set_language``             switch the conversation's human language
===========================  ==================================================

Imports :mod:`config`, :mod:`jarvis.monitor`, :mod:`jarvis.protocols`,
:mod:`jarvis.languages` and :mod:`jarvis.locales`. Never :mod:`jarvis.core` or
:mod:`jarvis.ui` -- the HUD arrives as a constructor argument and is duck-typed.
"""

from __future__ import annotations

import html
import inspect
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable

import httpx
import psutil

from config import settings
from jarvis import apps, languages, locales
from jarvis.monitor import collect_telemetry, format_bytes, telemetry_report
from jarvis.permissions import PermissionBroker

logger = logging.getLogger(__name__)

_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

if sys.platform == "win32":  # pragma: no cover - platform specific
    _NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
else:  # pragma: no cover - platform specific
    _NO_WINDOW = 0


# ══════════════════════════════════════════════════════════════════════════════════════
# Result and specification types
# ══════════════════════════════════════════════════════════════════════════════════════


@dataclass
class ToolResult:
    """What an instrument reports back."""

    name: str
    ok: bool
    content: str
    data: dict | None = None
    duration: float = 0.0

    def as_message(self) -> str:
        """Exactly the text fed back to the model as the observation."""
        if self.ok:
            return self.content
        return f"[{self.name} failed] {self.content}"


@dataclass
class ToolSpec:
    """A callable plus the schema Ollama needs to call it."""

    name: str
    description: str
    parameters: dict
    func: Callable[..., Any]
    dangerous: bool = False

    def to_schema(self) -> dict:
        """The Ollama / OpenAI function-calling shape."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


def _obj(properties: dict, required: list[str] | None = None) -> dict:
    """Shorthand for a JSON-Schema object."""
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
    }


# ══════════════════════════════════════════════════════════════════════════════════════
# Registry
# ══════════════════════════════════════════════════════════════════════════════════════


class ToolRegistry:
    """Holds the instruments and dispatches calls to them."""

    def __init__(self, protocol_engine=None, hud=None, monitor=None, broker=None) -> None:
        self.protocol_engine = protocol_engine
        self.hud = hud
        self.monitor = monitor
        self.broker = broker
        self._specs: dict[str, ToolSpec] = {}

    # -- registration ------------------------------------------------------------------

    def register(self, spec: ToolSpec) -> None:
        self._specs[spec.name] = spec

    def tool(
        self, name: str, description: str, parameters: dict, dangerous: bool = False
    ):
        """Decorator form of :meth:`register`."""

        def decorate(func: Callable[..., Any]) -> Callable[..., Any]:
            self.register(ToolSpec(name, description, parameters, func, dangerous))
            return func

        return decorate

    # -- introspection -----------------------------------------------------------------

    def schemas(self) -> list[dict]:
        return [spec.to_schema() for spec in self._specs.values()]

    def names(self) -> list[str]:
        return list(self._specs)

    def has(self, name: str) -> bool:
        return name in self._specs

    def describe(self) -> list[tuple[str, str]]:
        """``(name, first sentence of the description)`` for the ``/tools`` table."""
        rows = []
        for spec in self._specs.values():
            summary = spec.description.strip().split(". ")[0].rstrip(".")
            rows.append((spec.name, summary))
        return rows

    # -- dispatch ----------------------------------------------------------------------

    def call(self, name: str, arguments: dict | str | None) -> ToolResult:
        """Run a tool. Never raises; every failure is a ``ToolResult``."""
        started = time.perf_counter()
        spec = self._specs.get(name)
        if spec is None:
            return ToolResult(
                name=name or "unknown",
                ok=False,
                content=(
                    f"There is no instrument called '{name}'. Available: "
                    f"{', '.join(self.names())}."
                ),
            )

        kwargs = _coerce_arguments(arguments)
        if kwargs is None:
            return ToolResult(
                name=name,
                ok=False,
                content="The arguments were not valid JSON and could not be read.",
            )

        # A model that invents an extra parameter should not take the tool down with it.
        filtered, dropped = _filter_kwargs(spec.func, kwargs)

        try:
            raw = spec.func(**filtered)
        except TypeError as exc:
            return ToolResult(
                name=name,
                ok=False,
                content=f"Wrong arguments for {name}: {exc}",
                duration=time.perf_counter() - started,
            )
        except Exception as exc:
            logger.exception("Tool %s raised", name)
            return ToolResult(
                name=name,
                ok=False,
                content=f"The instrument faulted: {exc}",
                duration=time.perf_counter() - started,
            )

        result = _normalise_result(name, raw)
        result.duration = time.perf_counter() - started
        if dropped and result.ok:
            result.content += f"\n\n(Ignored unrecognised argument(s): {', '.join(dropped)}.)"
        return result


def _coerce_arguments(arguments: dict | str | None) -> dict | None:
    """Accept a dict, a JSON string, or nothing. ``None`` signals unparseable."""
    if arguments is None:
        return {}
    if isinstance(arguments, dict):
        return dict(arguments)
    if isinstance(arguments, str):
        text = arguments.strip()
        if not text:
            return {}
        try:
            parsed = json.loads(text)
        except ValueError:
            return None
        return parsed if isinstance(parsed, dict) else {"value": parsed}
    return {}


def _filter_kwargs(func: Callable, kwargs: dict) -> tuple[dict, list[str]]:
    """Keep only the arguments ``func`` actually accepts."""
    try:
        signature = inspect.signature(func)
    except (TypeError, ValueError):
        return kwargs, []
    if any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in signature.parameters.values()
    ):
        return kwargs, []
    allowed = set(signature.parameters)
    kept = {k: v for k, v in kwargs.items() if k in allowed}
    dropped = [k for k in kwargs if k not in allowed]
    return kept, dropped


def _normalise_result(name: str, raw: Any) -> ToolResult:
    """Let a tool return a ToolResult, a string, or a dict."""
    if isinstance(raw, ToolResult):
        return raw
    if isinstance(raw, dict):
        return ToolResult(
            name=name,
            ok=bool(raw.get("ok", True)),
            content=str(raw.get("content", "")),
            data=raw.get("data"),
        )
    return ToolResult(name=name, ok=True, content=str(raw))


def _truncate(text: str, limit: int, note: str = "output") -> str:
    """Trim long output, keeping both ends -- errors hide at the top and the bottom."""
    if text is None:
        return ""
    text = str(text)
    if len(text) <= limit:
        return text
    head = text[: limit // 2].rstrip()
    tail = text[-limit // 2 :].lstrip()
    elided = len(text) - len(head) - len(tail)
    return f"{head}\n\n... [{elided} characters of {note} elided] ...\n\n{tail}"


# ══════════════════════════════════════════════════════════════════════════════════════
# DuckDuckGo parsing
# ══════════════════════════════════════════════════════════════════════════════════════


class _DuckDuckGoParser(HTMLParser):
    """Pulls results out of DuckDuckGo's HTML endpoint.

    Deliberately stdlib-only: adding BeautifulSoup for one page of scraping is not worth
    the dependency, and the markup we need is shallow.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[dict[str, str]] = []
        self._mode: str | None = None
        self._href: str = ""
        self._buffer: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag != "a":
            return
        attributes = dict(attrs)
        classes = (attributes.get("class") or "").split()
        if "result__a" in classes or "result-link" in classes:
            self._mode = "title"
            self._href = attributes.get("href") or ""
            self._buffer = []
        elif "result__snippet" in classes:
            self._mode = "snippet"
            self._buffer = []

    def handle_data(self, data: str) -> None:
        if self._mode:
            self._buffer.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag != "a" or not self._mode:
            return
        text = " ".join("".join(self._buffer).split())
        if self._mode == "title" and text:
            self.results.append(
                {"title": text, "url": _unwrap_ddg(self._href), "snippet": ""}
            )
        elif self._mode == "snippet" and text and self.results:
            if not self.results[-1]["snippet"]:
                self.results[-1]["snippet"] = text
        self._mode = None
        self._buffer = []


def _unwrap_ddg(href: str) -> str:
    """Turn ``/l/?uddg=https%3A%2F%2F...`` back into a real URL."""
    if not href:
        return ""
    if href.startswith("//"):
        href = "https:" + href
    try:
        parsed = urllib.parse.urlparse(href)
        if "uddg" in urllib.parse.parse_qs(parsed.query):
            return urllib.parse.parse_qs(parsed.query)["uddg"][0]
    except ValueError:
        pass
    return html.unescape(href)


# ══════════════════════════════════════════════════════════════════════════════════════
# Code execution helpers
# ══════════════════════════════════════════════════════════════════════════════════════

_JAVA_CLASS_RE = re.compile(r"\bpublic\s+(?:final\s+|abstract\s+)?class\s+(\w+)")

#: Patterns Veronica refuses to execute while lockdown is engaged. Language-aware,
#: because ``rm -rf`` and ``shutil.rmtree`` are the same intention in different clothes.
_DESTRUCTIVE_CODE = re.compile(
    r"shutil\.rmtree|os\.remove|os\.unlink|os\.system|subprocess|"
    r"fs\.unlink|fs\.rm\b|rimraf|Remove-Item|\brm\s+-rf|\bdel\s+/|"
    r"Runtime\.getRuntime|std::filesystem::remove|File\.Delete|"
    r"\bunlink\s*\(|\bsocket\b",
    re.IGNORECASE,
)


#: Absolute paths appearing in source. Windows drive letters and POSIX system roots --
#: enough to catch code reaching out of its sandbox without flagging every string.
_ABSOLUTE_PATH_RE = re.compile(
    # (?<![A-Za-z]) matters: a drive letter is exactly one character, so without it
    # the "s:" in "https://..." reads as drive S and every URL trips a prompt.
    r"""(?:(?<![A-Za-z])[A-Za-z]:[\\/][^\s'"\)\]]{2,}"""
    r"""|/(?:etc|usr|var|home|root|Users|opt)/[^\s'"\)\]]+)"""
)


def _external_paths(source: str) -> list[str]:
    """Absolute paths in ``source`` that fall outside the workspace.

    A heuristic, and deliberately a coarse one. It exists because the tool boundary in
    ``file_ops`` is only a boundary if it cannot be walked around by asking the code
    executor to open the same file -- which is exactly what a model will do when the
    shorter route occurs to it first. It is not, and cannot be, airtight: this is a
    speed bump on an honest path, not a sandbox wall.
    """
    try:
        workspace = Path(settings.WORKSPACE_ROOT).resolve()
    except (OSError, ValueError):
        return []

    found: list[str] = []
    for raw in _ABSOLUTE_PATH_RE.findall(source or ""):
        candidate = raw.strip().rstrip("\\/")
        try:
            resolved = Path(candidate).expanduser().resolve()
        except (OSError, ValueError):
            continue
        if _within(resolved, workspace):
            continue
        text = str(resolved)
        if text not in found:
            found.append(text)
    return found


def _lockdown_reason(engine, subject: str) -> str | None:
    """Ask the protocol engine whether Veronica forbids this."""
    if engine is None:
        return None
    try:
        if not getattr(engine, "lockdown", False):
            return None
    except Exception:
        return None
    return (
        f"Veronica Protocol is engaged; {subject} is restricted until it is stood down. "
        f"Run `/protocol clean_slate` or ask me to deactivate it."
    )


# ══════════════════════════════════════════════════════════════════════════════════════
# Registry construction
# ══════════════════════════════════════════════════════════════════════════════════════


def build_registry(
    protocol_engine=None, hud=None, monitor=None, broker=None
) -> ToolRegistry:
    """Assemble the full instrument rack.

    ``broker`` is the consent layer. Without one, every capability that reaches outside
    the workspace refuses rather than proceeding -- failing closed is the only sane
    default for a component whose whole job is asking first.
    """
    if broker is None:
        broker = PermissionBroker(hud=hud, protocol_engine=protocol_engine)
    registry = ToolRegistry(protocol_engine, hud, monitor, broker)

    # ── 1. system_diagnostics ─────────────────────────────────────────────────────────
    @registry.tool(
        name="system_diagnostics",
        description=(
            "Read live hardware telemetry from this machine: CPU load and per-core "
            "usage, memory, disk space, battery, GPU/VRAM, temperatures and the "
            "heaviest processes. Use this instead of guessing whenever the operator "
            "asks how the machine is doing."
        ),
        parameters=_obj(
            {
                "scope": {
                    "type": "string",
                    "enum": [
                        "summary", "full", "cpu", "memory", "disk",
                        "battery", "gpu", "processes",
                    ],
                    "description": "Which subsystem to report on. Defaults to a summary.",
                }
            }
        ),
    )
    def system_diagnostics(scope: str = "summary") -> ToolResult:
        """Take one honest measurement of the machine."""
        scope = (scope or "summary").strip().lower()
        telemetry = collect_telemetry(top_n=8)
        report = telemetry_report(telemetry)

        if scope not in {"summary", "full"}:
            section = _telemetry_section(telemetry, scope)
            if section:
                report = section

        data = {
            "cpu_percent": telemetry.cpu_percent,
            "ram_percent": telemetry.ram_percent,
            "ram_used_gb": round(telemetry.ram_used_gb, 2),
            "ram_total_gb": round(telemetry.ram_total_gb, 2),
            "battery_percent": telemetry.battery_percent,
            "battery_plugged": telemetry.battery_plugged,
            "disks": [
                {"mount": d.mountpoint, "percent": d.percent,
                 "total_gb": round(d.total_gb, 1)}
                for d in telemetry.disks
            ],
            "gpus": [
                {"name": g.name, "utilization": g.utilization_percent}
                for g in telemetry.gpus
            ],
            "process_count": telemetry.process_count,
            "uptime_seconds": round(telemetry.uptime_seconds),
        }
        if hud is not None:
            _safe(hud.set_telemetry, telemetry)
        return ToolResult("system_diagnostics", True, report, data)

    # ── 2. web_search ─────────────────────────────────────────────────────────────────
    @registry.tool(
        name="web_search",
        description=(
            "Search the web and return titles, URLs and snippets. Use it for anything "
            "current, external, or outside your own knowledge -- documentation, release "
            "notes, error messages, prices, news."
        ),
        parameters=_obj(
            {
                "query": {"type": "string", "description": "The search query."},
                "max_results": {
                    "type": "integer",
                    "description": "How many results to return (1-10).",
                },
            },
            ["query"],
        ),
    )
    def web_search(query: str, max_results: int = 5) -> ToolResult:
        """Query DuckDuckGo and summarise what comes back."""
        if not settings.WEB_SEARCH_ENABLED:
            return ToolResult("web_search", False, "Web search is disabled in configuration.")
        query = str(query or "").strip()
        if not query:
            return ToolResult("web_search", False, "No query was supplied.")

        try:
            count = max(1, min(10, int(max_results)))
        except (TypeError, ValueError):
            count = settings.WEB_SEARCH_RESULTS

        endpoints = (
            ("https://html.duckduckgo.com/html/", {"q": query}),
            ("https://lite.duckduckgo.com/lite/", {"q": query}),
        )
        errors: list[str] = []
        for url, payload in endpoints:
            try:
                response = httpx.post(
                    url,
                    data=payload,
                    headers={"User-Agent": _BROWSER_UA, "Accept-Language": "en-GB,en"},
                    timeout=settings.WEB_SEARCH_TIMEOUT,
                    follow_redirects=True,
                )
                response.raise_for_status()
            except Exception as exc:
                errors.append(f"{urllib.parse.urlparse(url).netloc}: {exc}")
                continue

            parser = _DuckDuckGoParser()
            try:
                parser.feed(response.text)
            except Exception as exc:
                errors.append(f"parse failure: {exc}")
                continue

            results = [r for r in parser.results if r["url"]][:count]
            if not results:
                errors.append(f"{urllib.parse.urlparse(url).netloc}: no results parsed")
                continue

            lines = [f"Search results for **{query}**:", ""]
            for index, item in enumerate(results, 1):
                lines.append(f"{index}. **{item['title']}**")
                lines.append(f"   {item['url']}")
                if item["snippet"]:
                    lines.append(f"   {item['snippet']}")
                lines.append("")
            return ToolResult(
                "web_search", True, "\n".join(lines).strip(),
                {"query": query, "count": len(results), "results": results},
            )

        return ToolResult(
            "web_search", False,
            "The search could not be completed. " + "; ".join(errors[:2]),
        )

    # ── 3. code_executor ──────────────────────────────────────────────────────────────
    @registry.tool(
        name="code_executor",
        description=(
            "Execute source code in any language installed on this machine -- Python, "
            "JavaScript, TypeScript, C, C++, C#, Java, Go, Rust, Bash, PowerShell, SQL "
            "and more. Compiled languages are built first and the compiler diagnostics "
            "are returned on failure. Use this to verify code actually runs, to compute "
            "precise answers, and to test ideas. Call code_toolchains first if you are "
            "unsure whether a language is available."
        ),
        parameters=_obj(
            {
                "code": {"type": "string", "description": "The complete source to run."},
                "language": {
                    "type": "string",
                    "description": (
                        "Language name, e.g. python, javascript, typescript, cpp, "
                        "csharp, go, rust, bash, powershell, sql. Use 'auto' to infer."
                    ),
                },
                "timeout": {"type": "number", "description": "Seconds before giving up."},
                "stdin": {"type": "string", "description": "Text piped to the program."},
                "args": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Command-line arguments for the program.",
                },
            },
            ["code"],
        ),
    )
    def code_executor(
        code: str = "",
        language: str = "",
        timeout: float | None = None,
        stdin: str | None = None,
        args: list | None = None,
        python_code: str | None = None,
    ) -> ToolResult:
        """Compile and run a snippet in a throwaway directory.

        This is subprocess isolation with a timeout, **not** a security sandbox: the code
        runs with the agent's own privileges and can reach the filesystem and network.
        Veronica lockdown blocks obviously destructive source, which is a seatbelt, not
        an airbag.
        """
        source = str(code or python_code or "")
        if not source.strip():
            return ToolResult("code_executor", False, "No code was supplied.")
        if not settings.CODE_EXEC_ENABLED:
            return ToolResult("code_executor", False, "Code execution is disabled in configuration.")

        name = (language or "").strip().lower()
        if not name or name == "auto":
            name = languages.guess_language(source)

        lang = languages.get_language(name)
        if lang is None:
            return ToolResult(
                "code_executor", False,
                f"'{language}' is not a language I recognise. Try one of: "
                f"{', '.join(sorted(languages.LANGUAGES))}.",
            )

        # Data and markup languages are written and checked, not executed.
        if not lang.executable:
            return _check_non_executable(lang, source)

        reason = _lockdown_reason(protocol_engine, "running code")
        if reason and _DESTRUCTIVE_CODE.search(source):
            return ToolResult("code_executor", False, reason)

        # Code that names a path outside the workspace is doing what file_ops would
        # have needed permission for, so it needs the same permission.
        external = _external_paths(source)
        if external:
            if registry.broker is None:
                return ToolResult(
                    "code_executor", False,
                    "This code reaches outside the workspace ("
                    + ", ".join(external[:3])
                    + ") and there is no consent layer attached to approve it.",
                )
            decision = registry.broker.check_path("access from code", external[0])
            if not decision.granted:
                return _denied("code_executor", decision)

        detection = languages.detect(lang.key)
        if not detection.available or detection.toolchain is None:
            hint = ""
            tool = lang.toolchains[0] if lang.toolchains else None
            if tool and tool.install_hint:
                hint = f" Install it with: `{tool.install_hint}`."
            alternatives = ", ".join(
                d.display for d in languages.available_languages()[:8]
            )
            return ToolResult(
                "code_executor", False,
                f"{lang.display} has no working toolchain on this machine "
                f"({detection.error or 'not found'}).{hint} "
                f"Available instead: {alternatives}.",
            )

        return _run_source(lang, detection, source, timeout, stdin, args, hud)

    # ── 4. run_command ────────────────────────────────────────────────────────────────
    @registry.tool(
        name="run_command",
        description=(
            "Run a shell command in the workspace: build tools, test suites, package "
            "managers, git, linters, compilers. Use it for `npm test`, `dotnet build`, "
            "`pytest -q`, `cargo check`, `git diff` and similar. Destructive commands "
            "are refused."
        ),
        parameters=_obj(
            {
                "command": {"type": "string", "description": "The command line to run."},
                "cwd": {
                    "type": "string",
                    "description": "Working directory; must be inside the workspace.",
                },
                "timeout": {"type": "number", "description": "Seconds before giving up."},
            },
            ["command"],
        ),
        dangerous=True,
    )
    def run_command(
        command: str, cwd: str | None = None, timeout: float | None = None
    ) -> ToolResult:
        """Run a build or test command and report both streams."""
        command = str(command or "").strip()
        if not command:
            return ToolResult("run_command", False, "No command was supplied.")
        if not settings.SHELL_TOOL_ENABLED:
            return ToolResult("run_command", False, "The command runner is disabled in configuration.")

        reason = _lockdown_reason(protocol_engine, "running shell commands")
        if reason:
            return ToolResult("run_command", False, reason)

        for pattern in settings.SHELL_DENY_PATTERNS:
            try:
                if re.search(pattern, command, re.IGNORECASE):
                    return ToolResult(
                        "run_command", False,
                        f"That command is on the refusal list (matched `{pattern}`). "
                        f"If you genuinely need it, {settings.USER_TITLE} can run it directly.",
                    )
            except re.error:
                logger.debug("Bad deny pattern %r", pattern)

        workspace = Path(settings.WORKSPACE_ROOT).resolve()
        try:
            directory = Path(cwd).expanduser().resolve() if cwd else workspace
        except (OSError, ValueError) as exc:
            return ToolResult("run_command", False, f"Invalid working directory: {exc}")
        if not directory.is_dir():
            return ToolResult("run_command", False, f"No such directory: {directory}")

        # Outside the workspace he must ask. Inside it, he may work.
        if not _within(directory, workspace):
            if registry.broker is None:
                return ToolResult(
                    "run_command", False,
                    f"`{directory}` is outside the workspace and no consent layer is "
                    f"attached, so I cannot ask for permission.",
                )
            decision = registry.broker.check_shell(command, str(directory))
            if not decision.granted:
                return _denied("run_command", decision)

        limit = _positive(timeout, settings.SHELL_TIMEOUT)
        started = time.perf_counter()
        timed_out = False
        try:
            proc = subprocess.Popen(
                command,
                shell=True,
                cwd=str(directory),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=_NO_WINDOW,
            )
        except OSError as exc:
            return ToolResult("run_command", False, f"Could not launch the command: {exc}")

        try:
            stdout, stderr = proc.communicate(timeout=limit)
            code = proc.returncode
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_process_tree(proc.pid)
            try:
                stdout, stderr = proc.communicate(timeout=5)
            except Exception:
                stdout, stderr = "", ""
            code = -1
        stdout, stderr = stdout or "", stderr or ""

        duration = time.perf_counter() - started
        body = [f"`{command}` in `{directory}`", ""]
        if timed_out:
            body.append(f"**Timed out after {limit:.0f}s** -- partial output below.")
        body.append(f"exit code: {code}")
        if stdout.strip():
            body += ["", "**stdout**", "```", _truncate(stdout, settings.SHELL_MAX_OUTPUT), "```"]
        if stderr.strip():
            body += ["", "**stderr**", "```", _truncate(stderr, settings.SHELL_MAX_OUTPUT), "```"]
        if not stdout.strip() and not stderr.strip():
            body.append("\n(no output)")

        return ToolResult(
            "run_command",
            ok=(code == 0 and not timed_out),
            content="\n".join(body),
            data={
                "command": command, "cwd": str(directory), "exit_code": code,
                "timed_out": timed_out, "duration_ms": round(duration * 1000),
            },
        )

    # ── 5. code_toolchains ────────────────────────────────────────────────────────────
    @registry.tool(
        name="code_toolchains",
        description=(
            "List which programming languages can actually be compiled and run on this "
            "machine, with the backing binary and version, plus install hints for the "
            "ones that are missing. Consult this before promising to run code in a "
            "language you have not used yet this session."
        ),
        parameters=_obj(
            {
                "refresh": {
                    "type": "boolean",
                    "description": "Re-probe every toolchain instead of using the cache.",
                }
            }
        ),
    )
    def code_toolchains(refresh: bool = False) -> ToolResult:
        """Report the language toolchains present on this host."""
        report = languages.toolchain_report(bool(refresh))
        online = languages.available_languages()
        absent = languages.missing_languages()
        return ToolResult(
            "code_toolchains", True, report,
            {
                "available": [d.language for d in online],
                "missing": [d.language for d in absent],
                "count": len(online),
                "total": len(online) + len(absent),
            },
        )

    # ── 6. file_ops ───────────────────────────────────────────────────────────────────
    @registry.tool(
        name="file_ops",
        description=(
            "Read, write, append to, list, inspect or delete files. Reading and listing "
            "work anywhere readable; writing and deleting are confined to the workspace. "
            "Use this to inspect real file contents rather than assuming them."
        ),
        parameters=_obj(
            {
                "action": {
                    "type": "string",
                    "enum": ["read", "write", "append", "list", "info", "exists",
                             "mkdir", "delete"],
                    "description": "What to do.",
                },
                "path": {"type": "string", "description": "The target path."},
                "content": {
                    "type": "string",
                    "description": "Text to write or append (write/append only).",
                },
            },
            ["action", "path"],
        ),
        dangerous=True,
    )
    def file_ops(action: str, path: str, content: str | None = None) -> ToolResult:
        """Filesystem access, with the workspace boundary enforced for mutations."""
        action = str(action or "").strip().lower()
        if not path:
            return ToolResult("file_ops", False, "No path was supplied.")

        try:
            target = Path(str(path)).expanduser()
            resolved = target.resolve()
        except (OSError, ValueError) as exc:
            return ToolResult("file_ops", False, f"That path is not usable: {exc}")

        workspace = Path(settings.WORKSPACE_ROOT).resolve()
        mutating = action in {"write", "append", "mkdir", "delete"}
        revealing = action in {"read", "list"}

        if mutating and protocol_engine is not None:
            try:
                refusal = protocol_engine.guard_destructive(action, str(resolved))
            except Exception:
                refusal = None
            if refusal:
                return ToolResult("file_ops", False, refusal)

        # Anything touching the world beyond the workspace -- changing it, or merely
        # reading it -- is the operator's call, not his.
        if (mutating or revealing) and not _within(resolved, workspace):
            if registry.broker is None:
                return ToolResult(
                    "file_ops", False,
                    f"`{resolved}` is outside the workspace and no consent layer is "
                    f"attached, so I cannot ask for permission.",
                )
            decision = registry.broker.check_path(action, resolved)
            if not decision.granted:
                return _denied("file_ops", decision)

        try:
            return _do_file_op(action, resolved, content, hud)
        except PermissionError as exc:
            return ToolResult("file_ops", False, f"Permission denied: {exc}")
        except OSError as exc:
            return ToolResult("file_ops", False, f"Filesystem error: {exc}")

    # ── 7. execute_protocol ───────────────────────────────────────────────────────────
    @registry.tool(
        name="execute_protocol",
        description=(
            "Fire a named Stark Protocol. HOUSE PARTY runs full diagnostics, brings dev "
            "environments up and scans workspace repositories. VERONICA engages security "
            "lockdown. CLEAN SLATE clears context, purges caches and resets the display. "
            "Call this the moment the operator names a protocol."
        ),
        parameters=_obj(
            {
                "protocol_name": {
                    "type": "string",
                    "description": "house_party, veronica or clean_slate.",
                }
            },
            ["protocol_name"],
        ),
    )
    def execute_protocol(protocol_name: str) -> ToolResult:
        """Resolve a protocol by loose name and run it."""
        if protocol_engine is None:
            return ToolResult("execute_protocol", False, "No protocol engine is bound.")
        key = None
        try:
            key = protocol_engine.resolve(str(protocol_name or ""))
        except Exception:
            logger.debug("Protocol resolution failed", exc_info=True)
        if not key:
            try:
                available = ", ".join(protocol_engine.names())
            except Exception:
                available = "unknown"
            return ToolResult(
                "execute_protocol", False,
                f"No protocol matches '{protocol_name}'. Registered: {available}.",
            )
        result = protocol_engine.execute(key)
        try:
            body = result.to_markdown()
            ok = bool(result.success)
        except Exception:
            body, ok = str(result), True
        return ToolResult("execute_protocol", ok, body, {"protocol": key})

    # ── 8. open_app ───────────────────────────────────────────────────────────────────
    @registry.tool(
        name="open_app",
        description=(
            "Open an application, file, folder or web page on the operator's computer. "
            "Accepts a friendly name ('notepad', 'chrome', 'vscode', 'spotify', "
            "'settings'), a full path, or a URL. The operator is asked for permission "
            "before anything is opened, so state plainly what you intend to open and "
            "why. If permission is refused, accept it and do not ask again."
        ),
        parameters=_obj(
            {
                "target": {
                    "type": "string",
                    "description": "App name, file path, folder or URL to open.",
                },
                "args": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional arguments, e.g. a file to open in an editor.",
                },
            },
            ["target"],
        ),
        dangerous=True,
    )
    def open_app(target: str, args: list | None = None) -> ToolResult:
        """Resolve a name to something launchable, get consent, then start it."""
        wanted = str(target or "").strip()
        if not wanted:
            return ToolResult("open_app", False, "Nothing was named to open.")
        if not settings.APP_CONTROL_ENABLED:
            return ToolResult(
                "open_app", False, "Application control is disabled in configuration."
            )

        app = apps.resolve(wanted)
        if app is None:
            known = ", ".join(sorted(apps.KNOWN_APPS)[:18])
            return ToolResult(
                "open_app", False,
                f"I could not find anything called '{wanted}' -- not on PATH, not in the "
                f"Start Menu, and not a path or URL that exists. Names I recognise "
                f"include: {known}.",
            )

        if registry.broker is None:
            return ToolResult(
                "open_app", False,
                "No consent layer is attached, so I cannot ask to open anything.",
            )
        decision = registry.broker.check_launch(app.name, f"{app.kind}: {app.path}")
        if not decision.granted:
            return _denied("open_app", decision)

        extra = [str(a) for a in (args or [])]
        ok, message, pid = apps.launch(app, extra)
        if hud is not None and ok:
            _safe(hud.log_system, message, "success")
        return ToolResult(
            "open_app", ok, message,
            {"target": app.name, "kind": app.kind, "path": app.path,
             "pid": pid, "args": extra},
        )

    # ── 9. app_control ────────────────────────────────────────────────────────────────
    @registry.tool(
        name="app_control",
        description=(
            "Inspect or manage running applications. 'list' shows what is open (no "
            "permission needed); 'close' asks an application to quit; 'focus' brings a "
            "window to the front. Closing requires the operator's approval and cannot "
            "be undone, so confirm which application they mean before calling it."
        ),
        parameters=_obj(
            {
                "action": {
                    "type": "string",
                    "enum": ["list", "close", "focus"],
                    "description": "What to do.",
                },
                "name": {
                    "type": "string",
                    "description": "Application name, required for close and focus.",
                },
                "force": {
                    "type": "boolean",
                    "description": "Kill outright if it ignores a polite close request.",
                },
            },
            ["action"],
        ),
        dangerous=True,
    )
    def app_control(action: str, name: str = "", force: bool = False) -> ToolResult:
        """List, close or focus running applications."""
        verb = str(action or "").strip().lower()
        if not settings.APP_CONTROL_ENABLED:
            return ToolResult(
                "app_control", False, "Application control is disabled in configuration."
            )

        if verb == "list":
            rows = apps.running_apps(30)
            if not rows:
                return ToolResult("app_control", True, "Nothing notable is running.")
            lines = ["| Application | Processes | Memory |", "|---|---|---|"]
            for row in rows:
                lines.append(
                    f"| {row['name']} | {row['count']} | {row['memory_mb']:.0f} MB |"
                )
            return ToolResult(
                "app_control", True, "\n".join(lines),
                {"count": len(rows), "apps": [r["name"] for r in rows]},
            )

        target = str(name or "").strip()
        if not target:
            return ToolResult("app_control", False, f"'{verb}' needs an application name.")

        if verb == "focus":
            ok, message = apps.focus_app(target)
            return ToolResult("app_control", ok, message, {"app": target})

        if verb == "close":
            matches = apps.find_processes(target)
            if not matches:
                return ToolResult(
                    "app_control", False, f"Nothing matching '{target}' is running."
                )
            if registry.broker is None:
                return ToolResult(
                    "app_control", False,
                    "No consent layer is attached, so I cannot ask to close anything.",
                )
            # Name the actual executables: approving "close code" should show that it
            # means Code.exe six times over, not an opaque list of pids.
            names = sorted({(m.info.get("name") or "?") for m in matches})
            detail = (
                f"{len(matches)} process(es): "
                + ", ".join(names[:5])
                + (" ..." if len(names) > 5 else "")
            )
            decision = registry.broker.check_process(target, detail)
            if not decision.granted:
                return _denied("app_control", decision)

            closed, notes = apps.close_app(target, bool(force))
            body = f"Closed {closed} process(es) belonging to {target}."
            if notes:
                body += "\n\n" + "\n".join(f"- {n}" for n in notes)
            return ToolResult(
                "app_control", closed > 0, body, {"app": target, "closed": closed}
            )

        return ToolResult(
            "app_control", False,
            f"Unknown action '{action}'. Valid: list, close, focus.",
        )

    # ── 10. set_language ──────────────────────────────────────────────────────────────
    @registry.tool(
        name="set_language",
        description=(
            "Switch the language you reply in. Call this when the operator asks you to "
            "speak in another language -- English, Hindi, Bengali, Telugu, Marathi or "
            "Tamil. Your confirmation and everything after it must be in the new "
            "language."
        ),
        parameters=_obj(
            {
                "language": {
                    "type": "string",
                    "description": "Language name or code: english, hindi, hi, tamil, ta...",
                }
            },
            ["language"],
        ),
    )
    def set_language(language: str) -> ToolResult:
        """Change the conversation's human language."""
        wanted = str(language or "").strip()
        if not wanted:
            return ToolResult("set_language", False, "No language was named.")

        locale = locales.set_active(wanted)
        if locale is None:
            enabled = ", ".join(
                f"{loc.name} ({loc.native_name})" for loc in locales.enabled_locales()
            )
            return ToolResult(
                "set_language", False,
                f"'{wanted}' is not among the enabled languages. Currently available: "
                f"{enabled}.",
            )

        if hud is not None:
            # English name in the status strip on purpose: complex Indic conjuncts
            # shape to different cell counts across terminal fonts, and a bar that must
            # keep its right-hand border aligned is the wrong place to gamble. The
            # native script still appears in the confirmation and in /locales.
            _safe(hud.set_voice_status, f"voice: {locale.name}")
        confirmation = locales.localise("ack", locale)
        return ToolResult(
            "set_language", True,
            f"Language switched to {locale.name} ({locale.native_name}). "
            f"Reply in {locale.name} from here on.\n\n{confirmation}",
            {
                "code": locale.code, "name": locale.name,
                "native_name": locale.native_name,
                "voice": locales.voice_for(locale), "stt_code": locale.stt_code,
            },
        )

    return registry


# ══════════════════════════════════════════════════════════════════════════════════════
# Implementation helpers
# ══════════════════════════════════════════════════════════════════════════════════════


def _safe(func, *args) -> None:
    """Call an optional HUD hook without letting it break a tool."""
    try:
        func(*args)
    except Exception:
        logger.debug("HUD hook failed", exc_info=True)


def _within(path: Path, root: Path) -> bool:
    """True when ``path`` is inside ``root``, defeating ``..`` traversal."""
    try:
        return path == root or path.is_relative_to(root)
    except (AttributeError, ValueError):
        return str(path).startswith(str(root))


def _positive(value, fallback: float) -> float:
    """Coerce a model-supplied timeout into something sane."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    return number if number > 0 else fallback


def _decode(raw) -> str:
    """Bytes or str from a timed-out subprocess."""
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        return raw.decode("utf-8", "replace")
    return str(raw)


def _kill_process_tree(pid: int) -> None:
    """Kill one process and its descendants after a timeout.

    The previous implementation reaped *every* child of the agent, which meant a timed
    out ``npm install`` also killed the ``ffplay`` process speaking the reply. Only the
    offending tree is touched now.
    """
    try:
        parent = psutil.Process(pid)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return
    try:
        children = parent.children(recursive=True)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        children = []
    for victim in (*children, parent):
        try:
            victim.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    try:
        psutil.wait_procs([*children, parent], timeout=3)
    except Exception:
        logger.debug("Reaping the process tree of pid %s failed", pid, exc_info=True)


def _denied(name: str, decision) -> ToolResult:
    """Turn a refused permission request into an observation the model can act on."""
    return ToolResult(
        name, False,
        f"{getattr(decision, 'reason', '') or 'Permission was refused.'} "
        f"Do not retry this without being asked to.",
    )


def _telemetry_section(telemetry, scope: str) -> str:
    """Narrow a full telemetry report down to one subsystem."""
    if scope == "cpu":
        cores = ", ".join(f"{p:.0f}%" for p in telemetry.cpu_per_core[:16])
        freq = f"{telemetry.cpu_freq_mhz:.0f} MHz" if telemetry.cpu_freq_mhz else "unknown"
        return (
            f"**CPU** — {telemetry.cpu_percent:.1f}% across {telemetry.cpu_count} logical "
            f"cores at {freq}.\n\nPer core: {cores}"
        )
    if scope == "memory":
        return (
            f"**Memory** — {telemetry.ram_used_gb:.1f} GB of {telemetry.ram_total_gb:.1f} GB "
            f"in use ({telemetry.ram_percent:.1f}%), {telemetry.ram_available_gb:.1f} GB "
            f"available. Swap at {telemetry.swap_percent:.1f}%."
        )
    if scope == "disk":
        rows = [
            f"- `{d.mountpoint}` — {d.used_gb:.1f} / {d.total_gb:.1f} GB ({d.percent:.1f}%)"
            for d in telemetry.disks
        ]
        return "**Storage**\n" + ("\n".join(rows) or "- no volumes reported")
    if scope == "battery":
        if telemetry.battery_percent is None:
            return "**Battery** — no battery detected; this machine runs on mains power."
        state = "charging" if telemetry.battery_plugged else "on battery"
        left = (
            f", about {telemetry.battery_minutes_left} minutes remaining"
            if telemetry.battery_minutes_left else ""
        )
        return f"**Battery** — {telemetry.battery_percent:.0f}%, {state}{left}."
    if scope == "gpu":
        if not telemetry.gpus:
            return "**GPU** — no discrete GPU telemetry available (nvidia-smi not found)."
        rows = []
        for gpu in telemetry.gpus:
            used = f"{gpu.memory_used_mb:.0f}/{gpu.memory_total_mb:.0f} MB" if gpu.memory_total_mb else "VRAM unknown"
            util = f"{gpu.utilization_percent:.0f}%" if gpu.utilization_percent is not None else "?"
            rows.append(f"- {gpu.name} — {util} utilisation, {used}")
        return "**GPU**\n" + "\n".join(rows)
    if scope == "processes":
        rows = [
            f"| {p.pid} | {p.name} | {p.cpu_percent:.1f}% | {p.memory_mb:.0f} MB |"
            for p in telemetry.top_processes
        ]
        return (
            f"**Heaviest processes** ({telemetry.process_count} running)\n\n"
            "| PID | Name | CPU | Memory |\n|---|---|---|---|\n" + "\n".join(rows)
        )
    return ""


def _check_non_executable(lang, source: str) -> ToolResult:
    """Validate markup and data languages rather than refusing outright."""
    if lang.key == "json":
        try:
            parsed = json.loads(source)
        except ValueError as exc:
            return ToolResult("code_executor", False, f"Invalid JSON: {exc}")
        kind = type(parsed).__name__
        size = len(parsed) if isinstance(parsed, (dict, list)) else 1
        return ToolResult(
            "code_executor", True,
            f"Valid JSON — a {kind} with {size} top-level "
            f"{'keys' if isinstance(parsed, dict) else 'items'}.",
            {"valid": True, "type": kind},
        )
    if lang.key == "yaml":
        try:
            import yaml  # optional; only present if the operator installed it
        except ImportError:
            return ToolResult(
                "code_executor", True,
                "YAML is written, not executed. PyYAML is not installed, so I cannot "
                "validate it here — `pip install pyyaml` if you want that check.",
            )
        try:
            yaml.safe_load(source)
        except Exception as exc:
            return ToolResult("code_executor", False, f"Invalid YAML: {exc}")
        return ToolResult("code_executor", True, "Valid YAML.", {"valid": True})
    return ToolResult(
        "code_executor", True,
        f"{lang.display} is markup rather than a program, so there is nothing to "
        f"execute. {lang.notes or 'Write it to a file with file_ops instead.'}",
    )


def _run_source(
    lang, detection, source: str, timeout, stdin, args, hud
) -> ToolResult:
    """Compile (if needed) and run source in a disposable directory."""
    tool = detection.toolchain
    workdir = tempfile.mkdtemp(prefix="jarvis_exec_")
    started = time.perf_counter()

    try:
        # Java insists the file be named after its public class.
        stem = "Program" if lang.key == "java" else "main"
        if lang.key == "java":
            match = _JAVA_CLASS_RE.search(source)
            if match:
                stem = match.group(1)

        source_path = Path(workdir) / f"{stem}{lang.extension}"
        source_path.write_text(source, encoding="utf-8")

        artifact = ""
        if tool.artifact:
            artifact = str(Path(workdir) / tool.artifact.format(stem=stem))

        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
        binary = detection.path or tool.binary

        # Compile step, when the toolchain has one.
        if tool.compile:
            argv = languages.render_command(
                tool.compile, binary=binary, file=str(source_path),
                directory=workdir, stem=stem, artifact=artifact, name=stem,
            )
            try:
                built = subprocess.run(
                    argv, cwd=workdir, capture_output=True, text=True,
                    encoding="utf-8", errors="replace",
                    timeout=settings.CODE_EXEC_COMPILE_TIMEOUT,
                    env=env, creationflags=_NO_WINDOW,
                )
            except subprocess.TimeoutExpired:
                return ToolResult(
                    "code_executor", False,
                    f"The {lang.display} compiler did not finish within "
                    f"{settings.CODE_EXEC_COMPILE_TIMEOUT:.0f}s.",
                )
            if built.returncode != 0:
                diagnostics = _truncate(
                    (built.stderr or "") + (built.stdout or ""),
                    settings.CODE_EXEC_MAX_OUTPUT, "compiler output",
                )
                return ToolResult(
                    "code_executor", False,
                    f"**{lang.display} compilation failed** "
                    f"(exit {built.returncode})\n\n```\n{diagnostics.strip()}\n```",
                    {"language": lang.key, "stage": "compile",
                     "exit_code": built.returncode},
                )

        # Run step.
        argv = languages.render_command(
            tool.run, binary=binary, file=str(source_path),
            directory=workdir, stem=stem, artifact=artifact, name=stem,
        )
        if args:
            argv += [str(a) for a in args]

        # SQL is fed to the shell on stdin; everything else takes a file path.
        payload = source if lang.key == "sql" else stdin

        limit = _positive(timeout, settings.CODE_EXEC_TIMEOUT)
        try:
            proc = subprocess.run(
                argv, cwd=workdir, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=limit,
                input=payload, env=env, creationflags=_NO_WINDOW,
            )
            stdout, stderr, code = proc.stdout or "", proc.stderr or "", proc.returncode
            timed_out = False
        except subprocess.TimeoutExpired as exc:
            stdout, stderr = _decode(exc.stdout), _decode(exc.stderr)
            code, timed_out = -1, True
        except OSError as exc:
            return ToolResult(
                "code_executor", False,
                f"Could not launch {lang.display}: {exc}",
            )

        duration = time.perf_counter() - started
        label = tool.describe()
        body = [
            f"**{lang.display}** via `{label}`"
            + (f" {detection.version}" if detection.version else "")
        ]
        if timed_out:
            body.append(f"\n**Timed out after {limit:.0f}s** — partial output below.")
        if stdout.strip():
            body += ["", "**Output**", "```",
                     _truncate(stdout, settings.CODE_EXEC_MAX_OUTPUT), "```"]
        if stderr.strip():
            body += ["", "**stderr**", "```",
                     _truncate(stderr, settings.CODE_EXEC_MAX_OUTPUT), "```"]
        if not stdout.strip() and not stderr.strip():
            body.append("\n(ran cleanly with no output)")
        body.append(f"\nexit code {code}, {duration * 1000:.0f} ms")

        if hud is not None:
            _safe(hud.render_code, source, lang.highlight, f"{lang.display} — executed")

        return ToolResult(
            "code_executor",
            ok=(code == 0 and not timed_out),
            content="\n".join(body),
            data={
                "language": lang.key, "binary": tool.binary,
                "version": detection.version, "exit_code": code,
                "stdout": _truncate(stdout, 4000), "stderr": _truncate(stderr, 2000),
                "compiled": bool(tool.compile), "timed_out": timed_out,
                "duration_ms": round(duration * 1000),
            },
        )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _do_file_op(action: str, target: Path, content: str | None, hud) -> ToolResult:
    """The filesystem verbs, once the path has been vetted."""
    if action == "exists":
        kind = "directory" if target.is_dir() else "file" if target.exists() else "nothing"
        return ToolResult(
            "file_ops", True,
            f"`{target}` — {kind if kind != 'nothing' else 'does not exist'}.",
            {"exists": target.exists(), "kind": kind},
        )

    if action == "read":
        if not target.is_file():
            return ToolResult("file_ops", False, f"No such file: `{target}`")
        size = target.stat().st_size
        limit = settings.FILE_OPS_MAX_BYTES
        with target.open("r", encoding="utf-8", errors="replace") as handle:
            text = handle.read(limit + 1)
        truncated = len(text) > limit
        text = text[:limit]
        lang = languages.language_for_path(target) or "text"
        if hud is not None:
            _safe(hud.render_code, text[:4000], languages.highlight_for(str(target)), str(target.name))
        note = (
            f"\n\n*(truncated at {format_bytes(limit)} of {format_bytes(size)})*"
            if truncated else ""
        )
        return ToolResult(
            "file_ops", True,
            f"`{target}` ({format_bytes(size)}, {lang})\n\n```{lang}\n{text}\n```{note}",
            {"path": str(target), "bytes": size, "truncated": truncated,
             "language": lang},
        )

    if action in {"write", "append"}:
        if content is None:
            return ToolResult("file_ops", False, f"'{action}' needs content to write.")
        if action == "write" and target.is_file():
            existing = target.stat().st_size
            if existing > settings.FILE_OPS_MAX_BYTES:
                return ToolResult(
                    "file_ops", False,
                    f"`{target}` is {format_bytes(existing)} — larger than the "
                    f"overwrite threshold. Confirm explicitly before I replace it.",
                )
        target.parent.mkdir(parents=True, exist_ok=True)
        mode = "a" if action == "append" else "w"
        with target.open(mode, encoding="utf-8", errors="replace", newline="") as handle:
            handle.write(content)
        written = len(content.encode("utf-8"))
        if hud is not None:
            _safe(hud.render_code, content[:4000],
                  languages.highlight_for(str(target)), str(target))
        verb = "Appended" if action == "append" else "Wrote"
        return ToolResult(
            "file_ops", True,
            f"{verb} {format_bytes(written)} to `{target}`.",
            {"path": str(target), "bytes": written, "action": action},
        )

    if action == "list":
        if not target.is_dir():
            return ToolResult("file_ops", False, f"Not a directory: `{target}`")
        entries = sorted(
            target.iterdir(), key=lambda p: (p.is_file(), p.name.lower())
        )[:200]
        rows = []
        for entry in entries:
            try:
                marker = "dir " if entry.is_dir() else "file"
                size = "--" if entry.is_dir() else format_bytes(entry.stat().st_size)
            except OSError:
                marker, size = "?   ", "--"
            rows.append(f"| {marker} | `{entry.name}` | {size} |")
        return ToolResult(
            "file_ops", True,
            f"`{target}` — {len(entries)} entries\n\n"
            "| Kind | Name | Size |\n|---|---|---|\n" + "\n".join(rows),
            {"path": str(target), "count": len(entries)},
        )

    if action == "info":
        if not target.exists():
            return ToolResult("file_ops", False, f"`{target}` does not exist.")
        stat = target.stat()
        return ToolResult(
            "file_ops", True,
            f"`{target}`\n\n"
            f"- kind: {'directory' if target.is_dir() else 'file'}\n"
            f"- size: {format_bytes(stat.st_size)}\n"
            f"- modified: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(stat.st_mtime))}",
            {"path": str(target), "bytes": stat.st_size, "mtime": stat.st_mtime},
        )

    if action == "mkdir":
        target.mkdir(parents=True, exist_ok=True)
        return ToolResult("file_ops", True, f"Created `{target}`.", {"path": str(target)})

    if action == "delete":
        if not target.exists():
            return ToolResult("file_ops", False, f"`{target}` does not exist.")
        if target.is_dir():
            if any(target.iterdir()):
                return ToolResult(
                    "file_ops", False,
                    f"`{target}` is not empty. I do not delete populated directories.",
                )
            target.rmdir()
        else:
            target.unlink()
        return ToolResult("file_ops", True, f"Deleted `{target}`.", {"path": str(target)})

    return ToolResult(
        "file_ops", False,
        f"Unknown action '{action}'. Valid: read, write, append, list, info, exists, "
        f"mkdir, delete.",
    )
