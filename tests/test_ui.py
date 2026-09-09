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

import sys
import time

import pytest

from jarvis import desktop as desktop_mod
from jarvis import theme as theme_mod

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
def window():
    """One running app and one browser page, shared by every test here."""
    app = desktop_mod.DesktopApp(
        desktop_mod.AttachedBackend(
            on_submit=lambda text: None,
            snapshot=lambda: {"model": "test-model", "tools": ["alpha"], "protocols": []},
        ),
        open_window_on_start=False,
        theme=theme_mod.PRESETS["graphite"],
    )
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


def test_the_page_boots_without_a_single_error(window):
    page, app, errors = window
    assert errors == []
    assert page.evaluate("typeof Shell") == "object"


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


def test_copying_code_does_not_bring_the_line_numbers(window):
    """`user-select: none` does not keep a gutter out of a spanning selection.

    The numbers are drawn with generated content instead, which no selection
    can ever include.
    """
    page, app, errors = window
    page.evaluate("Rail.show('code'); Code.open('jarvis/theme.py')")
    page.wait_for_function("document.querySelectorAll('#code-body tr').length > 50", timeout=15000)
    picked = page.evaluate("""() => {
        const rows = [...document.querySelectorAll('#code-body tr')].slice(20, 30);
        const range = document.createRange();
        range.setStartBefore(rows[0]); range.setEndAfter(rows[rows.length - 1]);
        const sel = window.getSelection();
        sel.removeAllRanges(); sel.addRange(range);
        const text = String(sel); sel.removeAllRanges(); return text;
    }""")
    numbered = [
        line for line in picked.splitlines()
        if line.strip() and line.strip().split()[0].isdigit()
    ]
    assert numbered == [], numbered
    assert page.evaluate("document.querySelectorAll('#code-body tr').length") > 50


def test_the_agent_pane_marks_its_own_tab(window):
    """It used to put its unread pip on the Terminal tab, which is the shell."""
    page, app, hud_errors = window
    page.evaluate("Rail.show('code'); Rail.unmark('agent'); Rail.unmark('terminal')")
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
