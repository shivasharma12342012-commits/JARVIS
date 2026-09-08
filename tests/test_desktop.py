"""The desktop front end: the HUD adapter, the event stream and the server.

Nothing here needs a browser, an Ollama daemon or a window. The app is driven
over its own HTTP API with :mod:`urllib`, which is exactly what the front end
does, so a passing suite means the front end has something real to talk to.

The security tests are not optional colour: this server can run shell commands
on the operator's machine, so "a page on another origin cannot reach it" is a
correctness property, not a nicety.
"""

from __future__ import annotations

import json
import queue
import threading
import time
import urllib.error
import urllib.request

import pytest

from jarvis import desktop as desktop_mod
from jarvis import theme as theme_mod
from jarvis.desktop import AttachedBackend, DesktopApp, DesktopHUD, EventHub


# ══════════════════════════════════════════════════════════════════════════════════════
# Fixtures
# ══════════════════════════════════════════════════════════════════════════════════════
class Recorder:
    """A stand-in terminal HUD, to prove the mirror actually mirrors."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple]] = []

    def __getattr__(self, name: str):
        def record(*args, **kwargs):
            self.calls.append((name, args))
        return record

    def names(self) -> list[str]:
        return []


@pytest.fixture
def app():
    """A running desktop app with no window and a backend that only records."""
    submitted: list[str] = []
    interrupts: list[int] = []
    application = DesktopApp(
        AttachedBackend(
            on_submit=submitted.append,
            on_interrupt=lambda: interrupts.append(1),
            snapshot=lambda: {"model": "fake-model", "tools": ["alpha", "beta"]},
        ),
        open_window_on_start=False,
        theme=theme_mod.PRESETS["arc_reactor"],
    )
    application.start()
    application.submitted = submitted      # type: ignore[attr-defined]
    application.interrupts = interrupts    # type: ignore[attr-defined]
    try:
        yield application
    finally:
        application.stop()


def _request(app, path, body=None, token=None, headers=None, timeout=5.0):
    base = app.url.split("?")[0].rstrip("/")
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(f"{base}{path}", data=data,
                                     method="GET" if data is None else "POST")
    request.add_header("Content-Type", "application/json")
    request.add_header("X-Jarvis-Token", app.token if token is None else token)
    for name, value in (headers or {}).items():
        request.add_header(name, value)
    return urllib.request.urlopen(request, timeout=timeout)


def get_json(app, path, **kwargs):
    return json.load(_request(app, path, **kwargs))


def post_json(app, path, body, **kwargs):
    return json.load(_request(app, path, body, **kwargs))


def read_events(app, seconds=1.0, until=None):
    """Open an event stream and collect frames for a moment."""
    frames: list[str] = []

    def pump():
        try:
            stream = _request(app, "/api/events", timeout=seconds + 2)
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                line = stream.readline()
                if not line:
                    break
                frames.append(line.decode("utf-8"))
                if until and until in "".join(frames):
                    break
        except Exception:
            pass

    worker = threading.Thread(target=pump, daemon=True)
    worker.start()
    time.sleep(0.35)          # let the subscription land before the caller acts
    return frames, worker


# ══════════════════════════════════════════════════════════════════════════════════════
# The event hub
# ══════════════════════════════════════════════════════════════════════════════════════
def test_every_subscriber_gets_every_event():
    hub = EventHub()
    first, second = hub.subscribe(), hub.subscribe()
    hub.publish("system", text="hello")
    assert first.get_nowait().data["text"] == "hello"
    assert second.get_nowait().data["text"] == "hello"


def test_events_carry_a_monotonic_sequence_number():
    hub = EventHub()
    sink = hub.subscribe()
    hub.publish("a")
    hub.publish("b")
    assert [sink.get_nowait().seq for _ in range(2)] == [1, 2]


def test_a_slow_client_loses_history_rather_than_blocking_the_agent():
    """The agent must never be held up by a browser tab nobody is looking at."""
    hub = EventHub()
    sink = hub.subscribe()
    for index in range(desktop_mod.CLIENT_BACKLOG + 40):
        hub.publish("system", text=str(index))   # would deadlock if this blocked
    assert sink.qsize() <= desktop_mod.CLIENT_BACKLOG


def test_the_client_limit_is_enforced():
    hub = EventHub()
    subscriptions = [hub.subscribe() for _ in range(desktop_mod.MAX_CLIENTS)]
    assert all(s is not None for s in subscriptions)
    assert hub.subscribe() is None


def test_history_replays_facts_but_not_instants():
    hub = EventHub()
    hub.publish("agent", text="a fact")
    hub.publish("token", text="an instant")
    hub.publish("telemetry", cpu=1.0)
    kinds = [event.kind for event in hub.history()]
    assert kinds == ["agent"]


def test_unsubscribed_clients_stop_receiving():
    hub = EventHub()
    sink = hub.subscribe()
    hub.unsubscribe(sink)
    hub.publish("system", text="after")
    assert sink.empty()
    assert hub.client_count == 0


def test_an_event_encodes_as_a_server_sent_events_frame():
    hub = EventHub()
    sink = hub.subscribe()
    hub.publish("system", text="hello", level="info")
    frame = sink.get_nowait().encode().decode("utf-8")
    assert frame.startswith("id: 1\nevent: system\ndata: ")
    assert frame.endswith("\n\n")
    assert json.loads(frame.split("data: ", 1)[1])["text"] == "hello"


# ══════════════════════════════════════════════════════════════════════════════════════
# The HUD adapter
# ══════════════════════════════════════════════════════════════════════════════════════
def test_the_hud_speaks_the_protocol_the_agent_expects():
    """core.py calls these by name on whatever it was handed; all must exist."""
    hud = DesktopHUD(EventHub())
    for name in (
        "log_user", "log_agent", "log_system", "log_interim", "log_thought",
        "log_tool_start", "log_tool", "push_alert", "render_table", "render_code",
        "clear_transcript", "stream_begin", "stream_token", "stream_end",
        "set_state", "set_amplitude", "set_telemetry", "set_protocol",
        "set_voice_status", "set_model_status", "set_metrics", "set_warm",
        "set_palette", "print_banner", "start", "stop",
        "ask_permission", "confirm", "choose", "prompt_input",
    ):
        assert callable(getattr(hud, name)), name


def test_a_streamed_turn_becomes_the_expected_events():
    hub = EventHub()
    hud = DesktopHUD(hub)
    sink = hub.subscribe()
    hud.stream_begin()
    hud.stream_token("Good ")
    hud.stream_token("evening.")
    hud.stream_end("Good evening.")
    kinds = [sink.get_nowait().kind for _ in range(4)]
    assert kinds == ["stream_begin", "token", "token", "stream_end"]


def test_the_mirror_receives_everything_the_window_does():
    mirror = Recorder()
    hud = DesktopHUD(EventHub(), mirror=mirror)
    hud.log_agent("hello")
    hud.set_state("thinking")
    assert ("log_agent", ("hello", True)) in mirror.calls
    assert ("set_state", ("thinking", "")) in mirror.calls


def test_a_broken_mirror_does_not_break_the_window():
    class Hostile:
        def log_agent(self, *args, **kwargs):
            raise RuntimeError("the terminal fell over")

    hub = EventHub()
    hud = DesktopHUD(hub, mirror=Hostile())
    sink = hub.subscribe()
    hud.log_agent("still delivered")
    assert sink.get_nowait().data["text"] == "still delivered"


def test_telemetry_is_flattened_to_what_the_gauges_draw():
    class Snapshot:
        cpu_percent = 34.2
        cpu_per_core = [10.0, 20.0]
        ram_percent = 61.4
        ram_used_gb = 9.8
        ram_total_gb = 16.0
        battery_percent = 78.0
        battery_plugged = True
        process_count = 412
        uptime_seconds = 3600.0
        platform = "macOS"
        disks = [type("Disk", (), {"percent": 47.5})()]

    hub = EventHub()
    sink = hub.subscribe()
    DesktopHUD(hub).set_telemetry(Snapshot())
    payload = sink.get_nowait().data
    assert (payload["cpu"], payload["ram"], payload["disk"]) == (34.2, 61.4, 47.5)
    assert payload["cores"] == [10.0, 20.0]
    json.dumps(payload)


def test_telemetry_missing_a_reading_becomes_none_not_a_crash():
    hub = EventHub()
    sink = hub.subscribe()
    DesktopHUD(hub).set_telemetry(object())
    assert sink.get_nowait().data["battery"] is None


def test_a_question_parks_its_thread_until_the_window_answers():
    hub = EventHub()
    hud = DesktopHUD(hub)
    sink = hub.subscribe()
    hub.subscribe()   # client_count must be non-zero or the HUD refuses instead
    answers: list[bool] = []
    threading.Thread(target=lambda: answers.append(hud.confirm("Shall I?")), daemon=True).start()

    event = None
    for _ in range(40):
        try:
            candidate = sink.get(timeout=0.2)
        except queue.Empty:
            continue
        if candidate.kind == "ask":
            event = candidate
            break
    assert event is not None, "no question reached the window"

    assert hud.resolve(event.data["token"], "y") is True
    for _ in range(40):
        if answers:
            break
        time.sleep(0.05)
    assert answers == [True]


def test_a_question_with_no_window_open_fails_closed():
    """Nobody to ask means no permission -- the safe default for a shell command."""
    hud = DesktopHUD(EventHub())
    assert hud.ask_permission(object()) == "n"
    assert hud.confirm("Delete everything?") is False


def test_a_question_with_no_window_falls_back_to_the_terminal():
    class Terminal:
        def ask_permission(self, request):
            return "a"

    assert DesktopHUD(EventHub(), mirror=Terminal()).ask_permission(object()) == "a"


def test_prompt_input_belongs_to_the_terminal_when_there_is_one():
    """Two front ends must not both be reading lines into one queue."""
    class Terminal:
        def prompt_input(self, text=""):
            return "typed in the terminal"

    hud = DesktopHUD(EventHub(), mirror=Terminal())
    assert hud.prompt_input("> ") == "typed in the terminal"


def test_resolving_an_unknown_token_is_harmless():
    assert DesktopHUD(EventHub()).resolve("not-a-token", "y") is False


def test_cancel_pending_releases_every_parked_thread():
    hud = DesktopHUD(EventHub())
    hud.hub.subscribe()
    done = threading.Event()

    def ask():
        hud.confirm("waiting")
        done.set()

    threading.Thread(target=ask, daemon=True).start()
    time.sleep(0.3)
    hud.cancel_pending()
    assert done.wait(3.0), "a parked thread was left waiting on shutdown"


def test_a_protocol_palette_change_recolours_the_window():
    hub = EventHub()
    sink = hub.subscribe()
    DesktopHUD(hub).set_palette("veronica")
    event = sink.get_nowait()
    assert event.kind == "theme" and event.data["theme"]["name"] == "Veronica"


# ══════════════════════════════════════════════════════════════════════════════════════
# The server
# ══════════════════════════════════════════════════════════════════════════════════════
def test_the_page_arrives_with_the_theme_and_the_token_already_in_it(app):
    html = _request(app, "/").read().decode("utf-8")
    assert "__JARVIS_THEME_CSS__" not in html and "--accent: #22d3ee;" in html
    assert "__JARVIS_BOOT__" not in html and '"presets"' in html
    # Subresources cannot send a header, so their URLs must carry the token.
    assert f"/static/app.css?token={app.token}" in html
    assert f"/static/app.js?token={app.token}" in html


def test_the_front_end_files_are_all_served(app):
    assert b"J.A.R.V.I.S. Desktop" in _request(app, "/static/app.css").read()
    assert b"const Studio" in _request(app, "/static/app.js").read()


def test_the_boot_state_describes_the_session(app):
    state = get_json(app, "/api/state")
    assert state["model"] == "fake-model" and state["tools"] == ["alpha", "beta"]
    assert state["theme"]["seed"] == "#22d3ee"
    assert state["variables"]["--accent"] == "#22d3ee"
    assert len(state["presets"]) == len(theme_mod.PRESETS)
    assert state["modes"] == list(theme_mod.MODES)


@pytest.mark.parametrize("token", ["", "wrong-token"])
def test_a_request_without_the_session_token_is_refused(app, token):
    with pytest.raises(urllib.error.HTTPError) as caught:
        get_json(app, "/api/state", token=token)
    assert caught.value.code == 403


def test_a_request_claiming_another_host_is_refused(app):
    """Closes DNS rebinding: a hostname an attacker owns, pointed at 127.0.0.1."""
    with pytest.raises(urllib.error.HTTPError) as caught:
        get_json(app, "/api/state", headers={"Host": "attacker.example.com"})
    assert caught.value.code == 403


def test_a_cross_origin_request_is_refused(app):
    with pytest.raises(urllib.error.HTTPError) as caught:
        get_json(app, "/api/state", headers={"Origin": "https://attacker.example.com"})
    assert caught.value.code == 403


@pytest.mark.parametrize("path", ["/static/../config.py", "/static/../../etc/passwd"])
def test_static_serving_cannot_escape_the_web_directory(app, path):
    with pytest.raises(urllib.error.HTTPError) as caught:
        _request(app, path)
    assert caught.value.code == 404


def test_an_unknown_route_is_a_clean_404(app):
    with pytest.raises(urllib.error.HTTPError) as caught:
        get_json(app, "/api/nonsense")
    assert caught.value.code == 404


def test_a_typed_line_reaches_the_backend(app):
    _request(app, "/api/chat", {"text": "hello there"})
    assert app.submitted == ["hello there"]


def test_an_empty_line_is_rejected_rather_than_queued(app):
    with pytest.raises(urllib.error.HTTPError) as caught:
        _request(app, "/api/chat", {"text": "   "})
    assert caught.value.code == 400
    assert app.submitted == []


def test_interrupt_reaches_the_backend(app):
    post_json(app, "/api/interrupt", {})
    assert app.interrupts == [1]


def test_the_event_stream_delivers_what_the_hud_publishes(app):
    frames, worker = read_events(app, seconds=1.2, until='"kind": "stream_end"')
    app.hud.log_user("a question")
    app.hud.stream_begin()
    app.hud.stream_token("an ")
    app.hud.stream_token("answer")
    app.hud.stream_end("an answer")
    worker.join(timeout=3)
    blob = "".join(frames)
    assert '"kind": "user"' in blob
    assert '"text": "an "' in blob
    assert '"kind": "stream_end"' in blob


def test_a_window_that_opens_late_is_given_the_transcript(app):
    app.hud.log_agent("said before the window opened")
    frames, worker = read_events(app, seconds=0.8, until=": ready")
    worker.join(timeout=3)
    assert "said before the window opened" in "".join(frames)


# ══════════════════════════════════════════════════════════════════════════════════════
# Colour, over the wire
# ══════════════════════════════════════════════════════════════════════════════════════
def test_a_chosen_colour_is_adopted_and_derived(app):
    result = post_json(app, "/api/theme", {"seed": "#ff2d55", "persist": False})
    assert result["ok"] and result["theme"]["seed"] == "#ff2d55"
    assert result["theme"]["name"] == "Custom"        # no longer any named preset
    assert result["variables"]["--accent"] == "#ff2d55"
    assert app.theme.seed == "#ff2d55"


def test_a_preset_can_be_chosen_by_key(app):
    assert post_json(app, "/api/theme", {"preset": "matrix", "persist": False})["theme"]["name"] == "Matrix"


def test_surprise_returns_something_different_and_usable(app):
    result = post_json(app, "/api/theme", {"surprise": True, "persist": False})
    assert result["theme"]["name"] == "Surprise"
    assert theme_mod.contrast(result["variables"]["--text"], result["variables"]["--bg"]) >= 4.5


def test_the_feel_knobs_come_through_individually(app):
    result = post_json(
        app, "/api/theme",
        {"mode": "light", "tint": 0.2, "glow": 0.1, "radius": 4, "font": "serif", "persist": False},
    )["theme"]
    assert (result["mode"], result["tint"], result["radius"], result["font"]) == ("light", 0.2, 4, "serif")


def test_a_whole_theme_can_be_sent_at_once(app):
    payload = theme_mod.Theme(name="Mine", seed="#00ff88", mode="midnight").to_dict()
    assert post_json(app, "/api/theme", {"theme": payload, "persist": False})["theme"]["name"] == "Mine"


def test_an_empty_theme_change_is_rejected(app):
    assert post_json(app, "/api/theme", {"persist": False})["ok"] is False


def test_a_colour_change_is_announced_to_every_open_window(app):
    frames, worker = read_events(app, seconds=1.0, until='"kind": "theme"')
    post_json(app, "/api/theme", {"seed": "#00ff88", "persist": False})
    worker.join(timeout=3)
    assert '"--accent": "#00ff88"' in "".join(frames)


def test_a_listener_is_told_when_the_colours_change(app):
    """This is how an attached terminal session follows the window's theme."""
    seen: list[str] = []
    app.on_theme_change = lambda theme: seen.append(theme.seed)
    post_json(app, "/api/theme", {"seed": "#123456", "persist": False})
    assert seen == ["#123456"]


