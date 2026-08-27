"""Human languages: how J.A.R.V.I.S. listens, thinks and speaks in Indian tongues.

He answers in whichever supported language he is addressed in. That requires four
distinct things to line up, and this module owns all four:

* **Detection** -- :func:`detect_locale` decides which language a message is in, by
  Unicode script first (decisive and cheap) and by romanised word markers second.
* **Instruction** -- :func:`instruction_for` produces the line injected into the system
  prompt telling the model which language to answer in.
* **Voice** -- :func:`voice_for` and :func:`stt_code` pick the neural TTS voice and the
  speech-recognition language code for the active locale.
* **Stock phrases** -- greetings, acknowledgements and farewells, written natively rather
  than machine-translated at runtime.

Naming discipline: this module is about *human* languages. Programming languages live in
:mod:`jarvis.languages`.

Imports :mod:`config` and the standard library only.
"""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass, field

from config import settings

logger = logging.getLogger(__name__)

DEFAULT_LOCALE = "en"

#: A message shorter than this is not worth classifying; punctuation and "ok" are not
#: evidence of anything.
_MIN_DETECT_CHARS = 3

#: Fraction of alphabetic characters that must belong to a script before we call it.
_SCRIPT_THRESHOLD = 0.15

#: Romanised detection demands two distinct markers, so one loan word in an English
#: sentence ("give me the kya equivalent") cannot flip the whole conversation.
_MIN_ROMAN_MARKERS = 2


@dataclass(frozen=True)
class Locale:
    """One human language J.A.R.V.I.S. can hold a conversation in."""

    code: str
    name: str
    native_name: str
    script: str
    ranges: tuple[tuple[int, int], ...]
    stt_code: str
    voice_male: str
    voice_female: str
    honorifics: dict[str, str]
    wake_words: tuple[str, ...]
    greeting: str
    ack: str
    listening: str
    farewell: str
    #: Spoken by the wake daemon the instant the call word lands.
    wake_greeting: str = ""
    romanised_markers: tuple[str, ...] = ()
    native_markers: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()
    notes: str = ""


# ══════════════════════════════════════════════════════════════════════════════════════
# The registry. Native strings are written, not translated at runtime -- a machine
# translation of "at your service" tends to arrive as something no one would ever say.
# ══════════════════════════════════════════════════════════════════════════════════════

_LATIN = ((0x0041, 0x005A), (0x0061, 0x007A))
_DEVANAGARI = ((0x0900, 0x097F),)
_BENGALI = ((0x0980, 0x09FF),)
_TELUGU = ((0x0C00, 0x0C7F),)
_TAMIL = ((0x0B80, 0x0BFF),)
_KANNADA = ((0x0C80, 0x0CFF),)
_MALAYALAM = ((0x0D00, 0x0D7F),)
_GUJARATI = ((0x0A80, 0x0AFF),)
_GURMUKHI = ((0x0A00, 0x0A7F),)
_ARABIC = ((0x0600, 0x06FF), (0x0750, 0x077F))


