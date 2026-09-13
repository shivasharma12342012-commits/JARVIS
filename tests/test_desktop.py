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
import urllib.parse
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
    assert "__JARVIS_THEME_CSS__" not in html
    assert "--accent: " + theme_mod.PRESETS["arc_reactor"].seed + ";" in html
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
    assert state["theme"]["seed"] == theme_mod.PRESETS["arc_reactor"].seed
    assert state["variables"]["--accent"] == theme_mod.PRESETS["arc_reactor"].seed
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


# ══════════════════════════════════════════════════════════════════════════════════════
# The workspace: what the file browser and the code viewer may see
#
# Which is the workspace, and nothing outside it. These are the tests that stop
# the code section becoming a way to read ~/.ssh/id_rsa over a local port.
# ══════════════════════════════════════════════════════════════════════════════════════
@pytest.fixture
def workspace(tmp_path):
    """A small tree with something to find, and something to try to escape to."""
    (tmp_path / "secret-outside.txt").write_text("private", encoding="utf-8")
    root = tmp_path / "work"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "mod.py").write_text("x = 1\ny = 2\n", encoding="utf-8")
    (root / "readme.md").write_text("# hello\n", encoding="utf-8")
    (root / "script").write_text("#!/usr/bin/env bash\necho hi\n", encoding="utf-8")
    (root / "picture.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00binary")
    (root / "__pycache__").mkdir()
    (root / "__pycache__" / "junk.pyc").write_bytes(b"noise")
    (root / ".git").mkdir()
    return desktop_mod.Workspace(root)


def test_the_workspace_lists_a_directory(workspace):
    listing = workspace.listing("")
    names = [entry["name"] for entry in listing["entries"]]
    assert "pkg" in names and "readme.md" in names
    assert listing["atRoot"] is True


def test_directories_come_before_files(workspace):
    kinds = [entry["kind"] for entry in workspace.listing("")["entries"]]
    assert kinds == sorted(kinds, key=lambda k: k != "dir")


def test_noise_directories_are_not_shown(workspace):
    names = [entry["name"] for entry in workspace.listing("")["entries"]]
    assert "__pycache__" not in names and ".git" not in names


def test_a_nested_directory_reports_its_own_path(workspace):
    listing = workspace.listing("pkg")
    assert listing["path"] == "pkg" and listing["parent"] == "" and listing["atRoot"] is False
    assert [entry["name"] for entry in listing["entries"]] == ["mod.py"]


def test_a_file_comes_back_with_its_language_and_line_count(workspace):
    found = workspace.read("pkg/mod.py")
    assert found["content"] == "x = 1\ny = 2\n"
    assert found["language"] == "python" and found["lines"] == 3
    assert found["binary"] is False and found["truncated"] is False


def test_an_extensionless_script_is_read_from_its_shebang(workspace):
    """`jarvis-desktop` is one of these; plain text would be the wrong answer."""
    assert workspace.read("script")["language"] == "bash"


def test_a_binary_file_is_reported_rather_than_decoded(workspace):
    found = workspace.read("picture.png")
    assert found["binary"] is True and found["content"] == ""


def test_a_file_longer_than_the_cap_is_truncated_and_says_so(workspace, monkeypatch):
    monkeypatch.setattr(desktop_mod, "MAX_FILE_BYTES", 40)
    (workspace.root / "long.py").write_text("# " + "x" * 500, encoding="utf-8")
    found = workspace.read("long.py")
    assert found["truncated"] is True and len(found["content"]) <= 40


@pytest.mark.parametrize(
    "escape",
    [
        "../secret-outside.txt",
        "../../etc/passwd",
        "pkg/../../secret-outside.txt",
        "pkg/../..",
        "..",
    ],
)
def test_the_workspace_cannot_be_escaped(workspace, escape):
    assert workspace.resolve(escape) is None
    assert "error" in workspace.read(escape)
    assert "error" in workspace.listing(escape)


def test_an_absolute_path_is_read_as_workspace_relative(workspace):
    """Not an escape: a leading slash means the workspace root, not the disk root."""
    resolved = workspace.resolve("/pkg/mod.py")
    assert resolved is not None and resolved == workspace.root / "pkg" / "mod.py"


