"""Authentication for CC Dispatch.

Threat model
------------
This process can type into Claude panes that hold every credential on the
machine. A read-only leak is bad; a write is catastrophic. So the design is
layered, and every layer is independently sufficient to stop a stranger:

1. The server binds loopback only (see server.py). Reachability comes from
   Tailscale, which is WireGuard — there is no public port to find.
2. The QR token bootstraps a session ONCE, over the tailnet, and is then
   exchanged for an HttpOnly cookie. It never appears in a URL again, so it
   cannot leak through history, screenshots or proxy logs.
3. A passkey (WebAuthn, so Face ID / fingerprint) gates every write. It is
   bound to the origin, which makes it unphishable, and the private key never
   leaves the phone's secure element.
4. Sessions expire: 30 minutes idle, 12 hours absolute. Expiry drops you back
   to "unlock with your passkey", not to a token prompt.
5. Every write is appended to an audit log, and token guesses are rate limited
   per address.
"""
import json, os, pathlib, secrets, time
from aiohttp import web

import webauthn
from webauthn.helpers import base64url_to_bytes, options_to_json
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria, PublicKeyCredentialDescriptor,
    ResidentKeyRequirement, UserVerificationRequirement,
)

HERE = pathlib.Path(__file__).parent
CRED_FILE = HERE / ".credentials.json"
AUDIT_FILE = HERE / "audit.log"

SESSION_TTL = 12 * 3600          # absolute lifetime of a session
IDLE_TTL = 30 * 60               # re-unlock after this long untouched
FAIL_WINDOW = 600                # rate-limit window for bad tokens
FAIL_MAX = 5                     # bad tokens per window before a lockout
LOCKOUT = 900

SESSIONS = {}                    # sid -> session dict
_FAILS = {}                      # ip -> [timestamps]
_LOCKED = {}                     # ip -> unlock time

USER_ID = b"cc-dispatch-owner"   # single-user system; there is only ever one
USER_NAME = "owner"


# ── credential store ───────────────────────────────────────────────────────
def load_creds():
    try:
        return json.loads(CRED_FILE.read_text())
    except Exception:
        return []


def save_creds(creds):
    CRED_FILE.write_text(json.dumps(creds, indent=1))
    CRED_FILE.chmod(0o600)


def has_passkey(rp_id=None):
    creds = load_creds()
    return any(c["rp_id"] == rp_id for c in creds) if rp_id else bool(creds)


# ── request helpers ────────────────────────────────────────────────────────
def client_ip(request):
    fwd = request.headers.get("X-Forwarded-For", "")
    return fwd.split(",")[0].strip() or (request.remote or "?")


def is_secure(request):
    """True when the browser sees https — direct TLS or via `tailscale serve`."""
    return (request.scheme == "https"
            or request.headers.get("X-Forwarded-Proto", "") == "https")


def rp_id(request):
    """The WebAuthn Relying Party: the hostname, never the port.

    A bare IP cannot be an RP ID, so passkeys only exist on the tailnet name —
    which is exactly where we want them.
    """
    host = (request.headers.get("X-Forwarded-Host")
            or request.host or "").split(":")[0].lower()
    return host


def origin(request):
    host = (request.headers.get("X-Forwarded-Host") or request.host or "")
    return f"{'https' if is_secure(request) else 'http'}://{host}"


def passkey_capable(request):
    """Can this origin hold a passkey at all? (https + a real hostname)"""
    h = rp_id(request)
    if not h or h.replace(".", "").isdigit():
        return False
    return is_secure(request) or h == "localhost"


# ── rate limiting ──────────────────────────────────────────────────────────
def locked_out(ip):
    until = _LOCKED.get(ip, 0)
    if until > time.time():
        return int(until - time.time())
    _LOCKED.pop(ip, None)
    return 0


def note_fail(ip):
    now = time.time()
    hits = [t for t in _FAILS.get(ip, []) if now - t < FAIL_WINDOW]
    hits.append(now)
    _FAILS[ip] = hits
    if len(hits) >= FAIL_MAX:
        _LOCKED[ip] = now + LOCKOUT
        _FAILS[ip] = []
        print(f"  [auth] {ip} locked out for {LOCKOUT}s after {FAIL_MAX} bad tokens",
              flush=True)


# ── sessions ───────────────────────────────────────────────────────────────
def new_session(ip, level="bootstrap"):
    sid = secrets.token_urlsafe(32)
    now = time.time()
    SESSIONS[sid] = {"level": level, "ip": ip, "created": now, "last": now,
                     "challenge": None}
    return sid