LOCALES: dict[str, Locale] = {
    loc.code: loc
    for loc in (
        Locale(
            code="en",
            name="English",
            native_name="English",
            script="Latin",
            ranges=_LATIN,
            stt_code="en-IN",
            voice_male="en-GB-RyanNeural",
            voice_female="en-GB-SoniaNeural",
            honorifics={"male": "Sir", "female": "Ma'am", "neutral": "Boss"},
            wake_words=("hello jarvis", "namaste jarvis"),
            greeting="All systems nominal, {user_title}. {agent_name} online and at your disposal.",
            ack="At your service, {user_title}.",
            listening="Listening.",
            farewell="Going dark, {user_title}. I will be here.",
            wake_greeting=(
                "Namaste {honorific}. I am {agent_name}, your assistant. "
                "How may I help?"
            ),
            aliases=("eng", "english", "angrezi"),
        ),
        Locale(
            code="hi",
            name="Hindi",
            native_name="हिन्दी",
            script="Devanagari",
            ranges=_DEVANAGARI,
            stt_code="hi-IN",
            voice_male="hi-IN-MadhurNeural",
            voice_female="hi-IN-SwaraNeural",
            honorifics={
                "male": "सर",
                "female": "मैडम",
                "neutral": "जी",
            },
            wake_words=(
                "नमस्ते जार्विस",
                "हैलो जार्विस",
                # Measured: Google transliterates spoken "hello" as हेलो (with े), not
                # हैलो. Without this the English call word stops working the moment the
                # recogniser is in Hindi mode.
                "हेलो जार्विस",
                "नमस्कार जार्विस",
                "namaste jarvis",
            ),
            greeting=(
                "सभी सिस्टम "
                "सामान्य हैं, {user_title}। "
                "जार्विस ऑनलाइन "
                "है और आपकी सेवा "
                "में हाज़िर है।"
            ),
            ack=(
                "आपकी सेवा में "
                "हाज़िर हूँ, {user_title}।"
            ),
            listening="सुन रहा हूँ।",
            farewell=(
                "सिस्टम बंद कर "
                "रहा हूँ। फिर "
                "मिलेंगे, {user_title}।"
            ),
            wake_greeting=(
                "नमस्ते {honorific}, कैसे हैं आप? "
                "मैं हूँ जार्विस, आपका असिस्टेंट।"
            ),
            romanised_markers=(
                "kya", "kaise", "kaisa", "hai", "hain", "nahi", "nahin", "aap", "tum",
                "mujhe", "karo", "kar", "batao", "chahiye", "accha", "theek", "kyun",
                "kyu", "bhai", "yaar", "matlab", "kaam", "samajh", "bata", "hoga",
            ),
            aliases=("hindi", "hin", "हिंदी"),
        ),
        Locale(
            code="bn",
            name="Bengali",
            native_name="বাংলা",
            script="Bengali",
            ranges=_BENGALI,
            stt_code="bn-IN",
            voice_male="bn-IN-BashkarNeural",
            voice_female="bn-IN-TanishaaNeural",
            honorifics={
                "male": "স্যার",
                "female": "ম্যাডাম",
                "neutral": "জি",
            },
            wake_words=(
                "নমস্তে জার্ভিস",
                "হ্যালো জার্ভিস",
            ),
            greeting=(
                "সমস্ত সিস্টেম "
                "স্বাভাবিক, {user_title}। "
                "জার্ভিস অনলাইন "
                "এবং আপনার সেবায় "
                "প্রস্তুত।"
            ),
            ack=(
                "আপনার সেবায় "
                "হাজির, {user_title}।"
            ),
            listening="শুনছি।",
            farewell=(
                "সিস্টেম বন্ধ "
                "করছি। আবার দেখা "
                "হবে, {user_title}।"
            ),
            romanised_markers=(
                "kemon", "achen", "ache", "ami", "apni", "tumi", "korbo", "kore",
                "bolo", "kotha", "hoyeche", "khub", "keno", "tahole", "amar",
            ),
            aliases=("bengali", "bangla", "ben"),
        ),
        Locale(
            code="te",
            name="Telugu",
            native_name="తెలుగు",
            script="Telugu",
            ranges=_TELUGU,
            stt_code="te-IN",
            voice_male="te-IN-MohanNeural",
            voice_female="te-IN-ShrutiNeural",
            honorifics={
                "male": "సర్",
                "female": "మేడమ్",
                "neutral": "గారు",
            },
            wake_words=(
                "నమస్తే జార్విస్",
                "హలో జార్విస్",
            ),
            greeting=(
                "అన్ని వ్యవస్థలు "
                "సాధారణంగా ఉన్నాయి, "
                "{user_title}. జార్విస్ "
                "ఆన్‌లైన్‌లో ఉంది."
            ),
            ack="మీ సేవలో ఉన్నాను, {user_title}.",
            listening="విన్నాను.",
            farewell=(
                "సిస్టమ్ మూసివేస్తున్నాను. "
                "మళ్ళీ కలుద్దాం, {user_title}."
            ),
            romanised_markers=(
                "emi", "ela", "unnaru", "undi", "nenu", "meeru", "cheyyi", "cheppu",
                "ledu", "kavali", "bagundi", "enti", "chala", "manchi",
            ),
            aliases=("telugu", "tel"),
        ),
        Locale(
            code="mr",
            name="Marathi",
            native_name="मराठी",
            script="Devanagari",
            ranges=_DEVANAGARI,
            stt_code="mr-IN",
            voice_male="mr-IN-ManoharNeural",
            voice_female="mr-IN-AarohiNeural",
            honorifics={
                "male": "सर",
                "female": "मॅडम",
                "neutral": "जी",
            },
            wake_words=(
                "नमस्ते जार्विस",
                "हॅलो जार्विस",
            ),
            greeting=(
                "सर्व यंत्रणा "
                "सुरळीत आहेत, {user_title}. "
                "जार्विस ऑनलाइन "
                "आहे आणि आपल्या "
                "सेवेत हजर आहे."
            ),
            ack=(
                "आपल्या सेवेत "
                "हजर आहे, {user_title}."
            ),
            listening="ऐकत आहे.",
            farewell=(
                "यंत्रणा बंद करत "
                "आहे. पुन्हा भेटू, {user_title}."
            ),
            # These are what separate Marathi from Hindi -- both use Devanagari, so the
            # script check alone cannot tell them apart.
            native_markers=(
                "आहे", "नाही", "मला",
                "तुम्ही", "काय",
                "आपण", "करा", "होते",
            ),
            romanised_markers=(
                "aahe", "ahe", "nahi", "mala", "tumhi", "kay", "kara", "aapan",
                "kasa", "kashi", "khup", "pahije",
            ),
            aliases=("marathi", "mar"),
        ),
        Locale(
            code="ta",
            name="Tamil",
            native_name="தமிழ்",
            script="Tamil",
            ranges=_TAMIL,
            stt_code="ta-IN",
            voice_male="ta-IN-ValluvarNeural",
            voice_female="ta-IN-PallaviNeural",
            honorifics={
                "male": "சார்",
                "female": "மேடம்",
                "neutral": "ஐயா",
            },
            wake_words=(
                "வணக்கம் ஜார்விஸ்",
                "ஹலோ ஜார்விஸ்",
            ),
            greeting=(
                "அனைத்து அமைப்புகளும் "
                "இயல்பாக உள்ளன, {user_title}. "
                "ஜார்விஸ் உங்கள் "
                "சேவைக்கு தயார்."
            ),
            ack=(
                "உங்கள் சேவையில் "
                "இருக்கிறேன், {user_title}."
            ),
            listening="கேட்டுக்கொண்டிருக்கிறேன்.",
            farewell=(
                "அமைப்பை மூடுகிறேன். "
                "மீண்டும் சந்திப்போம், {user_title}."
            ),
            romanised_markers=(
                "enna", "eppadi", "irukku", "naan", "neenga", "pannu", "sollu",
                "illa", "venum", "nalla", "romba", "seri", "vaa",
            ),
            aliases=("tamil", "tam"),
        ),
        # ---- Registered but off by default; one ENABLED_LOCALES edit away. -------------
        Locale(
            code="kn",
            name="Kannada",
            native_name="ಕನ್ನಡ",
            script="Kannada",
            ranges=_KANNADA,
            stt_code="kn-IN",
            voice_male="kn-IN-GaganNeural",
            voice_female="kn-IN-SapnaNeural",
            honorifics={"male": "ಸರ್", "female": "ಮೇಡಮ್",
                        "neutral": "ರವರು"},
            wake_words=("ನಮಸ್ಕಾರ ಜಾರ್ವಿಸ್",),
            greeting="ಎಲ್ಲಾ ವ್ಯವಸ್ಥೆಗಳು ಸರಿಯಾಗಿವೆ, {user_title}.",
            ack="ನಿಮ್ಮ ಸೇವೆಯಲ್ಲಿದ್ದೇನೆ, {user_title}.",
            listening="ಕೇಳುತ್ತಿದ್ದೇನೆ.",
            farewell="ವ್ಯವಸ್ಥೆ ಮುಚ್ಲಿದೆ, {user_title}.",
            romanised_markers=("hegidira", "enu", "illa", "beku", "chennagide", "madi"),
            aliases=("kannada", "kan"),
        ),
        Locale(
            code="ml",
            name="Malayalam",
            native_name="മലയാളം",
            script="Malayalam",
            ranges=_MALAYALAM,
            stt_code="ml-IN",
            voice_male="ml-IN-MidhunNeural",
            voice_female="ml-IN-SobhanaNeural",
            honorifics={"male": "സർ", "female": "മേഡം",
                        "neutral": "അവർ"},
            wake_words=("നമസ്കാരം ജാർവിസ്",),
            greeting="എല്ലാ സിസ്റ്റങ്ങളും സാധാരണമാണ്, {user_title}.",
            ack="നിങ്ങളുടെ സേവനത്തിലുണ്ട്, {user_title}.",
            listening="കേൾക്കുന്നു.",
            farewell="സിസ്റ്റം അടയ്ക്കുന്നു, {user_title}.",
            romanised_markers=("engane", "undu", "illa", "venam", "nalla", "cheyyu"),
            aliases=("malayalam", "mal"),
        ),
        Locale(
            code="gu",
            name="Gujarati",
            native_name="ગુજરાતી",
            script="Gujarati",
            ranges=_GUJARATI,
            stt_code="gu-IN",
            voice_male="gu-IN-NiranjanNeural",
            voice_female="gu-IN-DhwaniNeural",
            honorifics={"male": "સર", "female": "મેડમ",
                        "neutral": "જી"},
            wake_words=("નમસ્તે જાર્વિસ",),
            greeting="બધી સિસ્ટમ સામાન્ય છે, {user_title}.",
            ack="તમારી સેવામાં છું, {user_title}.",
            listening="સાંભળું છું.",
            farewell="સિસ્ટમ બંધ કરું છું, {user_title}.",
            romanised_markers=("kem", "chho", "nathi", "joie", "saras", "karo"),
            aliases=("gujarati", "guj"),
        ),
        Locale(
            code="ur",
            name="Urdu",
            native_name="اردو",
            script="Arabic",
            ranges=_ARABIC,
            stt_code="ur-IN",
            voice_male="ur-IN-SalmanNeural",
            voice_female="ur-IN-GulNeural",
            honorifics={"male": "جناب", "female": "میڈم",
                        "neutral": "جی"},
            wake_words=("سلام جاروس",),
            greeting="تمام نظام معمول کے مطابق ہیں، {user_title}.",
            ack="آپ کی خدمت میں حاضر ہوں، {user_title}.",
            listening="سن رہا ہوں.",
            farewell="نظام بند کر رہا ہوں، {user_title}.",
            romanised_markers=("kaise", "hain", "nahi", "aap", "chahiye", "shukriya"),
            aliases=("urdu", "urd"),
        ),
        Locale(
            code="pa",
            name="Punjabi",
            native_name="ਪੰਜਾਬੀ",
            script="Gurmukhi",
            ranges=_GURMUKHI,
            stt_code="pa-IN",
            # Edge has no pa-IN voice; Hindi is the nearest intelligible substitute.
            voice_male="hi-IN-MadhurNeural",
            voice_female="hi-IN-SwaraNeural",
            honorifics={"male": "ਸਰ", "female": "ਮੈਡਮ",
                        "neutral": "ਜੀ"},
            wake_words=("ਸਤ ਸ੍ਰੀ ਅਕਾਲ ਜਾਰਵਿਸ",),
            greeting="ਸਾਰੇ ਸਿਸਟਮ ਠੀਕ ਹਨ, {user_title}.",
            ack="ਤੁਹਾਡੀ ਸੇਵਾ ਵਿੱਚ ਹਾਜ਼ਰ ਹਾਂ, {user_title}.",
            listening="ਸੁਣ ਰਿਹਾ ਹਾਂ.",
            farewell="ਸਿਸਟਮ ਬੰਦ ਕਰ ਰਿਹਾ ਹਾਂ, {user_title}.",
            romanised_markers=("kiddan", "haiga", "nahi", "chahida", "changa", "karo"),
            aliases=("punjabi", "pan", "gurmukhi"),
            notes="No dedicated edge-tts voice; speaks with the Hindi voice.",
        ),
    )
}