def test_a_symlink_pointing_out_of_the_tree_is_refused(workspace, tmp_path):
    link = workspace.root / "escape-hatch"
    try:
        link.symlink_to(tmp_path / "secret-outside.txt")
    except (OSError, NotImplementedError):
        pytest.skip("this platform will not make symlinks")
    assert workspace.resolve("escape-hatch") is None
    assert "error" in workspace.read("escape-hatch")


def test_reading_something_that_is_not_there(workspace):
    assert "error" in workspace.read("nope.py")
    assert "error" in workspace.listing("nope")
    assert "error" in workspace.read("pkg")          # a directory is not a file
    assert "error" in workspace.listing("readme.md")  # nor a file a directory


def test_the_listing_is_capped(workspace, monkeypatch):
    monkeypatch.setattr(desktop_mod, "MAX_DIR_ENTRIES", 3)
    for i in range(10):
        (workspace.root / f"file{i}.txt").write_text("x", encoding="utf-8")
    listing = workspace.listing("")
    assert listing["truncated"] is True
    assert len(listing["entries"]) <= 3


# -- over the wire ---------------------------------------------------------------------
def test_the_file_routes_serve_the_workspace(app, tmp_path):
    (tmp_path / "hello.py").write_text("print('hi')\n", encoding="utf-8")
    app.workspace = desktop_mod.Workspace(tmp_path)

    listing = get_json(app, "/api/files?path=")
    assert [entry["name"] for entry in listing["entries"]] == ["hello.py"]

    found = get_json(app, "/api/file?path=hello.py")
    assert found["content"] == "print('hi')\n" and found["language"] == "python"


def test_the_file_routes_refuse_to_escape(app, tmp_path):
    (tmp_path / "outside.txt").write_text("private", encoding="utf-8")
    inner = tmp_path / "inner"
    inner.mkdir()
    app.workspace = desktop_mod.Workspace(inner)

    assert "error" in get_json(app, "/api/file?path=../outside.txt")
    assert "error" in get_json(app, "/api/file?path=" + urllib.parse.quote("../../etc/passwd"))
    assert "error" in get_json(app, "/api/files?path=..")


def test_the_file_routes_need_the_session_token(app):
    for route in ("/api/files?path=", "/api/file?path=x"):
        with pytest.raises(urllib.error.HTTPError) as caught:
            get_json(app, route, token="wrong")
        assert caught.value.code == 403


def test_the_boot_state_names_the_workspace(app):
    state = get_json(app, "/api/state")
    assert state["workspace"] and state["workspaceName"]


