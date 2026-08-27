"""Programming-language toolchains: what this machine can actually build and run.

J.A.R.V.I.S. writes code in any language. Whether he can *run* it depends on what is
installed, and that is a property of the host, not of his ambition. This module answers
one question honestly: for a given language, is there a working toolchain here, which
binary is it, and what version?

Two things are deliberately separated:

* **The registry** -- :data:`LANGUAGES` describes ~35 languages declaratively: file
  extension, syntax-highlighting lexer, aliases, and an ordered list of candidate
  :class:`Toolchain` recipes. Nothing in the registry touches the filesystem.
* **Detection** -- :func:`detect` probes PATH, runs the version command, and caches the
  answer. Probing three dozen binaries is slow enough that it must happen once per day,
  not once per tool call.

Naming discipline: this module is about *programming* languages. Human languages
(Hindi, Tamil, ...) live in :mod:`jarvis.locales`. The two are never conflated.

Imports :mod:`config` and the standard library only, so it sits at the bottom of the
dependency graph beside :mod:`jarvis.monitor`.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from config import PROJECT_ROOT, settings

logger = logging.getLogger(__name__)

#: How long a probe result stays trustworthy before we look again.
_CACHE_FILE = PROJECT_ROOT / ".jarvis_cache" / "toolchains.json"

#: Probing a binary that has decided to be interactive must not hang the agent.
_PROBE_TIMEOUT = 8.0

_VERSION_RE = re.compile(r"(\d+(?:\.\d+)*)")

# Windows hides console windows for subprocesses only if asked; probing 35 binaries
# should not flash 35 console windows across the operator's screen.
if sys.platform == "win32":  # pragma: no cover - platform specific
    _NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
else:  # pragma: no cover - platform specific
    _NO_WINDOW = 0


# ══════════════════════════════════════════════════════════════════════════════════════
# Registry types
# ══════════════════════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class Toolchain:
    """One concrete way to build and/or run a language on this machine.

    ``run`` and ``compile`` are argv templates. Each element is formatted with the
    placeholders ``{binary} {file} {dir} {stem} {artifact} {name}``; an element that
    formats to an empty string is dropped, which lets a single template serve
    invocations that sometimes need an extra flag and sometimes do not.
    """

    binary: str
    version_args: tuple[str, ...] = ("--version",)
    run: tuple[str, ...] = ()
    compile: tuple[str, ...] | None = None
    artifact: str | None = None
    min_major: int | None = None
    label: str = ""
    install_hint: str = ""

    def describe(self) -> str:
        """Human-readable name for this recipe, e.g. ``gcc`` or ``node (type-stripping)``."""
        return self.label or self.binary


@dataclass(frozen=True)
class Language:
    """A programming language and every way we know of to execute it."""

    key: str
    display: str
    extension: str
    highlight: str
    aliases: tuple[str, ...] = ()
    toolchains: tuple[Toolchain, ...] = ()
    executable: bool = True
    notes: str = ""


@dataclass
class Detection:
    """The result of probing one language's toolchains."""

    language: str
    available: bool
    binary: str | None = None
    path: str | None = None
    version: str | None = None
    major: int | None = None
    toolchain: Toolchain | None = None
    error: str = ""

    @property
    def display(self) -> str:
        lang = LANGUAGES.get(self.language)
        return lang.display if lang else self.language

    def describe(self) -> str:
        """One-line status, e.g. ``Python 3.11.15`` or ``Go -- not installed``."""
        if not self.available:
            return f"{self.display} -- not installed"
        bits = [self.display]
        if self.binary:
            label = self.toolchain.describe() if self.toolchain else self.binary
            bits.append(f"({label}{' ' + self.version if self.version else ''})")
        return " ".join(bits)


# ══════════════════════════════════════════════════════════════════════════════════════
# The registry
# ══════════════════════════════════════════════════════════════════════════════════════

_PY = sys.executable or "python"


