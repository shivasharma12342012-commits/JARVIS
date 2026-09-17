from __future__ import annotations

import base64
import http.client
import json
import threading
import urllib.request
from pathlib import Path

import pytest

from jarvis.school_portal import OllamaProvider, PortalApp, PortalServer, PortalStore, ProviderUnavailable, User


class FakeProvider:
    name = "ollama"

    def __init__(self, content="## Structure of the Atom\n- Objective: explain atomic structure"):
        self.content = content

    def generate(self, prompt, system):
        return self.content

    def health(self):
        return {"available": True, "provider": self.name, "model": "fake"}


@pytest.fixture()
def store(tmp_path: Path):
    return PortalStore(tmp_path / "portal.sqlite3", tmp_path / "files")


def test_passwords_are_hashed_and_roles_persist(store):
    admin = store.create_user("Principal@School.test", "Principal", "correct horse", "admin")
    teacher = store.create_user("teacher@school.test", "Teacher", "teacher-pass", "teacher")
    assert store.authenticate("principal@school.test", "correct horse") == admin
    assert store.authenticate("teacher@school.test", "wrong") is None
    assert teacher.role == "teacher"


def test_session_expiry_and_revoke(store):
    teacher = store.create_user("teacher@school.test", "Teacher", "teacher-pass")
    token = store.issue_session(teacher.id, 0.01)
    assert store.resolve_session(token) == teacher
    store.revoke_session(token)
    assert store.resolve_session(token) is None


def test_disabling_a_user_invalidates_future_sessions(store):
    teacher = store.create_user("teacher@school.test", "Teacher", "teacher-pass")
    token = store.issue_session(teacher.id, 3600)
    assert store.set_user_active(teacher.id, False) is True
    assert store.resolve_session(token) is None


def test_teacher_workspace_isolation(store):
    one = store.create_user("one@school.test", "One", "one-pass-1")
    two = store.create_user("two@school.test", "Two", "two-pass-2")
    store.save_workspace(one.id, "Private plan", "lesson-plan", "secret")
    assert [x["title"] for x in store.workspace(one.id)] == ["Private plan"]
    assert store.workspace(two.id) == []
    assert store.delete_workspace(two.id, 1) is False


def test_lesson_plan_is_generated_and_saved(store):
    teacher = store.create_user("teacher@school.test", "Teacher", "teacher-pass")
    app = PortalApp(store, FakeProvider(), {"school_name": "Test School", "session_hours": 12, "max_file_bytes": 100})
    result = app.generate_lesson_plan(teacher, {"grade": "Class 9", "subject": "Science", "topic": "Atoms", "duration": "40 minutes"})
    assert result["item"]["kind"] == "lesson-plan"
    assert store.workspace(teacher.id)[0]["content"].startswith("## Structure")


def test_teacher_assistant_history_is_private(store):
    one = store.create_user("one@school.test", "One", "one-pass-1")
    two = store.create_user("two@school.test", "Two", "two-pass-2")
    app = PortalApp(store, FakeProvider(), {"school_name": "Test School", "session_hours": 12, "max_file_bytes": 100})
    response = app.chat(one, "Give me one activity for Class 7 science")
    assert response["conversation"]["messages"][-1]["role"] == "assistant"
    assert len(store.conversations(one.id)) == 1
    assert store.conversations(two.id) == []


def test_provider_unavailable_is_honest(store):
    teacher = store.create_user("teacher@school.test", "Teacher", "teacher-pass")
    class Unavailable(FakeProvider):
        def generate(self, prompt, system):
            raise ProviderUnavailable("Ollama is unavailable")
    app = PortalApp(store, Unavailable(), {"school_name": "Test School", "session_hours": 12, "max_file_bytes": 100})
    with pytest.raises(ProviderUnavailable):
        app.generate_lesson_plan(teacher, {"grade": "9", "subject": "Science", "topic": "Atoms", "duration": "40"})
    assert store.workspace(teacher.id) == []


def test_document_validation_and_safe_filename(store, tmp_path):
    teacher = store.create_user("teacher@school.test", "Teacher", "teacher-pass")
    doc = store.add_document(teacher.id, "../../chapter.pdf", "application/pdf", b"pdf")
    assert doc["filename"] == "chapter.pdf"
    assert list((tmp_path / "files").glob("*") )[0].name != "chapter.pdf"
    with pytest.raises(ValueError):
        store.add_document(teacher.id, "chapter.pdf", "text/plain", b"wrong type")


def test_http_rbac_and_lesson_flow(tmp_path):
    store = PortalStore(tmp_path / "portal.sqlite3", tmp_path / "files")
    app = PortalApp(store, FakeProvider(), {"school_name": "Test School", "session_hours": 12, "max_file_bytes": 100})
    try:
        server = PortalServer(("127.0.0.1", 0), app)
    except PermissionError:
        pytest.skip("the managed test sandbox does not permit binding a local socket")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def request(method, path, payload=None, cookie=""):
        connection = http.client.HTTPConnection(*server.server_address)
        body = json.dumps(payload).encode() if payload is not None else None
        headers = {"Content-Type": "application/json"} if body is not None else {}
        if cookie:
            headers["Cookie"] = cookie
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        result = response.status, json.loads(response.read().decode()), response.getheader("Set-Cookie", "")
        connection.close()
        return result

    try:
        assert request("GET", "/api/workspace")[0] == 401
        status, data, cookie = request("POST", "/api/setup", {"name": "Admin", "email": "admin@school.test", "password": "admin-pass"})
        assert status == 200 and data["user"]["role"] == "admin"
        status, data, _ = request("POST", "/api/teaching/lesson-plan", {"grade": "9", "subject": "Science", "topic": "Atoms", "duration": "40"}, cookie)
        assert status == 200 and data["item"]["kind"] == "lesson-plan"
        assert request("GET", "/api/workspace", cookie=cookie)[1]["items"]
    finally:
        server.shutdown()
        server.server_close()
