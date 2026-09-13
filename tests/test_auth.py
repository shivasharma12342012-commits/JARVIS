"""The lock on the front door: the credential, the sessions, the Google flow.

Nothing here touches the operator's real credential file — every test is handed
a scratch path — and nothing reaches the network: the Google exchange is pointed
at a local stub that answers the way Google's token endpoint does.
"""

from __future__ import annotations

import base64
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from jarvis import auth


@pytest.fixture(autouse=True)
def scratch_credentials(tmp_path, monkeypatch):
    """A credential file per test, and a cheap scrypt so the suite stays quick."""
    monkeypatch.setattr(auth, "CREDENTIALS_PATH", tmp_path / "credentials.json")
    monkeypatch.setattr(auth, "SCRYPT_N", 2 ** 10)
    monkeypatch.setattr(auth, "SCRYPT_MAXMEM", 128 * (2 ** 10) * 8 * 2)
    from config import settings
    monkeypatch.setattr(settings, "DESKTOP_AUTH_MODE", "off", raising=False)
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_ID", "", raising=False)
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_SECRET", "", raising=False)
    monkeypatch.setattr(settings, "GOOGLE_ALLOWED_ACCOUNTS", "", raising=False)
    yield


# ══ the password ══════════════════════════════════════════════════════════════════════
def test_a_password_round_trips():
    auth.set_password("the correct password")
    assert auth.has_password() is True
    assert auth.verify_password("the correct password") is True
    assert auth.verify_password("the incorrect password") is False


def test_the_password_is_never_written_down():
    """The whole reason this is a hash in a file and not a literal in a module."""
    auth.set_password("a memorable phrase")
    raw = auth.CREDENTIALS_PATH.read_text(encoding="utf-8")
    assert "a memorable phrase" not in raw
    record = json.loads(raw)["password"]
    assert record["algorithm"] == "scrypt"
    assert set(record) >= {"salt", "key", "n", "r", "p"}


def test_the_same_password_hashes_differently_every_time():
    """A per-record salt, so two machines with the same password share nothing."""
    auth.set_password("identical")
    first = json.loads(auth.CREDENTIALS_PATH.read_text())["password"]
    auth.set_password("identical")
    second = json.loads(auth.CREDENTIALS_PATH.read_text())["password"]
    assert first["salt"] != second["salt"]
    assert first["key"] != second["key"]


def test_a_password_can_be_removed():
    auth.set_password("temporary one")
    auth.clear_password()
    assert auth.has_password() is False
    assert auth.verify_password("temporary one") is False


def test_a_very_short_password_is_refused():
    with pytest.raises(ValueError):
        auth.set_password("123")
    assert auth.has_password() is False


def test_a_four_character_pin_is_allowed():
    """This is a lock on a laptop, not a login to a service. A person who will
    not set a PIN sets nothing at all, and six guesses a minute makes ten
    thousand combinations about a day's work."""
    auth.set_password("1234")
    assert auth.verify_password("1234") is True


def test_verifying_against_nothing_is_false_rather_than_an_error():
    assert auth.verify_password("anything at all") is False


def test_a_corrupt_record_refuses_rather_than_crashes():
    auth.CREDENTIALS_PATH.write_text(json.dumps({"password": {"algorithm": "scrypt",
                                                              "salt": "not hex", "key": "nor this"}}))
    assert auth.verify_password("anything") is False


def test_an_unreadable_credential_file_is_treated_as_absent():
    auth.CREDENTIALS_PATH.write_text("{ this is not json")
    assert auth.has_password() is False


# ══ the lockout ═══════════════════════════════════════════════════════════════════════
def test_the_throttle_closes_the_door_and_reopens_it():
    throttle = auth.Throttle(limit=3, window=0.4)
    assert throttle.locked_for() == 0
    for _ in range(3):
        throttle.fail()
    assert throttle.locked_for() > 0
    time.sleep(0.5)
    assert throttle.locked_for() == 0


def test_a_success_forgives_the_failures_before_it():
    throttle = auth.Throttle(limit=3, window=60.0)
    throttle.fail()
    throttle.fail()
    throttle.succeed()
    throttle.fail()
    assert throttle.locked_for() == 0


# ══ sessions ══════════════════════════════════════════════════════════════════════════
def test_a_session_resolves_to_who_minted_it():
    sessions = auth.Sessions(ttl=60.0)
    identity = auth.Identity(method="password", subject="operator")
    token = sessions.mint(identity)
    assert sessions.resolve(token) == identity


def test_an_unknown_or_missing_token_resolves_to_nobody():
    sessions = auth.Sessions(ttl=60.0)
    assert sessions.resolve("never minted") is None
    assert sessions.resolve("") is None
    assert sessions.resolve(None) is None


def test_two_sessions_do_not_share_a_token():
    sessions = auth.Sessions(ttl=60.0)
    first = sessions.mint(auth.Identity("password", "a"))
    second = sessions.mint(auth.Identity("google", "b@example.com"))
    assert first != second
    assert sessions.resolve(first).subject == "a"
    assert sessions.resolve(second).subject == "b@example.com"