# ══════════════════════════════════════════════════════════════════════════════════════
# The Terminal pane's shell, over the wire
# ══════════════════════════════════════════════════════════════════════════════════════
def _settle(predicate, timeout=6.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_the_boot_state_describes_the_shell(app):
    described = get_json(app, "/api/state")["shell"]
    assert described["available"] is True
    assert described["name"] and described["prompt"] and described["cwd"]


def test_the_shell_is_not_started_until_it_is_used(app):
    """A window that never opens the pane never spawns a process."""
    assert app._shell is None
    assert get_json(app, "/api/state")["shell"]["running"] is False


def test_a_line_runs_in_the_shell_and_streams_back(app):
    # Waited on the echoed text, not on shell_done: start() fires a priming
    # completion of its own, and stopping at that one reads nothing.
    frames, worker = read_events(app, seconds=4.0, until="streamed-to-the-window")
    post_json(app, "/api/shell", {"input": "echo streamed-to-the-window"})
    worker.join(timeout=6)
    blob = "".join(frames)
    assert "streamed-to-the-window" in blob
    assert '"kind": "shell_done"' in blob


def test_the_working_directory_comes_back_with_each_completion(app):
    frames, worker = read_events(app, seconds=3.0)   # no early break: collect them all
    post_json(app, "/api/shell", {"input": "cd .."})
    worker.join(timeout=6)
    payloads = [
        json.loads(line[len("data: "):])
        for line in "".join(frames).splitlines()
        if line.startswith("data: ") and '"kind": "shell_done"' in line
    ]
    # The priming completion, then the cd — and the cd moved us.
    assert len(payloads) >= 2
    assert payloads[-1]["cwd"] and payloads[-1]["cwd"] != payloads[0]["cwd"]


def test_the_shell_route_needs_the_session_token(app):
    with pytest.raises(urllib.error.HTTPError) as caught:
        post_json(app, "/api/shell", {"input": "echo nope"}, token="wrong")
    assert caught.value.code == 403


def test_the_shell_route_rejects_a_forged_host(app):
    with pytest.raises(urllib.error.HTTPError) as caught:
        post_json(app, "/api/shell", {"input": "echo nope"},
                  headers={"Host": "attacker.example.com"})
    assert caught.value.code == 403


def test_a_shell_request_with_nothing_to_run_is_refused(app):
    assert post_json(app, "/api/shell", {})["ok"] is False


def test_interrupt_and_restart_are_reachable(app):
    post_json(app, "/api/shell", {"input": "echo warm"})
    assert _settle(lambda: app._shell is not None)
    assert "ok" in post_json(app, "/api/shell", {"interrupt": True})
    restarted = post_json(app, "/api/shell", {"restart": True})
    assert restarted["ok"] is True and restarted["running"] is True


def test_the_pane_can_be_switched_off_entirely(app, monkeypatch):
    """DESKTOP_SHELL_ENABLED=false removes the pane and refuses the route."""
    from config import settings

    monkeypatch.setattr(settings, "DESKTOP_SHELL_ENABLED", False)
    assert app.shell() is None
    described = get_json(app, "/api/state")["shell"]
    assert described["available"] is False and described["disabled"] is True
    refused = post_json(app, "/api/shell", {"input": "echo nope"})
    assert refused["ok"] is False and "switched off" in refused["error"]


def test_shell_traffic_is_not_replayed_to_a_new_window(app):
    """A thousand lines of scrollback into a reloaded tab helps nobody."""
    assert "shell_out" in desktop_mod._EPHEMERAL_EVENTS
    assert "shell_done" in desktop_mod._EPHEMERAL_EVENTS
    assert "shell_exit" in desktop_mod._EPHEMERAL_EVENTS


def test_stopping_the_app_stops_the_shell(app):
    post_json(app, "/api/shell", {"input": "echo warm"})
    assert _settle(lambda: app._shell is not None and app._shell.running)
    session = app._shell
    app.stop()
    assert _settle(lambda: not session.running)


# ══ saving, searching and indexing ═══════════════════════════════════════════════════
# The editor needs three things the viewer never did: to write a file back, to
# find text across the tree, and to be told every path once so it can offer them
# by name. All three go through the same resolve-then-compare boundary as the
# reader, and these hold them to it.
def test_a_file_can_be_written_back(workspace):
    result = workspace.write("pkg/mod.py", "x = 9\n")
    assert result["ok"] is True and result["lines"] == 2
    assert workspace.read("pkg/mod.py")["content"] == "x = 9\n"


def test_a_write_creates_the_directories_it_needs(workspace):
    assert workspace.write("deep/er/still.py", "pass\n")["ok"] is True
    assert workspace.read("deep/er/still.py")["content"] == "pass\n"


def test_a_write_leaves_no_temporary_file_behind(workspace):
    workspace.write("pkg/mod.py", "x = 1\n")
    leftovers = [p.name for p in workspace.root.rglob("*.jarvis-save")]
    assert leftovers == []


@pytest.mark.parametrize("escape", ["../outside.py", "pkg/../../away.py", "../../../tmp/away"])
def test_a_write_cannot_escape_the_workspace(workspace, escape):
    result = workspace.write(escape, "owned")
    assert result["ok"] is False and "outside the workspace" in result["error"]


def test_an_absolute_path_is_written_inside_the_workspace(workspace):
    """The same rule the reader uses, for the same reason: one boundary, decided
    in one place. A path that looks absolute is taken as workspace-relative
    rather than refused, so `/etc/passwd` lands at `<workspace>/etc/passwd` and
    touches nothing outside the tree."""
    assert workspace.write("/etc/passwd", "harmless\n")["ok"] is True
    assert (workspace.root / "etc" / "passwd").read_text() == "harmless\n"


def test_a_write_over_a_changed_file_is_refused_rather_than_silent(workspace):
    """An agent turn rewriting the file you have open is the ordinary case."""
    opened = workspace.read("pkg/mod.py")
    (workspace.root / "pkg" / "mod.py").write_text("someone else got here\n", encoding="utf-8")
    refused = workspace.write("pkg/mod.py", "mine\n", expect=opened["digest"])
    assert refused["ok"] is False and refused["stale"] is True
    assert (workspace.root / "pkg" / "mod.py").read_text() == "someone else got here\n"
    # Without the expectation it is an ordinary overwrite, which is what the
    # editor asks for once the operator has been told and said yes.
    assert workspace.write("pkg/mod.py", "mine\n")["ok"] is True


def test_a_write_larger_than_the_cap_is_refused(workspace, monkeypatch):
    from config import settings

    monkeypatch.setattr(settings, "DESKTOP_EDIT_MAX_BYTES", 16)
    result = workspace.write("pkg/mod.py", "x" * 64)
    assert result["ok"] is False and "limit" in result["error"]


def test_saving_can_be_switched_off_entirely(workspace, monkeypatch):
    from config import settings

    monkeypatch.setattr(settings, "DESKTOP_EDIT_ENABLED", False)
    assert workspace.write("pkg/mod.py", "nope")["ok"] is False
    assert workspace.read("pkg/mod.py")["editable"] is False


def test_a_truncated_file_is_not_offered_as_editable(workspace, monkeypatch):
    monkeypatch.setattr(desktop_mod, "MAX_FILE_BYTES", 4)
    assert workspace.read("pkg/mod.py")["editable"] is False


def test_the_search_finds_text_and_says_where(workspace):
    found = workspace.search("y = 2")
    assert found["hits"] == 1
    assert found["files"][0]["path"] == "pkg/mod.py"
    assert found["files"][0]["matches"][0]["line"] == 2


def test_the_search_is_case_insensitive_unless_asked(workspace):
    assert workspace.search("HELLO")["hits"] == 1
    assert workspace.search("HELLO", case=True)["hits"] == 0


def test_a_one_character_search_is_refused(workspace):
    assert workspace.search("y")["error"]


def test_the_search_does_not_read_binary_files(workspace):
    """A PNG full of the needle by accident is not a search result."""
    (workspace.root / "blob.py").write_bytes(b"needle\x00\x00needle")
    assert workspace.search("needle")["hits"] == 0


def test_the_index_lists_every_text_file_once(workspace):
    paths = workspace.index()["paths"]
    assert "pkg/mod.py" in paths and "readme.md" in paths
    assert len(paths) == len(set(paths))
    assert not any(p.startswith("__pycache__") or p.startswith(".git") for p in paths)


def test_the_index_is_capped(workspace):
    for i in range(20):
        (workspace.root / f"n{i}.py").write_text("x\n", encoding="utf-8")
    capped = workspace.index(limit=5)
    assert len(capped["paths"]) == 5 and capped["truncated"] is True


def test_the_new_routes_serve_the_workspace(app, tmp_path):
    (tmp_path / "hello.py").write_text("print('hi')\n", encoding="utf-8")
    app.workspace = desktop_mod.Workspace(tmp_path)

    assert "hello.py" in get_json(app, "/api/index")["paths"]
    assert get_json(app, "/api/search?q=print")["hits"] == 1
    saved = post_json(app, "/api/save", {"path": "hello.py", "content": "print('bye')\n"})
    assert saved["ok"] is True
    assert (tmp_path / "hello.py").read_text() == "print('bye')\n"


def test_the_save_route_refuses_to_escape(app, tmp_path):
    app.workspace = desktop_mod.Workspace(tmp_path)
    refused = post_json(app, "/api/save", {"path": "../owned.txt", "content": "x"})
    assert refused["ok"] is False
    assert not (tmp_path.parent / "owned.txt").exists()


def test_the_new_routes_need_the_session_token(app):
    for route in ("/api/index", "/api/search?q=print"):
        with pytest.raises(urllib.error.HTTPError) as caught:
            get_json(app, route, token="wrong")
        assert caught.value.code == 403
    with pytest.raises(urllib.error.HTTPError) as caught:
        post_json(app, "/api/save", {"path": "x", "content": "y"}, token="wrong")
    assert caught.value.code == 403


def test_every_language_the_server_names_is_a_plain_identifier(workspace):
    """The front end looks each one up in its own table; a stray name is a file
    that silently renders as plain text."""
    names = set(desktop_mod.LANGUAGES.values()) | set(desktop_mod.FILENAMES.values())
    assert len(names) >= 30
    assert all(name and name.replace("+", "").replace("#", "").isalnum() for name in names)


def test_a_file_is_named_by_its_name_before_its_suffix(workspace):
    (workspace.root / "Dockerfile").write_text("FROM x\n", encoding="utf-8")
    (workspace.root / "Makefile").write_text("all:\n\techo\n", encoding="utf-8")
    assert workspace.read("Dockerfile")["language"] == "dockerfile"
    assert workspace.read("Makefile")["language"] == "makefile"


def test_the_front_end_manifest_matches_what_is_shipped(tmp_path, monkeypatch):
    """A half-copied install should name what is missing, not open an empty window."""
    assert desktop_mod.available()[0] is True
    for name in desktop_mod.FRONT_END_FILES:
        assert (desktop_mod.WEB_ROOT / name).is_file(), name
    monkeypatch.setattr(desktop_mod, "WEB_ROOT", tmp_path)
    ok, why = desktop_mod.available()
    assert ok is False and "ide.js" in why and "lang.js" in why


def test_the_page_pulls_in_every_script_and_stylesheet(app):
    """The token travels in the subresource URLs; a file left out of the page
    is a file the browser never fetches, however present it is on disk."""
    page = _request(app, "/").read().decode("utf-8")
    for name in desktop_mod.FRONT_END_FILES:
        # The two .html files are pages in their own right, not subresources of
        # each other: index.html is this one, and signin.html replaces it.
        if name.endswith(".html"):
            continue
        assert f"/static/{name}?token=" in page, name


# ══ the lock on the front door ═══════════════════════════════════════════════════════
# The loopback bind and the session token answer "can something on the network
# reach this?". These answer "is this the person who started it?" — and, just as
# importantly, that switching the lock on does not switch anything else off.
@pytest.fixture
def locked(app, tmp_path, monkeypatch):
    """The same app, with a password set and the lock switched on."""
    from config import settings

    from jarvis import auth

    monkeypatch.setattr(auth, "CREDENTIALS_PATH", tmp_path / "credentials.json")
    monkeypatch.setattr(auth, "SCRYPT_N", 2 ** 10)
    monkeypatch.setattr(auth, "SCRYPT_MAXMEM", 128 * (2 ** 10) * 8 * 2)
    monkeypatch.setattr(settings, "DESKTOP_AUTH_MODE", "password", raising=False)
    auth.set_password("let me in please")
    app.sessions = auth.Sessions()
    app.throttle = auth.Throttle()
    return app


def _raw(app, path, body=None, token=True, cookie=None, headers=None):
    """A request with full control over what it carries. Returns (status, body, headers)."""
    base = app.url.split("?")[0].rstrip("/")
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(base + path, data=data,
                                     method="GET" if data is None else "POST")
    request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("X-Jarvis-Token", app.token)
    if cookie:
        request.add_header("Cookie", cookie)
    for name, value in (headers or {}).items():
        request.add_header(name, value)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.read().decode("utf-8", "replace"), response.headers
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode("utf-8", "replace"), error.headers


def _sign_in(app, password="let me in please"):
    """Sign in and return the cookie header a browser would then send."""
    _, _, headers = _raw(app, "/api/auth/password", {"password": password}, token=False)
    return (headers.get("Set-Cookie") or "").split(";")[0]


def test_with_the_lock_off_nothing_changes(app):
    """The default. Every existing route answers exactly as it always did."""
    assert get_json(app, "/api/state")["token"] == app.token
    assert _raw(app, "/")[0] == 200
    assert "__JARVIS_BOOT__" not in _raw(app, "/")[1]


def test_the_locked_window_serves_a_lock_screen_instead_of_itself(locked):
    status, page, _ = _raw(locked, "/")
    assert status == 200
    assert "This window is locked" in page
    # The token lives in the page; a browser that has not signed in must not get
    # a page, because getting the page *is* getting the token.
    assert locked.token not in page
    assert "__JARVIS_TOKEN__" not in page


@pytest.mark.parametrize("route", ["/api/state", "/api/files?path=", "/api/index", "/api/events"])
def test_a_valid_token_alone_opens_nothing(locked, route):
    assert _raw(locked, route)[0] == 401


def test_the_shell_is_behind_the_lock_too(locked):
    status, _, _ = _raw(locked, "/api/shell", {"input": "echo should not run"})
    assert status == 401


def test_the_sign_in_state_is_readable_without_signing_in(locked):
    status, body, _ = _raw(locked, "/api/auth/state", token=False)
    payload = json.loads(body)
    assert status == 200
    assert payload["required"] is True and payload["signedIn"] is False
    assert payload["password"] is True and payload["passwordSet"] is True


def test_the_right_password_opens_the_door(locked):
    status, body, headers = _raw(locked, "/api/auth/password",
                                 {"password": "let me in please"}, token=False)
    assert status == 200 and json.loads(body)["ok"] is True
    cookie = headers.get("Set-Cookie") or ""
    assert "jarvis_session=" in cookie
    # Unreadable by script, and never sent by another site.
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie


def test_the_wrong_password_does_not(locked):
    status, body, headers = _raw(locked, "/api/auth/password",
                                 {"password": "not the password"}, token=False)
    assert json.loads(body)["ok"] is False
    assert "Set-Cookie" not in headers


def test_both_the_cookie_and_the_token_are_required(locked):
    cookie = _sign_in(locked)
    assert _raw(locked, "/api/state", cookie=cookie)[0] == 200
    assert _raw(locked, "/api/state", cookie=cookie, token=False)[0] == 403
    assert _raw(locked, "/api/state", cookie="jarvis_session=invented")[0] == 401


def test_signing_out_puts_the_lock_back(locked):
    cookie = _sign_in(locked)
    assert _raw(locked, "/api/auth/logout", {}, cookie=cookie)[0] == 200
    assert _raw(locked, "/api/state", cookie=cookie)[0] == 401
    assert "This window is locked" in _raw(locked, "/", cookie=cookie)[1]


def test_the_host_and_origin_checks_still_apply_when_signed_in(locked):
    """The lock is a third layer, not a replacement for the two under it."""
    cookie = _sign_in(locked)
    assert _raw(locked, "/api/state", cookie=cookie,
                headers={"Host": "evil.example"})[0] == 403
    assert _raw(locked, "/api/state", cookie=cookie,
                headers={"Origin": "https://evil.example"})[0] == 403


def test_the_lock_screen_is_refused_to_a_forged_host(locked):
    assert _raw(locked, "/", token=False, headers={"Host": "evil.example"})[0] == 403


def test_too_many_wrong_guesses_closes_the_door(locked):
    from jarvis import auth

    locked.throttle = auth.Throttle(limit=3, window=30.0)
    for _ in range(3):
        _raw(locked, "/api/auth/password", {"password": "wrong"}, token=False)
    _, body, _ = _raw(locked, "/api/auth/password", {"password": "let me in please"}, token=False)
    result = json.loads(body)
    assert result["ok"] is False and "too many attempts" in result["error"]


def test_google_start_is_refused_when_google_is_switched_off(locked):
    status, body, _ = _raw(locked, "/api/auth/google/start", token=False)
    assert status == 400 and "switched off" in json.loads(body)["error"]


def test_google_start_redirects_to_google_when_it_is_configured(locked, monkeypatch):
    from config import settings

    monkeypatch.setattr(settings, "DESKTOP_AUTH_MODE", "any", raising=False)
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_ID", "abc.apps.googleusercontent.com",
                        raising=False)
    base = locked.url.split("?")[0].rstrip("/")
    request = urllib.request.Request(base + "/api/auth/google/start")

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None

    opener = urllib.request.build_opener(NoRedirect)
    try:
        with opener.open(request, timeout=5) as response:
            pytest.fail(f"expected a redirect, got {response.status}")
    except urllib.error.HTTPError as error:
        assert error.code == 302
        target = error.headers["Location"]
    assert target.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert "code_challenge_method=S256" in target
    assert "code_verifier" not in target


def test_a_google_callback_that_did_not_start_here_lands_back_on_the_lock_screen(locked, monkeypatch):
    from config import settings

    monkeypatch.setattr(settings, "DESKTOP_AUTH_MODE", "any", raising=False)
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_ID", "abc.apps.googleusercontent.com",
                        raising=False)
    status, page, _ = _raw(locked, "/api/auth/google/callback?code=x&state=invented", token=False)
    assert status == 200
    assert "This window is locked" in page
    assert locked.token not in page


def test_the_boot_payload_says_who_signed_in(locked):
    cookie = _sign_in(locked)
    page = _raw(locked, "/", cookie=cookie)[1]
    assert "This window is locked" not in page
    boot = json.loads(page.split("window.JARVIS_BOOT = ", 1)[1].split(";</script>", 1)[0])
    assert boot["auth"]["required"] is True
    assert boot["identity"]["method"] == "password"
    assert boot["token"] == locked.token
