"""Browser regression tests for the desktop front end.

Skipped entirely unless Playwright and a Chromium build are both present, so
the ordinary ``pytest -q`` run stays browserless. Install them with::

    pip install playwright && playwright install chromium

Every test here guards a bug that was actually found by driving the interface
rather than by reading it, and each one names the failure it prevents. They are
deliberately few: the exhaustive sweep lives outside the repository, and what is
kept here is the handful of invariants that would silently rot without a guard.
"""

from __future__ import annotations

import shutil
import sys
import time
from pathlib import Path

import pytest

from jarvis import desktop as desktop_mod
from jarvis import theme as theme_mod

PROJECT = Path(__file__).resolve().parent.parent

sync_playwright = pytest.importorskip(
    "playwright.sync_api", reason="playwright is not installed"
).sync_playwright

#: Where Playwright's own download puts Chromium, and where this image keeps it.
_CHROMIUM_HINTS = (
    "/opt/pw-browsers/chromium-1194/chrome-linux/chrome",
    "/opt/pw-browsers/chromium/chrome-linux/chrome",
)


def _chromium_path() -> str | None:
    import os

    for candidate in _CHROMIUM_HINTS:
        if os.path.isfile(candidate):
            return candidate
    return None   # let Playwright find its own


@pytest.fixture(scope="module")
def window(tmp_path_factory):
    """One running app and one browser page, shared by every test here.

    The workspace is a *copy*. These tests press Ctrl+S, and the editor saves
    for real \u2014 that is the point of it \u2014 so pointing the window at the checkout
    would mean the suite edits the source it is testing. It did, once, and left
    a commented-out line in `theme.py`.
    """
    root = tmp_path_factory.mktemp("workspace")
    (root / "jarvis").mkdir()
    for name in ("theme.py", "desktop.py", "shell.py"):
        shutil.copy(PROJECT / "jarvis" / name, root / "jarvis" / name)
    shutil.copy(PROJECT / "main.py", root / "main.py")

    app = desktop_mod.DesktopApp(
        desktop_mod.AttachedBackend(
            on_submit=lambda text: None,
            snapshot=lambda: {"model": "test-model", "tools": ["alpha"], "protocols": []},
        ),
        open_window_on_start=False,
        theme=theme_mod.PRESETS["graphite"],
    )
    app.workspace = desktop_mod.Workspace(root)
    url = app.start()
    playwright = sync_playwright().start()
    try:
        browser = playwright.chromium.launch(executable_path=_chromium_path())
    except Exception as exc:                        # pragma: no cover - no browser here
        playwright.stop()
        app.stop()
        pytest.skip(f"no usable chromium: {exc}")

    page = browser.new_page(viewport={"width": 1600, "height": 950})
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.goto(url, wait_until="networkidle")
    page.wait_for_timeout(600)
    try:
        yield page, app, errors
    finally:
        browser.close()
        playwright.stop()
        app.stop()


def test_these_tests_cannot_edit_the_checkout(window):
    """The editor saves for real, so the window must never point at the source.

    It did, and a Ctrl+S in the shortcut test below left a commented-out line in
    `jarvis/theme.py`. This runs first so a fixture that regresses fails here
    rather than in whatever it happens to overwrite.
    """
    page, app, errors = window
    root = Path(app.workspace.root)
    assert root != PROJECT
    assert PROJECT not in root.parents and root not in PROJECT.parents


def test_the_page_boots_without_a_single_error(window):
    page, app, errors = window
    assert errors == []
    assert page.evaluate("typeof Shell") == "object"
    assert page.evaluate("typeof Lang") == "object"
    assert page.evaluate("typeof Ide") == "object"
    assert page.evaluate("Lang.catalogue().length") >= 30


@pytest.mark.parametrize("width", [1600, 1280, 1180, 1100, 900, 880, 760, 430])
def test_the_conversation_never_collapses(window, width):
    """The grid bug: `position: fixed` on a pane shifted every sibling a column.

    Below 1180px the responsive rules make the sidebar and the rail overlay,
    which takes them out of the grid's flow. Without an explicit ``grid-column``
    on each pane, auto-placement moved the conversation into a zero-width track
    and the middle of the application disappeared.
    """
    page, app, errors = window
    page.set_viewport_size({"width": width, "height": 820})
    page.wait_for_timeout(420)
    measured = page.evaluate(
        "() => document.querySelector('.conversation').getBoundingClientRect().width"
    )
    assert measured > width * 0.4, f"conversation is {measured}px wide at {width}px"
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")


