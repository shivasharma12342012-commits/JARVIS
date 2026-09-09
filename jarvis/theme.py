"""Colour engine for J.A.R.V.I.S.

One seed colour in, a whole coherent interface out.

The operator picks a colour — any colour, by hex, by name, off a wheel — and this
module derives every other value the desktop app, the full-screen HUD and the
pinned status strip need: backgrounds tinted towards that hue, surfaces stepped
away from it, a readable variant of the accent for text, and a contrasting ink
for anything printed *on* the accent. Nothing here is hard-coded to cyan, so a
theme built from ``#ff2d55`` is exactly as considered as the shipped default.

Two rules govern the derivation.

*The seed is honoured exactly.* ``--accent`` is the colour the operator chose,
byte for byte, because a colour picker that quietly "improves" your choice is a
colour picker nobody trusts. Fills, glows and the reactor ring all use it raw.

*Text is never allowed to be unreadable.* A seed can be any lightness, including
ones that vanish against the background, so every value used for **text** is
walked towards or away from the background until it clears WCAG AA (4.5:1).
That derived variant is published separately as ``--accent-text``; the raw seed
is left alone.

Typical use::

    from jarvis import theme
    t = theme.Theme(seed="#ff8c42", mode="dark", tint=0.6)
    css = t.css_variables()          # {"--accent": "#ff8c42", ...}
    theme.save(t)                    # remembered across restarts
"""

from __future__ import annotations

import colorsys
import json
import logging
import random
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable

LOG = logging.getLogger("jarvis.theme")

PROJECT_ROOT = Path(__file__).resolve().parent.parent

#: Where the operator's chosen colours are remembered between runs. Sits beside
#: the profile and the voiceprint, and is git-ignored for the same reason: it is
#: one person's preference, not part of the program.
THEME_PATH = PROJECT_ROOT / ".jarvis_theme.json"

#: The three grounds a theme can stand on. ``dark`` is the workshop default;
#: ``midnight`` is true black for OLED panels; ``light`` is for daylight.
MODES = ("dark", "midnight", "light")

#: WCAG AA for body text. Everything this module calls "text" clears it.
CONTRAST_TARGET = 4.5
#: WCAG AA for large text and UI furniture, where a little less is acceptable.
CONTRAST_TARGET_SOFT = 3.0


# ══════════════════════════════════════════════════════════════════════════════════════
# Colour arithmetic
#
# Plain sRGB and HSL, deliberately. A full OKLab pipeline would be perceptually
# nicer, but it would also be several hundred lines of matrices in a module that
# has to import with nothing but the standard library. HSL plus a real contrast
# check gets the same practical result: cohesive ramps that stay readable.
# ══════════════════════════════════════════════════════════════════════════════════════
_HEX_RE = re.compile(r"^#?([0-9a-fA-F]{3}|[0-9a-fA-F]{4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")
_RGB_FUNC_RE = re.compile(
    r"^rgba?\(\s*([0-9.]+)\s*[, ]\s*([0-9.]+)\s*[, ]\s*([0-9.]+)", re.IGNORECASE
)
_HSL_FUNC_RE = re.compile(
    r"^hsla?\(\s*([0-9.\-]+)\s*(?:deg)?\s*[, ]\s*([0-9.]+)%\s*[, ]\s*([0-9.]+)%", re.IGNORECASE
)

