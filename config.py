"""Central configuration for J.A.R.V.I.S.

All runtime knobs live here as a single Pydantic settings object. Values are read
from the process environment and from a ``.env`` file sitting next to this module,
so an operator can retune the system without touching code.

Import the shared singleton rather than constructing your own::

    from config import settings
    print(settings.MODEL_NAME)
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import List, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent

# --------------------------------------------------------------------------------------
# Palette identifiers. ui.py owns the actual colour values; protocols.py only ever passes
# these names around, which keeps the theme engine decoupled from the renderer.
# --------------------------------------------------------------------------------------
PALETTE_STANDARD = "standard"
PALETTE_HOUSE_PARTY = "house_party"
PALETTE_VERONICA = "veronica"
PALETTE_CLEAN_SLATE = "clean_slate"

# HUD activity states, shared by ui.py, voice.py and core.py.
STATE_IDLE = "idle"
STATE_LISTENING = "listening"
STATE_THINKING = "thinking"
STATE_SPEAKING = "speaking"
STATE_WORKING = "working"

# --------------------------------------------------------------------------------------
# How J.A.R.V.I.S. addresses the operator. Stark's original said "Sir"; ours asks once,
# remembers the answer, and never brings it up again.
# --------------------------------------------------------------------------------------
HONORIFICS: dict[str, str] = {
    "male": "Sir",
    "female": "Ma'am",
    "neutral": "Boss",
}
PROFILE_PATH = PROJECT_ROOT / ".jarvis_profile.json"

# --------------------------------------------------------------------------------------
# Bare keywords. Typed on their own, with no slash, these act immediately rather than
# going to the model: they are the two things an operator most often needs to say in a
# hurry, and reaching for a command prefix mid-sentence defeats the point. They live
# here rather than in main.py so the display can highlight them as they are typed
# without importing the entry point.
# --------------------------------------------------------------------------------------
#: Speaking mode on. Bare words are matched against the *whole* line only, so
#: "talk to me about generics" still reaches the model.
TALK_WORDS: frozenset[str] = frozenset({
    "talk", "speak", "talk to me", "voice", "voice on", "speak up", "unmute",
    "bolo", "baat karo", "bol",
})

#: ...and these shut him up immediately. Hinglish included because that is how the
#: instruction actually arrives when he is halfway through a sentence.
QUIET_WORDS: frozenset[str] = frozenset({
    "quiet", "be quiet", "shut up", "shutup", "silence", "mute", "hush",
    "stop talking", "chup", "chup kar", "chup ho ja", "bas", "bas karo",
})

#: Only silences him while he is actually speaking; otherwise it is a normal message,
#: which is why these are deliberately *not* highlighted as live keywords.
INTERRUPT_WORDS: frozenset[str] = frozenset({"stop", "ruko", "wait", "enough"})



class Settings(BaseSettings):
    """Every tunable parameter in the system."""

    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # -- Identity -----------------------------------------------------------------
    AGENT_NAME: str = "J.A.R.V.I.S."
    AGENT_FULL_NAME: str = "Just A Rather Very Intelligent System"
    USER_TITLE: str = "Sir"
    USER_NAME: str = ""
    # "unset" triggers the one-time onboarding question at first launch.
    USER_GENDER: Literal["male", "female", "neutral", "custom", "unset"] = "unset"
    HONORIFIC_ASK_ON_FIRST_RUN: bool = True

    # -- Ollama -------------------------------------------------------------------
    OLLAMA_HOST: str = "http://127.0.0.1:11434"
    MODEL_NAME: str = "gemma4:31b-cloud"
    MODEL_TEMPERATURE: float = 0.6
    MODEL_TOP_P: float = 0.9
    MODEL_NUM_CTX: int = 16384
    MODEL_THINKING: bool = False
    OLLAMA_TIMEOUT: float = 180.0
    OLLAMA_KEEP_ALIVE: str = "10m"

    # -- ReAct loop ---------------------------------------------------------------
    MAX_TOOL_ITERATIONS: int = 8
    HISTORY_MAX_MESSAGES: int = 40
    STREAM_RESPONSES: bool = True

    # -- Voice --------------------------------------------------------------------
    VOICE_ENABLED: bool = True
    TTS_ENABLED: bool = True
    STT_ENABLED: bool = True
    TTS_ENGINE: Literal["pyttsx3", "edge-tts", "auto", "none"] = "auto"
    TTS_VOICE: str = "en-GB-RyanNeural"      # edge-tts voice id
    TTS_VOICE_HINT: str = "george"            # substring match for a pyttsx3 UK voice
    TTS_RATE: int = 172
    TTS_VOLUME: float = 0.95
    TTS_MAX_CHARS: int = 700                  # spoken summaries stay short
    # The call words. Speech recognition never returns the dots, so "Hello J.A.R.V.I.S."
    # arrives as "hello jarvis" -- normalisation in voice.py collapses both forms to the same
    # key before matching. Devanagari variants let the operator wake him in Hindi.
    WAKE_WORDS: List[str] = Field(
        default_factory=lambda: [
            "hello jarvis",
            "namaste jarvis",
            "नमस्ते जार्विस",
            "हैलो जार्विस",
        ]
    )
    WAKE_WORD_REQUIRED: bool = True
    # Bare "Jarvis" on its own does NOT wake him -- the call word is the full greeting.
    WAKE_ALLOW_BARE_NAME: bool = False
    # Speech recognition mangles the name in predictable ways; accept the near misses.
    WAKE_NAME_VARIANTS: List[str] = Field(
        default_factory=lambda: [
            "jarvis", "jarvis's", "jervis", "javis", "jarwis", "jaarvis", "jarviss",
            "जार्विस", "जारविस",
        ]
    )
    MIC_INDEX: int | None = None
    STT_ENERGY_THRESHOLD: int = 300
    STT_DYNAMIC_ENERGY: bool = True
    STT_PAUSE_THRESHOLD: float = 0.8
    STT_PHRASE_TIME_LIMIT: float = 12.0
    STT_AMBIENT_CALIBRATION: float = 1.0
    STT_LANGUAGE: str = "en-GB"

    # -- HUD ----------------------------------------------------------------------
    HUD_ENABLED: bool = True
    HUD_FPS: int = 12
    HUD_WAVEFORM_WIDTH: int = 48
    HUD_TRANSCRIPT_LINES: int = 200
    HUD_SHOW_TELEMETRY: bool = True
    HUD_TELEMETRY_REFRESH: float = 2.0

    # -- Ambient monitor ----------------------------------------------------------
    MONITOR_ENABLED: bool = True
    MONITOR_INTERVAL_SECONDS: float = 15.0
    MONITOR_ALERT_COOLDOWN: float = 300.0
    CPU_ALERT_THRESHOLD: float = 90.0
    RAM_ALERT_THRESHOLD: float = 90.0
    DISK_ALERT_THRESHOLD: float = 92.0
    BATTERY_ALERT_THRESHOLD: int = 20
    TEMP_ALERT_THRESHOLD: float = 85.0
    MONITOR_WATCH_BUILD_LOGS: bool = True

    # -- Desktop ------------------------------------------------------------------
    #: The shell pane in the desktop window. It is exactly as powerful as a
    #: terminal, which is the point of it, and sits behind the same loopback +
    #: token boundary as everything else the window can reach. Set false to
    #: remove the pane and refuse the route outright.
    DESKTOP_SHELL_ENABLED: bool = True
    #: Whether the editor may write. The code surface reads regardless; this
    #: decides only whether Ctrl+S reaches the disk. Writes are confined to
    #: WORKSPACE_ROOT by the same resolve-then-compare boundary the reader uses,
    #: so switching it on widens what the window may change, never where.
    DESKTOP_EDIT_ENABLED: bool = True
    #: Ceiling on one editor save. Larger than the read cap on purpose: a file
    #: you opened and grew must still be saveable.
    DESKTOP_EDIT_MAX_BYTES: int = 2_000_000

    # -- Who may open the window --------------------------------------------------
    #: "off" (the default), "password", "google", or "any" for either. The window
    #: has always been loopback-only and token-gated, which answers whether
    #: something on the network can reach it. This answers whether the person at
    #: the keyboard is the one who started it.
    DESKTOP_AUTH_MODE: str = "off"
    #: How long a signed-in browser stays signed in. Sessions live in memory, so
    #: restarting J.A.R.V.I.S. signs everyone out regardless.
    DESKTOP_AUTH_TTL_HOURS: float = 12.0
    #: The OAuth client from your Google Cloud project. A Desktop-app client is
    #: the right kind; its "secret" is not confidential on a user's machine and
    #: PKCE is what actually protects the flow, so leaving it unset is fine.
    GOOGLE_CLIENT_ID: str = ""
    GOOGLE_CLIENT_SECRET: str = ""
    #: Addresses allowed to sign in, space- or comma-separated. Left empty, the
    #: first account to sign in claims the window and is remembered.
    GOOGLE_ALLOWED_ACCOUNTS: str = ""

    # -- Tools --------------------------------------------------------------------
    WORKSPACE_ROOT: Path = PROJECT_ROOT
    FILE_OPS_MAX_BYTES: int = 200_000
    CODE_EXEC_ENABLED: bool = True
    CODE_EXEC_TIMEOUT: float = 20.0
    CODE_EXEC_MAX_OUTPUT: int = 8_000
    WEB_SEARCH_ENABLED: bool = True
    WEB_SEARCH_RESULTS: int = 5
    WEB_SEARCH_TIMEOUT: float = 15.0

    # -- Polyglot coding ----------------------------------------------------------
    CODE_DEFAULT_LANGUAGE: str = "python"
    CODE_EXEC_COMPILE_TIMEOUT: float = 90.0
    TOOLCHAIN_CACHE_TTL: float = 86400.0
    SHELL_TOOL_ENABLED: bool = True
    SHELL_TIMEOUT: float = 180.0
    SHELL_MAX_OUTPUT: int = 12_000
    SHELL_DENY_PATTERNS: List[str] = Field(
        default_factory=lambda: [
            r"rm\s+-rf\s+[/~]\s*$",
            r"rm\s+-rf\s+/(?!\w)",
            r"del\s+/[fs]\s+/[sq]",
            r"format\s+[a-z]:",
            r"mkfs(\.|\s)",
            r"diskpart",
            r"shutdown",
            r"reg\s+delete",
            r"Remove-Item\s+.*-Recurse.*-Force\s+['\"]?([a-zA-Z]:\\?|/)\s*['\"]?$",
            r":\(\)\s*\{\s*:\|:&\s*\}\s*;:",
            r"(curl|wget)\s+[^|]+\|\s*(ba)?sh",
        ]
    )

    # -- Speaker verification -----------------------------------------------------
    # A voiceprint taken once at enrolment, compared against whoever says the call word.
    # A convenience gate, not biometric security -- see jarvis/speaker.py.
    SPEAKER_VERIFY_ENABLED: bool = True
    # Measured, twice, on this machine against seven synthetic voices.
    #
    # The metric is cosine similarity over a scale-equalised MFCC embedding, and the
    # right threshold depends on how much audio it gets. On full sentences the operator
    # scored 0.870-0.917 and the closest impostor (same accent, same sex) 0.783 -- a
    # comfortable gap. On short wake phrases, which is what verification actually
    # receives, the operator scored 0.696-0.827 and that same impostor 0.673-0.707,
    # which overlaps.
    #
    # 0.70 was the first recommendation and it is wrong for this input: it rejected the
    # operator on one wake phrase in three. Being ignored by your own assistant a third
    # of the time is the failure you notice; a stranger occasionally getting a greeting
    # is not. 0.65 rejects the operator on neither audio length and still turns away
    # about five impostor clips in six.
    SPEAKER_THRESHOLD: float = 0.65
    SPEAKER_VAD_FACTOR: float = 0.35
    SPEAKER_ENROLL_SECONDS: float = 4.0
    # Five, not three. Measured: the separation between the operator and an impostor
    # widens from about 0.01 at three clips to 0.045 at five, because the per-dimension
    # spread estimate sharpens with every take.
    SPEAKER_ENROLL_PHRASES: int = 5
    VOICEPRINT_PATH: Path = PROJECT_ROOT / ".jarvis_voiceprint.json"

    # -- Wake daemon --------------------------------------------------------------
    DAEMON_WAKE_COOLDOWN: float = 8.0
    DAEMON_TERMINAL: str = "auto"
    # Spoken the moment the call word lands. {honorific} becomes the operator's chosen
    # form of address in whichever language is speaking.
    WAKE_GREETING: str = (
        "\u0928\u092e\u0938\u094d\u0924\u0947 {honorific}, \u0915\u0948\u0938\u0947 \u0939\u0948\u0902 \u0906\u092a? "
        "\u092e\u0948\u0902 \u0939\u0942\u0901 \u091c\u093e\u0930\u094d\u0935\u093f\u0938, \u0906\u092a\u0915\u093e \u0905\u0938\u093f\u0938\u094d\u091f\u0947\u0902\u091f\u0964"
    )

    # He boots silent and stays that way until asked to speak. An assistant that
    # starts talking the moment you open a terminal is a nuisance; one that waits to be
    # invited is not. Type `talk` to switch speech on, `quiet` to switch it back off.
    START_MUTED: bool = True

    # -- Assistant-grade speech ---------------------------------------------------
    # Speak each sentence as it streams rather than waiting for the whole reply: the
    # difference between answering in one second and answering in eight.
    SPEAK_STREAMING: bool = True
    SPEAK_MIN_SENTENCE: int = 12
    # Stop talking the moment the operator starts.
    BARGE_IN_ENABLED: bool = True
    # Keep listening briefly after a reply, so a follow-up needs no second call word.
    CONTINUED_CONVERSATION: bool = True
    FOLLOW_UP_SECONDS: float = 7.0
    WAKE_CHIME: bool = True
    TTS_PITCH: str = "+0Hz"
    # Wake detection always runs in this language: en-IN is the only one that transcribes
    # both English and spoken Hindi well enough to match every call phrase. Measured.
    STT_WAKE_LANGUAGE: str = "en-IN"

    # -- Permissions and reach ----------------------------------------------------
    # J.A.R.V.I.S. may act freely inside WORKSPACE_ROOT. Everything beyond it -- opening
    # applications, writing elsewhere on the disk, closing programs -- goes through the
    # broker in jarvis/permissions.py and is refused unless the operator approves it.
    PERMISSION_MODE: Literal["ask", "allow", "deny"] = "ask"
    PERMISSION_SPEAK: bool = True
    ALLOWED_APPS: List[str] = Field(default_factory=list)
    APP_CONTROL_ENABLED: bool = True
    APP_WAIT_TIMEOUT: float = 30.0
    APP_CLOSE_TIMEOUT: float = 5.0

    # -- Human languages ----------------------------------------------------------
    # J.A.R.V.I.S. answers in whichever supported language he is addressed in.
    RESPONSE_LOCALE: str = "auto"
    ENABLED_LOCALES: List[str] = Field(
        default_factory=lambda: ["en", "hi", "bn", "te", "mr", "ta"]
    )
    LOCALE_AUTO_DETECT: bool = True
    LOCALE_SPEAK_NATIVE: bool = True
    TTS_VOICE_GENDER: Literal["male", "female"] = "male"

    # -- Protocols ----------------------------------------------------------------
    PROTOCOL_DEV_COMMANDS: List[str] = Field(default_factory=list)
    PROTOCOL_SCAN_ROOT: Path | None = None
    VERONICA_PROTECTED_PATHS: List[str] = Field(default_factory=list)
    VERONICA_KILL_THRESHOLD_MB: float = 1500.0
    VERONICA_DRY_RUN: bool = True

    # -- Logging ------------------------------------------------------------------
    LOG_LEVEL: str = "INFO"
    LOG_FILE: Path = PROJECT_ROOT / "logs" / "jarvis.log"

    @field_validator(
        "WAKE_WORDS", "WAKE_NAME_VARIANTS", "ENABLED_LOCALES", "ALLOWED_APPS",
        mode="before",
    )
    @classmethod
    def _split_wake_words(cls, value: object) -> object:
        """Accept ``WAKE_WORDS="hello jarvis,jarvis"`` from the environment."""
        if isinstance(value, str):
            return [w.strip().lower() for w in value.split(",") if w.strip()]
        if isinstance(value, list):
            return [str(w).strip().lower() for w in value if str(w).strip()]
        return value

    @field_validator(
        "PROTOCOL_DEV_COMMANDS", "VERONICA_PROTECTED_PATHS", "SHELL_DENY_PATTERNS",
        mode="before",
    )
    @classmethod
    def _split_list(cls, value: object) -> object:
        if isinstance(value, str):
            return [v.strip() for v in value.split(";") if v.strip()]
        return value

    @field_validator("WORKSPACE_ROOT", "PROTOCOL_SCAN_ROOT", mode="before")
    @classmethod
    def _expand_path(cls, value: object) -> object:
        if isinstance(value, str):
            if not value.strip():
                return None
            return Path(os.path.expandvars(value)).expanduser()
        return value

    @property
    def scan_root(self) -> Path:
        """Where PROTOCOL HOUSE PARTY goes looking for repositories."""
        return self.PROTOCOL_SCAN_ROOT or self.WORKSPACE_ROOT

    @property
    def voice_wanted(self) -> bool:
        """True when the operator has not globally disabled audio."""
        return self.VOICE_ENABLED


def load_profile() -> dict[str, str]:
    """Read the persisted operator profile, if one has been saved."""
    try:
        if PROFILE_PATH.exists():
            data = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {str(k): str(v) for k, v in data.items()}
    except (OSError, ValueError):
        pass  # a corrupt profile just means we ask again
    return {}


def save_profile(gender: str, title: str, name: str = "") -> bool:
    """Persist how the operator wishes to be addressed. Returns success."""
    payload = {"gender": gender, "title": title, "name": name}
    try:
        PROFILE_PATH.parent.mkdir(parents=True, exist_ok=True)
        PROFILE_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return True
    except OSError:
        return False


def honorific_for(gender: str, fallback: str = "Sir") -> str:
    """Map a gender key onto the honorific J.A.R.V.I.S. will use."""
    return HONORIFICS.get(gender.strip().lower(), fallback)


def apply_profile(target: "Settings") -> bool:
    """Overlay a saved profile onto ``target``. Returns True if one was applied.

    The environment still wins: an explicit ``USER_GENDER`` in ``.env`` means the
    operator has already answered the question, so we do not override it.
    """
    if target.USER_GENDER != "unset":
        if target.USER_GENDER != "custom":
            target.USER_TITLE = honorific_for(target.USER_GENDER, target.USER_TITLE)
        return True
    profile = load_profile()
    if not profile:
        return False
    gender = profile.get("gender", "unset")
    if gender == "unset":
        return False
    target.USER_GENDER = gender  # type: ignore[assignment]
    target.USER_TITLE = profile.get("title") or honorific_for(gender, target.USER_TITLE)
    target.USER_NAME = profile.get("name", "")
    return True


def set_honorific(target: "Settings", gender: str, title: str = "", name: str = "") -> str:
    """Set and persist the operator's honorific. Returns the title now in use."""
    gender = gender.strip().lower()
    resolved = title.strip() or honorific_for(gender, target.USER_TITLE)
    target.USER_GENDER = gender if gender in HONORIFICS else "custom"  # type: ignore[assignment]
    target.USER_TITLE = resolved
    if name:
        target.USER_NAME = name.strip()
    save_profile(target.USER_GENDER, resolved, target.USER_NAME)
    return resolved


settings = Settings()

# A previously answered onboarding question is honoured before anything else imports us.
PROFILE_APPLIED = apply_profile(settings)


def needs_onboarding() -> bool:
    """True when we still have to ask the operator how to address them."""
    return settings.USER_GENDER == "unset" and settings.HONORIFIC_ASK_ON_FIRST_RUN