def _lang(*args, **kwargs) -> Language:
    """Small constructor shim so the table below stays readable."""
    return Language(*args, **kwargs)


LANGUAGES: dict[str, Language] = {
    lang.key: lang
    for lang in (
        _lang(
            key="python",
            display="Python",
            extension=".py",
            highlight="python",
            aliases=("py", "python3", "cpython"),
            toolchains=(
                Toolchain(_PY, ("--version",), ("{binary}", "{file}"),
                          label="project interpreter",
                          install_hint="https://www.python.org/downloads/"),
                Toolchain("python3", ("--version",), ("{binary}", "{file}")),
                Toolchain("python", ("--version",), ("{binary}", "{file}")),
            ),
        ),
        _lang(
            key="javascript",
            display="JavaScript",
            extension=".js",
            highlight="javascript",
            aliases=("js", "node", "nodejs", "ecmascript"),
            toolchains=(
                Toolchain("node", ("--version",), ("{binary}", "{file}"),
                          install_hint="winget install OpenJS.NodeJS"),
                Toolchain("deno", ("--version",), ("{binary}", "run", "-A", "{file}")),
                Toolchain("bun", ("--version",), ("{binary}", "run", "{file}")),
            ),
        ),
        _lang(
            key="typescript",
            display="TypeScript",
            extension=".ts",
            highlight="typescript",
            aliases=("ts",),
            toolchains=(
                Toolchain("deno", ("--version",), ("{binary}", "run", "-A", "{file}"),
                          label="deno"),
                Toolchain("bun", ("--version",), ("{binary}", "run", "{file}"), label="bun"),
                Toolchain("tsx", ("--version",), ("{binary}", "{file}"), label="tsx"),
                Toolchain("ts-node", ("--version",), ("{binary}", "{file}"), label="ts-node"),
                # Node 22.6+ strips types natively; 23+ does it without a flag.
                Toolchain("node", ("--version",), ("{binary}", "{file}"), min_major=22,
                          label="node (type-stripping)",
                          install_hint="winget install OpenJS.NodeJS"),
            ),
            notes="Node 22+ executes .ts directly by stripping types; no tsc step needed.",
        ),
        _lang(
            key="bash",
            display="Bash",
            extension=".sh",
            highlight="bash",
            aliases=("sh", "shell", "zsh"),
            toolchains=(
                Toolchain("bash", ("--version",), ("{binary}", "{file}")),
                Toolchain("sh", ("--version",), ("{binary}", "{file}")),
            ),
        ),
        _lang(
            key="powershell",
            display="PowerShell",
            extension=".ps1",
            highlight="powershell",
            aliases=("pwsh", "ps1", "ps"),
            toolchains=(
                Toolchain("pwsh", ("-Version",),
                          ("{binary}", "-NoProfile", "-File", "{file}"),
                          label="pwsh 7"),
                Toolchain("powershell", ("-Command", "$PSVersionTable.PSVersion.ToString()"),
                          ("{binary}", "-NoProfile", "-ExecutionPolicy", "Bypass",
                           "-File", "{file}"),
                          label="Windows PowerShell"),
            ),
        ),
        _lang(
            key="batch",
            display="Batch",
            extension=".bat",
            highlight="batch",
            aliases=("cmd", "bat", "dos"),
            toolchains=(
                Toolchain("cmd", ("/c", "ver"), ("{binary}", "/c", "{file}")),
            ),
        ),
        _lang(
            key="c",
            display="C",
            extension=".c",
            highlight="c",
            aliases=("ansi-c", "c99", "c11"),
            toolchains=(
                Toolchain("gcc", ("--version",), ("{artifact}",),
                          ("{binary}", "-O2", "-std=c17", "-o", "{artifact}", "{file}"),
                          artifact="{stem}.exe",
                          install_hint="winget install BrechtSanders.WinLibs.POSIX.UCRT"),
                Toolchain("clang", ("--version",), ("{artifact}",),
                          ("{binary}", "-O2", "-o", "{artifact}", "{file}"),
                          artifact="{stem}.exe",
                          install_hint="winget install LLVM.LLVM"),
                Toolchain("cl", ("/?",), ("{artifact}",),
                          ("{binary}", "/nologo", "/Fe:{artifact}", "{file}"),
                          artifact="{stem}.exe", label="MSVC cl"),
                Toolchain("tcc", ("-v",), ("{binary}", "-run", "{file}"), label="tcc -run"),
            ),
        ),
        _lang(
            key="cpp",
            display="C++",
            extension=".cpp",
            highlight="cpp",
            aliases=("c++", "cplusplus", "cxx", "cc"),
            toolchains=(
                Toolchain("g++", ("--version",), ("{artifact}",),
                          ("{binary}", "-O2", "-std=c++20", "-o", "{artifact}", "{file}"),
                          artifact="{stem}.exe",
                          install_hint="winget install BrechtSanders.WinLibs.POSIX.UCRT"),
                Toolchain("clang++", ("--version",), ("{artifact}",),
                          ("{binary}", "-O2", "-std=c++20", "-o", "{artifact}", "{file}"),
                          artifact="{stem}.exe"),
                Toolchain("cl", ("/?",), ("{artifact}",),
                          ("{binary}", "/nologo", "/EHsc", "/std:c++20",
                           "/Fe:{artifact}", "{file}"),
                          artifact="{stem}.exe", label="MSVC cl"),
            ),
        ),
        _lang(
            key="csharp",
            display="C#",
            extension=".cs",
            highlight="csharp",
            aliases=("cs", "c#", "dotnet"),
            toolchains=(
                # .NET 10 runs a single .cs file directly -- no project scaffolding.
                Toolchain("dotnet", ("--version",), ("{binary}", "run", "{file}"),
                          min_major=10, label="dotnet file-based app",
                          install_hint="winget install Microsoft.DotNet.SDK.10"),
                Toolchain("csc", ("/?",), ("{artifact}",),
                          ("{binary}", "/nologo", "/out:{artifact}", "{file}"),
                          artifact="{stem}.exe"),
                Toolchain("mcs", ("--version",), ("mono", "{artifact}"),
                          ("{binary}", "-out:{artifact}", "{file}"),
                          artifact="{stem}.exe", label="mono/mcs"),
            ),
        ),
        _lang(
            key="fsharp",
            display="F#",
            extension=".fsx",
            highlight="fsharp",
            aliases=("fs", "f#"),
            toolchains=(
                Toolchain("dotnet", ("--version",), ("{binary}", "fsi", "{file}"),
                          label="dotnet fsi",
                          install_hint="winget install Microsoft.DotNet.SDK.10"),
            ),
        ),
        _lang(
            key="java",
            display="Java",
            extension=".java",
            highlight="java",
            aliases=("jdk", "openjdk"),
            toolchains=(
                # Java 11+ runs a single source file without a separate compile step.
                Toolchain("java", ("-version",), ("{binary}", "{file}"), min_major=11,
                          label="java single-file",
                          install_hint="winget install EclipseAdoptium.Temurin.21.JDK"),
                Toolchain("javac", ("-version",), ("java", "-cp", "{dir}", "{name}"),
                          ("{binary}", "{file}"), label="javac + java",
                          install_hint="winget install EclipseAdoptium.Temurin.21.JDK"),
            ),
            notes="A public class must match the filename; the executor renames the file to suit.",
        ),
        _lang(
            key="kotlin",
            display="Kotlin",
            extension=".kts",
            highlight="kotlin",
            aliases=("kt",),
            toolchains=(
                Toolchain("kotlinc", ("-version",), ("{binary}", "-script", "{file}"),
                          label="kotlinc -script",
                          install_hint="winget install JetBrains.Kotlin"),
            ),
        ),
        _lang(
            key="go",
            display="Go",
            extension=".go",
            highlight="go",
            aliases=("golang",),
            toolchains=(
                Toolchain("go", ("version",), ("{binary}", "run", "{file}"),
                          install_hint="winget install GoLang.Go"),
            ),
        ),
        _lang(
            key="rust",
            display="Rust",
            extension=".rs",
            highlight="rust",
            aliases=("rs", "cargo"),
            toolchains=(
                Toolchain("rustc", ("--version",), ("{artifact}",),
                          ("{binary}", "-O", "-o", "{artifact}", "{file}"),
                          artifact="{stem}.exe",
                          install_hint="winget install Rustlang.Rustup"),
            ),
        ),
        _lang(
            key="ruby",
            display="Ruby",
            extension=".rb",
            highlight="ruby",
            aliases=("rb",),
            toolchains=(
                Toolchain("ruby", ("--version",), ("{binary}", "{file}"),
                          install_hint="winget install RubyInstallerTeam.Ruby.3.3"),
            ),
        ),
        _lang(
            key="php",
            display="PHP",
            extension=".php",
            highlight="php",
            toolchains=(
                Toolchain("php", ("--version",), ("{binary}", "{file}"),
                          install_hint="winget install PHP.PHP.8.3"),
            ),
        ),
        _lang(
            key="perl",
            display="Perl",
            extension=".pl",
            highlight="perl",
            aliases=("pl",),
            toolchains=(
                Toolchain("perl", ("--version",), ("{binary}", "{file}"),
                          install_hint="winget install StrawberryPerl.StrawberryPerl"),
            ),
        ),
        _lang(
            key="lua",
            display="Lua",
            extension=".lua",
            highlight="lua",
            toolchains=(
                Toolchain("lua", ("-v",), ("{binary}", "{file}")),
                Toolchain("luajit", ("-v",), ("{binary}", "{file}")),
            ),
        ),
        _lang(
            key="r",
            display="R",
            extension=".R",
            highlight="r",
            aliases=("rlang", "rscript"),
            toolchains=(
                Toolchain("Rscript", ("--version",), ("{binary}", "{file}"),
                          install_hint="winget install RProject.R"),
            ),
        ),
        _lang(
            key="julia",
            display="Julia",
            extension=".jl",
            highlight="julia",
            aliases=("jl",),
            toolchains=(
                Toolchain("julia", ("--version",), ("{binary}", "{file}"),
                          install_hint="winget install Julialang.Julia"),
            ),
        ),
        _lang(
            key="dart",
            display="Dart",
            extension=".dart",
            highlight="dart",
            toolchains=(
                Toolchain("dart", ("--version",), ("{binary}", "run", "{file}"),
                          install_hint="winget install Google.DartSDK"),
            ),
        ),
        _lang(
            key="swift",
            display="Swift",
            extension=".swift",
            highlight="swift",
            toolchains=(
                Toolchain("swift", ("--version",), ("{binary}", "{file}"),
                          install_hint="winget install Swift.Toolchain"),
            ),
        ),
        _lang(
            key="zig",
            display="Zig",
            extension=".zig",
            highlight="zig",
            toolchains=(
                Toolchain("zig", ("version",), ("{binary}", "run", "{file}"),
                          install_hint="winget install zig.zig"),
            ),
        ),
        _lang(
            key="nim",
            display="Nim",
            extension=".nim",
            highlight="nim",
            toolchains=(
                Toolchain("nim", ("--version",),
                          ("{binary}", "r", "--hints:off", "{file}")),
            ),
        ),
        _lang(
            key="haskell",
            display="Haskell",
            extension=".hs",
            highlight="haskell",
            aliases=("hs", "ghc"),
            toolchains=(
                Toolchain("runghc", ("--version",), ("{binary}", "{file}")),
                Toolchain("runhaskell", ("--version",), ("{binary}", "{file}")),
                Toolchain("ghc", ("--version",), ("{artifact}",),
                          ("{binary}", "-o", "{artifact}", "{file}"),
                          artifact="{stem}.exe"),
            ),
        ),
        _lang(
            key="scala",
            display="Scala",
            extension=".scala",
            highlight="scala",
            toolchains=(
                Toolchain("scala-cli", ("--version",), ("{binary}", "run", "{file}")),
                Toolchain("scala", ("-version",), ("{binary}", "{file}")),
            ),
        ),
        _lang(
            key="elixir",
            display="Elixir",
            extension=".exs",
            highlight="elixir",
            aliases=("ex",),
            toolchains=(
                Toolchain("elixir", ("--version",), ("{binary}", "{file}")),
            ),
        ),
        _lang(
            key="groovy",
            display="Groovy",
            extension=".groovy",
            highlight="groovy",
            toolchains=(
                Toolchain("groovy", ("--version",), ("{binary}", "{file}")),
            ),
        ),
        _lang(
            key="ocaml",
            display="OCaml",
            extension=".ml",
            highlight="ocaml",
            toolchains=(
                Toolchain("ocaml", ("-version",), ("{binary}", "{file}")),
            ),
        ),
        _lang(
            key="sql",
            display="SQL",
            extension=".sql",
            highlight="sql",
            aliases=("sqlite", "sqlite3"),
            toolchains=(
                # -batch keeps the shell from going interactive and hanging the run.
                Toolchain("sqlite3", ("--version",),
                          ("{binary}", "-batch", "-box", ":memory:"),
                          label="sqlite3 (in-memory)",
                          install_hint="winget install SQLite.SQLite"),
            ),
            notes="The script is fed on stdin against a fresh in-memory database.",
        ),
        _lang(
            key="vbscript",
            display="VBScript",
            extension=".vbs",
            highlight="vbnet",
            aliases=("vbs",),
            toolchains=(
                Toolchain("cscript", ("/?",), ("{binary}", "//NoLogo", "{file}")),
            ),
        ),
        _lang(
            key="awk",
            display="AWK",
            extension=".awk",
            highlight="awk",
            toolchains=(
                Toolchain("awk", ("--version",), ("{binary}", "-f", "{file}")),
                Toolchain("gawk", ("--version",), ("{binary}", "-f", "{file}")),
            ),
        ),
        _lang(key="html", display="HTML", extension=".html", highlight="html",
              executable=False, notes="Written and checked, not executed."),
        _lang(key="css", display="CSS", extension=".css", highlight="css",
              executable=False, notes="Written and checked, not executed."),
        _lang(key="json", display="JSON", extension=".json", highlight="json",
              executable=False, notes="Validated with json.loads rather than executed."),
        _lang(key="yaml", display="YAML", extension=".yaml", highlight="yaml",
              aliases=("yml",), executable=False,
              notes="Validated when PyYAML is importable."),
        _lang(key="markdown", display="Markdown", extension=".md", highlight="markdown",
              aliases=("md",), executable=False),
    )
}