#: Colours an operator might reasonably type instead of a hex code. Covers the
#: CSS basics plus the handful of Rich names the shipped palettes were written
#: in, so ``/theme gold1`` and ``/theme orange`` both mean something.
NAMED_COLOURS: dict[str, str] = {
    "black": "#000000", "white": "#ffffff", "red": "#ef4444", "crimson": "#dc143c",
    "maroon": "#800000", "orange": "#f97316", "amber": "#f59e0b", "gold": "#fbbf24",
    "yellow": "#eab308", "lime": "#84cc16", "green": "#22c55e", "emerald": "#10b981",
    "mint": "#4ade80", "teal": "#14b8a6", "cyan": "#22d3ee", "aqua": "#00ffff",
    "sky": "#38bdf8", "azure": "#0ea5e9", "blue": "#3b82f6", "indigo": "#6366f1",
    "violet": "#8b5cf6", "purple": "#a855f7", "magenta": "#e879f9", "fuchsia": "#d946ef",
    "pink": "#ec4899", "rose": "#f43f5e", "salmon": "#fa8072", "coral": "#ff7f50",
    "brown": "#a16207", "tan": "#d2b48c", "grey": "#9ca3af", "gray": "#9ca3af",
    "silver": "#cbd5e1", "slate": "#94a3b8", "steel": "#7dd3fc", "navy": "#1e3a8a",
    "olive": "#84cc16", "turquoise": "#40e0d0", "lavender": "#c4b5fd",
    "peach": "#fdba74", "ruby": "#e11d48", "sapphire": "#2563eb", "jade": "#00a86b",
    # Rich names carried over from the original terminal palettes.
    "bright_cyan": "#22d3ee", "gold1": "#fbbf24", "red1": "#ef4444",
    "bright_white": "#f8fafc", "deep_pink2": "#ec4899", "dark_orange": "#fb923c",
    "steel_blue1": "#5dade2", "light_steel_blue": "#b0c4de", "orange1": "#f97316",
}


def parse_color(value: str, default: str = "#22d3ee") -> str:
    """Normalise anything colour-shaped into ``#rrggbb``.

    Accepts hex with or without ``#`` (3, 4, 6 or 8 digits — alpha is dropped,
    since every surface here is opaque), ``rgb()``/``rgba()``, ``hsl()``/``hsla()``
    and the names in :data:`NAMED_COLOURS`. Anything unrecognisable yields
    ``default`` rather than raising: a mistyped colour should leave the interface
    looking ordinary, not stop the program.
    """
    if not isinstance(value, str):
        return default
    raw = value.strip()
    if not raw:
        return default

    named = NAMED_COLOURS.get(raw.lower().replace(" ", "_").replace("-", "_"))
    if named:
        return named

    match = _HEX_RE.match(raw)
    if match:
        digits = match.group(1)
        if len(digits) in (3, 4):  # #rgb / #rgba → expand each nibble
            digits = "".join(ch * 2 for ch in digits[:3])
        return f"#{digits[:6].lower()}"

    match = _RGB_FUNC_RE.match(raw)
    if match:
        channels = [_clamp(float(g), 0.0, 255.0) for g in match.groups()]
        return to_hex(tuple(int(round(c)) for c in channels))  # type: ignore[arg-type]

    match = _HSL_FUNC_RE.match(raw)
    if match:
        hue, sat, light = (float(g) for g in match.groups())
        return hsl_to_hex((hue % 360.0) / 360.0, sat / 100.0, light / 100.0)

    return default


def is_color(value: str) -> bool:
    """True when :func:`parse_color` would understand ``value`` on its merits."""
    sentinel = "#010203"
    other = "#040506"
    return parse_color(value, sentinel) == parse_color(value, other)


def to_rgb(hex_color: str) -> tuple[int, int, int]:
    """``#rrggbb`` → ``(r, g, b)`` in 0-255."""
    value = parse_color(hex_color).lstrip("#")
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)


def to_hex(rgb: tuple[int, int, int]) -> str:
    """``(r, g, b)`` → ``#rrggbb``, each channel clamped into range."""
    return "#" + "".join(f"{int(round(_clamp(c, 0, 255))):02x}" for c in rgb)


def to_hsl(hex_color: str) -> tuple[float, float, float]:
    """``#rrggbb`` → ``(hue, saturation, lightness)``, each 0..1."""
    r, g, b = (c / 255.0 for c in to_rgb(hex_color))
    h, l, s = colorsys.rgb_to_hls(r, g, b)
    return h, s, l


def hsl_to_hex(hue: float, sat: float, light: float) -> str:
    """``(hue, saturation, lightness)`` in 0..1 → ``#rrggbb``."""
    r, g, b = colorsys.hls_to_rgb(hue % 1.0, _clamp(light, 0.0, 1.0), _clamp(sat, 0.0, 1.0))
    return to_hex((int(round(r * 255)), int(round(g * 255)), int(round(b * 255))))


