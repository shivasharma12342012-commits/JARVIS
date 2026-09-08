"""The colour engine: derivation, readability, persistence and parsing.

The promise this module tests is a specific one. An operator may pick *any*
colour -- including the awkward ones, a near-black seed on a dark ground or a
near-white one on a light ground -- and the interface that comes out must still
be readable. So most of what follows is contrast arithmetic rather than
comparisons against fixed hex values: pinning the exact output would make the
derivation impossible to tune without rewriting the tests.
"""

from __future__ import annotations

import json
import random

import pytest

from jarvis import theme as theme_mod
from jarvis.theme import CONTRAST_TARGET, CONTRAST_TARGET_SOFT, Theme

#: The seeds most likely to break something, not the ones most likely to work.
AWKWARD_SEEDS = [
    "#000000",   # black: cannot be darkened
    "#ffffff",   # white: cannot be lightened
    "#eab308",   # yellow: bright, and collides with the warning colour
    "#0000ff",   # pure blue: very dark for its saturation
    "#7f7f7f",   # mid grey: no hue to derive a background from
    "#ff00ff",   # full magenta
    "#111111",   # nearly the dark background itself
    "#22d3ee",   # the shipped default, for good measure
]


# ══════════════════════════════════════════════════════════════════════════════════════
# Parsing
# ══════════════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize(
    "written, expected",
    [
        ("#22d3ee", "#22d3ee"),
        ("22d3ee", "#22d3ee"),
        ("#2DE", "#22ddee"),
        ("#22D3EE", "#22d3ee"),
        ("  #22d3ee  ", "#22d3ee"),
        ("#22d3eeff", "#22d3ee"),          # alpha is dropped, not honoured
        ("rgb(34, 211, 238)", "#22d3ee"),
        ("rgba(34, 211, 238, 0.5)", "#22d3ee"),
        ("violet", "#8b5cf6"),
        ("VIOLET", "#8b5cf6"),
        ("light steel blue", "#b0c4de"),   # spaces and dashes both normalise
        ("gold1", "#fbbf24"),              # a Rich name from the old palettes
    ],
)
def test_parse_color_understands_what_an_operator_might_type(written, expected):
    assert theme_mod.parse_color(written) == expected


@pytest.mark.parametrize("nonsense", ["", "   ", "not a colour", "#12345", "#gggggg", None, 42])
def test_parse_color_falls_back_rather_than_raising(nonsense):
    """A mistyped colour leaves the interface ordinary; it never stops the program."""
    assert theme_mod.parse_color(nonsense, "#abcdef") == "#abcdef"
    assert not theme_mod.is_color(nonsense)


def test_hsl_round_trips():
    for seed in AWKWARD_SEEDS:
        h, s, light = theme_mod.to_hsl(seed)
        assert theme_mod.hsl_to_hex(h, s, light) == seed


def test_mix_reaches_both_ends():
    assert theme_mod.mix("#000000", "#ffffff", 0.0) == "#000000"
    assert theme_mod.mix("#000000", "#ffffff", 1.0) == "#ffffff"
    assert theme_mod.mix("#000000", "#ffffff", 0.5) == "#808080"


def test_contrast_matches_the_known_extremes():
    assert theme_mod.contrast("#000000", "#ffffff") == pytest.approx(21.0, abs=0.01)
    assert theme_mod.contrast("#123456", "#123456") == pytest.approx(1.0, abs=0.01)


# ══════════════════════════════════════════════════════════════════════════════════════
# Readability -- the part that has to hold for every colour, not most of them
# ══════════════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("seed", AWKWARD_SEEDS)
@pytest.mark.parametrize("mode", theme_mod.MODES)
def test_every_seed_produces_a_readable_interface(seed, mode):
    tokens = Theme(seed=seed, mode=mode).css_variables()
    ground = tokens["--bg"]

    # Body text clears AA; supporting text and status colours clear the large-text
    # threshold, which is what they are used at.
    assert theme_mod.contrast(tokens["--text"], ground) >= CONTRAST_TARGET
    assert theme_mod.contrast(tokens["--accent-text"], ground) >= CONTRAST_TARGET
    for name in ("--text-dim", "--text-faint", "--ok", "--warn", "--danger", "--secondary-text"):
        assert theme_mod.contrast(tokens[name], ground) >= CONTRAST_TARGET_SOFT, name

    # And anything printed on the accent itself -- button labels, the send arrow.
    assert theme_mod.contrast(tokens["--on-accent"], tokens["--accent"]) >= CONTRAST_TARGET


@pytest.mark.parametrize("seed", AWKWARD_SEEDS)
def test_the_seed_is_honoured_exactly(seed):
    """The accent is the colour the operator chose, byte for byte."""
    assert Theme(seed=seed).css_variables()["--accent"] == seed