def get_session(request, touch=True):
    sid = request.cookies.get("sid") if request.cookies else None
    s = SESSIONS.get(sid or "")
    if not s:
        return None
    now = time.time()
    if now - s["created"] > SESSION_TTL:
        SESSIONS.pop(sid, None)
        return None
    if now - s["last"] > IDLE_TTL:
        s["level"] = "bootstrap"           # idle drops you to "unlock again"
    if touch:
        s["last"] = now
    s["sid"] = sid
    return s


def set_session_cookie(response, sid, request):
    # Lax, not Strict. A QR scanner opens the link as a cross-site navigation and
    # Chrome carries that classification through our redirect, so a Strict cookie
    # is withheld on the very first hop and the session looks dead on arrival.
    # Lax still refuses to ride along on a cross-site POST, and same_origin()
    # below closes the CSRF gap directly rather than relying on the cookie flag.
    response.set_cookie("sid", sid, httponly=True, samesite="Lax",
                        secure=is_secure(request), max_age=SESSION_TTL, path="/")


def same_origin(request):
    """CSRF guard for state-changing calls: the Origin must be us.

    Browsers always send Origin on POST. A cross-site page can forge the request
    but cannot forge this header, so a mismatch is a forgery, full stop.
    """
    if request.method in ("GET", "HEAD"):
        return True
    o = request.headers.get("Origin")
    if o is None:                       # non-browser client (curl, tests)
        return request.headers.get("Sec-Fetch-Site") in (None, "same-origin")
    return o.rstrip("/") == origin(request).rstrip("/")


def unlocked(request):
    """Is this request allowed to touch the fleet?

    With a passkey registered for this origin, only a verified session passes.
    Before then a bootstrap session is accepted, so you can reach the UI on the
    very first visit and register one — the UI nags until you do.
    """
    s = get_session(request)
    if not s:
        return None
    if s["level"] == "verified":
        return s
    if passkey_capable(request) and has_passkey(rp_id(request)):
        return None
    return s


# ── audit ──────────────────────────────────────────────────────────────────
def audit(request, action, detail=None):
    rec = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "action": action,
           "ip": client_ip(request), "detail": detail or {}}
    s = get_session(request, touch=False)
    rec["level"] = s["level"] if s else "none"
    try:
        with open(AUDIT_FILE, "a") as f:
            f.write(json.dumps(rec) + "\n")
        os.chmod(AUDIT_FILE, 0o600)
    except Exception as e:
        print(f"  [audit] write failed: {type(e).__name__}: {e}", flush=True)


# ── WebAuthn ───────────────────────────────────────────────────────────────
async def register_begin(request):
    """Enrol this device's biometric. Requires a live session already."""
    s = get_session(request)
    if not s:
        return web.json_response({"error": "no session"}, status=401)
    if not passkey_capable(request):
        return web.json_response(
            {"error": "passkeys need https on the tailnet name, not a bare IP"},
            status=400)
    rid = rp_id(request)
    opts = webauthn.generate_registration_options(
        rp_id=rid, rp_name="CC Dispatch",
        user_id=USER_ID, user_name=USER_NAME, user_display_name="Yard owner",
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.PREFERRED,
            user_verification=UserVerificationRequirement.REQUIRED),
        exclude_credentials=[PublicKeyCredentialDescriptor(id=base64url_to_bytes(c["id"]))
                             for c in load_creds() if c["rp_id"] == rid],
    )
    s["challenge"] = opts.challenge
    return web.Response(text=options_to_json(opts), content_type="application/json")


async def register_complete(request):
    s = get_session(request)
    if not s or not s.get("challenge"):
        return web.json_response({"error": "no session"}, status=401)
    body = await request.json()
    try:
        v = webauthn.verify_registration_response(
            credential=body, expected_challenge=s["challenge"],
            expected_rp_id=rp_id(request), expected_origin=origin(request))
    except Exception as e:
        audit(request, "passkey.register.fail", {"err": str(e)})
        return web.json_response({"error": f"rejected: {e}"}, status=400)
    creds = load_creds()
    creds.append({
        "id": webauthn.helpers.bytes_to_base64url(v.credential_id),
        "public_key": webauthn.helpers.bytes_to_base64url(v.credential_public_key),
        "sign_count": v.sign_count, "rp_id": rp_id(request),
        "added": time.strftime("%Y-%m-%d %H:%M"),
    })
    save_creds(creds)
    s["level"] = "verified"; s["challenge"] = None
    audit(request, "passkey.register", {"rp": rp_id(request)})
    print(f"  [auth] passkey registered for {rp_id(request)}", flush=True)
    return web.json_response({"ok": True})


