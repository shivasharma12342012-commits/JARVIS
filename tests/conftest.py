"""Shared fixtures. Nothing here touches a network or a microphone."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def _quiet_settings():
    """Keep every test out of the operator's real profile, voice and workspace."""
    from config import settings

    before = (settings.VOICE_ENABLED, settings.MONITOR_ENABLED, settings.START_MUTED)
    settings.VOICE_ENABLED = False
    settings.MONITOR_ENABLED = False
    settings.START_MUTED = True
    yield
    settings.VOICE_ENABLED, settings.MONITOR_ENABLED, settings.START_MUTED = before