_ALIASES: dict[str, str] = {}
for _loc in LOCALES.values():
    _ALIASES[_loc.code] = _loc.code
    _ALIASES[_loc.name.lower()] = _loc.code
    _ALIASES[_loc.native_name.lower()] = _loc.code
    for _a in _loc.aliases:
        _ALIASES[_a.lower()] = _loc.code


# ══════════════════════════════════════════════════════════════════════════════════════
# Active-locale state (read by the voice thread while the main thread may change it)
# ══════════════════════════════════════════════════════════════════════════════════════

_lock = threading.RLock()
_active_code: str | None = None


def get_locale(name: str) -> Locale | None:
    """Resolve a code, English name, native name or alias to a :class:`Locale`."""
    if not name:
        return None
    return LOCALES.get(_ALIASES.get(str(name).strip().lower(), ""))


def enabled_locales() -> list[Locale]:
    """The locales the operator has switched on. English is always available."""
    codes: list[str] = []
    for raw in settings.ENABLED_LOCALES or []:
        code = _ALIASES.get(str(raw).strip().lower())
        if code and code not in codes:
            codes.append(code)
    if DEFAULT_LOCALE not in codes:
        codes.insert(0, DEFAULT_LOCALE)
    return [LOCALES[c] for c in codes if c in LOCALES]


