"""Teacher-only institutional web portal for J.A.R.V.I.S.

This service is intentionally adjacent to the existing desktop application. The desktop
assistant is a single-operator local tool; this module adds a small, dependency-free
school service with SQLite persistence, signed-in sessions, server-side RBAC and an
Ollama provider boundary. It is suitable for a local demonstration and a clear base for
deployment behind a school reverse proxy. It never exposes the Ollama credential (or
the desktop shell/editor) to a browser.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import logging
import mimetypes
import os
import secrets
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from config import settings

LOG = logging.getLogger("jarvis.school_portal")
PORTAL_ROOT = Path(__file__).resolve().parent
WEB_ROOT = PORTAL_ROOT / "web"


def _env_path(name: str, default: Path) -> Path:
    value = os.environ.get(name, "").strip()
    return Path(value).expanduser() if value else default


def _portal_config() -> dict[str, Any]:
    root = Path(settings.WORKSPACE_ROOT).resolve()
    return {
        "host": os.environ.get("JARVIS_PORTAL_HOST", "127.0.0.1"),
        "port": int(os.environ.get("JARVIS_PORTAL_PORT", "8787")),
        "db": _env_path("JARVIS_PORTAL_DB", root / "data" / "school_portal.sqlite3"),
        "storage": _env_path("JARVIS_PORTAL_STORAGE", root / "data" / "school_files"),
        "school_name": os.environ.get("JARVIS_SCHOOL_NAME", "Your School"),
        "session_hours": float(os.environ.get("JARVIS_PORTAL_SESSION_HOURS", "12")),
        "max_file_bytes": int(os.environ.get("JARVIS_PORTAL_MAX_FILE_BYTES", "10000000")),
    }


def _hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    key = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${key.hex()}"


def _verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, salt_hex, expected_hex = encoded.split("$", 2)
        if algorithm != "scrypt":
            return False
        candidate = _hash_password(password, bytes.fromhex(salt_hex)).split("$", 2)[2]
        return hmac.compare_digest(candidate, expected_hex)
    except (ValueError, TypeError):
        return False


@dataclass(frozen=True)
class User:
    id: int
    email: str
    name: str
    role: str
    active: bool


class PortalStore:
    """SQLite store with a fresh connection per operation for thread safety."""

    def __init__(self, db_path: str | Path, storage_root: str | Path | None = None) -> None:
        self.path = Path(db_path).expanduser().resolve()
        self.storage_root = Path(storage_root or self.path.parent / "school_files").expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.storage_root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    email TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    name TEXT NOT NULL,
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('teacher','admin')),
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    expires_at REAL NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS conversations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    title TEXT NOT NULL,
                    messages_json TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS workspace_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    title TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    content TEXT NOT NULL,
                    folder TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS documents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    owner_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    filename TEXT NOT NULL,
                    stored_name TEXT NOT NULL UNIQUE,
                    mime_type TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS knowledge (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    content TEXT NOT NULL,
                    source TEXT NOT NULL,
                    created_by INTEGER NOT NULL REFERENCES users(id),
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS usage_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                    provider TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    input_chars INTEGER NOT NULL DEFAULT 0,
                    output_chars INTEGER NOT NULL DEFAULT 0,
                    success INTEGER NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                    event TEXT NOT NULL,
                    detail TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_sessions_expiry ON sessions(expires_at);
                CREATE INDEX IF NOT EXISTS idx_workspace_owner ON workspace_items(user_id, updated_at);
                CREATE INDEX IF NOT EXISTS idx_usage_created ON usage_events(created_at);
                """
            )

    def count_users(self) -> int:
        with self._connect() as db:
            return int(db.execute("SELECT COUNT(*) FROM users").fetchone()[0])

    def create_user(self, email: str, name: str, password: str, role: str = "teacher") -> User:
        email = email.strip().lower()
        name = name.strip() or email.split("@", 1)[0]
        if "@" not in email or email.startswith("@") or email.endswith("@"):
            raise ValueError("a valid school email is required")
        if role not in {"teacher", "admin"}:
            raise ValueError("role must be teacher or admin")
        if len(password) < 8:
            raise ValueError("password must be at least 8 characters")
        with self._connect() as db:
            cur = db.execute(
                "INSERT INTO users(email,name,password_hash,role) VALUES(?,?,?,?)",
                (email, name, _hash_password(password), role),
            )
            return User(int(cur.lastrowid), email, name, role, True)

    def authenticate(self, email: str, password: str) -> User | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM users WHERE email=? COLLATE NOCASE", (email.strip(),)).fetchone()
        if not row or not row["active"] or not _verify_password(password, row["password_hash"]):
            return None
        return User(int(row["id"]), row["email"], row["name"], row["role"], bool(row["active"]))

    def user(self, user_id: int) -> User | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        return User(int(row["id"]), row["email"], row["name"], row["role"], bool(row["active"])) if row else None

    def issue_session(self, user_id: int, ttl_seconds: float) -> str:
        token = secrets.token_urlsafe(32)
        with self._connect() as db:
            db.execute("DELETE FROM sessions WHERE expires_at<?", (time.time(),))
            db.execute("INSERT INTO sessions(token_hash,user_id,expires_at) VALUES(?,?,?)",
                       (hashlib.sha256(token.encode()).hexdigest(), user_id, time.time() + ttl_seconds))
        return token

    def resolve_session(self, token: str) -> User | None:
        if not token:
            return None
        with self._connect() as db:
            row = db.execute(
                "SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id "
                "WHERE s.token_hash=? AND s.expires_at>? AND u.active=1",
                (hashlib.sha256(token.encode()).hexdigest(), time.time()),
            ).fetchone()
        return User(int(row["id"]), row["email"], row["name"], row["role"], True) if row else None

    def revoke_session(self, token: str) -> None:
        with self._connect() as db:
            db.execute("DELETE FROM sessions WHERE token_hash=?", (hashlib.sha256(token.encode()).hexdigest(),))

    def audit(self, user_id: int | None, event: str, detail: str = "") -> None:
        with self._connect() as db:
            db.execute("INSERT INTO audit_events(user_id,event,detail) VALUES(?,?,?)", (user_id, event, detail[:500]))

    def usage(self, user_id: int | None, operation: str, input_chars: int, output_chars: int, success: bool) -> None:
        with self._connect() as db:
            db.execute("INSERT INTO usage_events(user_id,provider,operation,input_chars,output_chars,success) VALUES(?,?,?,?,?,?)",
                       (user_id, "ollama", operation, input_chars, output_chars, int(success)))

    def workspace(self, user_id: int) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute("SELECT id,title,kind,folder,content,created_at,updated_at FROM workspace_items WHERE user_id=? ORDER BY updated_at DESC", (user_id,)).fetchall()
        return [dict(row) for row in rows]

    def conversations(self, user_id: int) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute("SELECT id,title,messages_json,created_at,updated_at FROM conversations WHERE user_id=? ORDER BY updated_at DESC", (user_id,)).fetchall()
        return [dict(row) for row in rows]

    def save_conversation(self, user_id: int, title: str, messages: list[dict[str, str]], conversation_id: int | None = None) -> dict[str, Any]:
        encoded = json.dumps(messages, ensure_ascii=False)
        with self._connect() as db:
            if conversation_id:
                cur = db.execute("UPDATE conversations SET messages_json=?,updated_at=CURRENT_TIMESTAMP WHERE id=? AND user_id=?", (encoded, conversation_id, user_id))
                if cur.rowcount == 0:
                    raise ValueError("conversation not found")
                row = db.execute("SELECT id,title,messages_json,created_at,updated_at FROM conversations WHERE id=?", (conversation_id,)).fetchone()
            else:
                cur = db.execute("INSERT INTO conversations(user_id,title,messages_json) VALUES(?,?,?)", (user_id, title.strip()[:160] or "New conversation", encoded))
                row = db.execute("SELECT id,title,messages_json,created_at,updated_at FROM conversations WHERE id=?", (cur.lastrowid,)).fetchone()
        result = dict(row)
        result["messages"] = messages
        result.pop("messages_json", None)
        return result

    def save_workspace(self, user_id: int, title: str, kind: str, content: str, folder: str = "") -> dict[str, Any]:
        if not title.strip() or not content.strip():
            raise ValueError("title and content are required")
        with self._connect() as db:
            cur = db.execute("INSERT INTO workspace_items(user_id,title,kind,content,folder) VALUES(?,?,?,?,?)",
                             (user_id, title.strip()[:160], kind.strip()[:40], content, folder.strip()[:120]))
            row = db.execute("SELECT id,title,kind,folder,content,created_at,updated_at FROM workspace_items WHERE id=?", (cur.lastrowid,)).fetchone()
        return dict(row)

    def delete_workspace(self, user_id: int, item_id: int) -> bool:
        with self._connect() as db:
            cur = db.execute("DELETE FROM workspace_items WHERE id=? AND user_id=?", (item_id, user_id))
        return cur.rowcount > 0

    def add_document(self, owner_id: int, filename: str, mime_type: str, content: bytes) -> dict[str, Any]:
        safe_name = Path(filename).name.strip() or "upload"
        extension_types = {
            ".pdf": "application/pdf",
            ".txt": "text/plain",
            ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        }
        expected = extension_types.get(Path(safe_name).suffix.lower())
        if expected and expected != mime_type:
            raise ValueError("the file extension and content type do not match")
        stored = secrets.token_hex(16) + Path(safe_name).suffix.lower()
        target = self.storage_root / stored
        target.write_bytes(content)
        with self._connect() as db:
            cur = db.execute("INSERT INTO documents(owner_id,filename,stored_name,mime_type,size_bytes) VALUES(?,?,?,?,?)",
                             (owner_id, safe_name[:240], stored, mime_type[:120], len(content)))
        return {"id": int(cur.lastrowid), "filename": safe_name, "mime_type": mime_type, "size_bytes": len(content)}

    def add_knowledge(self, title: str, content: str, source: str, created_by: int) -> dict[str, Any]:
        with self._connect() as db:
            cur = db.execute("INSERT INTO knowledge(title,content,source,created_by) VALUES(?,?,?,?)",
                             (title.strip()[:200], content.strip(), source.strip()[:240], created_by))
        return {"id": int(cur.lastrowid), "title": title.strip(), "source": source.strip()}

    def knowledge(self, query: str = "") -> list[dict[str, Any]]:
        with self._connect() as db:
            if query.strip():
                needle = f"%{query.strip()}%"
                rows = db.execute("SELECT id,title,content,source,created_at FROM knowledge WHERE title LIKE ? OR content LIKE ? ORDER BY created_at DESC", (needle, needle)).fetchall()
            else:
                rows = db.execute("SELECT id,title,content,source,created_at FROM knowledge ORDER BY created_at DESC").fetchall()
        return [dict(row) for row in rows]

    def admin_summary(self) -> dict[str, Any]:
        with self._connect() as db:
            users = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            teachers = db.execute("SELECT COUNT(*) FROM users WHERE role='teacher' AND active=1").fetchone()[0]
            items = db.execute("SELECT COUNT(*) FROM workspace_items").fetchone()[0]
            docs = db.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
            requests = db.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0]
            failures = db.execute("SELECT COUNT(*) FROM usage_events WHERE success=0").fetchone()[0]
        return {"users": users, "active_teachers": teachers, "workspace_items": items, "documents": docs, "ai_requests": requests, "ai_failures": failures}

    def users(self) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute("SELECT id,email,name,role,active,created_at FROM users ORDER BY name").fetchall()
        return [dict(row) for row in rows]

    def set_user_active(self, user_id: int, active: bool) -> bool:
        with self._connect() as db:
            cur = db.execute("UPDATE users SET active=? WHERE id=?", (int(active), user_id))
        return cur.rowcount > 0