def mix(a: str, b: str, ratio: float) -> str:
    """Blend ``a`` towards ``b``. ``ratio=0`` is all ``a``, ``1`` is all ``b``."""
    ratio = _clamp(ratio, 0.0, 1.0)
    ar, ag, ab = to_rgb(a)
    br, bg, bb = to_rgb(b)
    return to_hex(
        (
            int(round(ar + (br - ar) * ratio)),
            int(round(ag + (bg - ag) * ratio)),
            int(round(ab + (bb - ab) * ratio)),
        )
    )


def lighten(hex_color: str, amount: float) -> str:
    """Raise lightness by ``amount`` (0..1 of the remaining headroom)."""
    h, s, l = to_hsl(hex_color)
    return hsl_to_hex(h, s, l + (1.0 - l) * _clamp(amount, 0.0, 1.0))


def darken(hex_color: str, amount: float) -> str:
    """Lower lightness by ``amount`` (0..1 of the distance to black)."""
    h, s, l = to_hsl(hex_color)
    return hsl_to_hex(h, s, l * (1.0 - _clamp(amount, 0.0, 1.0)))


def saturate(hex_color: str, amount: float) -> str:
    """Push saturation up (positive) or down (negative) by ``amount``."""
    h, s, l = to_hsl(hex_color)
    return hsl_to_hex(h, _clamp(s + amount, 0.0, 1.0), l)


def rotate(hex_color: str, degrees: float) -> str:
    """Spin the hue by ``degrees`` around the wheel, keeping S and L."""
    h, s, l = to_hsl(hex_color)
    return hsl_to_hex(h + degrees / 360.0, s, l)


def with_alpha(hex_color: str, alpha: float) -> str:
    """``rgba(...)`` string for the CSS that wants real translucency."""
    r, g, b = to_rgb(hex_color)
    return f"rgba({r}, {g}, {b}, {round(_clamp(alpha, 0.0, 1.0), 3)})"


def luminance(hex_color: str) -> float:
    """Relative luminance per WCAG 2.1, used by :func:`contrast`."""
    channels = []
    for raw in to_rgb(hex_color):
        c = raw / 255.0
        channels.append(c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4)
    r, g, b = channels
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: str, b: str) -> float:
    """WCAG contrast ratio between two colours: 1.0 (identical) to 21.0."""
    la, lb = luminance(a), luminance(b)
    lighter, darker = max(la, lb), min(la, lb)
    return (lighter + 0.05) / (darker + 0.05)


def ink_for(background: str) -> str:
    """Black or white — whichever can actually be read on ``background``."""
    return "#0b0b0d" if contrast(background, "#0b0b0d") >= contrast(background, "#ffffff") else "#ffffff"


def readable(colour: str, background: str, target: float = CONTRAST_TARGET) -> str:
    """Walk ``colour`` away from ``background`` until it clears ``target``.

    Hue and saturation are preserved; only lightness moves, so an operator's
    magenta stays recognisably magenta even when it has to be lifted three stops
    to be legible. If the hue simply cannot reach the target — a saturated blue
    on white, say — the closest attempt is returned rather than a grey, because
    losing the colour entirely is the worse failure.
    """
    if contrast(colour, background) >= target:
        return colour
    h, s, l = to_hsl(colour)
    # Move away from the background: lift on dark grounds, drop on light ones.
    direction = 1.0 if luminance(background) < 0.5 else -1.0
    best, best_ratio = colour, contrast(colour, background)
    for step in range(1, 51):
        candidate = hsl_to_hex(h, s, l + direction * step * 0.02)
        ratio = contrast(candidate, background)
        if ratio > best_ratio:
            best, best_ratio = candidate, ratio
        if ratio >= target:
            return candidate
    return best


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


