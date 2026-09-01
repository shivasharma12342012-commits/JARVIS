"""Shared fixtures. Nothing here touches a network or a microphone."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


#: Everything ``main.apply_overrides`` writes into the shared settings object. It
#: mutates the singleton by design -- every component reads it at construction --
#: which means one test's ``--no-hud`` silently becomes the next test's default
#: unless the whole set is put back.
_MUTATED_BY_CLI = (
    "MODEL_NAME", "OLLAMA_HOST", "VOICE_ENABLED", "TTS_ENABLED", "STT_ENABLED",
    "HUD_ENABLED", "MONITOR_ENABLED", "LOG_LEVEL", "PERMISSION_MODE",
    "START_MUTED", "RESPONSE_LOCALE", "USER_NAME", "USER_TITLE",
)


@pytest.fixture(autouse=True)
def _quiet_settings():
    """Keep every test out of the operator's real profile, voice and workspace,
    and out of every other test's settings."""
    from config import settings

    before = {name: getattr(settings, name) for name in _MUTATED_BY_CLI}
    settings.VOICE_ENABLED = False
    settings.MONITOR_ENABLED = False
    settings.START_MUTED = True
    yield
    for name, value in before.items():
        setattr(settings, name, value)
