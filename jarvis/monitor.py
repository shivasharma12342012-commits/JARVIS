"""Hardware telemetry and the ambient watch that runs alongside it.

Two jobs live here and they are deliberately separated:

* **Measurement** -- :func:`collect_telemetry` takes one honest snapshot of the
  machine (CPU, memory, disks, battery, GPUs, thermals, processes, network) and
  hands back a plain :class:`Telemetry` dataclass. :func:`telemetry_summary` and
  :func:`telemetry_report` render that snapshot for the model: one dense line for
  a system note, or full Markdown when the operator actually asked.
* **Vigilance** -- :class:`AmbientMonitor` runs a daemon thread that samples on an
  interval, compares the numbers against the thresholds in ``config.settings``,
  and raises pre-written :class:`Alert` objects. J.A.R.V.I.S. is expected to
  mention a dying battery before it dies, not afterwards.

This module imports :mod:`config` and :mod:`psutil` and nothing else from the
project. It sits at the bottom of the dependency graph, so anything above it may
import it freely without risking a cycle.

Alert prose is generated at *call* time, never at import time, because the
operator's chosen form of address (``settings.USER_TITLE``) may be decided by
onboarding long after this module has been imported.
"""

from __future__ import annotations

import logging
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

import psutil

from config import settings

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------------------
# Tuning constants that are implementation detail rather than operator policy. Anything
# an operator would plausibly want to change lives in config.py instead.
# --------------------------------------------------------------------------------------
_BYTES_PER_MB = 1024.0**2
_BYTES_PER_GB = 1024.0**3

# The CPU alert only fires on the second consecutive breach; a single 100% sample is
# usually a compiler starting up, not a problem worth interrupting the operator over.
_CPU_BREACHES_REQUIRED = 2

# psutil's non-blocking cpu_percent() reports load since the previous call, so two calls
# in quick succession produce noise. Below this gap we pay for a short blocking sample.
_CPU_MIN_SAMPLE_GAP = 0.12

# Build-log scanning limits. Kept tight: this runs on every monitor tick.
_LOG_SCAN_MAX_DEPTH = 3
_LOG_SCAN_MAX_FILES = 40
_LOG_TAIL_BYTES = 8 * 1024
_LOG_SKIP_DIRS = frozenset({"node_modules", ".git", ".venv", "__pycache__"})
_LOG_ERROR_PATTERN = re.compile(r"error|traceback|failed|exception", re.IGNORECASE)

_NVIDIA_FIELDS = "name,memory.total,memory.used,utilization.gpu,temperature.gpu"
# Windows would otherwise flash a console window for every nvidia-smi invocation.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# ASCII only: an alert line may be printed to a legacy console with no Live layer.
_SEVERITY_MARKS = {"info": "i", "warning": "!", "critical": "!!"}

# Filesystem types that are not real storage and only clutter the disk table.
_PSEUDO_FSTYPES = frozenset(
    {"", "tmpfs", "devtmpfs", "squashfs", "overlay", "proc", "sysfs", "devfs", "autofs"}
)

_cpu_state_lock = threading.Lock()
_last_cpu_sample: float = 0.0

_gpu_state_lock = threading.Lock()
# None = not probed yet, False = nvidia-smi is absent so stop paying for the subprocess.
_nvidia_available: bool | None = None


def _prime_counters() -> None:
    """Warm psutil's delta counters so the first real reading means something.

    Import-time work is normally a smell, but ``cpu_percent`` is a difference
    engine: without a reference sample its first answer is a flat, dishonest zero.
    """
    global _last_cpu_sample
    try:
        psutil.cpu_percent(interval=None, percpu=True)
        psutil.cpu_percent(interval=None)
        _last_cpu_sample = time.monotonic()
    except Exception:  # pragma: no cover - psutil is very unlikely to fail here
        logger.debug("Could not prime CPU counters", exc_info=True)


_prime_counters()


# ======================================================================================
# Data model
# ======================================================================================
@dataclass(slots=True)
class DiskInfo:
    """One mounted filesystem worth reporting on."""

    device: str
    mountpoint: str
    total_gb: float
    used_gb: float
    percent: float


@dataclass(slots=True)
class GpuInfo:
    """A discrete GPU as reported by ``nvidia-smi``; fields are None when unknown."""

    name: str
    memory_total_mb: float | None = None
    memory_used_mb: float | None = None
    utilization_percent: float | None = None
    temperature_c: float | None = None


@dataclass(slots=True)
class ProcInfo:
    """A single process in the heaviest-consumers table."""

    pid: int
    name: str
    cpu_percent: float
    memory_mb: float