#: alias -> canonical key, built once.
_ALIASES: dict[str, str] = {}
for _lang_obj in LANGUAGES.values():
    _ALIASES[_lang_obj.key] = _lang_obj.key
    _ALIASES[_lang_obj.display.lower()] = _lang_obj.key
    _ALIASES[_lang_obj.extension.lstrip(".").lower()] = _lang_obj.key
    for _alias in _lang_obj.aliases:
        _ALIASES[_alias.lower()] = _lang_obj.key


# ══════════════════════════════════════════════════════════════════════════════════════
# Lookup helpers
# ══════════════════════════════════════════════════════════════════════════════════════


def normalise(name: str) -> str | None:
    """Resolve an alias, display name or bare extension to a canonical language key."""
    if not name:
        return None
    key = str(name).strip().lower().lstrip(".")
    return _ALIASES.get(key)


def get_language(name: str) -> Language | None:
    """Look up a :class:`Language` by any of its names."""
    key = normalise(name)
    return LANGUAGES.get(key) if key else None


def language_for_path(path: str | Path) -> str | None:
    """Infer the language key from a file extension."""
    suffix = Path(str(path)).suffix.lower()
    return _ALIASES.get(suffix.lstrip(".")) if suffix else None


def highlight_for(name: str) -> str:
    """Pygments lexer name for ``rich.syntax.Syntax``; ``"text"`` when unknown."""
    lang = get_language(name)
    if lang:
        return lang.highlight
    by_path = language_for_path(name)
    if by_path:
        return LANGUAGES[by_path].highlight
    return "text"