class OllamaProvider:
    name = "ollama"

    def __init__(self, host: str | None = None, model: str | None = None, timeout: float = 120) -> None:
        self.host = (host or settings.OLLAMA_HOST).rstrip("/")
        self.model = model or settings.MODEL_NAME
        self.timeout = timeout

    def health(self) -> dict[str, Any]:
        try:
            request = urllib.request.Request(self.host + "/api/version", method="GET")
            with urllib.request.urlopen(request, timeout=3) as response:
                payload = json.loads(response.read().decode("utf-8"))
            return {"available": True, "provider": self.name, "model": self.model, "version": payload.get("version", "")}
        except (OSError, ValueError, urllib.error.URLError) as exc:
            return {"available": False, "provider": self.name, "model": self.model, "reason": "Ollama is unavailable"}

    def generate(self, prompt: str, system: str) -> str:
        body = json.dumps({"model": self.model, "stream": False, "messages": [
            {"role": "system", "content": system}, {"role": "user", "content": prompt}
        ]}).encode("utf-8")
        request = urllib.request.Request(self.host + "/api/chat", data=body, method="POST",
                                         headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
            content = payload.get("message", {}).get("content", "")
            if not content:
                raise RuntimeError("Ollama returned an empty response")
            return str(content)
        except (OSError, ValueError, urllib.error.URLError, TimeoutError) as exc:
            raise ProviderUnavailable("The Ollama service is temporarily unavailable. Start Ollama and try again.") from exc


class ProviderUnavailable(RuntimeError):
    pass


def _lesson_prompt(data: dict[str, Any]) -> str:
    return json.dumps({
        "task": "Create a classroom-ready lesson plan for an Indian school teacher.",
        "requirements": data,
        "output": ["title", "objectives", "prerequisites", "introduction", "explanation", "activity", "questions", "assessment", "homework", "teacher_notes"],
        "instruction": "Return clear Markdown with headings and bullet points. Do not mention being an AI.",
    }, ensure_ascii=False)


class PortalApp:
    def __init__(self, store: PortalStore, provider: OllamaProvider | None = None, config: dict[str, Any] | None = None) -> None:
        self.store = store
        self.provider = provider or OllamaProvider()
        self.config = config or _portal_config()

    def generate_lesson_plan(self, user: User, data: dict[str, Any]) -> dict[str, Any]:
        required = ("grade", "subject", "topic", "duration")
        if any(not str(data.get(key, "")).strip() for key in required):
            raise ValueError("grade, subject, topic and duration are required")
        system = "You are J.A.R.V.I.S., a private teacher-only school assistant. Keep content age-appropriate and practical."
        prompt = _lesson_prompt(data)
        try:
            content = self.provider.generate(prompt, system)
            success = True
        except ProviderUnavailable:
            self.store.usage(user.id, "lesson_plan", len(prompt), 0, False)
            raise
        self.store.usage(user.id, "lesson_plan", len(prompt), len(content), success)
        saved = self.store.save_workspace(user.id, f"{data['subject']}: {data['topic']}", "lesson-plan", content, f"{data['grade']} {data['subject']}")
        self.store.audit(user.id, "workspace.create", f"lesson plan {saved['id']}")
        return {"item": saved, "provider": self.provider.name}

    def chat(self, user: User, text: str, conversation_id: int | None = None) -> dict[str, Any]:
        text = text.strip()
        if not text or len(text) > 12_000:
            raise ValueError("message must be between 1 and 12,000 characters")
        messages: list[dict[str, str]] = []
        if conversation_id:
            existing = next((x for x in self.store.conversations(user.id) if x["id"] == conversation_id), None)
            if existing:
                try: messages = json.loads(existing["messages_json"])
                except (TypeError, ValueError): messages = []
        messages.append({"role": "user", "content": text})
        context = self.store.knowledge()
        source_context = "\n\n".join(f"[{x['source']}] {x['content']}" for x in context[:5])
        system = "You are J.A.R.V.I.S., a private teacher-only school assistant. Give practical, age-appropriate guidance. Use the approved school sources below when relevant and cite their source name.\n" + source_context
        prompt = "\n\n".join(f"{m['role'].title()}: {m['content']}" for m in messages)
        try: answer = self.provider.generate(prompt, system)
        except ProviderUnavailable:
            self.store.usage(user.id, "assistant", len(prompt), 0, False)
            raise
        messages.append({"role": "assistant", "content": answer})
        conversation = self.store.save_conversation(user.id, text[:70], messages, conversation_id)
        self.store.usage(user.id, "assistant", len(prompt), len(answer), True)
        return {"conversation": conversation, "answer": answer, "provider": self.provider.name}


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False).encode("utf-8")