def is_enabled(code: str) -> bool:
    """True when ``code`` names a locale that is currently switched on."""
    resolved = _ALIASES.get(str(code).strip().lower())
    return any(loc.code == resolved for loc in enabled_locales())


def active() -> Locale:
    """The locale J.A.R.V.I.S. is currently answering in."""
    with _lock:
        if _active_code and _active_code in LOCALES:
            return LOCALES[_active_code]
    configured = str(settings.RESPONSE_LOCALE or "").strip().lower()
    if configured and configured != "auto":
        loc = get_locale(configured)
        if loc:
            return loc
    return LOCALES[DEFAULT_LOCALE]


def set_active(name: str) -> Locale | None:
    """Switch the response language. Returns ``None`` if unknown or disabled."""
    global _active_code
    loc = get_locale(name)
    if loc is None or not is_enabled(loc.code):
        return None
    with _lock:
        _active_code = loc.code
    return loc


def reset_active() -> None:
    """Fall back to whatever ``RESPONSE_LOCALE`` says."""
    global _active_code
    with _lock:
        _active_code = None


# ══════════════════════════════════════════════════════════════════════════════════════
# Detection
# ══════════════════════════════════════════════════════════════════════════════════════


def script_ratio(text: str, locale: Locale) -> float:
    """Fraction of the alphabetic characters in ``text`` belonging to this script."""
    if not text:
        return 0.0
    alpha = 0
    hits = 0
    for ch in text:
        if not ch.isalpha():
            continue
        alpha += 1
        point = ord(ch)
        for low, high in locale.ranges:
            if low <= point <= high:
                hits += 1
                break
    return (hits / alpha) if alpha else 0.0