# Ordered most-specific-first: the first pattern that matches wins.
_SYNTAX_HINTS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("python", re.compile(r"^\s*(?:def |class |import |from \w+ import|print\()", re.M)),
    ("javascript", re.compile(r"\b(?:console\.log|=>|const |let |require\()")),
    ("typescript", re.compile(r"\b(?:interface |type \w+ =|: (?:string|number|boolean)\b)")),
    ("java", re.compile(r"\bpublic\s+(?:static\s+)?(?:void|class)\b")),
    ("csharp", re.compile(r"\b(?:using System|Console\.WriteLine|namespace )\b")),
    ("cpp", re.compile(r"#include\s*<(?:iostream|vector|string)>|std::")),
    ("c", re.compile(r"#include\s*<(?:stdio|stdlib)\.h>")),
    ("go", re.compile(r"\bpackage main\b|\bfunc main\(\)")),
    ("rust", re.compile(r"\bfn main\(\)|\blet mut\b|println!")),
    ("ruby", re.compile(r"\bputs\b|\bdef \w+\s*$|\bend\s*$", re.M)),
    ("php", re.compile(r"<\?php")),
    ("powershell", re.compile(r"\$\w+\s*=|Write-(?:Host|Output)|Get-\w+")),
    ("bash", re.compile(r"\becho\b|\bfi\b|\bdone\b|\$\{")),
    ("sql", re.compile(r"\b(?:SELECT|INSERT INTO|CREATE TABLE)\b", re.I)),
    ("html", re.compile(r"<(?:html|div|body|!DOCTYPE)", re.I)),
    ("json", re.compile(r"^\s*[\{\[]")),
)


