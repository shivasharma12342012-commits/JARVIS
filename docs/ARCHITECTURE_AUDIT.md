# J.A.R.V.I.S. institutional platform audit

This audit treats the attached master engineering prompt as product requirements. The
user request is to implement those requirements; the prompt is not executable source
code and does not authorize destructive rewrites or invented integrations.

## Current repository

- **Runtime:** Python 3.11 desktop/TUI application. `main.py` wires the agent, voice,
  monitor, protocols, permissions and desktop window.
- **Frontend:** a standard-library loopback HTTP server in `jarvis/desktop.py` serving
  the HTML/CSS/JavaScript in `jarvis/web/`. The UI is a polished operator chat/code
  surface rather than a school portal.
- **AI:** `jarvis/core.py` and `jarvis/engine.py` implement synchronous and asynchronous
  ReAct loops around Ollama. `jarvis/tools.py` supplies tool calls and
  `jarvis/prompts.py` supplies system prompts.
- **Authentication:** `jarvis/auth.py` supports a single local desktop session, scrypt
  password records, optional Google PKCE, lockout and in-memory sessions. It is not a
  multi-user school identity or role system.
- **Authorization:** `jarvis/permissions.py` is an operator consent broker for local
  filesystem, shell and process access. It is useful for the desktop surface, but it
  does not isolate teachers or enforce administrator roles.
- **Persistence:** the desktop assistant stores a local profile, credentials, theme and
  workspace files. There is no database schema for users, conversations, documents,
  school knowledge, usage or audit events.
- **Testing:** `tests/` covers authentication, engine behavior, desktop routes, UI,
  shell, themes and integration boot paths. It does not cover school RBAC or ownership.
- **Deployment:** local process and local Ollama daemon. No school service entrypoint,
  migrations, or deployment/recovery documentation exists.

## Risks and product conflicts

The existing desktop shell/editor is intentionally powerful and should remain a
local-operator feature. Exposing it as a teacher portal would violate the teacher-only
boundary and create cross-user file and command risks. The institutional service must
therefore have its own authenticated routes, SQLite persistence, ownership checks and a
provider boundary. Unsupported providers and external ERP/LMS/calendar integrations
must report unavailable rather than pretending to work.

## Migration decisions

Preserve the Ollama agent, model configuration, desktop server, permission broker,
workspace safety checks and existing tests. Add an adjacent `jarvis.school_portal`
service using the Python standard library and SQLite, and expose it through a `portal`
command. Reuse the existing `config.settings.OLLAMA_HOST` and `MODEL_NAME`, but do not
reuse desktop tokens or local operator credentials for school accounts.

The first implementation slice is deliberately reviewable: secure configuration and
SQLite persistence; teacher/admin sessions and RBAC; an accessible teacher dashboard;
one complete lesson-plan workflow that calls Ollama when available, saves the output to
the owning teacher's workspace, and supports retrieval/export; admin user and knowledge
management; usage, health and audit summaries. Documents, additional teaching tools,
communications, and multi-step agents can build on these same tables and provider
interfaces.

## Validation plan

Run the existing suite, then portal unit tests for password/session expiry, role checks,
cross-teacher isolation, provider unavailable behavior, lesson-plan persistence,
knowledge ownership and file validation. Run the CLI help/build/import checks and a
manual local portal smoke test with Ollama stopped to confirm the UI surfaces a useful
unavailable message instead of a fabricated answer.