def test_a_session_expires():
    sessions = auth.Sessions(ttl=0.2)
    token = sessions.mint(auth.Identity("password", "operator"))
    time.sleep(0.3)
    assert sessions.resolve(token) is None
    assert sessions.count == 0


def test_a_session_can_be_revoked():
    sessions = auth.Sessions(ttl=60.0)
    token = sessions.mint(auth.Identity("password", "operator"))
    assert sessions.revoke(token) is True
    assert sessions.resolve(token) is None
    assert sessions.revoke(token) is False


# ══ the mode ══════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize(
    "mode, password, google",
    [("off", False, False), ("password", True, False),
     ("google", False, True), ("any", True, True)],
)
def test_the_mode_decides_what_the_lock_screen_offers(monkeypatch, mode, password, google):
    from config import settings

    monkeypatch.setenv("DESKTOP_AUTH_MODE", mode)
    monkeypatch.setattr(settings, "DESKTOP_AUTH_MODE", mode, raising=False)
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_ID", "client.apps.googleusercontent.com",
                        raising=False)
    offers = auth.offers()
    assert offers["password"] is password
    assert offers["google"] is google
    assert auth.required() is (mode != "off")


def test_google_is_not_offered_without_a_client_id(monkeypatch):
    from config import settings

    monkeypatch.setenv("DESKTOP_AUTH_MODE", "any")
    monkeypatch.setattr(settings, "DESKTOP_AUTH_MODE", "any", raising=False)
    assert auth.offers()["google"] is False


def test_an_unknown_mode_falls_back_to_off(monkeypatch):
    from config import settings

    monkeypatch.setenv("DESKTOP_AUTH_MODE", "nonsense")
    monkeypatch.setattr(settings, "DESKTOP_AUTH_MODE", "nonsense", raising=False)
    assert auth.mode() == "off"
    assert auth.required() is False


# ══ Google ════════════════════════════════════════════════════════════════════════════
CLIENT_ID = "1234567890.apps.googleusercontent.com"
REDIRECT = "http://127.0.0.1:9999/api/auth/google/callback"


def _jwt(claims: dict) -> str:
    """An unsigned ID token. The signature is never checked — see ``auth._claims``."""
    part = lambda data: base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()
    return part({"alg": "RS256"}) + "." + part(claims) + ".signature-goes-here"


def _claims(**overrides) -> dict:
    base = {
        "aud": CLIENT_ID, "iss": "https://accounts.google.com",
        "exp": time.time() + 600, "email": "operator@example.com",
        "email_verified": True, "name": "The Operator",
    }
    base.update(overrides)
    return base