def guess_language(code: str, filename: str = "") -> str:
    """Best-effort language identification: shebang, then filename, then syntax."""
    if filename:
        by_name = language_for_path(filename)
        if by_name:
            return by_name

    text = code or ""
    first = text.lstrip().split("\n", 1)[0] if text.strip() else ""
    if first.startswith("#!"):
        # A shebang is the author telling us outright; believe it.
        for token in reversed(re.split(r"[/\s]+", first)):
            key = normalise(token)
            if key:
                return key

    for key, pattern in _SYNTAX_HINTS:
        if pattern.search(text):
            return key
    return settings.CODE_DEFAULT_LANGUAGE


# ══════════════════════════════════════════════════════════════════════════════════════
# Detection
# ══════════════════════════════════════════════════════════════════════════════════════

_lock = threading.RLock()
_cache: dict[str, Detection] = {}
_cache_stamp: float = 0.0


def _parse_version(text: str) -> tuple[str | None, int | None]:
    """Pull the first dotted number out of a ``--version`` blurb."""
    match = _VERSION_RE.search(text or "")
    if not match:
        return None, None
    version = match.group(1)
    try:
        return version, int(version.split(".", 1)[0])
    except (ValueError, IndexError):
        return version, None


def _probe(tool: Toolchain) -> tuple[bool, str | None, str | None, int | None, str]:
    """Resolve and version-check one candidate.

    Returns ``(ok, path, version, major, error)``. A binary that resolves on PATH counts
    as present even when its version command exits non-zero -- plenty of compilers print
    usage to stderr and exit 1 when asked politely.
    """
    path = shutil.which(tool.binary)
    if not path:
        return False, None, None, None, f"{tool.binary} not found on PATH"

    try:
        proc = subprocess.run(
            [path, *tool.version_args],
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT,
            encoding="utf-8",
            errors="replace",
            creationflags=_NO_WINDOW,
        )
        blurb = f"{proc.stdout or ''}\n{proc.stderr or ''}"
    except subprocess.TimeoutExpired:
        return True, path, None, None, f"{tool.binary} did not answer --version in time"
    except OSError as exc:
        return False, None, None, None, f"{tool.binary} failed to launch: {exc}"

    version, major = _parse_version(blurb)
    if tool.min_major is not None and (major is None or major < tool.min_major):
        return (
            False, path, version, major,
            f"{tool.binary} {version or '?'} is below the required major version "
            f"{tool.min_major}",
        )
    return True, path, version, major, ""


