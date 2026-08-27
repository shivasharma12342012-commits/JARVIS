"""The consent layer: nothing leaves the workspace without the operator's say-so.

J.A.R.V.I.S. can open applications, write outside his project directory and close
running programs. Those are exactly the capabilities that must never be exercised on a
guess, so every one of them passes through :class:`PermissionBroker` first.

The model is deliberately simple, because a permission prompt nobody reads is worse than
no prompt at all:

* **Inside the workspace** -- no prompt. That is his desk; he may use it.
* **Outside it** -- one clear question naming the exact action and target, answered
  ``y`` (once), ``a`` (always, for this app or this directory), or ``n``.
* **Under Veronica lockdown** -- refused outright, with no prompt at all. A security
  protocol that can be talked out of its own rules is decoration.

Grants live for the session only. Nothing is persisted to disk: a permission the
operator forgot they granted last Tuesday is a trap.

Imports :mod:`config` and the standard library only. The HUD, voice and protocol engine
arrive as duck-typed constructor arguments and may all be ``None``.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from config import settings

logger = logging.getLogger(__name__)

# -- Scopes -----------------------------------------------------------------------------
SCOPE_FILESYSTEM = "filesystem"   # reading or writing outside the workspace
SCOPE_LAUNCH = "launch"           # starting an application or opening a document
SCOPE_PROCESS = "process"         # closing something already running
SCOPE_SHELL = "shell"             # running a command outside the workspace

SCOPE_LABELS = {
    SCOPE_FILESYSTEM: "Filesystem access outside the workspace",
    SCOPE_LAUNCH: "Launch an application",
    SCOPE_PROCESS: "Close a running application",
    SCOPE_SHELL: "Run a command outside the workspace",
}

#: How long a refusal answers repeat requests for the same thing. A model that is told
#: "no" often tries once more; re-prompting for that is how consent dialogs get trained
#: into reflexive approval. Short enough that a genuine later request still gets asked.
_DENIAL_WINDOW = 90.0

#: Answers accepted at the prompt.
_YES = {"y", "yes", "ok", "okay", "sure", "go", "go ahead", "do it", "haan", "ha"}
_ALWAYS = {"a", "always", "all", "allow", "yes always"}
_NO = {"n", "no", "nope", "stop", "cancel", "deny", "nahi", "nahin"}


@dataclass(frozen=True)
class PermissionRequest:
    """One thing J.A.R.V.I.S. would like to do that he cannot do unilaterally."""

    scope: str
    action: str
    target: str
    detail: str = ""
    reversible: bool = True

    @property
    def label(self) -> str:
        return SCOPE_LABELS.get(self.scope, self.scope)

    def question(self) -> str:
        """The sentence the operator actually reads."""
        title = settings.USER_TITLE
        if self.scope == SCOPE_LAUNCH:
            return f"{title}, may I open {self.target}?"
        if self.scope == SCOPE_PROCESS:
            return f"{title}, may I close {self.target}?"
        if self.scope == SCOPE_SHELL:
            return f"{title}, may I run `{self.target}` outside the workspace?"
        return f"{title}, may I {self.action} `{self.target}`? It is outside my workspace."


@dataclass
class Decision:
    """The answer, and why."""

    granted: bool
    reason: str = ""
    remembered: bool = False

    def __bool__(self) -> bool:
        return self.granted


@dataclass
class Grant:
    """A remembered approval."""

    scope: str
    target: str
    prefix: bool = False   # filesystem grants cover a whole directory tree

    def covers(self, scope: str, target: str) -> bool:
        if scope != self.scope:
            return False
        if not self.prefix:
            return _norm(target) == _norm(self.target)
        try:
            candidate = Path(target).expanduser().resolve()
            root = Path(self.target).expanduser().resolve()
        except (OSError, ValueError):
            return False
        try:
            return candidate == root or candidate.is_relative_to(root)
        except (AttributeError, ValueError):
            return str(candidate).startswith(str(root))


def _norm(value: str) -> str:
    return str(value).strip().lower().replace("\\", "/")


class PermissionBroker:
    """Asks the operator before J.A.R.V.I.S. reaches outside his own workspace."""

    def __init__(self, hud=None, voice=None, protocol_engine=None) -> None:
        self.hud = hud
        self.voice = voice
        self.protocol_engine = protocol_engine
        self._lock = threading.RLock()
        self._grants: list[Grant] = []
        self._denied_scopes: set[str] = set()
        self._recent_denials: dict[tuple[str, str], float] = {}
        self._log: list[tuple[str, str, bool]] = []
        self._seed_preapproved()

    def bind(self, hud=None, voice=None, protocol_engine=None) -> None:
        """Attach collaborators after construction, as main.py wires things up."""
        if hud is not None:
            self.hud = hud
        if voice is not None:
            self.voice = voice
        if protocol_engine is not None:
            self.protocol_engine = protocol_engine

    def _seed_preapproved(self) -> None:
        """Pre-approve whatever the operator listed in ``ALLOWED_APPS``."""
        for name in settings.ALLOWED_APPS or []:
            cleaned = str(name).strip()
            if cleaned:
                self._grants.append(Grant(SCOPE_LAUNCH, cleaned))

    # -- state -------------------------------------------------------------------------

    @property
    def mode(self) -> str:
        return (settings.PERMISSION_MODE or "ask").strip().lower()

    def grants(self) -> list[Grant]:
        with self._lock:
            return list(self._grants)

    def history(self) -> list[tuple[str, str, bool]]:
        """``(scope, target, granted)`` for everything asked this session."""
        with self._lock:
            return list(self._log)

    def allow(self, scope: str, target: str, prefix: bool = False) -> None:
        """Pre-grant something without asking -- used by ``/allow``."""
        with self._lock:
            self._grants.append(Grant(scope, target, prefix))

    def revoke_all(self) -> int:
        """Forget every grant made this session."""
        with self._lock:
            count = len(self._grants)
            self._grants.clear()
            self._denied_scopes.clear()
            self._recent_denials.clear()
            self._seed_preapproved()
            return count - len(self._grants)

    def _remembered(self, scope: str, target: str) -> bool:
        with self._lock:
            return any(g.covers(scope, target) for g in self._grants)

    # -- the decision ------------------------------------------------------------------

    def request(self, req: PermissionRequest) -> Decision:
        """Decide whether ``req`` may proceed. Never raises."""
        try:
            return self._decide(req)
        except Exception:
            # A broker that crashes must fail closed, never open.
            logger.exception("Permission broker faulted; denying by default")
            return Decision(False, "The permission check faulted, so I have declined.")

    def _decide(self, req: PermissionRequest) -> Decision:
        title = settings.USER_TITLE

        # 1. A security lockdown is not negotiable.
        if self.protocol_engine is not None:
            try:
                if getattr(self.protocol_engine, "lockdown", False):
                    return Decision(
                        False,
                        f"Veronica Protocol is engaged, {title}. I cannot reach outside "
                        f"the workspace until it is stood down.",
                    )
            except Exception:
                logger.debug("Lockdown check failed", exc_info=True)

        # 2. Global posture.
        mode = self.mode
        if mode == "deny":
            return Decision(
                False,
                f"Permissions are set to deny, {title}. Change PERMISSION_MODE to ask "
                f"if you want me to be able to request access.",
            )

        with self._lock:
            if req.scope in self._denied_scopes:
                return Decision(
                    False,
                    f"You asked me not to request {req.label.lower()} again this session.",
                )

        # 3. Just refused? Do not make them say it twice.
        key = (req.scope, _norm(req.target))
        with self._lock:
            refused_at = self._recent_denials.get(key)
            if refused_at is not None:
                if time.monotonic() - refused_at < _DENIAL_WINDOW:
                    return Decision(
                        False,
                        f"You already declined this a moment ago, {title}. I have not "
                        f"asked again.",
                    )
                del self._recent_denials[key]

        # 4. Already approved?
        if self._remembered(req.scope, req.target):
            self._record(req, True)
            return Decision(True, "Previously approved this session.", remembered=True)

        if mode == "allow":
            self._record(req, True)
            return Decision(True, "PERMISSION_MODE is set to allow.")

        # 5. Ask.
        return self._ask(req)

    def _ask(self, req: PermissionRequest) -> Decision:
        """Put the question to the operator through the HUD."""
        title = settings.USER_TITLE
        question = req.question()

        if self.hud is None:
            return Decision(
                False,
                "There is no console attached, so I could not ask for permission.",
            )

        # Say it out loud too -- this is a voice assistant, and a silent prompt during a
        # spoken conversation is a dead end.
        if settings.PERMISSION_SPEAK and self.voice is not None:
            try:
                self.voice.speak(question)
            except Exception:
                logger.debug("Could not speak the permission request", exc_info=True)

        asker = getattr(self.hud, "ask_permission", None)
        try:
            if callable(asker):
                answer = asker(req)
            else:  # a HUD without the richer panel still gets a plain confirm
                answer = "y" if self.hud.confirm(question) else "n"
        except (EOFError, KeyboardInterrupt):
            answer = "n"
        except Exception:
            logger.debug("Permission prompt failed", exc_info=True)
            answer = "n"

        answer = str(answer or "").strip().lower()

        if answer in _ALWAYS:
            grant = self._grant_for(req)
            with self._lock:
                self._grants.append(grant)
            self._record(req, True)
            scope_note = (
                f"everything under `{grant.target}`"
                if grant.prefix
                else f"`{grant.target}`"
            )
            return Decision(
                True, f"Approved, and I shall not ask again for {scope_note}.",
                remembered=True,
            )

        if answer in _YES:
            self._record(req, True)
            return Decision(True, "Approved for this one action.")

        if answer.startswith("!"):
            with self._lock:
                self._denied_scopes.add(req.scope)
            self._record(req, False)
            return Decision(
                False, f"Declined, {title}, and I will stop asking about that this session."
            )

        self._record(req, False)
        if answer and answer not in _NO:
            # Anything unrecognised is a refusal. Silence is not consent.
            return Decision(
                False,
                f"I did not read that as approval, {title}, so I have not proceeded.",
            )
        return Decision(False, f"Understood, {title}. I have left it alone.")

    @staticmethod
    def _grant_for(req: PermissionRequest) -> Grant:
        """What 'always' should mean for this scope.

        For a file, 'always' covers its directory rather than that single path --
        otherwise the operator is re-prompted for every file in the folder they just
        approved, which trains them to stop reading the prompt.
        """
        if req.scope == SCOPE_FILESYSTEM:
            try:
                path = Path(req.target).expanduser().resolve()
                parent = path if path.is_dir() else path.parent
                return Grant(req.scope, str(parent), prefix=True)
            except (OSError, ValueError):
                return Grant(req.scope, req.target)
        return Grant(req.scope, req.target)

    def _record(self, req: PermissionRequest, granted: bool) -> None:
        with self._lock:
            key = (req.scope, _norm(req.target))
            if granted:
                self._recent_denials.pop(key, None)
            else:
                self._recent_denials[key] = time.monotonic()
            self._log.append((req.scope, req.target, granted))
            if len(self._log) > 200:
                del self._log[:100]
        logger.info(
            "permission %s: %s %s -> %s",
            req.scope, req.action, req.target, "granted" if granted else "denied",
        )

    # -- convenience wrappers ----------------------------------------------------------

    def check_path(self, action: str, path: str | Path) -> Decision:
        """Approve a filesystem action, automatically for anything in the workspace."""
        try:
            target = Path(str(path)).expanduser().resolve()
            workspace = Path(settings.WORKSPACE_ROOT).resolve()
        except (OSError, ValueError) as exc:
            return Decision(False, f"That path is not usable: {exc}")

        inside = target == workspace
        if not inside:
            try:
                inside = target.is_relative_to(workspace)
            except (AttributeError, ValueError):
                inside = str(target).startswith(str(workspace))

        if inside:
            return Decision(True, "Inside the workspace.")

        return self.request(
            PermissionRequest(
                scope=SCOPE_FILESYSTEM,
                action=action,
                target=str(target),
                detail=f"Workspace is {workspace}",
                reversible=action not in {"delete", "write"},
            )
        )

    def check_launch(self, name: str, detail: str = "") -> Decision:
        return self.request(
            PermissionRequest(SCOPE_LAUNCH, "open", name, detail)
        )

    def check_process(self, name: str, detail: str = "") -> Decision:
        return self.request(
            PermissionRequest(SCOPE_PROCESS, "close", name, detail, reversible=False)
        )

    def check_shell(self, command: str, cwd: str = "") -> Decision:
        return self.request(
            PermissionRequest(SCOPE_SHELL, "run", command, f"in {cwd}" if cwd else "")
        )
