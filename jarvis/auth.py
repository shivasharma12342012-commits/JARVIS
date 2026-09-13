"""Who is allowed to open the window.

The desktop server has always been bound to loopback and gated on a per-session
token, which answers *"can something on the network reach this?"* It has never
answered *"is this the person who started it?"* — anyone who walks up to an
unlocked machine gets a shell, a file browser and an editor. This module is that
second answer: a lock on the front door, in two forms.

**A password.** Stored as a salted scrypt hash in a file beside the profile,
never as plaintext and never in source. That is deliberate and worth stating
plainly: this repository is pushed to GitHub, so a password written into a
Python file is a password published to the internet — and the same one people
reuse elsewhere. ``python main.py set-password`` writes the hash; nothing ever
writes the password.

**Google.** OAuth 2.0 with PKCE on a loopback redirect, which is the flow Google
documents for native applications. The authorisation code is exchanged by this
process, directly with Google's token endpoint, over TLS. Because the ID token
therefore arrives from Google over an authenticated channel rather than through
the browser, its signature does not need separate verification — that is Google's
own documented exception, and it is what lets this stay inside the standard
library rather than pulling in a JWT stack for one check. The claims it carries
are still checked: audience, issuer, expiry, and that the address is verified.

Neither is a replacement for the token and the loopback bind. It is a third
layer, and it is off until someone turns it on.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from config import PROJECT_ROOT, settings

LOG = logging.getLogger("jarvis.auth")

#: Where the credential lives. Beside the profile, and ignored by git for the
#: same reason the profile is: it is this machine's, not the project's.
CREDENTIALS_PATH = PROJECT_ROOT / ".jarvis_credentials.json"

#: scrypt cost. 2**15 takes roughly a tenth of a second on a laptop, which is
#: unnoticeable once per sign-in and ruinous a billion times over.
SCRYPT_N = 2 ** 15
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32
#: scrypt needs about 128 * N * r bytes. Asked for explicitly, because the
#: default ceiling is lower than this on some builds and the call simply fails.
SCRYPT_MAXMEM = 128 * SCRYPT_N * SCRYPT_R * 2

#: Wrong guesses before the door stops answering, and for how long.
MAX_ATTEMPTS = 6
LOCKOUT_SECONDS = 60.0

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_ISSUERS = ("accounts.google.com", "https://accounts.google.com")
#: A round trip to Google should not hang the window if the network is down.
GOOGLE_TIMEOUT = 20.0


@dataclass(frozen=True)
class Identity:
    """Who signed in, and how."""

    method: str          #: "password", "google", or "open" when the lock is off
    subject: str         #: an email address for Google, "operator" for a password
    name: str = ""       #: a display name, when there is one
    picture: str = ""    #: an avatar URL, when there is one

    def to_dict(self) -> dict[str, str]:
        return {"method": self.method, "subject": self.subject,
                "name": self.name, "picture": self.picture}


OPEN = Identity(method="open", subject="operator", name="")


# ══════════════════════════════════════════════════════════════════════════════════════
# The stored credential
# ══════════════════════════════════════════════════════════════════════════════════════
def _read() -> dict[str, Any]:
    try:
        if CREDENTIALS_PATH.exists():
            data = json.loads(CREDENTIALS_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except (OSError, ValueError):
        LOG.warning("The credential file could not be read; treating it as absent")
    return {}


def _write(data: dict[str, Any]) -> bool:
    try:
        CREDENTIALS_PATH.parent.mkdir(parents=True, exist_ok=True)
        CREDENTIALS_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
        # Readable only by its owner. Best effort: Windows has no such mode, and
        # a failure here is not worth refusing to store the credential over.
        try:
            os.chmod(CREDENTIALS_PATH, 0o600)
        except OSError:
            pass
        return True
    except OSError:
        LOG.error("The credential file could not be written")
        return False


def hash_password(plain: str) -> dict[str, Any]:
    """A fresh salt and the derived key, as the record that gets stored."""
    salt = secrets.token_bytes(16)
    key = hashlib.scrypt(plain.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R,
                         p=SCRYPT_P, dklen=SCRYPT_DKLEN, maxmem=SCRYPT_MAXMEM)
    return {
        "algorithm": "scrypt", "n": SCRYPT_N, "r": SCRYPT_R, "p": SCRYPT_P,
        "salt": salt.hex(), "key": key.hex(), "set": int(time.time()),
    }


def set_password(plain: str) -> bool:
    """Store a password. The password itself is not kept, only a hash of it."""
    if len(plain) < 8:
        raise ValueError("a password shorter than eight characters is not worth having")
    data = _read()
    data["password"] = hash_password(plain)
    return _write(data)


def clear_password() -> bool:
    data = _read()
    data.pop("password", None)
    return _write(data)


def has_password() -> bool:
    record = _read().get("password")
    return isinstance(record, dict) and bool(record.get("key"))


def verify_password(plain: str) -> bool:
    """Whether ``plain`` is the stored password. Constant time on the compare."""
    record = _read().get("password")
    if not isinstance(record, dict) or record.get("algorithm") != "scrypt":
        return False
    try:
        salt = bytes.fromhex(str(record["salt"]))
        expected = bytes.fromhex(str(record["key"]))
        candidate = hashlib.scrypt(
            plain.encode("utf-8"), salt=salt,
            n=int(record.get("n", SCRYPT_N)), r=int(record.get("r", SCRYPT_R)),
            p=int(record.get("p", SCRYPT_P)), dklen=len(expected),
            maxmem=SCRYPT_MAXMEM,
        )
    except (KeyError, ValueError, TypeError):
        LOG.warning("The stored password record is malformed")
        return False
    return hmac.compare_digest(candidate, expected)


def allowed_google_accounts() -> list[str]:
    """The addresses Google sign-in will accept.

    From settings when set, from the credential file otherwise. An empty list
    means the first account to sign in claims the window, and is remembered —
    which is the right default for a single-operator desktop application and
    the wrong one for anything shared, hence the setting.
    """
    configured = [
        address.strip().lower()
        for address in str(getattr(settings, "GOOGLE_ALLOWED_ACCOUNTS", "") or "").replace(",", " ").split()
        if address.strip()
    ]
    if configured:
        return configured
    stored = _read().get("google", {})
    if isinstance(stored, dict):
        return [str(a).lower() for a in stored.get("allowed", []) if a]
    return []


def remember_google_account(email: str) -> None:
    """Claim the window for the first account that signs in."""
    data = _read()
    google = data.get("google") if isinstance(data.get("google"), dict) else {}
    allowed = [str(a).lower() for a in google.get("allowed", []) if a]
    if email.lower() in allowed:
        return
    allowed.append(email.lower())
    google["allowed"] = allowed
    data["google"] = google
    _write(data)
    LOG.info("Google sign-in is now bound to %s", email)


# ══════════════════════════════════════════════════════════════════════════════════════
# Signed-in sessions
# ══════════════════════════════════════════════════════════════════════════════════════
class Sessions:
    """The set of browsers currently signed in.

    In memory only, and deliberately: a restart signs everyone out, which is the
    behaviour you want from a lock on a desktop application. Nothing here is
    written to disk, so there is no session file to steal.
    """

    def __init__(self, ttl: float | None = None) -> None:
        self._lock = threading.Lock()
        self._live: dict[str, tuple[Identity, float]] = {}
        self._ttl = ttl

    @property
    def ttl(self) -> float:
        if self._ttl is not None:
            return self._ttl
        return float(getattr(settings, "DESKTOP_AUTH_TTL_HOURS", 12)) * 3600.0

    def mint(self, identity: Identity) -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._sweep()
            self._live[token] = (identity, time.monotonic() + self.ttl)
        LOG.info("signed in: %s via %s", identity.subject, identity.method)
        return token

    def resolve(self, token: str | None) -> Identity | None:
        if not token:
            return None
        with self._lock:
            self._sweep()
            found = self._live.get(token)
        return found[0] if found else None

    def revoke(self, token: str | None) -> bool:
        if not token:
            return False
        with self._lock:
            return self._live.pop(token, None) is not None

    def revoke_all(self) -> None:
        with self._lock:
            self._live.clear()

    def _sweep(self) -> None:
        now = time.monotonic()
        for token in [t for t, (_, expiry) in self._live.items() if expiry <= now]:
            self._live.pop(token, None)

    @property
    def count(self) -> int:
        with self._lock:
            self._sweep()
            return len(self._live)


class Throttle:
    """A lockout after too many wrong guesses.

    Not much of a defence against a patient attacker with local access, who has
    better options than the login form. It is a defence against the guessable
    password, which is the realistic threat: six tries a minute is not enough to
    walk a dictionary.
    """

    def __init__(self, limit: int = MAX_ATTEMPTS, window: float = LOCKOUT_SECONDS) -> None:
        self._lock = threading.Lock()
        self._failures: list[float] = []
        self.limit = limit
        self.window = window

    def locked_for(self) -> float:
        """Seconds until the door answers again; zero when it is answering now."""
        with self._lock:
            now = time.monotonic()
            self._failures = [t for t in self._failures if now - t < self.window]
            if len(self._failures) < self.limit:
                return 0.0
            return max(0.0, self.window - (now - self._failures[0]))

    def fail(self) -> None:
        with self._lock:
            self._failures.append(time.monotonic())

    def succeed(self) -> None:
        with self._lock:
            self._failures.clear()


# ══════════════════════════════════════════════════════════════════════════════════════
# Google
# ══════════════════════════════════════════════════════════════════════════════════════
def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


class Google:
    """The authorisation-code flow with PKCE, for a loopback redirect.

    One pending request at a time, which is all a desktop window ever has. The
    verifier never leaves this process and the state is compared in constant
    time, so a page that guesses the callback URL cannot complete somebody
    else's sign-in.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: dict[str, tuple[str, float]] = {}   # state -> (verifier, expiry)

    @property
    def client_id(self) -> str:
        return str(getattr(settings, "GOOGLE_CLIENT_ID", "") or "").strip()

    @property
    def client_secret(self) -> str:
        return str(getattr(settings, "GOOGLE_CLIENT_SECRET", "") or "").strip()

    @property
    def configured(self) -> bool:
        """Whether there is a client ID to sign in against."""
        return bool(self.client_id)

    def start(self, redirect_uri: str) -> str:
        """The URL to send the browser to, with a fresh PKCE pair remembered."""
        verifier = _b64url(secrets.token_bytes(48))
        challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
        state = _b64url(secrets.token_bytes(24))
        with self._lock:
            now = time.monotonic()
            self._pending = {s: v for s, v in self._pending.items() if v[1] > now}
            self._pending[state] = (verifier, now + 600.0)

        query = urllib.parse.urlencode({
            "client_id": self.client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": "openid email profile",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
            "access_type": "online",
            # Always ask which account, rather than silently reusing whichever
            # one the browser happens to be signed into.
            "prompt": "select_account",
        })
        return GOOGLE_AUTH_URL + "?" + query

    def finish(self, code: str, state: str, redirect_uri: str) -> tuple[Identity | None, str]:
        """Exchange the code for an identity, or say why not."""
        if not self.configured:
            return None, "Google sign-in is not configured"

        with self._lock:
            found = None
            for known, (verifier, expiry) in list(self._pending.items()):
                if secrets.compare_digest(known, state or ""):
                    found = verifier
                    self._pending.pop(known, None)
                    break
        if found is None:
            return None, "that sign-in did not start here, or it expired"

        payload = {
            "client_id": self.client_id,
            "code": code,
            "code_verifier": found,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        }
        # Desktop clients are issued a secret too. It is not a secret in any
        # meaningful sense on a user's machine — PKCE is what actually protects
        # this flow — but Google's token endpoint wants it when the client was
        # registered as one that has it.
        if self.client_secret:
            payload["client_secret"] = self.client_secret

        try:
            request = urllib.request.Request(
                GOOGLE_TOKEN_URL,
                data=urllib.parse.urlencode(payload).encode("utf-8"),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=GOOGLE_TIMEOUT) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            detail = ""
            try:
                detail = json.loads(error.read().decode("utf-8")).get("error_description", "")
            except Exception:
                pass
            LOG.warning("Google refused the code exchange: %s %s", error.code, detail)
            return None, detail or f"Google refused that sign-in ({error.code})"
        except (urllib.error.URLError, OSError, ValueError) as error:
            LOG.warning("Could not reach Google's token endpoint: %s", error)
            return None, "could not reach Google"

        claims = self._claims(body.get("id_token", ""))
        if claims is None:
            return None, "Google's reply did not carry a usable identity"

        email = str(claims.get("email", "")).lower()
        if not email:
            return None, "that Google account has no address on it"
        if not claims.get("email_verified", False):
            return None, "that Google address is not verified"

        allowed = allowed_google_accounts()
        if allowed and email not in allowed:
            LOG.warning("refused Google sign-in for %s", email)
            return None, f"{email} is not allowed to open this window"
        if not allowed:
            remember_google_account(email)

        return Identity(method="google", subject=email,
                        name=str(claims.get("name", "")),
                        picture=str(claims.get("picture", ""))), ""

    def _claims(self, id_token: str) -> dict[str, Any] | None:
        """The ID token's payload, checked but not signature-verified.

        The token came back on the connection this process opened to Google's
        token endpoint over TLS, which is the case Google's own documentation
        names as not requiring local signature validation. What still has to be
        checked is that the token is for *this* client and has not expired.
        """
        parts = str(id_token).split(".")
        if len(parts) != 3:
            return None
        try:
            claims = json.loads(_b64url_decode(parts[1]).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None
        if not isinstance(claims, dict):
            return None
        if claims.get("aud") != self.client_id:
            LOG.warning("Google ID token is for a different client")
            return None
        if str(claims.get("iss", "")) not in GOOGLE_ISSUERS:
            LOG.warning("Google ID token has an unexpected issuer")
            return None
        try:
            if float(claims.get("exp", 0)) < time.time():
                LOG.warning("Google ID token has expired")
                return None
        except (TypeError, ValueError):
            return None
        return claims


# ══════════════════════════════════════════════════════════════════════════════════════
# What the server asks
# ══════════════════════════════════════════════════════════════════════════════════════
def mode() -> str:
    """``off``, ``password``, ``google`` or ``any``."""
    chosen = str(getattr(settings, "DESKTOP_AUTH_MODE", "off") or "off").strip().lower()
    return chosen if chosen in ("off", "password", "google", "any") else "off"


def required() -> bool:
    """Whether the window asks who you are before it opens."""
    return mode() != "off"


def offers() -> dict[str, bool]:
    """Which ways in the sign-in page should show."""
    which = mode()
    google = Google()
    return {
        "password": which in ("password", "any"),
        "google": which in ("google", "any") and google.configured,
        # First run with the lock on and nothing set: the page says so rather
        # than presenting a form that can never be satisfied.
        "passwordSet": has_password(),
        "googleConfigured": google.configured,
    }


def describe() -> dict[str, Any]:
    """Everything the sign-in page needs to draw itself."""
    return dict(offers(), mode=mode(), required=required())