def _detect_uncached(key: str) -> Detection:
    """Try each candidate toolchain in order; first success wins."""
    lang = LANGUAGES.get(key)
    if lang is None:
        return Detection(language=key, available=False, error="unknown language")
    if not lang.executable:
        return Detection(language=key, available=False, error="not an executable language")

    problems: list[str] = []
    for tool in lang.toolchains:
        try:
            ok, path, version, major, error = _probe(tool)
        except Exception as exc:  # a probe must never take the agent down
            logger.debug("Probe of %s raised", tool.binary, exc_info=True)
            problems.append(f"{tool.binary}: {exc}")
            continue
        if ok:
            return Detection(
                language=key, available=True, binary=tool.binary, path=path,
                version=version, major=major, toolchain=tool,
            )
        if error:
            problems.append(error)

    return Detection(language=key, available=False, error="; ".join(problems[:3]))


def _load_disk_cache() -> None:
    """Warm the in-memory cache from disk when it is still inside its TTL."""
    global _cache_stamp
    try:
        if not _CACHE_FILE.exists():
            return
        payload = json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
        stamp = float(payload.get("stamp", 0))
        if time.time() - stamp > settings.TOOLCHAIN_CACHE_TTL:
            return
        for key, row in (payload.get("languages") or {}).items():
            if key not in LANGUAGES:
                continue
            lang = LANGUAGES[key]
            tool = None
            if row.get("binary"):
                tool = next(
                    (t for t in lang.toolchains if t.binary == row["binary"]), None
                )
            _cache[key] = Detection(
                language=key,
                available=bool(row.get("available")),
                binary=row.get("binary"),
                path=row.get("path"),
                version=row.get("version"),
                major=row.get("major"),
                toolchain=tool,
                error=row.get("error", ""),
            )
        _cache_stamp = stamp
    except (OSError, ValueError, TypeError):
        # A corrupt cache is not an error condition; we simply re-probe.
        logger.debug("Toolchain cache unreadable; re-probing", exc_info=True)