_WORD_RE = re.compile(r"[a-z]+")


def detect_locale(text: str, default: str | None = None) -> str:
    """Identify the language of ``text``.

    Script evidence is decisive: Devanagari means Hindi or Marathi and nothing else.
    Romanised detection is a weaker signal and is only consulted for pure-Latin input,
    where it demands two distinct markers before it will override the default.
    """
    fallback = default or (
        settings.RESPONSE_LOCALE
        if settings.RESPONSE_LOCALE and settings.RESPONSE_LOCALE != "auto"
        else DEFAULT_LOCALE
    )
    fallback = _ALIASES.get(str(fallback).strip().lower(), DEFAULT_LOCALE)

    if not text or len(text.strip()) < _MIN_DETECT_CHARS:
        return fallback

    try:
        candidates = [loc for loc in enabled_locales() if loc.code != DEFAULT_LOCALE]

        # 1. Script.
        best: tuple[float, Locale] | None = None
        for loc in candidates:
            ratio = script_ratio(text, loc)
            if ratio >= _SCRIPT_THRESHOLD and (best is None or ratio > best[0]):
                best = (ratio, loc)

        if best is not None:
            winner = best[1]
            # Hindi and Marathi share Devanagari; only word evidence separates them.
            devanagari = [c for c in candidates if c.script == "Devanagari"]
            if winner.script == "Devanagari" and len(devanagari) > 1:
                for loc in devanagari:
                    if loc.native_markers and any(m in text for m in loc.native_markers):
                        return loc.code
                for loc in devanagari:
                    if loc.code == "hi":
                        return "hi"
            return winner.code

        # 2. Romanised, Latin-only input.
        words = set(_WORD_RE.findall(text.lower()))
        if words:
            scored: list[tuple[int, str]] = []
            for loc in candidates:
                hits = len(words & set(loc.romanised_markers))
                if hits >= _MIN_ROMAN_MARKERS:
                    scored.append((hits, loc.code))
            if scored:
                scored.sort(reverse=True)
                return scored[0][1]
    except Exception:
        logger.debug("Locale detection failed; falling back", exc_info=True)

    return fallback


# ══════════════════════════════════════════════════════════════════════════════════════
# Voice, prompts and stock phrases
# ══════════════════════════════════════════════════════════════════════════════════════


def voice_for(locale: Locale | None = None, gender: str | None = None) -> str:
    """The edge-tts neural voice id for a locale."""
    loc = locale or active()
    want = (gender or settings.TTS_VOICE_GENDER or "male").strip().lower()
    return loc.voice_female if want == "female" else loc.voice_male


def stt_code(locale: Locale | None = None) -> str:
    """Speech-recognition language code, e.g. ``hi-IN``."""
    return (locale or active()).stt_code