@dataclass(slots=True)
class Telemetry:
    """One complete snapshot of the machine at a single instant."""

    timestamp: float
    cpu_percent: float
    cpu_per_core: list[float]
    cpu_count: int
    cpu_freq_mhz: float | None
    load_avg: tuple[float, float, float] | None
    ram_total_gb: float
    ram_used_gb: float
    ram_available_gb: float
    ram_percent: float
    swap_percent: float
    disks: list[DiskInfo]
    battery_percent: float | None
    battery_plugged: bool | None
    battery_minutes_left: int | None
    gpus: list[GpuInfo]
    temperatures: dict[str, float]
    process_count: int
    top_processes: list[ProcInfo]
    boot_time: float
    uptime_seconds: float
    net_sent_mb: float
    net_recv_mb: float
    platform: str
    python_version: str


@dataclass(slots=True)
class Alert:
    """A threshold breach, already written in J.A.R.V.I.S.'s own voice.

    ``key`` is the cooldown identity (``"cpu"``, ``"disk:C:\\"``, ``"build:<path>"``)
    rather than anything meant for display. ``message`` is spoken verbatim, so it is
    composed at generation time and already carries ``settings.USER_TITLE``.
    """

    key: str
    severity: str
    title: str
    message: str
    suggestion: str
    timestamp: float = field(default_factory=time.time)

    def format_line(self) -> str:
        """Render a single ASCII line suitable for the HUD or a legacy console."""
        stamp = datetime.fromtimestamp(self.timestamp).strftime("%H:%M:%S")
        mark = _SEVERITY_MARKS.get(self.severity, "-")
        line = f"[{stamp}] {mark} {self.title}: {self.message}"
        if self.suggestion:
            line = f"{line} -> {self.suggestion}"
        return line