@pytest.fixture
def google(monkeypatch):
    """A Google flow pointed at a stub token endpoint on loopback."""
    from config import settings

    monkeypatch.setattr(settings, "GOOGLE_CLIENT_ID", CLIENT_ID, raising=False)
    replies: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 - base class name
            length = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(length)
            body = json.dumps(replies.pop(0) if replies else {}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # noqa: A003 - base class name
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(auth, "GOOGLE_TOKEN_URL",
                        f"http://127.0.0.1:{server.server_address[1]}/token")
    flow = auth.Google()
    flow.replies = replies                       # what the stub will answer with
    yield flow
    server.shutdown()
    server.server_close()


def test_the_authorisation_url_carries_pkce(google):
    from urllib.parse import parse_qs, urlparse

    query = parse_qs(urlparse(google.start(REDIRECT)).query)
    assert query["client_id"] == [CLIENT_ID]
    assert query["redirect_uri"] == [REDIRECT]
    assert query["response_type"] == ["code"]
    assert query["code_challenge_method"] == ["S256"]
    assert len(query["code_challenge"][0]) > 20
    assert "openid" in query["scope"][0] and "email" in query["scope"][0]
    # The verifier itself must never appear in a URL the browser sees.
    assert "code_verifier" not in query


def test_a_callback_that_did_not_start_here_is_refused(google):
    google.start(REDIRECT)
    identity, why = google.finish("some-code", "a state we never issued", REDIRECT)
    assert identity is None and "did not start here" in why


def test_a_state_cannot_be_replayed(google):
    from urllib.parse import parse_qs, urlparse

    state = parse_qs(urlparse(google.start(REDIRECT)).query)["state"][0]
    google.replies.append({"id_token": _jwt(_claims())})
    first, _ = google.finish("code", state, REDIRECT)
    assert first is not None
    google.replies.append({"id_token": _jwt(_claims())})
    second, why = google.finish("code", state, REDIRECT)
    assert second is None and "did not start here" in why


def _sign_in(google, claims=None, reply=None):
    from urllib.parse import parse_qs, urlparse

    state = parse_qs(urlparse(google.start(REDIRECT)).query)["state"][0]
    google.replies.append(reply if reply is not None
                          else {"id_token": _jwt(claims or _claims())})
    return google.finish("an-authorisation-code", state, REDIRECT)


def test_a_good_exchange_yields_an_identity(google):
    identity, why = _sign_in(google)
    assert why == ""
    assert identity.method == "google"
    assert identity.subject == "operator@example.com"
    assert identity.name == "The Operator"


def test_a_token_for_another_client_is_refused(google):
    identity, why = _sign_in(google, _claims(aud="somebody-elses-client"))
    assert identity is None and "usable identity" in why


def test_a_token_from_the_wrong_issuer_is_refused(google):
    identity, why = _sign_in(google, _claims(iss="https://accounts.evil.example"))
    assert identity is None


def test_an_expired_token_is_refused(google):
    identity, why = _sign_in(google, _claims(exp=time.time() - 10))
    assert identity is None


def test_an_unverified_address_is_refused(google):
    identity, why = _sign_in(google, _claims(email_verified=False))
    assert identity is None and "not verified" in why


def test_a_reply_with_no_token_is_refused(google):
    identity, why = _sign_in(google, reply={"access_token": "but no id_token"})
    assert identity is None


def test_the_first_account_to_sign_in_claims_the_window(google):
    identity, _ = _sign_in(google)
    assert identity is not None
    assert auth.allowed_google_accounts() == ["operator@example.com"]
    # And from then on, nobody else.
    other, why = _sign_in(google, _claims(email="stranger@example.com"))
    assert other is None and "not allowed" in why


def test_an_explicit_allowlist_wins_over_the_remembered_one(google, monkeypatch):
    from config import settings

    auth.remember_google_account("someone@example.com")
    monkeypatch.setattr(settings, "GOOGLE_ALLOWED_ACCOUNTS",
                        "operator@example.com, other@example.com", raising=False)
    assert auth.allowed_google_accounts() == ["operator@example.com", "other@example.com"]
    identity, _ = _sign_in(google)
    assert identity is not None
    refused, why = _sign_in(google, _claims(email="someone@example.com"))
    assert refused is None and "not allowed" in why


def test_finishing_without_a_client_id_says_so(monkeypatch):
    from config import settings

    monkeypatch.setattr(settings, "GOOGLE_CLIENT_ID", "", raising=False)
    identity, why = auth.Google().finish("code", "state", REDIRECT)
    assert identity is None and "not configured" in why


# ══ the lock follows what is set up ═══════════════════════════════════════════════════
# The whole model for somebody who never opens a terminal: set a password and
# the window locks; take it off and it does not. No mode to choose, no file to
# edit, no restart.
def test_setting_a_password_locks_the_window():
    assert auth.required() is False
    auth.set_password("1234")
    assert auth.mode() == "password"
    assert auth.required() is True


def test_removing_it_unlocks_the_window():
    auth.set_password("1234")
    auth.clear_password()
    assert auth.mode() == "off"
    assert auth.required() is False


def test_a_google_client_alone_locks_the_window_too():
    auth.set_google_client("abc.apps.googleusercontent.com")
    assert auth.mode() == "google"
    assert auth.google_client_id() == "abc.apps.googleusercontent.com"


def test_both_set_up_means_either_will_do():
    auth.set_password("1234")
    auth.set_google_client("abc.apps.googleusercontent.com")
    assert auth.mode() == "any"


def test_a_choice_made_in_the_panel_is_remembered():
    auth.set_password("1234")
    auth.set_mode("off")
    assert auth.mode() == "off"
    assert auth.required() is False
    auth.set_mode("password")
    assert auth.required() is True


def test_a_mode_written_down_beats_the_panel(monkeypatch):
    """Somebody who wrote it in .env meant it, and a panel must not overrule them."""
    auth.set_password("1234")
    auth.set_mode("off")
    monkeypatch.setenv("DESKTOP_AUTH_MODE", "password")
    from config import settings
    monkeypatch.setattr(settings, "DESKTOP_AUTH_MODE", "password", raising=False)
    assert auth.mode() == "password"
    assert auth.describe()["managed"] is True


def test_the_panel_knows_when_it_is_not_in_charge(monkeypatch):
    assert auth.describe()["managed"] is False
    monkeypatch.setenv("DESKTOP_AUTH_MODE", "off")
    assert auth.describe()["managed"] is True


def test_an_unknown_mode_cannot_be_stored():
    with pytest.raises(ValueError):
        auth.set_mode("sideways")


def test_a_google_client_can_be_pasted_in_and_taken_out():
    auth.set_google_client("abc.apps.googleusercontent.com", "a-secret")
    assert auth.Google().client_secret == "a-secret"
    auth.clear_google_client()
    assert auth.google_client_id() == ""
    assert auth.Google().configured is False


def test_the_settings_client_wins_over_a_pasted_one(monkeypatch):
    from config import settings

    auth.set_google_client("pasted.apps.googleusercontent.com")
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_ID", "env.apps.googleusercontent.com",
                        raising=False)
    assert auth.google_client_id() == "env.apps.googleusercontent.com"
    assert auth.describe()["googleManaged"] is True


def test_keep_me_signed_in_lasts_longer_than_a_working_day():
    sessions = auth.Sessions(ttl=3600.0)
    assert sessions.remembered_ttl >= 7 * 24 * 3600.0
    remembered = sessions.mint(auth.Identity("password", "operator"), remember=True)
    assert sessions.resolve(remembered) is not None