PORTAL_HTML = """<!doctype html>
<html lang=\"en\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><title>J.A.R.V.I.S. School Portal</title><link rel=\"stylesheet\" href=\"/static/school.css\"></head>
<body><div id=\"app\"><section id=\"auth\" class=\"auth-card\"><div class=\"brand-mark\">✦</div><p class=\"eyebrow\">__SCHOOL_NAME__</p><h1>Teacher intelligence, thoughtfully organised.</h1><p class=\"muted\">A private workspace for lesson planning and school knowledge.</p><div id=\"setup\" hidden><h2>Set up your school workspace</h2><form id=\"setup-form\"><input name=\"name\" placeholder=\"Administrator name\" required><input name=\"email\" type=\"email\" placeholder=\"School email\" required><input name=\"password\" type=\"password\" minlength=\"8\" placeholder=\"Password (8+ characters)\" required><button>Create administrator</button></form></div><div id=\"login\"><h2>Sign in</h2><form id=\"login-form\"><input name=\"email\" type=\"email\" placeholder=\"School email\" required><input name=\"password\" type=\"password\" placeholder=\"Password\" required><button>Continue</button></form></div><p id=\"auth-message\" class=\"message\"></p></section><main id=\"portal\" hidden><aside><div class=\"brand\"><span>✦</span><div><strong>J.A.R.V.I.S.</strong><small>School workspace</small></div></div><nav><button data-view=\"home\" class=\"active\">Home</button><button data-view=\"assistant\">AI assistant</button><button data-view=\"lesson\">Teaching tools</button><button data-view=\"documents\">Documents</button><button data-view=\"workspace\">My workspace</button><button data-view=\"knowledge\">School knowledge</button><button data-view=\"admin\" id=\"admin-nav\" hidden>Administration</button></nav><button id=\"logout\" class=\"quiet\">Sign out</button></aside><section class=\"content\"><header><div><p class=\"eyebrow\" id=\"greeting\"></p><h1 id=\"page-title\">Good morning.</h1></div><span id=\"status\" class=\"status\">Ready</span></header><div id=\"view\"></div></section></main></div><script src=\"/static/school.js\"></script></body></html>"""