def test_readable_keeps_the_hue_it_was_given():
    """Lifting a colour for legibility must not turn it grey."""
    lifted = theme_mod.readable("#3b0764", "#0b0b0d", CONTRAST_TARGET)
    original_hue, original_sat, _ = theme_mod.to_hsl("#3b0764")
    hue, sat, _ = theme_mod.to_hsl(lifted)
    assert hue == pytest.approx(original_hue, abs=0.02)
    assert sat == pytest.approx(original_sat, abs=0.02)
    assert theme_mod.contrast(lifted, "#0b0b0d") >= CONTRAST_TARGET


def test_readable_leaves_an_already_readable_colour_alone():
    assert theme_mod.readable("#ffffff", "#000000") == "#ffffff"


def test_ink_for_picks_the_side_with_more_contrast():
    assert theme_mod.ink_for("#ffffff") == "#0b0b0d"
    assert theme_mod.ink_for("#000000") == "#ffffff"


# ══════════════════════════════════════════════════════════════════════════════════════
# Derivation
# ══════════════════════════════════════════════════════════════════════════════════════
def test_tint_carries_the_seed_hue_into_the_background():
    """The knob that makes an arbitrary accent feel like a whole theme."""
    neutral = Theme(seed="#ef4444", tint=0.0).css_variables()["--bg"]
    tinted = Theme(seed="#ef4444", tint=1.0).css_variables()["--bg"]
    assert theme_mod.to_hsl(neutral)[1] < theme_mod.to_hsl(tinted)[1]


def test_the_three_grounds_are_ordered_by_lightness():
    seeds = {mode: Theme(seed="#22d3ee", mode=mode).css_variables()["--bg"] for mode in theme_mod.MODES}
    assert (
        theme_mod.luminance(seeds["midnight"])
        < theme_mod.luminance(seeds["dark"])
        < theme_mod.luminance(seeds["light"])
    )


def test_contrast_knob_separates_the_surfaces():
    flat = Theme(contrast=0.0).css_variables()
    stepped = Theme(contrast=1.0).css_variables()
    assert theme_mod.contrast(stepped["--elevated"], stepped["--bg"]) > theme_mod.contrast(
        flat["--elevated"], flat["--bg"]
    )


def test_surfaces_climb_away_from_the_ground_in_order():
    tokens = Theme(mode="dark").css_variables()
    steps = [tokens[name] for name in ("--bg", "--surface", "--surface-2", "--surface-3", "--elevated")]
    luminances = [theme_mod.luminance(value) for value in steps]
    assert luminances == sorted(luminances)


def test_a_secondary_is_derived_when_none_is_given_and_kept_when_one_is():
    assert Theme(seed="#22d3ee").secondary != "#22d3ee"
    assert Theme(seed="#22d3ee", secondary="#ff0000").secondary == "#ff0000"


def test_geometry_tokens_follow_the_radius():
    tokens = Theme(radius=20).css_variables()
    assert tokens["--radius"] == "20px"
    assert tokens["--radius-sm"] == "14px"
    assert tokens["--radius-lg"] == "28px"


def test_overrides_have_the_last_word():
    tokens = Theme(seed="#22d3ee", overrides={"--bg": "#010203", "ignored": "x"}).css_variables()
    assert tokens["--bg"] == "#010203"
    assert "ignored" not in tokens


def test_css_renders_a_usable_rule():
    css = Theme(seed="#ff8c42").css()
    assert css.startswith(":root {") and css.endswith("}")
    assert "--accent: #ff8c42;" in css


def test_out_of_range_values_are_clamped_not_rejected():
    """A hand-edited theme file must not be able to produce a broken interface."""
    theme = Theme(tint=9.0, contrast=-4.0, radius=999, glow=7.0, mode="chartreuse")
    assert (theme.tint, theme.contrast, theme.radius, theme.glow) == (1.0, 0.0, 28, 1.0)
    assert theme.mode == "dark"


# ══════════════════════════════════════════════════════════════════════════════════════
# Bridges to the terminal front ends
# ══════════════════════════════════════════════════════════════════════════════════════
def test_rich_palette_fills_every_field_the_status_strip_needs():
    from jarvis.ui import Palette

    palette = Palette(**Theme(seed="#ff8c42").rich_palette())
    assert palette.primary.startswith("#") and palette.border.startswith("#")


def test_textual_bridges_fill_every_field_the_full_screen_hud_needs():
    from textual.theme import Theme as TextualTheme

    from jarvis.tui import Ink

    theme = Theme(seed="#ff8c42", mode="light")
    built = TextualTheme(**theme.textual_theme())
    assert built.dark is False
    assert Ink(**theme.textual_ink()).accent == "#ff8c42"