def _save_disk_cache() -> None:
    """Persist probe results so the next launch does not pay for them again."""
    try:
        _CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "stamp": time.time(),
            "languages": {
                key: {
                    "available": det.available,
                    "binary": det.binary,
                    "path": det.path,
                    "version": det.version,
                    "major": det.major,
                    "error": det.error,
                }
                for key, det in _cache.items()
            },
        }
        _CACHE_FILE.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    except OSError:
        logger.debug("Could not write the toolchain cache", exc_info=True)


def detect(name: str, refresh: bool = False) -> Detection:
    """Probe one language's toolchains, using the cache unless ``refresh`` is set."""
    key = normalise(name)
    if key is None:
        return Detection(language=str(name), available=False, error="unknown language")

    with _lock:
        if not refresh:
            if not _cache:
                _load_disk_cache()
            cached = _cache.get(key)
            if cached is not None:
                return cached
        detection = _detect_uncached(key)
        _cache[key] = detection
        return detection


def detect_all(refresh: bool = False) -> dict[str, Detection]:
    """Probe every registered language. Slow on a cold cache; cheap thereafter."""
    with _lock:
        if refresh:
            _cache.clear()
        elif not _cache:
            _load_disk_cache()

        missing = [k for k in LANGUAGES if k not in _cache]
        for key in missing:
            _cache[key] = _detect_uncached(key)
        if missing:
            _save_disk_cache()
        return dict(_cache)