class PortalHandler(BaseHTTPRequestHandler):
    server_version = "JarvisSchoolPortal/1.0"
    protocol_version = "HTTP/1.1"

    @property
    def app(self) -> PortalApp:
        return self.server.app  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:
        LOG.info("%s - %s", self.address_string(), fmt % args)

    def _send(self, status: int, body: bytes, content_type: str = "application/json; charset=utf-8", headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, data: Any, status: int = 200, headers: dict[str, str] | None = None) -> None:
        self._send(status, _json_bytes(data), headers=headers)

    def _body(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 15_000_000:
                return {}
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError, OSError):
            return {}

    def _token(self) -> str:
        raw = self.headers.get("Cookie", "")
        for part in raw.split(";"):
            key, _, value = part.strip().partition("=")
            if key == "jarvis_school_session":
                return value
        return self.headers.get("X-Jarvis-Session", "")

    def _user(self) -> User | None:
        return self.app.store.resolve_session(self._token())

    def _safe_origin(self) -> bool:
        """Keep cookie-authenticated writes same-origin.

        SameSite cookies provide the browser-side defence; this header check also
        protects deployments where a reverse proxy or embedded browser changes
        cookie policy. Requests without an Origin header are allowed for the
        normal direct navigation and command-line clients.
        """
        origin = self.headers.get("Origin", "").strip()
        if not origin:
            return True
        origin_host = (urlparse(origin).hostname or "").lower()
        host = (self.headers.get("Host", "").split(":", 1)[0] or "").lower()
        return bool(origin_host and host and origin_host == host)

    def _require(self, role: str | None = None) -> User | None:
        user = self._user()
        if user is None:
            self._json({"error": "Please sign in to continue."}, 401)
            return None
        if role and user.role != role:
            self._json({"error": "You do not have permission to access this area."}, 403)
            return None
        return user

    def do_GET(self) -> None:  # noqa: N802
        if not self._safe_origin():
            self._json({"error": "Request origin was not authorised."}, 403)
            return
        route = urlparse(self.path).path
        if route in {"/", "/index.html"}:
            page = PORTAL_HTML.replace("__SCHOOL_NAME__", html.escape(str(self.app.config["school_name"])))
            self._send(200, page.encode(), "text/html; charset=utf-8", {"Content-Security-Policy": "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:"})
        elif route.startswith("/static/"):
            self._static(route.removeprefix("/static/"))
        elif route == "/api/state":
            self._json({"setup_required": self.app.store.count_users() == 0, "school_name": self.app.config["school_name"]})
        elif route == "/api/me":
            user = self._user()
            self._json({"user": user.__dict__ if user else None})
        elif route == "/api/health":
            user = self._require("admin")
            if user: self._json({"ollama": self.app.provider.health(), "database": "ok", "summary": self.app.store.admin_summary()})
        elif route == "/api/workspace":
            user = self._require()
            if user: self._json({"items": self.app.store.workspace(user.id)})
        elif route == "/api/conversations":
            user = self._require()
            if user:
                items = self.app.store.conversations(user.id)
                for item in items:
                    item.pop("messages_json", None)
                self._json({"items": items})
        elif route == "/api/workspace/export":
            user = self._require()
            if user:
                from urllib.parse import parse_qs
                try:
                    item_id = int(parse_qs(urlparse(self.path).query).get("id", ["0"])[0])
                except ValueError:
                    self._json({"error": "A valid workspace item id is required."}, 400)
                    return
                item = next((x for x in self.app.store.workspace(user.id) if x["id"] == item_id), None)
                if item is None:
                    self._json({"error": "That workspace item was not found."}, 404)
                else:
                    filename = "".join(c if c.isalnum() or c in " .-_" else "_" for c in item["title"]).strip() or "jarvis-output"
                    self._send(200, item["content"].encode("utf-8"), "text/plain; charset=utf-8", {"Content-Disposition": f'attachment; filename="{filename[:80]}.txt"'})
        elif route == "/api/documents":
            user = self._require()
            if user:
                with self.app.store._connect() as db:
                    rows = db.execute("SELECT id,filename,mime_type,size_bytes,created_at FROM documents WHERE owner_id=? ORDER BY created_at DESC", (user.id,)).fetchall()
                self._json({"documents": [dict(row) for row in rows]})
        elif route == "/api/documents/download":
            user = self._require()
            if user:
                from urllib.parse import parse_qs
                try: document_id = int(parse_qs(urlparse(self.path).query).get("id", ["0"])[0])
                except ValueError:
                    self._json({"error": "A valid document id is required."}, 400); return
                with self.app.store._connect() as db:
                    row = db.execute("SELECT filename,stored_name,mime_type FROM documents WHERE id=? AND owner_id=?", (document_id, user.id)).fetchone()
                if not row:
                    self._json({"error": "That document was not found."}, 404); return
                target = (self.app.store.storage_root / row["stored_name"]).resolve()
                if self.app.store.storage_root not in target.parents or not target.is_file():
                    self._json({"error": "That document is no longer available."}, 404); return
                self._send(200, target.read_bytes(), row["mime_type"], {"Content-Disposition": f'attachment; filename="{Path(row["filename"]).name}"'})
        elif route == "/api/knowledge":
            user = self._require()
            if user:
                query = urlparse(self.path).query
                from urllib.parse import parse_qs
                self._json({"items": self.app.store.knowledge(parse_qs(query).get("q", [""])[0])})
        elif route == "/api/users":
            user = self._require("admin")
            if user: self._json({"users": self.app.store.users()})
        elif route == "/api/dashboard":
            user = self._require()
            if user: self._json({"workspace": self.app.store.workspace(user.id)[:5], "knowledge": self.app.store.knowledge()[:5], "provider": self.app.provider.health()})
        else:
            self._json({"error": "Not found"}, 404)

    def _static(self, name: str) -> None:
        target = (WEB_ROOT / name).resolve()
        if not target.is_file() or WEB_ROOT.resolve() not in target.parents:
            self._json({"error": "Not found"}, 404); return
        content = target.read_bytes()
        self._send(200, content, mimetypes.guess_type(target.name)[0] or "application/octet-stream")

    def do_POST(self) -> None:  # noqa: N802
        if not self._safe_origin():
            self._json({"error": "Request origin was not authorised."}, 403)
            return
        route = urlparse(self.path).path
        body = self._body()
        if route == "/api/setup":
            if self.app.store.count_users(): self._json({"error": "Setup has already been completed."}, 409); return
            try:
                user = self.app.store.create_user(str(body.get("email", "")), str(body.get("name", "")), str(body.get("password", "")), "admin")
            except (ValueError, sqlite3.IntegrityError) as exc:
                message = "That account could not be created. Check the email and password." if isinstance(exc, sqlite3.IntegrityError) else str(exc)
                self._json({"error": message}, 400); return
            self.app.store.audit(user.id, "user.create", "initial administrator")
            self._signin(user); return
        if route == "/api/login":
            user = self.app.store.authenticate(str(body.get("email", "")), str(body.get("password", "")))
            if not user: self._json({"error": "Email or password was not recognised."}, 401); return
            self.app.store.audit(user.id, "login"); self._signin(user); return
        if route == "/api/logout":
            user = self._user()
            if user: self.app.store.audit(user.id, "logout")
            self.app.store.revoke_session(self._token()); self._json({"ok": True}, headers={"Set-Cookie": "jarvis_school_session=; Max-Age=0; Path=/; HttpOnly; SameSite=Strict"}); return
        user = self._require()
        if not user: return
        if route == "/api/teaching/lesson-plan":
            try: self._json(self.app.generate_lesson_plan(user, body))
            except ProviderUnavailable as exc: self._json({"error": str(exc), "provider": self.app.provider.name, "available": False}, 503)
            except ValueError as exc: self._json({"error": str(exc)}, 400)
        elif route == "/api/chat":
            try:
                conversation_id = body.get("conversation_id")
                result = self.app.chat(user, str(body.get("text", "")), int(conversation_id) if conversation_id else None)
                self._json(result)
            except ProviderUnavailable as exc: self._json({"error": str(exc), "provider": self.app.provider.name, "available": False}, 503)
            except (ValueError, TypeError) as exc: self._json({"error": str(exc)}, 400)
        elif route == "/api/workspace":
            try: self._json({"item": self.app.store.save_workspace(user.id, str(body.get("title", "")), str(body.get("kind", "note")), str(body.get("content", "")), str(body.get("folder", "")))}, 201)
            except ValueError as exc: self._json({"error": str(exc)}, 400)
        elif route == "/api/documents":
            try:
                raw = base64.b64decode(str(body.get("content_base64", "")), validate=True)
                if len(raw) > self.app.config["max_file_bytes"]: raise ValueError("file is larger than the configured limit")
                mime = str(body.get("mime_type", "application/octet-stream"))
                if mime not in {"application/pdf", "text/plain", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "application/vnd.openxmlformats-officedocument.presentationml.presentation"}:
                    raise ValueError("supported formats are PDF, DOCX, PPTX and TXT")
                self._json({"document": self.app.store.add_document(user.id, str(body.get("filename", "upload")), mime, raw)}, 201)
            except (ValueError, base64.binascii.Error) as exc: self._json({"error": str(exc)}, 400)
        elif route == "/api/knowledge":
            if user.role != "admin": self._json({"error": "Only administrators can manage school knowledge."}, 403); return
            try: self._json({"item": self.app.store.add_knowledge(str(body.get("title", "")), str(body.get("content", "")), str(body.get("source", "")), user.id)}, 201)
            except ValueError as exc: self._json({"error": str(exc)}, 400)
        elif route == "/api/users":
            if user.role != "admin": self._json({"error": "Only administrators can manage users."}, 403); return
            try:
                created = self.app.store.create_user(str(body.get("email", "")), str(body.get("name", "")), str(body.get("password", "")), str(body.get("role", "teacher")))
                self.app.store.audit(user.id, "user.create", created.email); self._json({"user": created.__dict__}, 201)
            except (ValueError, sqlite3.IntegrityError) as exc: self._json({"error": str(exc)}, 400)
        elif route == "/api/users/status":
            if user.role != "admin": self._json({"error": "Only administrators can manage users."}, 403); return
            try:
                target_id = int(body.get("id")); active = bool(body.get("active"))
                if target_id == user.id and not active: raise ValueError("you cannot disable your own account")
                if not self.app.store.set_user_active(target_id, active): raise ValueError("user not found")
                self.app.store.audit(user.id, "user.status", f"{target_id}={'active' if active else 'disabled'}")
                self._json({"ok": True})
            except (ValueError, TypeError): self._json({"error": "That user status change was not valid."}, 400)
        else: self._json({"error": "Not found"}, 404)

    def do_DELETE(self) -> None:  # noqa: N802
        if not self._safe_origin():
            self._json({"error": "Request origin was not authorised."}, 403)
            return
        route = urlparse(self.path).path
        user = self._require()
        if not user: return
        if route == "/api/workspace":
            from urllib.parse import parse_qs
            try:
                item_id = int(parse_qs(urlparse(self.path).query).get("id", ["0"])[0])
            except ValueError:
                self._json({"error": "A valid workspace item id is required."}, 400)
                return
            self._json({"ok": self.app.store.delete_workspace(user.id, item_id)})
        else: self._json({"error": "Not found"}, 404)

    def _signin(self, user: User) -> None:
        token = self.app.store.issue_session(user.id, self.app.config["session_hours"] * 3600)
        self._json({"user": user.__dict__}, headers={"Set-Cookie": f"jarvis_school_session={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={int(self.app.config['session_hours']*3600)}"})


class PortalServer(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int], app: PortalApp) -> None:
        super().__init__(address, PortalHandler)
        self.app = app
        self.daemon_threads = True
        self.allow_reuse_address = True


def run(host: str | None = None, port: int | None = None, db_path: str | Path | None = None) -> int:
    config = _portal_config()
    if host: config["host"] = host
    if port is not None: config["port"] = port
    if db_path: config["db"] = Path(db_path)
    store = PortalStore(config["db"], config["storage"])
    server = PortalServer((config["host"], config["port"]), PortalApp(store, config=config))
    LOG.info("School portal listening on http://%s:%d", *server.server_address)
    print(f"J.A.R.V.I.S. School Portal: http://{server.server_address[0]}:{server.server_address[1]}")
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()
    return 0


__all__ = ["PortalApp", "PortalServer", "PortalStore", "OllamaProvider", "ProviderUnavailable", "User", "run"]