def test_nothing_spills_out_of_the_window(window):
    page, app, errors = window
    page.set_viewport_size({"width": 900, "height": 700})
    page.wait_for_timeout(400)
    spills = page.evaluate("""() => {
      const out = [];
      const excused = (el) => {
        if (el.closest('.ambience, .scrim, .studio, .palette-wrap, .toasts, [hidden]')) return true;
        for (let p = el.parentElement; p && p !== document.body; p = p.parentElement) {
          const s = getComputedStyle(p);
          if (/(auto|scroll)/.test(s.overflowY + s.overflowX)) return true;
          const r = p.getBoundingClientRect();
          if (r.width < 2 || r.height < 2) return true;
        }
        return false;
      };
      document.querySelectorAll('body *').forEach((el) => {
        if (excused(el)) return;
        const cs = getComputedStyle(el);
        if (cs.display === 'none' || cs.visibility === 'hidden') return;
        const r = el.getBoundingClientRect();
        if (r.width === 0 && r.height === 0) return;
        if (r.right > window.innerWidth + 1.5 || r.left < -1.5)
          out.push((el.id || el.className) + ' ' + Math.round(r.left) + '..' + Math.round(r.right));
      });
      return out;
    }""")
    assert spills == []
    page.set_viewport_size({"width": 1600, "height": 950})
    page.wait_for_timeout(300)


def test_a_closed_pane_stays_closed_across_a_reload(window):
    """The persistence bug: a stored `0` was read as "no value stored".

    ``Shell.read`` rejected any value that was not greater than zero, so the
    flag saying "the operator closed this pane" was indistinguishable from the
    key being absent, and every reload reopened it.
    """
    page, app, errors = window
    page.evaluate("Shell.set('sidebar', false); Shell.set('rail', false)")
    page.wait_for_timeout(300)
    page.reload(wait_until="networkidle")
    page.wait_for_timeout(700)
    assert page.evaluate("Shell.isOpen('sidebar')") is False
    assert page.evaluate("Shell.isOpen('rail')") is False

    page.evaluate("Shell.set('sidebar', true); Shell.set('rail', true)")
    page.wait_for_timeout(300)
    page.reload(wait_until="networkidle")
    page.wait_for_timeout(700)
    assert page.evaluate("Shell.isOpen('sidebar')") is True


def test_restoring_a_view_does_not_force_the_rail_open(window):
    """`Rail.init` used to call `Shell.open`, overriding a closed pane."""
    page, app, errors = window
    page.evaluate("Rail.show('logs'); Shell.set('rail', false)")
    page.wait_for_timeout(300)
    page.reload(wait_until="networkidle")
    page.wait_for_timeout(700)
    assert page.evaluate("Rail.view") == "logs"
    assert page.evaluate("Shell.isOpen('rail')") is False
    page.evaluate("Shell.set('rail', true)")
    page.wait_for_timeout(300)


def test_the_two_layers_of_the_editor_stay_in_register(window):
    """The invariant the whole editor rests on.

    A transparent textarea sits exactly on top of a highlighted copy of the same
    text. If the painted layer ever differs from the buffer by so much as one
    character — an invented space on a blank line inside a docstring was the
    first way this broke — every glyph after it slides out from under the caret.
    """
    page, app, errors = window
    page.evaluate("Ide.surface('code'); Ide.open('jarvis/theme.py')")
    page.wait_for_function("document.querySelectorAll('#lin .eline').length > 300", timeout=20000)
    assert _aligned(page) is None

    # And it has to survive an edit that opens a block running past the line.
    page.evaluate("IdeEd.goto(1, 0)")
    page.click("#ed-input")
    page.keyboard.type('"""')
    page.wait_for_timeout(400)
    assert _aligned(page) is None
    page.keyboard.press("Control+z")
    page.wait_for_timeout(400)
    assert _aligned(page) is None
    page.evaluate("Ide.order.slice().forEach((p) => { "
                  "const d = Ide.docs.get(p); d.saved = d.text; Ide.close(p); })")
    page.evaluate("Ide.surface('chat')")
    page.wait_for_timeout(300)


def _aligned(page):
    """``None`` when the painted layer is the buffer, a description when it is not."""
    return page.evaluate(r"""() => {
      const text = document.getElementById('ed-input').value;
      const painted = [...document.querySelectorAll('#lin .eline')]
        .map((n) => n.textContent.replace(/\u00a0/g, '')).join('\n');
      if (painted === text) return null;
      const a = painted.split('\n'), b = text.split('\n');
      if (a.length !== b.length) return 'painted ' + a.length + ', buffer ' + b.length;
      for (let i = 0; i < a.length; i++) if (a[i] !== b[i]) return 'line ' + (i + 1);
      return 'unknown';
    }""")