def honorific_for(locale: Locale, gender: str, fallback: str) -> str:
    """The locale's own form of address, e.g. ``सर`` for Hindi.

    Before the operator has answered the onboarding question there is no gender to map,
    and falling back to the configured title drops a Latin "Sir" into the middle of a
    Devanagari sentence. For any non-Latin locale the neutral honorific is used instead,
    which is at least written in the same script as the words around it.
    """
    key = str(gender).strip().lower()
    known = locale.honorifics.get(key)
    if known:
        return known
    if locale.script != "Latin":
        neutral = locale.honorifics.get("neutral")
        if neutral:
            return neutral
    return fallback


def localise(key: str, locale: Locale | None = None, **fmt) -> str:
    """Fetch a stock phrase in the active language, falling back to English."""
    loc = locale or active()
    template = getattr(loc, key, "") or getattr(LOCALES[DEFAULT_LOCALE], key, "")
    values = {
        "user_title": settings.USER_TITLE,
        # The locale's own form of address, so a Hindi greeting says "सर" rather than
        # dropping a Latin word into the middle of a Devanagari sentence.
        "honorific": honorific_for(loc, settings.USER_GENDER, settings.USER_TITLE),
        "agent_name": settings.AGENT_NAME,
        "agent_full_name": settings.AGENT_FULL_NAME,
    }
    values.update(fmt)
    try:
        return template.format(**values)
    except (KeyError, IndexError):
        return template


def personalise_for(template: str, locale: Locale | None = None, **fmt) -> str:
    """Fill ``{honorific}`` / ``{user_title}`` / ``{agent_name}`` in an arbitrary string.

    Used for templates configured in ``.env`` -- the wake greeting chiefly -- which are
    not part of the locale registry but still need the operator's chosen form of address
    rendered in the right language.
    """
    loc = locale or active()
    values = {
        "user_title": settings.USER_TITLE,
        "honorific": honorific_for(loc, settings.USER_GENDER, settings.USER_TITLE),
        "agent_name": settings.AGENT_NAME,
        "agent_full_name": settings.AGENT_FULL_NAME,
    }
    values.update(fmt)
    try:
        return str(template).format(**values)
    except (KeyError, IndexError):
        return str(template)


def instruction_for(locale: Locale | None = None) -> str:
    """The system-prompt line that pins the reply language.

    Vague instructions ("you may answer in Hindi") get ignored under load, so this is
    written as an unambiguous directive, and it carves out the one exception that
    actually matters: code and identifiers stay in English.
    """
    loc = locale or active()
    if loc.code == DEFAULT_LOCALE:
        return (
            "Reply in English. If the operator writes to you in Hindi, Bengali, Telugu, "
            "Marathi or Tamil, switch to that language completely for your next reply -- "
            "including the spoken summary -- and switch back just as readily. Never "
            "remark on the switch; simply do it."
        )
    return (
        f"Respond entirely in {loc.name} ({loc.native_name}), written in "
        f"{loc.script} script. This applies to your spoken summary as well as the "
        f"detail below it. Address the operator as "
        f"'{honorific_for(loc, settings.USER_GENDER, settings.USER_TITLE)}'. "
        f"Keep code, identifiers, file paths, commands, flags and error messages in "
        f"English -- transliterating them helps no one. If the operator writes to you "
        f"in English, switch back to English without comment."
    )


def wake_vocabulary() -> list[str]:
    """Every phrase that wakes him, across the enabled locales."""
    phrases: list[str] = [str(w).strip().lower() for w in settings.WAKE_WORDS if str(w).strip()]
    for loc in enabled_locales():
        for word in loc.wake_words:
            cleaned = word.strip().lower()
            if cleaned and cleaned not in phrases:
                phrases.append(cleaned)
    return phrases


def report() -> str:
    """Markdown table of every registered locale and its status."""
    enabled = {loc.code for loc in enabled_locales()}
    current = active().code
    rows = [
        "| Code | Language | Native | Script | Status | Voice |",
        "|---|---|---|---|---|---|",
    ]
    for loc in LOCALES.values():
        if loc.code == current:
            status = "**active**"
        elif loc.code in enabled:
            status = "enabled"
        else:
            status = "off"
        rows.append(
            f"| `{loc.code}` | {loc.name} | {loc.native_name} | {loc.script} | "
            f"{status} | `{voice_for(loc)}` |"
        )
    rows.append("")
    rows.append(
        f"Detection is {'automatic' if settings.LOCALE_AUTO_DETECT else 'manual'}; "
        f"enable more with `ENABLED_LOCALES` in `.env`."
    )
    return "\n".join(rows)


def clear_cache() -> None:
    """Reset the active locale. Present for symmetry with :mod:`jarvis.languages`."""
    reset_active()