# ══════════════════════════════════════════════════════════════════════════════════════
# The theme itself
# ══════════════════════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class Theme:
    """A complete interface colour scheme, derived from one or two seeds.

    Only ``seed`` is required. Everything else has a defensible default, and
    every field is a knob the operator is allowed to turn:

    ``seed``       the accent — buttons, the reactor ring, the caret, links.
    ``secondary``  the supporting hue. Derived 40° off the seed when omitted.
    ``mode``       ``dark`` (default), ``midnight`` (true black) or ``light``.
    ``tint``       0..1: how much of the seed's hue bleeds into the background.
                   0 is neutral grey, 1 is unmistakably *your* colour. It
                   defaults low on purpose — see the note on restraint below.
    ``contrast``   0..1: how far the surfaces separate from the background.
    ``radius``     corner rounding in pixels, 0 (severe) to 28 (soft).
    ``glow``       0..1: strength of the accent bloom behind live elements.
    ``overrides``  raw CSS variable overrides for anyone who wants the last word.

    A note on restraint. The shipped default is grey — near-monochrome, with the
    accent reaching only the things that need to be noticed: the selected row,
    the caret, a status dot, a focus ring. A tool you keep open all day should
    not be shouting a hue at you, and an interface where every surface is tinted
    reads as a demo rather than an instrument. The colour is *there* — turn
    ``tint`` up and it floods the whole room — but you have to ask for it.
    """

    name: str = "Custom"
    seed: str = "#8b9099"
    secondary: str = ""
    mode: str = "dark"
    tint: float = 0.10
    contrast: float = 0.55
    radius: int = 8
    glow: float = 0.10
    font: str = "system"
    overrides: dict[str, str] = field(default_factory=dict)

    # -- normalisation -----------------------------------------------------------------
    def __post_init__(self) -> None:
        # Frozen dataclass: normalise through object.__setattr__ so a theme built
        # from a hand-edited JSON file is as trustworthy as one built in code.
        object.__setattr__(self, "seed", parse_color(self.seed))
        object.__setattr__(
            self,
            "secondary",
            parse_color(self.secondary, rotate(self.seed, 42)) if self.secondary else rotate(self.seed, 42),
        )
        object.__setattr__(self, "mode", self.mode if self.mode in MODES else "dark")
        object.__setattr__(self, "tint", _clamp(float(self.tint), 0.0, 1.0))
        object.__setattr__(self, "contrast", _clamp(float(self.contrast), 0.0, 1.0))
        object.__setattr__(self, "radius", int(_clamp(float(self.radius), 0, 28)))
        object.__setattr__(self, "glow", _clamp(float(self.glow), 0.0, 1.0))
        object.__setattr__(self, "overrides", dict(self.overrides or {}))

    @property
    def dark(self) -> bool:
        """True when this theme stands on a dark ground."""
        return self.mode != "light"

    # -- derivation --------------------------------------------------------------------
    def background(self) -> str:
        """The page ground: near-black, true black or near-white, hue-tinted."""
        hue, sat, _ = to_hsl(self.seed)
        if self.mode == "midnight":
            return hsl_to_hex(hue, sat * 0.35 * self.tint, 0.020 + 0.020 * self.tint)
        if self.mode == "light":
            return hsl_to_hex(hue, min(sat, 0.55) * 0.30 * self.tint, 0.985 - 0.030 * self.tint)
        return hsl_to_hex(hue, min(sat, 0.85) * 0.42 * self.tint, 0.055 + 0.022 * self.tint)

    def _surface(self, step: int) -> str:
        """Surfaces climb away from the ground in even, hue-consistent steps."""
        ground = self.background()
        hue, sat, light = to_hsl(ground)
        # Amount of separation per step. `contrast` scales it; dark grounds need
        # a smaller absolute delta than light ones to read as the same distance.
        span = (0.024 + 0.034 * self.contrast) if self.dark else (0.020 + 0.026 * self.contrast)
        direction = 1.0 if self.dark else -1.0
        return hsl_to_hex(hue, sat * (1.0 + 0.10 * step), light + direction * span * step)

    def css_variables(self) -> dict[str, str]:
        """The full token set, ready to be written into a ``:root`` block.

        Every value the front end needs comes from here — the front end contains
        no colour literals at all, which is what makes an arbitrary seed work.
        """
        bg = self.background()
        surface = self._surface(1)
        surface2 = self._surface(2)
        surface3 = self._surface(3)
        elevated = self._surface(4)

        accent = self.seed
        secondary = self.secondary
        # Text colours: strong enough to read, hue intact.
        text = readable(mix(ink_for(bg), accent, 0.06), bg, 12.0)
        text_dim = readable(mix(text, bg, 0.38), bg, 5.5)
        text_faint = readable(mix(text, bg, 0.62), bg, CONTRAST_TARGET_SOFT)

        # Status hues are pulled towards the seed just enough to belong to the
        # same family, then made readable independently — a warning must stay
        # legible even when the operator's chosen colour is also yellow.
        ok = readable(mix("#2fbf71", accent, 0.16), bg, CONTRAST_TARGET_SOFT)
        warn = readable(mix("#f5a524", accent, 0.12), bg, CONTRAST_TARGET_SOFT)
        danger = readable(mix("#f0525b", accent, 0.10), bg, CONTRAST_TARGET_SOFT)

        tokens: dict[str, str] = {
            "--bg": bg,
            "--bg-deep": darken(bg, 0.35) if self.dark else mix(bg, "#000000", 0.04),
            "--surface": surface,
            "--surface-2": surface2,
            "--surface-3": surface3,
            "--elevated": elevated,
            "--line": mix(surface2, accent, 0.10 + 0.10 * self.tint),
            "--line-strong": mix(surface3, accent, 0.26),
            "--text": text,
            "--text-dim": text_dim,
            "--text-faint": text_faint,
            "--accent": accent,
            "--accent-text": readable(accent, bg, CONTRAST_TARGET),
            "--accent-strong": saturate(lighten(accent, 0.12) if self.dark else darken(accent, 0.10), 0.06),
            "--accent-muted": mix(accent, bg, 0.55),
            "--accent-soft": mix(accent, bg, 0.82),
            "--accent-ghost": mix(accent, bg, 0.92),
            "--accent-line": with_alpha(accent, 0.32),
            "--accent-veil": with_alpha(accent, 0.10 + 0.14 * self.glow),
            "--accent-glow": with_alpha(accent, 0.18 + 0.42 * self.glow),
            "--on-accent": ink_for(accent),
            "--secondary": secondary,
            "--secondary-text": readable(secondary, bg, CONTRAST_TARGET),
            "--secondary-soft": mix(secondary, bg, 0.84),
            "--ok": ok,
            "--warn": warn,
            "--danger": danger,
            "--user-bubble": mix(surface2, accent, 0.14),
            "--user-line": with_alpha(accent, 0.28),
            "--agent-bubble": surface,
            "--scrim": with_alpha(darken(bg, 0.6) if self.dark else "#0b1220", 0.62),
            "--shadow": with_alpha("#000000", 0.42 if self.dark else 0.14),
            "--radius": f"{self.radius}px",
            "--radius-sm": f"{max(4, self.radius - 6)}px",
            "--radius-lg": f"{self.radius + 8}px",
            "--glow-strength": f"{round(self.glow, 3)}",
            # The ambient blooms read very differently on the two grounds: on
            # near-black they are light, on near-white they are dirt. Hence a
            # token rather than one opacity in the stylesheet.
            "--bloom-opacity": str(
                round((0.55 * self.glow) if self.dark else (0.20 * self.glow), 3)
            ),
            "--font-ui": _FONT_STACKS.get(self.font, _FONT_STACKS["system"]),
            "--font-mono": _FONT_STACKS["mono"],
            "--font-display": _FONT_STACKS["display"],
            "--color-scheme": "dark" if self.dark else "light",
        }
        tokens.update({k: v for k, v in self.overrides.items() if k.startswith("--")})
        return tokens

    def css(self, selector: str = ":root") -> str:
        """The token set rendered as a CSS rule."""
        body = "\n".join(f"  {k}: {v};" for k, v in self.css_variables().items())
        return f"{selector} {{\n{body}\n}}"

    # -- bridges to the terminal front ends ---------------------------------------------
    def rich_palette(self) -> dict[str, str]:
        """Field values for :class:`jarvis.ui.Palette`, so the strip matches.

        Rich accepts ``#rrggbb`` wherever it accepts a colour name, so the same
        derivation drives the terminal without a second colour system.
        """
        tokens = self.css_variables()
        return {
            "name": self.name,
            "primary": tokens["--accent-text"],
            "secondary": tokens["--secondary-text"],
            "accent": tokens["--accent-text"],
            "warn": tokens["--warn"],
            "danger": tokens["--danger"],
            "dim": tokens["--text-faint"],
            "text": tokens["--text"],
            "border": tokens["--accent-muted"],
        }

    def textual_theme(self) -> dict[str, Any]:
        """Constructor arguments for a ``textual.theme.Theme``."""
        tokens = self.css_variables()
        return {
            "name": f"jarvis-{_slug(self.name)}",
            "primary": tokens["--accent-text"],
            "secondary": tokens["--secondary-text"],
            "accent": tokens["--accent"],
            "warning": tokens["--warn"],
            "error": tokens["--danger"],
            "success": tokens["--ok"],
            "foreground": tokens["--text"],
            "background": tokens["--bg"],
            "surface": tokens["--surface"],
            "panel": tokens["--surface-2"],
            "dark": self.dark,
        }

    def textual_ink(self) -> dict[str, str]:
        """Field values for :class:`jarvis.tui.Ink`."""
        tokens = self.css_variables()
        return {
            "primary": tokens["--accent-text"],
            "secondary": tokens["--secondary-text"],
            "accent": tokens["--accent"],
            "warning": tokens["--warn"],
            "error": tokens["--danger"],
            "success": tokens["--ok"],
            "text": tokens["--text"],
            "soft": tokens["--text-dim"],
            "muted": tokens["--text-faint"],
            "faint": mix(tokens["--text-faint"], tokens["--bg"], 0.45),
        }

    # -- serialisation -----------------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        """A plain dict, suitable for JSON and for the desktop app's state feed."""
        return {
            "name": self.name,
            "seed": self.seed,
            "secondary": self.secondary,
            "mode": self.mode,
            "tint": round(self.tint, 3),
            "contrast": round(self.contrast, 3),
            "radius": self.radius,
            "glow": round(self.glow, 3),
            "font": self.font,
            "overrides": dict(self.overrides),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "Theme":
        """Rebuild a theme from :meth:`to_dict`, ignoring anything unexpected.

        Unknown keys are dropped rather than raising, so a theme file written by
        a newer version still loads on an older one.
        """
        if not isinstance(data, dict):
            return cls()
        allowed = {
            "name", "seed", "secondary", "mode", "tint",
            "contrast", "radius", "glow", "font", "overrides",
        }
        clean = {k: v for k, v in data.items() if k in allowed and v is not None}
        try:
            return cls(**clean)
        except (TypeError, ValueError):
            LOG.warning("Unusable theme payload; falling back to the default", exc_info=True)
            return cls()

    def evolve(self, **changes: Any) -> "Theme":
        """A copy with ``changes`` applied — the frozen-dataclass update."""
        return replace(self, **changes)


_FONT_STACKS: dict[str, str] = {
    "system": (
        '"Inter", "SF Pro Display", -apple-system, BlinkMacSystemFont, "Segoe UI", '
        'Roboto, "Helvetica Neue", Arial, sans-serif'
    ),
    "grotesk": '"Space Grotesk", "Inter", -apple-system, "Segoe UI", Roboto, sans-serif',
    "serif": '"Iowan Old Style", "Palatino Linotype", Palatino, Georgia, serif',
    # High-contrast serif for the wordmark only. Every face here ships with an
    # operating system, so the display type is right on the first paint and
    # never waits on a network that may not be there.
    "display": (
        'Didot, "Bodoni MT", "Playfair Display", "Big Caslon", '
        '"Hoefler Text", "Palatino Linotype", Palatino, Georgia, serif'
    ),
    "mono": (
        '"JetBrains Mono", "SF Mono", "Cascadia Code", "Fira Code", '
        'Consolas, "Liberation Mono", monospace'
    ),
    "rounded": '"Nunito", "SF Pro Rounded", "Segoe UI Variable", system-ui, sans-serif',
}

#: The font choices offered in the theme studio, as (key, label) pairs.
FONTS: tuple[tuple[str, str], ...] = (
    ("system", "System"),
    ("grotesk", "Grotesk"),
    ("rounded", "Rounded"),
    ("serif", "Serif"),
    ("mono", "Monospace"),
)


# ══════════════════════════════════════════════════════════════════════════════════════
# Presets
#
# Starting points, not a menu. Each one is a seed and a posture; the operator is
# expected to grab the wheel afterwards and make it theirs.
# ══════════════════════════════════════════════════════════════════════════════════════
PRESETS: dict[str, Theme] = {
    # -- Quiet. Where an instrument you keep open all day should start. --------
    "graphite": Theme(name="Graphite", seed="#8b9099", mode="dark", tint=0.10, contrast=0.55, radius=8, glow=0.10),
    "carbon": Theme(name="Carbon", seed="#7f8489", mode="midnight", tint=0.06, contrast=0.6, radius=6, glow=0.08),
    "paper": Theme(name="Paper", seed="#4a4f57", mode="light", tint=0.08, contrast=0.5, radius=8, glow=0.06),
    "bone": Theme(name="Bone", seed="#8a7f6d", mode="dark", tint=0.18, contrast=0.55, radius=10, glow=0.12),
    "arc_reactor": Theme(name="Arc Reactor", seed="#4aa8c0", mode="dark", tint=0.16, contrast=0.55, radius=8, glow=0.18),
    "olive": Theme(name="Olive", seed="#7d8b6a", mode="dark", tint=0.14, contrast=0.55, radius=8, glow=0.12),
    "oxide": Theme(name="Oxide", seed="#a8705a", mode="dark", tint=0.16, contrast=0.55, radius=8, glow=0.14),
    "ink": Theme(name="Ink", seed="#6b7a99", mode="midnight", tint=0.12, contrast=0.6, radius=6, glow=0.12),

    # -- Loud. Still here, still one click away, but no longer the default. ----
    "house_party": Theme(name="House Party", seed="#fbbf24", mode="dark", tint=0.6, contrast=0.5, radius=12, glow=0.7),
    "veronica": Theme(name="Veronica", seed="#ef4444", mode="midnight", tint=0.55, contrast=0.5, radius=10, glow=0.65),
    "clean_slate": Theme(name="Clean Slate", seed="#94a3b8", mode="dark", tint=0.22, contrast=0.5, radius=10, glow=0.25),
    "matrix": Theme(name="Matrix", seed="#4ade80", mode="midnight", tint=0.4, contrast=0.55, radius=6, glow=0.6),
    "ultraviolet": Theme(name="Ultraviolet", seed="#a855f7", mode="dark", tint=0.5, contrast=0.5, radius=14, glow=0.55),
    "sunset": Theme(name="Sunset", seed="#fb7185", mode="dark", tint=0.5, contrast=0.5, radius=14, glow=0.5),
    "daylight": Theme(name="Daylight", seed="#2563eb", mode="light", tint=0.35, contrast=0.5, radius=10, glow=0.2),
    "monochrome": Theme(name="Monochrome", seed="#e5e7eb", mode="midnight", tint=0.0, contrast=0.65, radius=4, glow=0.05),
}

#: The shipped terminal palette names, mapped onto their desktop equivalents, so
#: ``/theme veronica`` means the same thing in both front ends.
PALETTE_ALIASES: dict[str, str] = {
    "standard": "graphite",
    "house_party": "house_party",
    "veronica": "veronica",
    "clean_slate": "clean_slate",
}


def preset(name: str) -> Theme | None:
    """Look a preset up by key, label or terminal-palette alias."""
    key = _slug(name)
    if key in PRESETS:
        return PRESETS[key]
    if key in PALETTE_ALIASES:
        return PRESETS[PALETTE_ALIASES[key]]
    for candidate in PRESETS.values():
        if _slug(candidate.name) == key:
            return candidate
    return None


def presets_payload() -> list[dict[str, Any]]:
    """Every preset as a dict, for the theme studio's swatch grid."""
    return [dict(key=key, **value.to_dict()) for key, value in PRESETS.items()]


def surprise(rng: random.Random | None = None) -> Theme:
    """A random theme that is still a *good* theme.

    The hue is free, but saturation and lightness are drawn from bands that
    produce a usable accent, so "surprise me" never lands on mud.
    """
    rng = rng or random.Random()
    hue = rng.random()
    sat = rng.uniform(0.62, 0.95)
    light = rng.uniform(0.52, 0.68)
    mode = rng.choice(["dark", "dark", "dark", "midnight", "light"])
    if mode == "light":
        light = rng.uniform(0.38, 0.50)
    seed = hsl_to_hex(hue, sat, light)
    return Theme(
        name="Surprise",
        seed=seed,
        secondary=hsl_to_hex((hue + rng.uniform(0.08, 0.22)) % 1.0, sat * 0.9, light),
        mode=mode,
        # Restrained bands, matching the shipped defaults. "Surprise me" should
        # hand back something you would actually keep, not a lava lamp.
        tint=rng.uniform(0.06, 0.30),
        contrast=rng.uniform(0.45, 0.68),
        radius=rng.choice([4, 6, 8, 10, 12]),
        glow=rng.uniform(0.05, 0.25),
    )


# ══════════════════════════════════════════════════════════════════════════════════════
# Persistence
# ══════════════════════════════════════════════════════════════════════════════════════
def load(path: Path | None = None) -> Theme:
    """Read the remembered theme, or hand back the default.

    A missing, empty or corrupt file is not an error worth surfacing: the
    operator gets the shipped colours and can pick again.
    """
    target = path or THEME_PATH
    try:
        raw = target.read_text(encoding="utf-8")
    except (FileNotFoundError, NotADirectoryError):
        return PRESETS["graphite"]
    except OSError:
        LOG.warning("Could not read %s; using the default theme", target, exc_info=True)
        return PRESETS["graphite"]
    try:
        return Theme.from_dict(json.loads(raw))
    except (json.JSONDecodeError, TypeError):
        LOG.warning("%s is not valid JSON; using the default theme", target)
        return PRESETS["graphite"]


def save(theme: Theme, path: Path | None = None) -> bool:
    """Persist ``theme``. Returns False when the disk would not have it."""
    target = path or THEME_PATH
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(theme.to_dict(), indent=2) + "\n", encoding="utf-8")
        return True
    except OSError:
        LOG.warning("Could not write %s; the theme will not survive a restart", target, exc_info=True)
        return False


