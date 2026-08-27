"""Speaker verification — so the call word answers to the operator, not the television.

A wake word is a public string. Anyone who says it, and any advertisement that says
it, gets the assistant's attention. This module adds a second condition: the voice
saying it must resemble the one enrolled at setup.

**Read this before trusting it.** What lives here is a *convenience gate*, not
biometric security. It rejects a clearly different voice in a quiet room — a presenter
on television, a family member across the sofa — and that is the whole of its ambition.
It can be fooled by a recording of the operator, its margin narrows sharply on short or
noisy audio, and it is not a login. Nothing in J.A.R.V.I.S. should ever gate a
privileged action on this alone.

How it works: raw PCM becomes mel-frequency cepstral coefficients using nothing but
:mod:`numpy` — pre-emphasis, framing, a Hamming window, an ``rfft`` power spectrum, a
triangular mel filterbank, a logarithm and a DCT-II. Frames too quiet to be speech are
dropped, first-order deltas are appended, and the surviving frames are summarised as
``concat(mean, std)``, scale-equalised, centred and L2-normalised. Two voiceprints are
compared with cosine similarity. No model download, no torch, no network call; the
whole pipeline is a few tens of milliseconds of arithmetic per clip.

Those two extra steps — scale equalisation and centring — are what make the thing work
at all, and :func:`_embed_mfcc` documents the measurements that justify them. A naive
``concat(mean, std)`` of MFCCs scores *every* pair of speakers above 0.85 and cannot be
thresholded.

If `resemblyzer <https://github.com/resemble-ai/Resemblyzer>`_ happens to be installed
it is preferred — it is a real speaker-embedding network and is markedly better — but
it is entirely optional and is imported lazily, because it drags torch in with it and
importing torch to answer "is anyone enrolled?" would cost seconds.

Storage is a small JSON file at ``settings.VOICEPRINT_PATH``. A corrupt or truncated
file is treated exactly like an absent one: logged at debug, never fatal. And with no
voiceprint enrolled, :func:`verify` accepts everything. Verification is opt-in and must
never be able to lock the operator out of their own assistant.

Imports :mod:`config`, :mod:`numpy` and the standard library only.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from config import settings

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------------------
# numpy is the engine of this module, but a broken install must not take the whole
# application down at import time. Every public entry point degrades to "cannot verify"
# — which, per the fail-open rule below, means "accept" — rather than raising.
# --------------------------------------------------------------------------------------
try:  # pragma: no cover - availability is environmental
    import numpy as np
except ImportError:  # pragma: no cover
    np = None  # type: ignore[assignment]
except Exception as _exc:  # pragma: no cover
    np = None  # type: ignore[assignment]
    logger.warning("numpy failed to import; speaker verification disabled: %s", _exc)


# --------------------------------------------------------------------------------------
# Backend identifiers. These strings are persisted inside the voiceprint file, so a print
# enrolled with one backend is never compared against a probe computed with the other.
# --------------------------------------------------------------------------------------
BACKEND_MFCC = "mfcc"
BACKEND_RESEMBLYZER = "resemblyzer"

#: Everything is resampled to this before analysis. The mel filterbank is laid out in
#: terms of the sample rate, so embeddings computed at 44.1 kHz and at 16 kHz are simply
#: not comparable; forcing one rate is what makes a stored print portable.
TARGET_RATE = 16_000

#: Standard speech-analysis framing. 25 ms is long enough to resolve the spectral
#: envelope and short enough that the vocal tract is stationary across the frame.
_FRAME_MS = 25.0
_HOP_MS = 10.0
_N_MFCC = 20
_N_FILTERS = 40

#: Classic first-order high-pass. Speech falls off at roughly 6 dB/octave above 1 kHz;
#: pre-emphasis flattens that so the upper formants survive the log.
_PRE_EMPHASIS = 0.97

#: The mel bank stops short of the Nyquist frequency: the top of a 16 kHz band carries
#: codec artefacts and hiss, not voice.
_MEL_LOW_HZ = 20.0
_MEL_HIGH_HZ = 7_600.0

#: Half-width of the delta regression window, in frames.
_DELTA_WINDOW = 2

#: An utterance shorter than this cannot say anything about a speaker.
_MIN_SECONDS = 0.35
_MIN_VOICED_FRAMES = 15

#: Frames below this RMS are silence no matter what the median says — it stops a clip of
#: pure room tone from "detecting" speech in its own loudest hiss.
_ABS_ENERGY_FLOOR = 1e-4

#: Floor under the log, so a digitally silent filterbank channel yields a large finite
#: number instead of ``-inf`` and a cepstrum full of NaN.
_LOG_FLOOR = 1e-10

#: The zeroth cepstral coefficient is overall log energy: it tracks how close the mouth
#: was to the microphone and how loud the room is, not who was speaking. Dropping it
#: costs no identity information and removes the largest nuisance dimension.
_DROP_C0 = True

#: Relative weight of the four blocks of the summary, in the order they are built: the
#: static and delta halves of the per-frame mean, then of the per-frame dispersion.
#: Each block is scaled to unit norm before weighting, so these are true relative votes
#: rather than an artefact of the blocks' natural magnitudes.
#:
#: Measured over six voices and 36 held-out trials, sweeping 0, 0.25, 0.5 and 1.0 for
#: the last three: nearly all the identity lives in the static mean, and giving the
#: other three an equal vote (1, 1, 1, 1) narrows the same-versus-different gap from
#: 0.027 to 0.001. Half a vote each is the measured optimum and still lets the dynamics
#: contribute.
_BLOCK_WEIGHTS = (1.0, 0.5, 0.5, 0.5)

#: Added to every per-dimension standard deviation before dividing by it. Small enough
#: not to bias a real dispersion, large enough that a dimension which never moved
#: (a digitally silent band) cannot produce an infinity.
_SCALE_FLOOR = 1e-6


# ══════════════════════════════════════════════════════════════════════════════════════
# Data model
# ══════════════════════════════════════════════════════════════════════════════════════


@dataclass
class Voiceprint:
    """A stored summary of the operator's voice.

    Deliberately small and inspectable: a list of floats an operator can look at,
    delete, or copy to another machine. It contains no recording and no audio — the
    embedding is a lossy statistical summary from which speech cannot be reconstructed.
    """

    embedding: list[float]
    sample_rate: int
    samples: int
    created: float
    backend: str
    version: int = 1

    def to_dict(self) -> dict:
        """Serialise to the plain JSON structure written to disk."""
        return {
            "embedding": [float(value) for value in self.embedding],
            "sample_rate": int(self.sample_rate),
            "samples": int(self.samples),
            "created": float(self.created),
            "backend": str(self.backend),
            "version": int(self.version),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Voiceprint | None":
        """Rebuild from disk, returning ``None`` for anything that is not a voiceprint.

        Hand-validated rather than trusted: this file sits in the project root where a
        half-finished edit or an interrupted write can leave it malformed, and a
        malformed print must degrade to "not enrolled", never to an exception on a code
        path as central as the wake word.
        """
        if not isinstance(data, dict):
            return None
        raw = data.get("embedding")
        if not isinstance(raw, (list, tuple)) or not raw:
            return None
        try:
            embedding = [float(value) for value in raw]
        except (TypeError, ValueError):
            return None
        if not all(_is_finite(value) for value in embedding):
            return None
        try:
            return cls(
                embedding=embedding,
                sample_rate=int(data.get("sample_rate", TARGET_RATE)),
                samples=int(data.get("samples", 1)),
                created=float(data.get("created", 0.0)),
                backend=str(data.get("backend", BACKEND_MFCC)),
                version=int(data.get("version", 1)),
            )
        except (TypeError, ValueError):
            return None


@dataclass
class VerifyResult:
    """The outcome of comparing one utterance against the enrolled print.

    ``reason`` is written for a human — it ends up on the HUD and in the log when the
    assistant declines to answer, and "voice does not match the enrolled print" is a far
    more useful thing to read at midnight than a bare ``False``.
    """

    accepted: bool
    score: float
    threshold: float
    reason: str = ""


# ══════════════════════════════════════════════════════════════════════════════════════
# Small helpers
# ══════════════════════════════════════════════════════════════════════════════════════


def _is_finite(value: float) -> bool:
    """True for a real, usable number. ``NaN`` in a stored print poisons every score."""
    return value == value and value not in (float("inf"), float("-inf"))


def _hz_to_mel(hz: "np.ndarray | float") -> "np.ndarray | float":
    """O'Shaughnessy's mel scale, the one every MFCC implementation agrees on."""
    return 2595.0 * np.log10(1.0 + np.asarray(hz, dtype=np.float64) / 700.0)


def _mel_to_hz(mel: "np.ndarray | float") -> "np.ndarray | float":
    """Inverse of :func:`_hz_to_mel`."""
    return 700.0 * (10.0 ** (np.asarray(mel, dtype=np.float64) / 2595.0) - 1.0)


def _frame_signal(signal: "np.ndarray", frame_len: int, hop: int) -> "np.ndarray":
    """Cut a 1-D signal into overlapping frames, shape ``(frames, frame_len)``.

    Short input is zero-padded up to a single frame rather than returning an empty
    array: callers further up would otherwise have to special-case the difference
    between "too short" and "silent", and both mean the same thing here.
    """
    if signal.size < frame_len:
        signal = np.pad(signal, (0, frame_len - signal.size))
    n_frames = 1 + (signal.size - frame_len) // hop
    offsets = hop * np.arange(n_frames, dtype=np.int64)[:, None]
    return signal[offsets + np.arange(frame_len, dtype=np.int64)[None, :]]


def _mel_filterbank(n_filters: int, n_fft: int, sample_rate: int) -> "np.ndarray":
    """Triangular mel filters, shape ``(n_filters, n_fft // 2 + 1)``.

    Filters are built by linear interpolation between the three mel-spaced corner
    frequencies rather than by the usual integer-bin loop, so a narrow filter that
    falls between two FFT bins still has non-zero response instead of vanishing.
    """
    nyquist = sample_rate / 2.0
    high = min(_MEL_HIGH_HZ, nyquist * 0.999)
    low = min(_MEL_LOW_HZ, high / 2.0)
    corners = _mel_to_hz(np.linspace(_hz_to_mel(low), _hz_to_mel(high), n_filters + 2))
    bin_hz = np.linspace(0.0, nyquist, n_fft // 2 + 1)

    bank = np.zeros((n_filters, bin_hz.size), dtype=np.float64)
    for index in range(n_filters):
        left, centre, right = corners[index], corners[index + 1], corners[index + 2]
        if right <= left:
            continue  # degenerate at absurdly small n_fft; leave the row empty
        rising = (bin_hz - left) / max(centre - left, 1e-9)
        falling = (right - bin_hz) / max(right - centre, 1e-9)
        bank[index] = np.clip(np.minimum(rising, falling), 0.0, None)
    return bank


def _dct_ii(frames: "np.ndarray", n_out: int) -> "np.ndarray":
    """Orthonormal DCT-II along the last axis, keeping ``n_out`` coefficients.

    Written out rather than taken from ``scipy.fft`` because scipy is not a dependency
    of this project and the matrix is 40x20 — the cost of doing it by hand is nil.
    """
    n_in = frames.shape[-1]
    n_out = max(1, min(n_out, n_in))
    k = np.arange(n_out, dtype=np.float64)[:, None]
    n = np.arange(n_in, dtype=np.float64)[None, :]
    basis = np.cos(np.pi * (n + 0.5) * k / n_in)
    scale = np.full((n_out, 1), np.sqrt(2.0 / n_in))
    scale[0, 0] = np.sqrt(1.0 / n_in)
    return frames @ (basis * scale).T


def _deltas(features: "np.ndarray", window: int = _DELTA_WINDOW) -> "np.ndarray":
    """First-order regression deltas, computed over the whole sequence.

    Deltas describe *how* a speaker moves between sounds — coarticulation, speaking
    rate, the shape of a transition — which is exactly the part of a voice that a
    frame-wise average throws away. They are computed before any voice-activity
    selection so that the regression sees genuinely adjacent frames.
    """
    if features.shape[0] < 2:
        return np.zeros_like(features)
    padded = np.pad(features, ((window, window), (0, 0)), mode="edge")
    denominator = 2.0 * sum(step * step for step in range(1, window + 1))
    out = np.zeros_like(features)
    length = features.shape[0]
    for step in range(1, window + 1):
        ahead = padded[window + step : window + step + length]
        behind = padded[window - step : window - step + length]
        out += step * (ahead - behind)
    return out / denominator


def _resample_linear(pcm: "np.ndarray", source_rate: int, target_rate: int) -> "np.ndarray":
    """Rate-convert by linear interpolation.

    Not a good resampler — it aliases — but the alternative is a scipy dependency, and
    what matters here is only that enrolment and verification land on the *same* grid.
    Both sides go through this identical path, so whatever it distorts, it distorts
    consistently.
    """
    if source_rate == target_rate or pcm.size == 0 or source_rate <= 0:
        return pcm
    duration = pcm.size / float(source_rate)
    n_out = int(round(duration * target_rate))
    if n_out < 1:
        return np.zeros(0, dtype=np.float32)
    source_t = np.arange(pcm.size, dtype=np.float64) / float(source_rate)
    target_t = np.arange(n_out, dtype=np.float64) / float(target_rate)
    return np.interp(target_t, source_t, pcm).astype(np.float32)


def _l2_normalise(vector: "np.ndarray") -> "np.ndarray | None":
    """Scale to unit length, or ``None`` when the vector carries no information."""
    norm = float(np.linalg.norm(vector))
    if not _is_finite(norm) or norm <= 1e-12:
        return None
    return (vector / norm).astype(np.float32)


# ══════════════════════════════════════════════════════════════════════════════════════
# Feature extraction
# ══════════════════════════════════════════════════════════════════════════════════════


def mfcc(
    pcm: "np.ndarray",
    sample_rate: int,
    n_mfcc: int = 20,
    n_filters: int = 40,
    frame_ms: float = 25.0,
    hop_ms: float = 10.0,
) -> "np.ndarray":
    """Mel-frequency cepstral coefficients, shape ``(frames, n_mfcc)``.

    The textbook pipeline, hand-rolled: pre-emphasis, framing, a Hamming window, an
    ``rfft`` power spectrum, a triangular mel filterbank, a logarithm and a DCT-II.

    Every division is guarded. Silent input, a single sample, a clip shorter than one
    frame and an all-zero filterbank channel all produce finite numbers, because this
    runs on whatever the microphone happened to capture and the wake path cannot afford
    a ``RuntimeWarning`` turning into a ``NaN`` turning into an accepted stranger.

    :raises RuntimeError: if numpy is unavailable. Every caller inside this module
        checks first; the explicit error is for anyone calling it directly.
    """
    if np is None:  # pragma: no cover - environmental
        raise RuntimeError("numpy is required for MFCC extraction")

    signal = np.asarray(pcm, dtype=np.float32).reshape(-1)
    n_mfcc = max(1, int(n_mfcc))
    n_filters = max(1, int(n_filters))
    if signal.size == 0 or sample_rate <= 0:
        return np.zeros((0, n_mfcc), dtype=np.float32)

    frame_len = max(2, int(round(sample_rate * frame_ms / 1000.0)))
    hop = max(1, int(round(sample_rate * hop_ms / 1000.0)))

    # Pre-emphasis. The first sample has no predecessor and is kept as-is.
    emphasised = np.empty_like(signal)
    emphasised[0] = signal[0]
    if signal.size > 1:
        emphasised[1:] = signal[1:] - _PRE_EMPHASIS * signal[:-1]

    frames = _frame_signal(emphasised, frame_len, hop)
    if frames.shape[0] == 0:
        return np.zeros((0, n_mfcc), dtype=np.float32)

    windowed = frames * np.hamming(frame_len).astype(np.float32)[None, :]

    # A power-of-two transform length keeps the rfft on its fast path; zero-padding a
    # 400-sample frame to 512 costs nothing and changes no spectral content.
    n_fft = 1
    while n_fft < frame_len:
        n_fft *= 2

    spectrum = np.fft.rfft(windowed.astype(np.float64), n=n_fft, axis=1)
    power = (np.abs(spectrum) ** 2) / float(n_fft)

    bank = _mel_filterbank(n_filters, n_fft, sample_rate)
    energies = power @ bank.T
    log_energies = np.log(np.maximum(energies, _LOG_FLOOR))
    return _dct_ii(log_energies, n_mfcc).astype(np.float32)


def _voiced_mask(pcm: "np.ndarray", frame_len: int, hop: int) -> "np.ndarray":
    """Which frames are loud enough to be speech.

    An energy gate, not a classifier. Its job is narrow: in a four-second enrolment
    clip, a second or more is breath and room tone, and averaging cepstra over silence
    measures the room rather than the speaker. The threshold is relative to the clip's
    own median energy (``settings.SPEAKER_VAD_FACTOR``) so it adapts to a quiet talker
    without calibration, with an absolute floor underneath so that a clip containing
    nothing but hiss cannot pass its own loudest hiss off as voice.
    """
    frames = _frame_signal(pcm, frame_len, hop)
    energy = np.sqrt(np.mean(frames.astype(np.float64) ** 2, axis=1))
    factor = max(0.0, float(getattr(settings, "SPEAKER_VAD_FACTOR", 0.35)))
    threshold = max(float(np.median(energy)) * factor, _ABS_ENERGY_FLOOR)
    return energy > threshold


def embed(pcm: "np.ndarray", sample_rate: int) -> "np.ndarray | None":
    """Summarise an utterance as one L2-normalised speaker embedding.

    Voice activity is gated first, then MFCCs and their deltas are summarised over the
    surviving frames as ``concat(mean, std)``. The dispersion matters as much as the
    mean: two speakers can share an average spectrum while differing entirely in how
    far they range around it.

    Returns ``None`` — never a meaningless vector — when there is too little voiced
    audio for the answer to mean anything. A caller that receives ``None`` should treat
    the clip as unverifiable, not as a rejection.
    """
    if np is None:
        return None
    return _embed_with(pcm, sample_rate, active_backend())


def _embed_with(pcm: "np.ndarray", sample_rate: int, backend: str) -> "np.ndarray | None":
    """Embed with one named backend, so a probe always matches the print's backend."""
    if np is None:
        return None
    if backend == BACKEND_RESEMBLYZER:
        vector = _embed_resemblyzer(pcm, sample_rate)
        if vector is not None:
            return vector
        logger.debug("resemblyzer embedding unavailable; falling back to MFCC")
    return _embed_mfcc(pcm, sample_rate)


def _embed_mfcc(pcm: "np.ndarray", sample_rate: int) -> "np.ndarray | None":
    """The default, dependency-free embedding. See :func:`embed`.

    The two normalisation steps below are not decoration; without them this does not
    work at all. Measured over six synthetic voices, enrolling on three clips of one of
    them and scoring thirty-six held-out clips: with a plain ``concat(mean, std)`` the
    enrolled speaker scores 0.952 to 0.977 and five impostors score 0.853 to 0.943 —
    distributions that touch, and no threshold that separates them. With the two steps
    below, the same trials give 0.720 to 0.894 for the enrolled speaker and 0.348 to
    0.693 for the impostors.

    Step one, **scale equalisation**: each dimension's mean is divided by that same
    dimension's standard deviation over the clip, and the dispersion half is carried as
    a logarithm. Raw cepstral coefficients span two orders of magnitude between ``c1``
    and ``c19``, so an unscaled cosine is decided almost entirely by the first two
    coefficients — which encode spectral tilt, a property of the room and the
    microphone more than of the speaker.

    Step two, **centring**: every block has its own mean across dimensions removed. All
    speech shares a large common cepstral shape, and leaving it in place is what pins
    every cosine above 0.9. Removing it turns the comparison into a correlation of
    shape, which is the part that actually differs between two people.
    """
    signal = np.asarray(pcm, dtype=np.float32).reshape(-1)
    if sample_rate <= 0 or signal.size < int(_MIN_SECONDS * sample_rate):
        return None
    if sample_rate != TARGET_RATE:
        signal = _resample_linear(signal, sample_rate, TARGET_RATE)
        sample_rate = TARGET_RATE

    frame_len = max(2, int(round(sample_rate * _FRAME_MS / 1000.0)))
    hop = max(1, int(round(sample_rate * _HOP_MS / 1000.0)))

    coefficients = mfcc(
        signal,
        sample_rate,
        n_mfcc=_N_MFCC,
        n_filters=_N_FILTERS,
        frame_ms=_FRAME_MS,
        hop_ms=_HOP_MS,
    )
    if coefficients.shape[0] == 0:
        return None

    mask = _voiced_mask(signal, frame_len, hop)
    # Framing is identical on both sides, but never index one array with another's
    # length: a rounding difference here would be an exception on the wake path.
    span = min(mask.shape[0], coefficients.shape[0])
    mask, coefficients = mask[:span], coefficients[:span]
    if int(np.count_nonzero(mask)) < _MIN_VOICED_FRAMES:
        return None

    static = coefficients[:, 1:] if _DROP_C0 else coefficients
    if static.shape[1] == 0:
        return None
    features = np.concatenate([static, _deltas(static)], axis=1)
    kept = features[mask].astype(np.float64)

    dispersion = kept.std(axis=0) + _SCALE_FLOOR
    location = kept.mean(axis=0) / dispersion
    width = static.shape[1]

    # concat(mean, std) over the kept frames, in four blocks: the static and delta
    # halves of the mean, then the static and delta halves of the dispersion.
    blocks = (
        location[:width],
        location[width:],
        np.log(dispersion[:width]),
        np.log(dispersion[width:]),
    )

    parts: list["np.ndarray"] = []
    for block, weight in zip(blocks, _BLOCK_WEIGHTS):
        if weight <= 0.0:
            continue
        unit = _l2_normalise(block - block.mean())
        if unit is None:
            return None  # a block with no variation at all means no usable audio
        parts.append(weight * unit)

    summary = np.concatenate(parts).astype(np.float32)
    if not np.all(np.isfinite(summary)):
        return None
    return _l2_normalise(summary)


# --------------------------------------------------------------------------------------
# Optional resemblyzer backend. Imported on first use only: it pulls in torch, and
# paying seconds of import cost to answer "is anyone enrolled?" would be absurd.
# --------------------------------------------------------------------------------------
_encoder_lock = threading.Lock()
_encoder: Any = None
_encoder_tried = False


def _resemblyzer_encoder() -> Any:
    """The shared ``VoiceEncoder``, or ``None`` if resemblyzer is not installed."""
    global _encoder, _encoder_tried
    with _encoder_lock:
        if _encoder_tried:
            return _encoder
        _encoder_tried = True
        try:
            from resemblyzer import VoiceEncoder  # type: ignore

            _encoder = VoiceEncoder(verbose=False)
            logger.debug("resemblyzer speaker encoder loaded")
        except ImportError:
            _encoder = None
        except Exception as exc:  # pragma: no cover - depends on the local install
            _encoder = None
            logger.debug("resemblyzer unavailable: %s", exc)
        return _encoder


def resemblyzer_available() -> bool:
    """True when the optional neural backend can actually be used."""
    return _resemblyzer_encoder() is not None


def active_backend() -> str:
    """Which backend a fresh enrolment would use right now."""
    return BACKEND_RESEMBLYZER if resemblyzer_available() else BACKEND_MFCC


def _embed_resemblyzer(pcm: "np.ndarray", sample_rate: int) -> "np.ndarray | None":
    """Embed with the optional neural encoder, or ``None`` if it is not usable."""
    encoder = _resemblyzer_encoder()
    if encoder is None:
        return None
    signal = np.asarray(pcm, dtype=np.float32).reshape(-1)
    if sample_rate != TARGET_RATE:
        signal = _resample_linear(signal, sample_rate, TARGET_RATE)
    if signal.size < int(_MIN_SECONDS * TARGET_RATE):
        return None
    try:
        from resemblyzer import preprocess_wav  # type: ignore

        wav = preprocess_wav(signal.astype(np.float32), source_sr=TARGET_RATE)
        vector = np.asarray(encoder.embed_utterance(wav), dtype=np.float32)
    except Exception as exc:
        logger.debug("resemblyzer embedding failed: %s", exc)
        return None
    return _l2_normalise(vector)


# ══════════════════════════════════════════════════════════════════════════════════════
# Audio ingestion
# ══════════════════════════════════════════════════════════════════════════════════════


def pcm_from_audio(audio: Any) -> "tuple[np.ndarray, int] | None":
    """Coerce whatever the caller has into mono float32 in ``[-1, 1]`` at 16 kHz.

    Accepts a ``speech_recognition.AudioData`` (duck-typed on ``get_raw_data`` so that
    this module never *requires* speech_recognition), a path to a wav file, raw 16-bit
    little-endian PCM bytes, or a numpy array that is already float samples.

    Returns ``None`` rather than raising for anything unreadable: this sits on the wake
    path, where an unplayable clip is a shrug, not an incident.
    """
    if np is None or audio is None:
        return None

    # speech_recognition.AudioData — let it do the resampling and requantisation, since
    # it already owns the sample-width conversion logic.
    getter = getattr(audio, "get_raw_data", None)
    if callable(getter):
        try:
            raw = getter(convert_rate=TARGET_RATE, convert_width=2)
        except Exception as exc:
            logger.debug("AudioData conversion failed: %s", exc)
            return None
        return _from_int16_bytes(raw, TARGET_RATE)

    if isinstance(audio, (str, Path)):
        return _from_wav_path(Path(audio))

    if isinstance(audio, (bytes, bytearray, memoryview)):
        # No header, so the format has to be assumed; 16-bit mono at the target rate is
        # what every producer inside this project hands over.
        return _from_int16_bytes(bytes(audio), TARGET_RATE)

    if isinstance(audio, np.ndarray):
        signal = _to_mono(np.asarray(audio, dtype=np.float32))
        if signal.size == 0:
            return None
        peak = float(np.max(np.abs(signal)))
        if peak > 1.5:  # plainly integer-scaled samples handed over as floats
            signal = signal / 32768.0
        return signal.astype(np.float32), TARGET_RATE

    logger.debug("Unsupported audio object: %s", type(audio).__name__)
    return None


def _to_mono(signal: "np.ndarray") -> "np.ndarray":
    """Average any channel layout down to one. Stereo halves are near-identical here."""
    if signal.ndim <= 1:
        return signal.reshape(-1)
    return signal.mean(axis=1 if signal.shape[0] >= signal.shape[1] else 0).reshape(-1)


def _from_int16_bytes(raw: bytes, sample_rate: int) -> "tuple[np.ndarray, int] | None":
    """Decode 16-bit little-endian PCM. ``32768`` is the full-scale divisor for int16."""
    if not raw:
        return None
    try:
        samples = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    except ValueError as exc:
        logger.debug("Malformed PCM buffer: %s", exc)
        return None
    if samples.size == 0:
        return None
    return samples, sample_rate


def _from_wav_path(path: Path) -> "tuple[np.ndarray, int] | None":
    """Read a wav file of any common sample width and resample it to 16 kHz."""
    try:
        with wave.open(str(path), "rb") as handle:
            channels = handle.getnchannels()
            width = handle.getsampwidth()
            rate = handle.getframerate()
            raw = handle.readframes(handle.getnframes())
    except (OSError, wave.Error) as exc:
        logger.debug("Could not read wav %s: %s", path, exc)
        return None
    if not raw or rate <= 0:
        return None

    if width == 1:  # unsigned 8-bit, offset binary
        samples = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    elif width == 2:
        samples = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    elif width == 4:
        samples = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648.0
    else:
        logger.debug("Unsupported wav sample width: %d bytes", width)
        return None

    if channels > 1:
        usable = samples.size - (samples.size % channels)
        samples = samples[:usable].reshape(-1, channels).mean(axis=1)
    if samples.size == 0:
        return None
    return _resample_linear(samples.astype(np.float32), rate, TARGET_RATE), TARGET_RATE


def _coerce(clip: Any, sample_rate: int | None) -> "tuple[np.ndarray, int] | None":
    """:func:`pcm_from_audio` with an explicit rate hint for headerless input.

    Only raw buffers and bare arrays need the hint; anything self-describing (a wav
    file, an ``AudioData``) knows its own rate and ignores it.
    """
    if np is None:
        return None
    if sample_rate and sample_rate > 0 and isinstance(clip, (bytes, bytearray, memoryview)):
        decoded = _from_int16_bytes(bytes(clip), int(sample_rate))
        if decoded is None:
            return None
        signal, rate = decoded
        return _resample_linear(signal, rate, TARGET_RATE), TARGET_RATE
    if sample_rate and sample_rate > 0 and isinstance(clip, np.ndarray):
        signal = _to_mono(np.asarray(clip, dtype=np.float32))
        if signal.size == 0:
            return None
        return _resample_linear(signal, int(sample_rate), TARGET_RATE), TARGET_RATE
    return pcm_from_audio(clip)


# ══════════════════════════════════════════════════════════════════════════════════════
# Comparison, enrolment, verification
# ══════════════════════════════════════════════════════════════════════════════════════


def cosine(a: "np.ndarray", b: "np.ndarray") -> float:
    """Cosine similarity, roughly ``-1..1``. Returns ``0.0`` for anything degenerate.

    Zero is the right answer for a degenerate comparison: it is neither a match nor an
    active rejection, and it sits below any sane threshold, so an empty vector can never
    accidentally let someone in.
    """
    if np is None or a is None or b is None:
        return 0.0
    left = np.asarray(a, dtype=np.float64).reshape(-1)
    right = np.asarray(b, dtype=np.float64).reshape(-1)
    if left.size == 0 or left.size != right.size:
        return 0.0
    denominator = float(np.linalg.norm(left) * np.linalg.norm(right))
    if denominator <= 1e-12:
        return 0.0
    score = float(np.dot(left, right) / denominator)
    return score if _is_finite(score) else 0.0


def enroll(clips: Sequence, sample_rate: int | None = None) -> "Voiceprint | None":
    """Build a voiceprint from several clips of the same speaker.

    Each clip is embedded and L2-normalised separately, the results are averaged, and
    the average is re-normalised. Averaging *after* normalisation is what stops one
    loud, long clip from dominating three short ones — every clip gets one equal vote.

    More clips is materially better than longer clips, because the average has to cover
    the phonetic range of the speaker rather than of one sentence:
    ``settings.SPEAKER_ENROLL_PHRASES`` exists for that reason.

    Unusable clips are skipped with a debug note rather than failing the whole
    enrolment; a stutter or a coughing fit on take two should not cost the operator
    takes one and three. Returns ``None`` only when no clip produced an embedding.
    """
    if np is None:
        logger.debug("Cannot enrol: numpy unavailable")
        return None

    backend = active_backend()
    vectors: list["np.ndarray"] = []
    for index, clip in enumerate(clips or []):
        loaded = _coerce(clip, sample_rate)
        if loaded is None:
            logger.debug("Enrolment clip %d could not be decoded", index)
            continue
        signal, rate = loaded
        vector = _embed_with(signal, rate, backend)
        if vector is None:
            logger.debug("Enrolment clip %d held too little voiced audio", index)
            continue
        vectors.append(vector)

    if not vectors:
        logger.debug("Enrolment produced no usable embeddings")
        return None

    centroid = _l2_normalise(np.mean(np.stack(vectors), axis=0))
    if centroid is None:
        return None
    return Voiceprint(
        embedding=[float(value) for value in centroid],
        sample_rate=TARGET_RATE,
        samples=len(vectors),
        created=time.time(),
        backend=backend,
    )


def verify(
    audio: Any,
    print_: "Voiceprint | None" = None,
    threshold: float | None = None,
) -> VerifyResult:
    """Decide whether an utterance came from the enrolled speaker.

    **Fails open, deliberately.** With no voiceprint enrolled, with numpy missing, with
    an unreadable clip or with too little voiced audio to judge, the result is
    ``accepted=True`` and a ``reason`` saying why no judgement was possible. Speaker
    verification is an opt-in convenience; a bug in it must never be able to lock the
    operator out of their own assistant, and the cost of the failure mode it does have
    — occasionally greeting someone it should not have — is a wasted sentence.

    Only a *successful* comparison that scores below ``threshold`` returns
    ``accepted=False``.
    """
    limit = float(threshold if threshold is not None else settings.SPEAKER_THRESHOLD)

    if not settings.SPEAKER_VERIFY_ENABLED and print_ is None:
        return VerifyResult(True, 0.0, limit, "speaker verification disabled")
    if np is None:
        return VerifyResult(True, 0.0, limit, "numpy unavailable")

    reference = print_ if print_ is not None else load()
    if reference is None:
        return VerifyResult(True, 0.0, limit, "no voiceprint enrolled")

    if reference.backend == BACKEND_RESEMBLYZER and not resemblyzer_available():
        # The print was taken with a neural encoder that is no longer installed. An MFCC
        # probe would be compared against a vector from a different space entirely and
        # would score like noise, so refuse to judge rather than judge wrongly.
        return VerifyResult(True, 0.0, limit, "enrolled backend unavailable; re-enrol")

    loaded = pcm_from_audio(audio)
    if loaded is None:
        return VerifyResult(True, 0.0, limit, "audio could not be decoded")

    signal, rate = loaded
    probe = _embed_with(signal, rate, reference.backend)
    if probe is None:
        return VerifyResult(True, 0.0, limit, "too little voiced audio to verify")

    # A probe of a different width than the print cannot be compared at all --

    # an enrolment taken with one backend meeting a probe from another, say. cosine()

    # answers 0.0 for that, which would read as an emphatic rejection and lock the

    # operator out on a path this function promises will fail open.

    reference_vector = np.asarray(reference.embedding, dtype=np.float32).reshape(-1)

    if np.asarray(probe).reshape(-1).size != reference_vector.size:

        return VerifyResult(

            True, 0.0, limit, "voiceprint shape no longer matches; re-enrol"

        )


    score = cosine(probe, np.asarray(reference.embedding, dtype=np.float32))
    if score >= limit:
        return VerifyResult(True, score, limit, "voice matches the enrolled print")
    return VerifyResult(False, score, limit, "voice does not match the enrolled print")


# ══════════════════════════════════════════════════════════════════════════════════════
# Persistence
# ══════════════════════════════════════════════════════════════════════════════════════

_store_lock = threading.RLock()
_cached_print: "Voiceprint | None" = None
#: ``(mtime_ns, size)`` of the file the cache was built from. Cheaper than re-parsing
#: JSON on every wake, and still correct when the operator re-enrols or deletes the file
#: from another process.
_cache_stamp: tuple[int, int] | None = None


def _stamp(path: Path) -> tuple[int, int] | None:
    """Identity of the file on disk, or ``None`` when it is not there."""
    try:
        info = path.stat()
    except OSError:
        return None
    return (info.st_mtime_ns, info.st_size)


def save(print_: Voiceprint) -> bool:
    """Persist a voiceprint. Returns success rather than raising.

    Written to a sibling temp file and moved into place, so an interrupted write leaves
    the previous print intact instead of a half-file that reads as "not enrolled".
    """
    global _cached_print, _cache_stamp
    if print_ is None:
        return False
    path = Path(settings.VOICEPRINT_PATH)
    temporary = path.with_suffix(path.suffix + ".tmp")
    payload = json.dumps(print_.to_dict(), indent=2)
    with _store_lock:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(payload, encoding="utf-8")
            os.replace(temporary, path)
        except (OSError, TypeError, ValueError) as exc:
            logger.warning("Could not save voiceprint to %s: %s", path, exc)
            try:
                temporary.unlink()
            except OSError:
                logger.debug("Leftover temp voiceprint at %s", temporary, exc_info=True)
            return False
        _cached_print = print_
        _cache_stamp = _stamp(path)
    logger.info("Voiceprint saved (%d clips, %s backend)", print_.samples, print_.backend)
    return True


def load() -> "Voiceprint | None":
    """Read the stored voiceprint, cached until the file changes on disk.

    A missing, unreadable or corrupt file all mean the same thing to every caller —
    nobody is enrolled — so all three return ``None`` and are logged at debug.
    """
    global _cached_print, _cache_stamp
    path = Path(settings.VOICEPRINT_PATH)
    with _store_lock:
        stamp = _stamp(path)
        if stamp is None:
            _cached_print, _cache_stamp = None, None
            return None
        if _cached_print is not None and stamp == _cache_stamp:
            return _cached_print
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.debug("Voiceprint at %s is unreadable: %s", path, exc)
            _cached_print, _cache_stamp = None, None
            return None
        parsed = Voiceprint.from_dict(data if isinstance(data, dict) else {})
        if parsed is None:
            logger.debug("Voiceprint at %s is not a valid print", path)
        _cached_print, _cache_stamp = parsed, stamp
        return parsed


def forget() -> bool:
    """Delete the stored voiceprint. True when nothing is enrolled afterwards."""
    global _cached_print, _cache_stamp
    path = Path(settings.VOICEPRINT_PATH)
    with _store_lock:
        _cached_print, _cache_stamp = None, None
        try:
            path.unlink()
        except FileNotFoundError:
            return True  # already gone is the outcome the caller asked for
        except OSError as exc:
            logger.warning("Could not delete voiceprint %s: %s", path, exc)
            return False
    logger.info("Voiceprint deleted")
    return True


def enrolled() -> bool:
    """True when a usable voiceprint is on disk."""
    return load() is not None


def describe() -> str:
    """One line for the ``/voiceprint`` command and the ``--check`` banner."""
    if np is None:
        return "Voiceprint: unavailable — numpy is not installed"

    backend_note = active_backend()
    reference = load()
    if reference is None:
        return (
            "Voiceprint: not enrolled — the call word answers to any voice "
            f"(backend {backend_note})"
        )

    when = "unknown date"
    if reference.created > 0:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(reference.created))
    state = "on" if settings.SPEAKER_VERIFY_ENABLED else "off (enrolled but not enforced)"
    return (
        f"Voiceprint: enrolled {when} from {reference.samples} clip(s), "
        f"{reference.backend} backend, {len(reference.embedding)} dims, "
        f"threshold {settings.SPEAKER_THRESHOLD:.2f}, verification {state}"
    )