def available_languages(refresh: bool = False) -> list[Detection]:
    """Every language this machine can actually execute, in registry order."""
    found = detect_all(refresh)
    return [found[k] for k in LANGUAGES if k in found and found[k].available]


def missing_languages(refresh: bool = False) -> list[Detection]:
    """Executable languages with no working toolchain here."""
    found = detect_all(refresh)
    return [
        found[k]
        for k in LANGUAGES
        if k in found and not found[k].available and LANGUAGES[k].executable
    ]


def toolchain_summary(refresh: bool = False, limit: int = 0) -> str:
    """One dense line for the system prompt.

    The model needs to know what it can run *before* it proposes a language, so this is
    injected into the system prompt rather than left for it to discover by failing.
    """
    try:
        found = available_languages(refresh)
        absent = missing_languages(refresh)
    except Exception:
        logger.debug("Toolchain summary failed", exc_info=True)
        return "toolchain probe unavailable -- ask before assuming."

    if limit and len(found) > limit:
        found = found[:limit]

    online = ", ".join(
        f"{d.display}{' ' + d.version if d.version else ''}" for d in found
    ) or "none detected"
    offline = ", ".join(d.display for d in absent[:14])
    line = f"Executable here: {online}."
    if offline:
        line += f" Not installed: {offline}."
    line += (
        " Non-executable but writable: HTML, CSS, JSON, YAML, Markdown."
    )
    return line


def toolchain_report(refresh: bool = False) -> str:
    """Full Markdown table of every language and its status."""
    found = detect_all(refresh)
    rows = [
        "| Language | Status | Toolchain | Version | Extension |",
        "|---|---|---|---|---|",
    ]
    for key, lang in LANGUAGES.items():
        det = found.get(key)
        if not lang.executable:
            status, tool, version = "write-only", "--", "--"
        elif det and det.available:
            status = "online"
            tool = det.toolchain.describe() if det.toolchain else (det.binary or "--")
            version = det.version or "--"
        else:
            status = "absent"
            tool = "--"
            version = "--"
        rows.append(
            f"| {lang.display} | {status} | {tool} | {version} | `{lang.extension}` |"
        )

    online = sum(1 for d in found.values() if d.available)
    total = sum(1 for lang in LANGUAGES.values() if lang.executable)
    rows.append("")
    rows.append(f"**{online} of {total} executable language toolchains online.**")
    return "\n".join(rows)


def render_command(
    template: tuple[str, ...],
    *,
    binary: str,
    file: str,
    directory: str,
    stem: str,
    artifact: str = "",
    name: str = "",
) -> list[str]:
    """Format an argv template, dropping any element that resolves to nothing."""
    values = {
        "binary": binary,
        "file": file,
        "dir": directory,
        "stem": stem,
        "artifact": artifact,
        "name": name,
    }
    argv: list[str] = []
    for element in template:
        try:
            rendered = element.format(**values)
        except (KeyError, IndexError):
            rendered = element
        if rendered:
            argv.append(rendered)
    return argv


def clear_cache() -> None:
    """Forget every probe result, in memory and on disk."""
    global _cache_stamp
    with _lock:
        _cache.clear()
        _cache_stamp = 0.0
    try:
        _CACHE_FILE.unlink(missing_ok=True)
    except OSError:
        logger.debug("Could not remove the toolchain cache", exc_info=True)


def count_online(refresh: bool = False) -> tuple[int, int]:
    """``(online, total_executable)`` -- used for the boot summary line."""
    found = detect_all(refresh)
    total = sum(1 for lang in LANGUAGES.values() if lang.executable)
    online = sum(1 for d in found.values() if d.available)
    return online, total