def forget(path: Path | None = None) -> bool:
    """Delete the stored theme. Returns True when there was one to delete."""
    target = path or THEME_PATH
    try:
        target.unlink()
        return True
    except (FileNotFoundError, OSError):
        return False


def resolve(spec: str, base: Theme | None = None) -> Theme | None:
    """Turn one operator-typed word into a theme.

    Accepts a preset name, a bare colour (``#ff8c42``, ``violet``), a mode
    (``dark``, ``light``, ``midnight``) or ``surprise``. Returns None when the
    word means nothing, so the caller can print the list instead of guessing.
    """
    text = (spec or "").strip()
    if not text:
        return None
    key = _slug(text)

    if key in {"surprise", "random", "shuffle"}:
        return surprise()

    found = preset(text)
    if found is not None:
        return found

    current = base or load()
    if key in MODES:
        return current.evolve(mode=key)

    if is_color(text):
        colour = parse_color(text)
        return current.evolve(name="Custom", seed=colour, secondary="", overrides={})

    return None


def _slug(value: str) -> str:
    """``"House Party"`` → ``"house_party"``. The one key format used here."""
    return re.sub(r"[^a-z0-9]+", "_", (value or "").strip().lower()).strip("_")


def describe(theme: Theme) -> str:
    """A one-line human summary, for the terminal's confirmation message."""
    return (
        f"{theme.name} · {theme.seed} · {theme.mode} · "
        f"tint {int(theme.tint * 100)}% · glow {int(theme.glow * 100)}%"
    )


__all__ = [
    "Theme", "PRESETS", "MODES", "FONTS", "THEME_PATH", "NAMED_COLOURS",
    "parse_color", "is_color", "to_rgb", "to_hex", "to_hsl", "hsl_to_hex",
    "mix", "lighten", "darken", "saturate", "rotate", "with_alpha",
    "luminance", "contrast", "readable", "ink_for",
    "preset", "presets_payload", "surprise", "resolve", "describe",
    "load", "save", "forget",
]