def test_an_editor_shortcut_does_not_also_fire_the_conversations(window):
    """Ctrl+Shift+K deleted a line *and* opened the command palette over it.

    The palette was bound to Ctrl+K without excluding Shift, and the editor did
    not stop the event propagating to the document. Both halves are fixed; this
    guards both, because either one alone lets it back.
    """
    page, app, errors = window
    page.evaluate("Ide.surface('code'); Ide.open('jarvis/theme.py')")
    page.wait_for_function("document.querySelectorAll('#lin .eline').length > 50", timeout=20000)
    page.click("#ed-input")
    for combination in ("Control+Shift+k", "Control+d", "Control+Slash", "Control+s"):
        page.keyboard.press(combination)
        page.wait_for_timeout(180)
        open_overlays = page.evaluate("""() => ['palette-wrap', 'quick-wrap', 'keys-wrap', 'studio']
            .filter((id) => !document.getElementById(id).hidden)""")
        assert open_overlays == [], f"{combination} opened {open_overlays}"
    page.evaluate("Ide.order.slice().forEach((p) => { "
                  "const d = Ide.docs.get(p); d.saved = d.text; Ide.close(p); })")
    page.evaluate("Ide.surface('chat')")
    page.wait_for_timeout(300)


def test_the_code_surface_keeps_its_editor_at_every_width(window):
    """The same collapse the conversation had, in the surface next door."""
    page, app, errors = window
    page.evaluate("Ide.surface('code')")
    for width in (1600, 1240, 1180, 1000, 900, 700, 430):
        page.set_viewport_size({"width": width, "height": 820})
        page.wait_for_timeout(300)
        measured = page.evaluate(
            "() => document.getElementById('stage').getBoundingClientRect().width")
        assert measured > 150, f"the editor stage is {measured}px at {width}px"
        assert page.evaluate(
            "() => document.getElementById('abar').getBoundingClientRect().width") > 20
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")
    page.set_viewport_size({"width": 1600, "height": 950})
    page.evaluate("Ide.surface('chat')")
    page.wait_for_timeout(400)


def test_the_terminal_is_moved_between_surfaces_not_duplicated(window):
    """Two terminals would be two scrollbacks disagreeing about one shell."""
    page, app, errors = window
    page.evaluate("Ide.surface('code'); Ide.dock.show('terminal')")
    page.wait_for_timeout(400)
    assert page.evaluate("document.querySelectorAll('#shell-host').length") == 1
    assert page.evaluate("document.getElementById('shell-host').parentElement.id") == "dock-terminal"
    page.evaluate("Ide.surface('chat')")
    page.wait_for_timeout(400)
    assert page.evaluate("document.querySelectorAll('#shell-host').length") == 1
    assert page.evaluate("document.getElementById('shell-host').parentElement.id") == "view-terminal"


def test_typing_into_the_page_keeps_the_character(window):
    """Focus used to move to the composer without the key that moved it."""
    page, app, errors = window
    page.evaluate("Ide.surface('chat'); document.getElementById('input').value = ''")
    page.click(".transcript")
    page.keyboard.type("abc")
    page.wait_for_timeout(250)
    assert page.evaluate("document.getElementById('input').value") == "abc"
    page.evaluate("document.getElementById('input').value = ''")


def test_escape_does_not_interrupt_a_running_turn(window):
    """It did, which made a reflex into a way to lose work in progress."""
    page, app, errors = window
    page.evaluate("state.busy = true")
    page.keyboard.press("Escape")
    page.wait_for_timeout(250)
    assert page.evaluate("state.busy") is True
    page.evaluate("state.busy = false")


def test_the_agent_pane_marks_its_own_tab(window):
    """It used to put its unread pip on the Terminal tab, which is the shell."""
    page, app, hud_errors = window
    page.evaluate("Rail.show('logs'); Rail.unmark('agent'); Rail.unmark('terminal')")
    page.wait_for_timeout(250)
    app.hud.log_system("something for the agent pane", "info")
    page.wait_for_timeout(500)
    assert page.evaluate("(v) => !!document.querySelector(`.rail-tab[data-view=\"${v}\"] .pip`)", "agent")
    assert not page.evaluate("(v) => !!document.querySelector(`.rail-tab[data-view=\"${v}\"] .pip`)", "terminal")


def test_clearing_the_transcript_brings_the_welcome_back(window):
    """It used to leave an empty grey pane for the rest of the session."""
    page, app, errors = window
    app.hud.log_user("something to clear away")
    page.wait_for_timeout(400)
    app.hud.clear_transcript()
    page.wait_for_timeout(500)
    assert page.evaluate("document.querySelectorAll('.msg').length") == 0
    assert page.evaluate("!!document.getElementById('welcome')")
    assert "J.A.R.V.I.S." in page.evaluate("document.getElementById('transcript').textContent")


def test_the_shell_runs_a_command_from_the_pane(window):
    page, app, errors = window
    page.evaluate("Shell.set('rail', true); Rail.show('terminal')")
    page.wait_for_timeout(400)
    page.fill("#shell-input", "echo regression-probe")
    page.press("#shell-input", "Enter")
    page.wait_for_function(
        "document.getElementById('shell-out').textContent.split('regression-probe').length > 2",
        timeout=15000,
    )
    assert page.evaluate("$('shell-input').value") == ""


def test_no_errors_accumulated_over_the_whole_run(window):
    page, app, errors = window
    assert errors == [], errors