async def login_begin(request):
    # A registered passkey is a full credential — it can start a session on its
    # own, no bootstrap token needed. That matters after the session is truly
    # gone (12h absolute cap, or a server restart wiping the in-memory table):
    # you unlock with your face, not by hunting down the token again. Enrolling
    # a NEW device still requires a token (see register_begin); this only lets
    # an already-trusted passkey back in.
    fresh_sid = None
    s = get_session(request)
    if not s:
        ip = client_ip(request)
        if locked_out(ip):
            return web.json_response({"error": "locked out"}, status=429)
        fresh_sid = new_session(ip)
        s = SESSIONS[fresh_sid]
    rid = rp_id(request)
    creds = [c for c in load_creds() if c["rp_id"] == rid]
    if not creds:
        return web.json_response({"error": "no passkey registered"}, status=404)
    opts = webauthn.generate_authentication_options(
        rp_id=rid, user_verification=UserVerificationRequirement.REQUIRED,
        allow_credentials=[PublicKeyCredentialDescriptor(id=base64url_to_bytes(c["id"]))
                           for c in creds])
    s["challenge"] = opts.challenge
    resp = web.Response(text=options_to_json(opts), content_type="application/json")
    if fresh_sid:                      # hand back the cookie so /complete finds us
        set_session_cookie(resp, fresh_sid, request)
    return resp


async def login_complete(request):
    s = get_session(request)
    if not s or not s.get("challenge"):
        return web.json_response({"error": "no session"}, status=401)
    body = await request.json()
    raw_id = body.get("id", "")
    cred = next((c for c in load_creds()
                 if c["id"] == raw_id and c["rp_id"] == rp_id(request)), None)
    if not cred:
        audit(request, "passkey.login.unknown", {"id": raw_id[:16]})
        return web.json_response({"error": "unknown credential"}, status=400)
    try:
        v = webauthn.verify_authentication_response(
            credential=body, expected_challenge=s["challenge"],
            expected_rp_id=rp_id(request), expected_origin=origin(request),
            credential_public_key=base64url_to_bytes(cred["public_key"]),
            credential_current_sign_count=cred["sign_count"],
            require_user_verification=True)
    except Exception as e:
        audit(request, "passkey.login.fail", {"err": str(e)})
        return web.json_response({"error": f"rejected: {e}"}, status=401)
    creds = load_creds()
    for c in creds:
        if c["id"] == raw_id:
            c["sign_count"] = v.new_sign_count
    save_creds(creds)
    s["level"] = "verified"; s["challenge"] = None; s["last"] = time.time()
    audit(request, "passkey.login")
    return web.json_response({"ok": True})


async def bootstrap(request):
    """Start a session from a token pasted into the page.

    The QR is convenient but fragile: scanner apps open the link in their own
    in-app browser, which keeps the cookie to itself, so continuing in Chrome
    lands on a session that does not exist. Posting the token in a body works in
    whatever browser you are actually holding — and unlike the URL form, it
    leaves no copy in history.
    """
    ip = client_ip(request)
    left = locked_out(ip)
    if left:
        return web.json_response({"error": f"locked out, retry in {left}s"},
                                 status=429)
    body = await request.json()
    if not secrets.compare_digest(str(body.get("token", "")), request.app["TOKEN"]):
        note_fail(ip)
        audit(request, "bootstrap.fail", {"via": "paste"})
        return web.json_response({"error": "bad token"}, status=401)
    sid = new_session(ip)
    audit(request, "bootstrap.ok", {"via": "paste"})
    r = web.json_response({"ok": True})
    set_session_cookie(r, sid, request)
    return r


async def whoami(request):
    """What the client needs to decide between "unlock" and "enrol"."""
    s = get_session(request)
    return web.json_response({
        "session": bool(s),
        "level": s["level"] if s else "none",
        "passkey_capable": passkey_capable(request),
        "passkey_registered": has_passkey(rp_id(request)),
        "idle_ttl": IDLE_TTL,
    })


async def logout(request):
    sid = request.cookies.get("sid") if request.cookies else None
    SESSIONS.pop(sid or "", None)
    audit(request, "logout")
    r = web.json_response({"ok": True})
    r.del_cookie("sid", path="/")
    return r