def test_a_theme_is_written_to_disk_when_asked(app, tmp_path, monkeypatch):
    path = tmp_path / "theme.json"
    monkeypatch.setattr(theme_mod, "THEME_PATH", path)
    assert post_json(app, "/api/theme", {"seed": "#abcdef"})["persisted"] is True
    assert json.loads(path.read_text(encoding="utf-8"))["seed"] == "#abcdef"


def test_a_question_can_be_answered_over_the_api(app):
    frames, worker = read_events(app, seconds=1.5, until='"kind": "ask"')
    answers: list[bool] = []
    threading.Thread(target=lambda: answers.append(app.hud.confirm("Shall I?")), daemon=True).start()
    worker.join(timeout=3)

    tokens = [
        json.loads(line[len("data: "):])["token"]
        for line in "".join(frames).splitlines()
        if line.startswith("data: ") and '"kind": "ask"' in line
    ]
    assert tokens, "the question never reached the window"
    assert post_json(app, "/api/answer", {"token": tokens[-1], "answer": "y"})["ok"] is True
    for _ in range(40):
        if answers:
            break
        time.sleep(0.05)
    assert answers == [True]


# ══════════════════════════════════════════════════════════════════════════════════════
# Lifecycle
# ══════════════════════════════════════════════════════════════════════════════════════
def test_the_url_carries_the_token(app):
    assert app.url.startswith("http://127.0.0.1:") and f"token={app.token}" in app.url


def test_the_server_binds_only_to_loopback(app):
    assert app.server.server_address[0] == "127.0.0.1"


def test_stopping_twice_is_harmless(app):
    app.stop()
    app.stop()
    assert app.server is None


def test_availability_reports_a_missing_front_end(monkeypatch, tmp_path):
    monkeypatch.setattr(desktop_mod, "WEB_ROOT", tmp_path)
    ok, why = desktop_mod.available()
    assert not ok and "index.html" in why


def test_availability_passes_with_the_real_front_end():
    ok, why = desktop_mod.available()
    assert ok and why == ""


def test_no_window_is_opened_when_the_environment_forbids_it(monkeypatch):
    monkeypatch.setenv("JARVIS_DESKTOP_NO_WINDOW", "1")
    assert desktop_mod.open_window("http://127.0.0.1:1/") == "none"