# ══════════════════════════════════════════════════════════════════════════════════════
# Presets, resolution and surprise
# ══════════════════════════════════════════════════════════════════════════════════════
def test_every_preset_is_readable_too():
    for key, preset in theme_mod.PRESETS.items():
        tokens = preset.css_variables()
        assert theme_mod.contrast(tokens["--text"], tokens["--bg"]) >= CONTRAST_TARGET, key
        assert theme_mod.contrast(tokens["--accent-text"], tokens["--bg"]) >= CONTRAST_TARGET, key


def test_presets_are_reachable_by_key_label_and_old_palette_name():
    assert theme_mod.preset("house_party") is theme_mod.PRESETS["house_party"]
    assert theme_mod.preset("House Party") is theme_mod.PRESETS["house_party"]
    assert theme_mod.preset("standard") is theme_mod.PRESETS["arc_reactor"]
    assert theme_mod.preset("no such thing") is None


def test_presets_payload_is_json_serialisable_and_keyed():
    payload = theme_mod.presets_payload()
    assert {entry["key"] for entry in payload} == set(theme_mod.PRESETS)
    json.dumps(payload)   # the desktop app sends this straight to the browser


@pytest.mark.parametrize(
    "spec, check",
    [
        ("veronica", lambda t: t.name == "Veronica"),
        ("#ff8c42", lambda t: t.seed == "#ff8c42" and t.name == "Custom"),
        ("ff8c42", lambda t: t.seed == "#ff8c42"),
        ("violet", lambda t: t.seed == "#8b5cf6"),
        ("light", lambda t: t.mode == "light"),
        ("surprise", lambda t: t.name == "Surprise"),
    ],
)
def test_resolve_turns_one_typed_word_into_a_theme(spec, check):
    assert check(theme_mod.resolve(spec, Theme()))


@pytest.mark.parametrize("spec", ["", "   ", "aubergine-flavoured"])
def test_resolve_says_no_rather_than_guessing(spec):
    assert theme_mod.resolve(spec, Theme()) is None


def test_resolve_a_colour_keeps_the_rest_of_the_current_theme():
    base = Theme(seed="#22d3ee", mode="light", tint=0.9, radius=4)
    changed = theme_mod.resolve("#ff0000", base)
    assert (changed.seed, changed.mode, changed.tint, changed.radius) == ("#ff0000", "light", 0.9, 4)


def test_surprise_is_random_but_never_unusable():
    rng = random.Random(0)
    for _ in range(60):
        tokens = theme_mod.surprise(rng).css_variables()
        assert theme_mod.contrast(tokens["--text"], tokens["--bg"]) >= CONTRAST_TARGET
        assert theme_mod.contrast(tokens["--accent-text"], tokens["--bg"]) >= CONTRAST_TARGET


def test_describe_mentions_the_colour_and_the_ground():
    line = theme_mod.describe(Theme(name="Custom", seed="#ff8c42", mode="midnight"))
    assert "#ff8c42" in line and "midnight" in line


# ══════════════════════════════════════════════════════════════════════════════════════
# Persistence
# ══════════════════════════════════════════════════════════════════════════════════════
def test_a_theme_survives_a_round_trip_through_disk(tmp_path):
    path = tmp_path / "theme.json"
    original = Theme(name="Mine", seed="#ff8c42", mode="light", tint=0.31, radius=6, glow=0.2)
    assert theme_mod.save(original, path)
    assert theme_mod.load(path).to_dict() == original.to_dict()


def test_loading_a_missing_or_broken_file_gives_the_default(tmp_path):
    assert theme_mod.load(tmp_path / "absent.json").name == "Arc Reactor"
    broken = tmp_path / "broken.json"
    broken.write_text("{not json at all", encoding="utf-8")
    assert theme_mod.load(broken).name == "Arc Reactor"


def test_a_theme_file_from_a_newer_version_still_loads(tmp_path):
    """Unknown keys are dropped, not fatal."""
    path = tmp_path / "theme.json"
    path.write_text(json.dumps({"seed": "#ff8c42", "invented_later": True}), encoding="utf-8")
    assert theme_mod.load(path).seed == "#ff8c42"


def test_forget_removes_the_file_and_says_whether_it_had_to(tmp_path):
    path = tmp_path / "theme.json"
    theme_mod.save(Theme(), path)
    assert theme_mod.forget(path) is True
    assert theme_mod.forget(path) is False


def test_saving_somewhere_unwritable_reports_failure_rather_than_raising(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("not a directory", encoding="utf-8")
    assert theme_mod.save(Theme(), blocker / "nested" / "theme.json") is False


def test_evolve_leaves_the_original_alone():
    base = Theme(seed="#22d3ee")
    assert base.evolve(seed="#ff0000").seed == "#ff0000"
    assert base.seed == "#22d3ee"