# ======================================================================================
# Formatting helpers
# ======================================================================================
def format_bytes(n: float) -> str:
    """Render a byte count in binary units, e.g. ``1536`` -> ``"1.5 KB"``."""
    try:
        value = float(n)
    except (TypeError, ValueError):
        return "n/a"
    sign = "-" if value < 0 else ""
    value = abs(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024.0 or unit == "TB":
            precision = 0 if unit == "B" else 1
            return f"{sign}{value:.{precision}f} {unit}"
        value /= 1024.0
    return f"{sign}{value:.1f} TB"  # pragma: no cover - the loop always returns first


def _format_duration(seconds: float) -> str:
    """Human uptime: ``"3 d 4 h"``, trimmed to the two largest useful units."""
    total = max(0, int(seconds))
    days, rem = divmod(total, 86_400)
    hours, rem = divmod(rem, 3_600)
    minutes = rem // 60
    if days:
        return f"{days} d {hours} h"
    if hours:
        return f"{hours} h {minutes} m"
    return f"{minutes} m"


def _hottest(temperatures: dict[str, float]) -> tuple[str, float] | None:
    """Return the hottest ``(label, celsius)`` pair, or None when nothing is readable."""
    if not temperatures:
        return None
    label, value = max(temperatures.items(), key=lambda kv: kv[1])
    return label, value


def _busiest_disk(disks: list[DiskInfo]) -> DiskInfo | None:
    """The fullest filesystem -- the only one worth a slot in a one-line summary."""
    return max(disks, key=lambda d: d.percent) if disks else None


def _to_float(raw: str) -> float | None:
    """Parse an ``nvidia-smi`` cell; ``[N/A]`` and blanks become None."""
    try:
        return round(float(raw), 1)
    except (TypeError, ValueError):
        return None


# ======================================================================================
# Collection
# ======================================================================================
def _sample_cpu() -> tuple[float, list[float]]:
    """Sample overall and per-core CPU load, blocking briefly only when forced."""
    global _last_cpu_sample
    with _cpu_state_lock:
        now = time.monotonic()
        gap = now - _last_cpu_sample
        _last_cpu_sample = now
    interval = None if gap >= _CPU_MIN_SAMPLE_GAP else _CPU_MIN_SAMPLE_GAP
    try:
        per_core = [float(x) for x in psutil.cpu_percent(interval=interval, percpu=True)]
    except Exception:
        logger.debug("Per-core CPU sampling failed", exc_info=True)
        per_core = []
    if per_core:
        # Derived from the same sample as the per-core figures, so the headline number
        # can never contradict the bars drawn beside it in the HUD.
        total = round(sum(per_core) / len(per_core), 1)
    else:
        try:
            total = float(psutil.cpu_percent(interval=None))
        except Exception:
            logger.debug("Aggregate CPU sampling failed", exc_info=True)
            total = 0.0
    return total, per_core


def _cpu_frequency() -> float | None:
    """Current CPU clock in MHz; None on hosts that do not expose it."""
    try:
        freq = psutil.cpu_freq()
    except Exception:
        # Raises outright on some virtualised Linux hosts and odd laptop firmware.
        logger.debug("cpu_freq unavailable", exc_info=True)
        return None
    if freq is None or not getattr(freq, "current", 0):
        return None
    return round(float(freq.current), 1)


def _load_average() -> tuple[float, float, float] | None:
    """1/5/15 minute load average, or None on platforms without the concept."""
    getter = getattr(os, "getloadavg", None)
    if getter is None:
        return None
    try:
        one, five, fifteen = getter()
    except OSError:
        return None
    return (round(float(one), 2), round(float(five), 2), round(float(fifteen), 2))


def _collect_disks() -> list[DiskInfo]:
    """Usage for every real, readable filesystem."""
    disks: list[DiskInfo] = []
    try:
        partitions = psutil.disk_partitions(all=False)
    except Exception:
        logger.debug("disk_partitions failed", exc_info=True)
        return disks
    for part in partitions:
        opts = (part.opts or "").lower()
        if "cdrom" in opts or (part.fstype or "").lower() in _PSEUDO_FSTYPES:
            continue
        try:
            usage = psutil.disk_usage(part.mountpoint)
        except (PermissionError, OSError):
            # Empty card readers and unmounted optical drives raise here on Windows.
            continue
        if not usage.total:
            continue
        disks.append(
            DiskInfo(
                device=part.device,
                mountpoint=part.mountpoint,
                total_gb=round(usage.total / _BYTES_PER_GB, 2),
                used_gb=round(usage.used / _BYTES_PER_GB, 2),
                percent=round(float(usage.percent), 1),
            )
        )
    return disks


def _collect_battery() -> tuple[float | None, bool | None, int | None]:
    """``(percent, plugged_in, minutes_remaining)`` -- all None on a desktop."""
    getter = getattr(psutil, "sensors_battery", None)
    if getter is None:
        return None, None, None
    try:
        battery = getter()
    except Exception:
        logger.debug("sensors_battery failed", exc_info=True)
        return None, None, None
    if battery is None:
        return None, None, None
    minutes: int | None = None
    secs = getattr(battery, "secsleft", None)
    # psutil signals "unknown" and "plugged in" with large sentinel constants; only a
    # sane, non-negative second count is worth converting.
    if isinstance(secs, int) and 0 <= secs < 60 * 60 * 48:
        minutes = secs // 60
    plugged = battery.power_plugged if battery.power_plugged is not None else None
    return round(float(battery.percent), 1), plugged, minutes


def _collect_temperatures(gpus: list[GpuInfo]) -> dict[str, float]:
    """Every readable thermal sensor, keyed ``chip`` or ``chip:label``.

    GPU dies are folded in under a ``gpu:`` prefix so the thermal alert covers them
    too -- on Windows they are frequently the only temperature anything can read.
    """
    readings: dict[str, float] = {}
    getter = getattr(psutil, "sensors_temperatures", None)
    if getter is not None:
        try:
            raw = getter()
        except Exception:
            logger.debug("sensors_temperatures failed", exc_info=True)
            raw = {}
        for chip, entries in (raw or {}).items():
            for entry in entries:
                current = getattr(entry, "current", None)
                if current is None:
                    continue
                label = (getattr(entry, "label", "") or "").strip()
                key = f"{chip}:{label}" if label else str(chip)
                readings[key] = round(float(current), 1)
    for index, gpu in enumerate(gpus):
        if gpu.temperature_c is not None:
            readings[f"gpu:{gpu.name or index}"] = round(float(gpu.temperature_c), 1)
    return readings


def _collect_processes(top_n: int) -> tuple[int, list[ProcInfo]]:
    """``(process_count, heaviest processes)`` ranked by CPU, then resident memory.

    The very first call after import effectively ranks by memory alone: per-process CPU
    is also a delta measurement and has no reference sample yet. It corrects itself on
    the next sweep, which is soon enough for a background watch.
    """
    procs: list[ProcInfo] = []
    count = 0
    try:
        iterator = psutil.process_iter(["pid", "name", "cpu_percent", "memory_info"])
    except Exception:
        logger.debug("process_iter failed", exc_info=True)
        return 0, procs
    cores = psutil.cpu_count(logical=True) or 1
    for proc in iterator:
        count += 1
        try:
            info = proc.info
            mem = info.get("memory_info")
            rss = float(getattr(mem, "rss", 0.0)) if mem is not None else 0.0
            raw_cpu = float(info.get("cpu_percent") or 0.0)
            procs.append(
                ProcInfo(
                    pid=int(info.get("pid") or 0),
                    name=str(info.get("name") or "?"),
                    # psutil reports per-process CPU as a share of ONE core, so it can
                    # exceed 100%. Normalise so the column is directly comparable with
                    # the machine-wide figure above it.
                    cpu_percent=round(raw_cpu / cores, 1),
                    memory_mb=round(rss / _BYTES_PER_MB, 1),
                )
            )
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
        except Exception:
            logger.debug("Skipping unreadable process", exc_info=True)
            continue
    procs.sort(key=lambda p: (p.cpu_percent, p.memory_mb), reverse=True)
    return count, procs[: max(0, top_n)]


def detect_gpus() -> list[GpuInfo]:
    """Query ``nvidia-smi`` for discrete GPUs. Best effort; ``[]`` on any failure.

    A machine with no NVIDIA card is the common case, so the first missing binary is
    remembered and the subprocess is never spawned again for the rest of the session.
    """
    global _nvidia_available
    with _gpu_state_lock:
        if _nvidia_available is False:
            return []
    binary = shutil.which("nvidia-smi")
    if binary is None:
        with _gpu_state_lock:
            _nvidia_available = False
        return []
    try:
        completed = subprocess.run(
            [binary, f"--query-gpu={_NVIDIA_FIELDS}", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=6.0,
            encoding="utf-8",
            errors="replace",
            creationflags=_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        logger.debug("nvidia-smi invocation failed", exc_info=True)
        with _gpu_state_lock:
            _nvidia_available = False
        return []
    if completed.returncode != 0:
        logger.debug(
            "nvidia-smi exited %s: %s", completed.returncode, (completed.stderr or "").strip()
        )
        with _gpu_state_lock:
            _nvidia_available = False
        return []

    gpus: list[GpuInfo] = []
    for line in (completed.stdout or "").splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 5 or not parts[0]:
            continue
        gpus.append(
            GpuInfo(
                name=parts[0],
                memory_total_mb=_to_float(parts[1]),
                memory_used_mb=_to_float(parts[2]),
                utilization_percent=_to_float(parts[3]),
                temperature_c=_to_float(parts[4]),
            )
        )
    with _gpu_state_lock:
        _nvidia_available = bool(gpus)
    return gpus


def collect_telemetry(top_n: int = 5) -> Telemetry:
    """Take one full snapshot of the machine.

    Every sub-collector degrades to a neutral value rather than raising, so a locked
    sensor or an unreadable mount point can never take the monitor thread down with it.
    """
    now = time.time()
    cpu_total, cpu_cores = _sample_cpu()

    try:
        virtual = psutil.virtual_memory()
        ram_total = virtual.total / _BYTES_PER_GB
        ram_used = virtual.used / _BYTES_PER_GB
        ram_available = virtual.available / _BYTES_PER_GB
        ram_percent = float(virtual.percent)
    except Exception:
        logger.debug("virtual_memory failed", exc_info=True)
        ram_total = ram_used = ram_available = ram_percent = 0.0

    try:
        swap_percent = float(psutil.swap_memory().percent)
    except Exception:
        logger.debug("swap_memory failed", exc_info=True)
        swap_percent = 0.0

    battery_percent, battery_plugged, battery_minutes = _collect_battery()
    gpus = detect_gpus()
    process_count, top_processes = _collect_processes(top_n)

    try:
        boot_time = float(psutil.boot_time())
    except Exception:
        logger.debug("boot_time failed", exc_info=True)
        boot_time = now

    try:
        net = psutil.net_io_counters()
        net_sent = round(net.bytes_sent / _BYTES_PER_MB, 1)
        net_recv = round(net.bytes_recv / _BYTES_PER_MB, 1)
    except Exception:
        logger.debug("net_io_counters failed", exc_info=True)
        net_sent = net_recv = 0.0

    return Telemetry(
        timestamp=now,
        cpu_percent=cpu_total,
        cpu_per_core=cpu_cores,
        cpu_count=psutil.cpu_count(logical=True) or len(cpu_cores) or 1,
        cpu_freq_mhz=_cpu_frequency(),
        load_avg=_load_average(),
        ram_total_gb=round(ram_total, 2),
        ram_used_gb=round(ram_used, 2),
        ram_available_gb=round(ram_available, 2),
        ram_percent=round(ram_percent, 1),
        swap_percent=round(swap_percent, 1),
        disks=_collect_disks(),
        battery_percent=battery_percent,
        battery_plugged=battery_plugged,
        battery_minutes_left=battery_minutes,
        gpus=gpus,
        temperatures=_collect_temperatures(gpus),
        process_count=process_count,
        top_processes=top_processes,
        boot_time=boot_time,
        uptime_seconds=max(0.0, now - boot_time),
        net_sent_mb=net_sent,
        net_recv_mb=net_recv,
        platform=f"{platform.system()} {platform.release()}".strip() or sys.platform,
        python_version=platform.python_version(),
    )


# ======================================================================================
# Rendering for the model
# ======================================================================================
def telemetry_summary(t: Telemetry) -> str:
    """One dense ASCII line -- cheap enough to staple onto a system note every turn."""
    cpu_chunk = f"CPU {t.cpu_percent:.0f}% ({t.cpu_count} cores"
    cpu_chunk += f" @ {t.cpu_freq_mhz:.0f}MHz)" if t.cpu_freq_mhz else ")"
    parts: list[str] = [
        cpu_chunk,
        f"RAM {t.ram_percent:.0f}% ({t.ram_used_gb:.1f}/{t.ram_total_gb:.1f} GB)",
    ]
    if t.swap_percent:
        parts.append(f"SWAP {t.swap_percent:.0f}%")
    if t.load_avg:
        parts.append("LOAD " + "/".join(f"{v:.2f}" for v in t.load_avg))
    disk = _busiest_disk(t.disks)
    if disk is not None:
        free = max(0.0, disk.total_gb - disk.used_gb)
        parts.append(f"DISK {disk.mountpoint} {disk.percent:.0f}% ({free:.0f} GB free)")
    if t.battery_percent is not None:
        state = "charging" if t.battery_plugged else "on battery"
        eta = f", {t.battery_minutes_left} min left" if t.battery_minutes_left else ""
        parts.append(f"BATTERY {t.battery_percent:.0f}% {state}{eta}")
    for gpu in t.gpus:
        chunk = f"GPU {gpu.name}"
        if gpu.utilization_percent is not None:
            chunk += f" {gpu.utilization_percent:.0f}%"
        if gpu.memory_used_mb is not None and gpu.memory_total_mb:
            chunk += f" {gpu.memory_used_mb:.0f}/{gpu.memory_total_mb:.0f}MB"
        if gpu.temperature_c is not None:
            chunk += f" {gpu.temperature_c:.0f}C"
        parts.append(chunk)
    hottest = _hottest(t.temperatures)
    if hottest is not None:
        parts.append(f"TEMP {hottest[0]} {hottest[1]:.0f}C")
    parts.append(f"PROC {t.process_count}")
    parts.append(f"UP {_format_duration(t.uptime_seconds)}")
    parts.append(
        f"NET sent {format_bytes(t.net_sent_mb * _BYTES_PER_MB)}"
        f" / recv {format_bytes(t.net_recv_mb * _BYTES_PER_MB)}"
    )
    parts.append(f"{t.platform} Python {t.python_version}")
    return " | ".join(parts)


def telemetry_report(t: Telemetry) -> str:
    """Full Markdown diagnostics -- what the model shows when the operator asks."""
    stamp = datetime.fromtimestamp(t.timestamp).strftime("%d %b %Y %H:%M:%S")
    lines: list[str] = [f"### System telemetry -- {stamp}", ""]

    freq = f" at {t.cpu_freq_mhz:.0f} MHz" if t.cpu_freq_mhz else ""
    lines.append(f"**CPU** -- {t.cpu_percent:.1f}% across {t.cpu_count} logical cores{freq}")
    if t.cpu_per_core:
        lines.append("- per core: " + " ".join(f"{c:.0f}%" for c in t.cpu_per_core))
    if t.load_avg:
        lines.append(
            "- load average: " + ", ".join(f"{v:.2f}" for v in t.load_avg) + " (1/5/15 min)"
        )
    lines.append("")

    lines.append(
        f"**Memory** -- {t.ram_percent:.1f}% used "
        f"({t.ram_used_gb:.2f} GB of {t.ram_total_gb:.2f} GB, "
        f"{t.ram_available_gb:.2f} GB available); swap at {t.swap_percent:.1f}%"
    )
    lines.append("")

    if t.disks:
        lines.append("**Storage**")
        lines.append("")
        lines.append("| Mount | Device | Used | Total | Free | Load |")
        lines.append("| --- | --- | ---: | ---: | ---: | ---: |")
        for disk in t.disks:
            free = max(0.0, disk.total_gb - disk.used_gb)
            lines.append(
                f"| {disk.mountpoint} | {disk.device} | {disk.used_gb:.1f} GB "
                f"| {disk.total_gb:.1f} GB | {free:.1f} GB | {disk.percent:.1f}% |"
            )
        lines.append("")

    if t.battery_percent is not None:
        state = "charging" if t.battery_plugged else "running on battery"
        eta = (
            f", roughly {t.battery_minutes_left} minutes remaining"
            if t.battery_minutes_left is not None
            else ""
        )
        lines.append(f"**Battery** -- {t.battery_percent:.0f}%, {state}{eta}")
        lines.append("")

    if t.gpus:
        lines.append("**Graphics**")
        lines.append("")
        lines.append("| GPU | Utilisation | Memory | Temperature |")
        lines.append("| --- | ---: | ---: | ---: |")
        for gpu in t.gpus:
            util = (
                f"{gpu.utilization_percent:.0f}%"
                if gpu.utilization_percent is not None
                else "n/a"
            )
            if gpu.memory_used_mb is not None and gpu.memory_total_mb:
                mem = f"{gpu.memory_used_mb:.0f} / {gpu.memory_total_mb:.0f} MB"
            else:
                mem = "n/a"
            temp = f"{gpu.temperature_c:.0f} C" if gpu.temperature_c is not None else "n/a"
            lines.append(f"| {gpu.name} | {util} | {mem} | {temp} |")
        lines.append("")

    if t.temperatures:
        readings = sorted(t.temperatures.items(), key=lambda kv: kv[1], reverse=True)
        lines.append(
            "**Thermals** -- "
            + ", ".join(f"{label} {value:.0f} C" for label, value in readings[:6])
        )
        lines.append("")

    lines.append(f"**Processes** -- {t.process_count} running")
    if t.top_processes:
        lines.append("")
        lines.append("| PID | Process | CPU | Memory |")
        lines.append("| ---: | --- | ---: | ---: |")
        for proc in t.top_processes:
            lines.append(
                f"| {proc.pid} | {proc.name} | {proc.cpu_percent:.1f}% "
                f"| {proc.memory_mb:.0f} MB |"
            )
    lines.append("")

    booted = datetime.fromtimestamp(t.boot_time).strftime("%d %b %H:%M")
    lines.append(f"**Uptime** -- {_format_duration(t.uptime_seconds)} (booted {booted})")
    lines.append(
        f"**Network** -- {format_bytes(t.net_sent_mb * _BYTES_PER_MB)} sent, "
        f"{format_bytes(t.net_recv_mb * _BYTES_PER_MB)} received since boot"
    )
    lines.append(f"**Host** -- {t.platform}, Python {t.python_version}")
    return "\n".join(lines)


# ======================================================================================
# Ambient monitoring
# ======================================================================================
class AmbientMonitor:
    """Background watch that samples telemetry and raises alerts in character.

    The worker is a daemon thread, so a hard interpreter exit is never held hostage by
    it, while :meth:`stop` joins with a timeout for the orderly case. Callbacks fire on
    the monitor thread -- the HUD and the agent are both required to be thread-safe --
    and an exception from either is logged rather than allowed to kill the watch.
    """

    def __init__(
        self,
        on_alert: Callable[[Alert], None] | None = None,
        on_telemetry: Callable[[Telemetry], None] | None = None,
        interval: float | None = None,
    ) -> None:
        self._on_alert = on_alert
        self._on_telemetry = on_telemetry
        chosen = interval if interval is not None else settings.MONITOR_INTERVAL_SECONDS
        try:
            self._interval = float(chosen)
        except (TypeError, ValueError):
            self._interval = 15.0
        if self._interval <= 0:
            self._interval = 15.0
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        # Set means "sampling"; cleared by pause(). An Event rather than a bool so a
        # paused loop parks instead of spinning, and wakes the instant it is resumed.
        self._active = threading.Event()
        self._active.set()
        self._lock = threading.Lock()
        self._latest: Telemetry | None = None
        self._last_alert_at: dict[str, float] = {}
        self._breaches: dict[str, int] = {}
        self._last_log_scan: float = time.time()

    # -- lifecycle ---------------------------------------------------------------
    def start(self) -> None:
        """Start the watch. Idempotent, and a no-op when monitoring is disabled."""
        if not settings.MONITOR_ENABLED:
            logger.info("Ambient monitor disabled by configuration; not starting")
            return
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._active.set()
            self._last_log_scan = time.time()
            self._thread = threading.Thread(
                target=self._run, name="jarvis-ambient-monitor", daemon=True
            )
            self._thread.start()
        logger.info("Ambient monitor started on a %.1fs interval", self._interval)

    def stop(self, timeout: float = 3.0) -> None:
        """Signal the thread to finish and join it. Safe to call more than once."""
        self._stop_event.set()
        # A paused monitor is parked on _active.wait(); release it so it can observe the
        # stop flag and exit cleanly instead of stalling until the join times out.
        self._active.set()
        with self._lock:
            thread = self._thread
            self._thread = None
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
            if thread.is_alive():
                logger.warning("Ambient monitor did not stop within %.1fs", timeout)
        logger.info("Ambient monitor stopped")

    def pause(self) -> None:
        """Suspend sampling without tearing the thread down."""
        self._active.clear()
        logger.debug("Ambient monitor paused")

    def resume(self) -> None:
        """Resume sampling after :meth:`pause`."""
        self._active.set()
        logger.debug("Ambient monitor resumed")

    @property
    def running(self) -> bool:
        """True while the monitor thread is alive and has not been asked to stop."""
        with self._lock:
            thread = self._thread
        return bool(thread is not None and thread.is_alive() and not self._stop_event.is_set())

    # -- readings ----------------------------------------------------------------
    def latest(self) -> Telemetry | None:
        """The most recent snapshot, or None before the first sweep completes."""
        with self._lock:
            return self._latest

    def snapshot(self) -> Telemetry:
        """Collect a fresh snapshot right now and record it as the latest."""
        telemetry = collect_telemetry()
        with self._lock:
            self._latest = telemetry
        return telemetry

    def force_check(self) -> list[Alert]:
        """Run one synchronous sweep and return whatever it raises.

        Cooldowns still apply, so hammering this cannot make J.A.R.V.I.S. repeat
        himself. The alerts come back to the caller rather than going out through
        ``on_alert``: the caller asked, so it already has them.
        """
        telemetry = self.snapshot()
        return self._evaluate(telemetry)

    # -- internals ---------------------------------------------------------------
    def _run(self) -> None:
        """Sample, evaluate, sleep -- until stopped."""
        while not self._stop_event.is_set():
            started = time.monotonic()
            # Park here while paused, but never longer than one interval, so the stop
            # flag is still examined at a predictable cadence.
            if not self._active.wait(timeout=self._interval):
                continue
            if self._stop_event.is_set():
                break
            try:
                telemetry = collect_telemetry()
                with self._lock:
                    self._latest = telemetry
                self._notify_telemetry(telemetry)
                for alert in self._evaluate(telemetry):
                    self._emit(alert)
            except Exception:
                # A sensor failure must never end the watch; log it and try again.
                logger.exception("Ambient monitor sweep failed")
            elapsed = time.monotonic() - started
            self._stop_event.wait(max(0.5, self._interval - elapsed))

    def _notify_telemetry(self, telemetry: Telemetry) -> None:
        """Hand the snapshot to the HUD, tolerating a badly behaved callback."""
        if self._on_telemetry is None:
            return
        try:
            self._on_telemetry(telemetry)
        except Exception:
            logger.exception("on_telemetry callback raised")

    def _emit(self, alert: Alert) -> None:
        """Deliver one alert, tolerating a badly behaved callback."""
        if self._on_alert is None:
            return
        try:
            self._on_alert(alert)
        except Exception:
            logger.exception("on_alert callback raised for %s", alert.key)

    def _cooldown_ok(self, key: str) -> bool:
        """True when ``key`` has not fired within ``MONITOR_ALERT_COOLDOWN`` seconds.

        Claiming the slot is the side effect of asking, so every caller below treats a
        True answer as a commitment to actually raise the alert.
        """
        now = time.time()
        try:
            cooldown = float(settings.MONITOR_ALERT_COOLDOWN)
        except (TypeError, ValueError):
            cooldown = 300.0
        with self._lock:
            last = self._last_alert_at.get(key)
            if last is not None and (now - last) < cooldown:
                return False
            self._last_alert_at[key] = now
        return True

    def _count_breach(self, key: str, breached: bool) -> int:
        """Track consecutive breaches for ``key``; returns the running count."""
        with self._lock:
            if not breached:
                self._breaches[key] = 0
                return 0
            count = self._breaches.get(key, 0) + 1
            self._breaches[key] = count
            return count

    def _evaluate(self, t: Telemetry) -> list[Alert]:
        """Compare one snapshot against every configured threshold."""
        alerts: list[Alert] = []
        # Resolved now, not at import: onboarding may have changed it since.
        title = settings.USER_TITLE

        # -- Processor: two consecutive breaches, so a build spike stays quiet ------
        cpu_breached = t.cpu_percent >= float(settings.CPU_ALERT_THRESHOLD)
        streak = self._count_breach("cpu", cpu_breached)
        if cpu_breached and streak >= _CPU_BREACHES_REQUIRED and self._cooldown_ok("cpu"):
            offender = t.top_processes[0].name if t.top_processes else "an unidentified process"
            alerts.append(
                Alert(
                    key="cpu",
                    severity="critical" if t.cpu_percent >= 98.0 else "warning",
                    title="Processor load",
                    message=(
                        f"{title}, the processor has held at {t.cpu_percent:.0f} percent "
                        f"across two consecutive samples. {offender} is the heaviest "
                        "consumer at present."
                    ),
                    suggestion=(
                        f"Suspend {offender} or defer the workload until the load subsides."
                    ),
                    timestamp=t.timestamp,
                )
            )

        # -- Memory -----------------------------------------------------------------
        ram_breached = t.ram_percent >= float(settings.RAM_ALERT_THRESHOLD)
        self._count_breach("ram", ram_breached)
        if ram_breached and self._cooldown_ok("ram"):
            offender = (
                max(t.top_processes, key=lambda p: p.memory_mb).name
                if t.top_processes
                else "an unidentified process"
            )
            alerts.append(
                Alert(
                    key="ram",
                    severity="critical" if t.ram_percent >= 96.0 else "warning",
                    title="Memory pressure",
                    message=(
                        f"{title}, memory utilisation has reached {t.ram_percent:.0f} "
                        f"percent, with {t.ram_available_gb:.1f} gigabytes still available."
                    ),
                    suggestion=(
                        f"{offender} holds the largest share; closing it recovers the most "
                        "headroom."
                    ),
                    timestamp=t.timestamp,
                )
            )

        # -- Storage, per mount point ----------------------------------------------
        for disk in t.disks:
            key = f"disk:{disk.mountpoint}"
            breached = disk.percent >= float(settings.DISK_ALERT_THRESHOLD)
            self._count_breach(key, breached)
            if not breached or not self._cooldown_ok(key):
                continue
            free = max(0.0, disk.total_gb - disk.used_gb)
            alerts.append(
                Alert(
                    key=key,
                    severity="critical" if disk.percent >= 98.0 else "warning",
                    title="Storage capacity",
                    message=(
                        f"{title}, the volume at {disk.mountpoint} is "
                        f"{disk.percent:.0f} percent full. {free:.1f} gigabytes remain."
                    ),
                    suggestion=(
                        "Clearing build artefacts and caches is the quickest recovery. "
                        "I can run Clean Slate Protocol on your word."
                    ),
                    timestamp=t.timestamp,
                )
            )

        # -- Power reserve: only interesting while unplugged -------------------------
        if t.battery_percent is not None and not t.battery_plugged:
            breached = t.battery_percent <= float(settings.BATTERY_ALERT_THRESHOLD)
            self._count_breach("battery", breached)
            if breached and self._cooldown_ok("battery"):
                remaining = (
                    f" That is roughly {t.battery_minutes_left} minutes of runtime."
                    if t.battery_minutes_left is not None
                    else ""
                )
                alerts.append(
                    Alert(
                        key="battery",
                        severity="critical" if t.battery_percent <= 10 else "warning",
                        title="Power reserve",
                        message=(
                            f"{title}, the battery is down to {t.battery_percent:.0f} "
                            f"percent and we are running on reserve power.{remaining}"
                        ),
                        suggestion="Connect mains power before the reserve is exhausted.",
                        timestamp=t.timestamp,
                    )
                )
        else:
            # Mains power resets the streak, so the next unplugged run starts clean.
            self._count_breach("battery", False)

        # -- Thermals ---------------------------------------------------------------
        ceiling = float(settings.TEMP_ALERT_THRESHOLD)
        for label, celsius in t.temperatures.items():
            key = f"temp:{label}"
            breached = celsius >= ceiling
            self._count_breach(key, breached)
            if not breached or not self._cooldown_ok(key):
                continue
            alerts.append(
                Alert(
                    key=key,
                    severity="critical" if celsius >= ceiling + 10 else "warning",
                    title="Thermal load",
                    message=(
                        f"{title}, the {label} sensor reads {celsius:.0f} degrees Celsius, "
                        f"above the {ceiling:.0f} degree ceiling."
                    ),
                    suggestion=(
                        "Ease the workload and confirm the intakes are clear before the "
                        "hardware throttles itself."
                    ),
                    timestamp=t.timestamp,
                )
            )

        if settings.MONITOR_WATCH_BUILD_LOGS:
            alerts.extend(self._scan_build_logs())
        return alerts

    # -- build log watching ------------------------------------------------------
    def _scan_build_logs(self) -> list[Alert]:
        """Look for freshly written ``*.log`` files that have recorded a failure.

        Only logs touched since the previous scan are opened, and only their final 8 KB
        is read: the tail is where a build writes its verdict, and re-reading whole logs
        on a fifteen second cadence would be an act of vandalism against the disk.
        """
        now = time.time()
        with self._lock:
            since = self._last_log_scan
            self._last_log_scan = now
        # Floor the window at one sample interval so a sweep that ran late -- or the very
        # first sweep after start() -- still sees logs written while we were busy.
        window = max(self._interval, now - since)

        try:
            root = Path(settings.scan_root).expanduser()
            if not root.is_dir():
                return []
        except (OSError, ValueError):
            return []

        alerts: list[Alert] = []
        examined = 0
        try:
            for dirpath, dirnames, filenames in os.walk(root, topdown=True):
                current = Path(dirpath)
                try:
                    depth = len(current.relative_to(root).parts)
                except ValueError:  # a symlink escaping the root
                    dirnames[:] = []
                    continue
                if depth >= _LOG_SCAN_MAX_DEPTH:
                    dirnames[:] = []
                else:
                    dirnames[:] = [d for d in dirnames if d.lower() not in _LOG_SKIP_DIRS]
                for filename in filenames:
                    if not filename.lower().endswith(".log"):
                        continue
                    if examined >= _LOG_SCAN_MAX_FILES:
                        return alerts
                    path = current / filename
                    try:
                        stat = path.stat()
                    except OSError:
                        continue
                    if (now - stat.st_mtime) > window:
                        continue
                    examined += 1
                    alert = self._inspect_log(path, stat.st_size)
                    if alert is not None:
                        alerts.append(alert)
        except OSError:
            logger.debug("Build log walk failed under %s", root, exc_info=True)
        return alerts

    def _inspect_log(self, path: Path, size: int) -> Alert | None:
        """Read the tail of one log and raise an alert if it smells of failure."""
        try:
            with path.open("rb") as handle:
                if size > _LOG_TAIL_BYTES:
                    handle.seek(size - _LOG_TAIL_BYTES)
                tail = handle.read(_LOG_TAIL_BYTES)
        except OSError:
            # Usually still locked by the process writing it, on Windows especially.
            logger.debug("Could not read log tail: %s", path, exc_info=True)
            return None

        offending = ""
        for line in tail.decode("utf-8", errors="replace").splitlines():
            if _LOG_ERROR_PATTERN.search(line):
                offending = line.strip()[:160]
                break
        if not offending:
            return None

        key = f"build:{path}"
        if not self._cooldown_ok(key):
            return None
        title = settings.USER_TITLE
        return Alert(
            key=key,
            severity="warning",
            title="Build log",
            message=(
                f"{title}, {path.name} has just recorded a fault. The build is not clean."
            ),
            suggestion=f"First fault in {path}: {offending}",
            timestamp=time.time(),
        )


__all__ = [
    "Alert",
    "AmbientMonitor",
    "DiskInfo",
    "GpuInfo",
    "ProcInfo",
    "Telemetry",
    "collect_telemetry",
    "detect_gpus",
    "format_bytes",
    "telemetry_report",
    "telemetry_summary",
]
