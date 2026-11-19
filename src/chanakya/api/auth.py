"""Local accounts for the HTTP API, matching the harness UI's auth contract (contracts/api.md "Auth").

PBKDF2-SHA256 passwords, random session tokens stored only as SHA-256 hashes, httpOnly cookie `chanakya_session`
(14 days, sliding), 5 failed logins per email in 15 min -> 429. The first account is the owner.
The signed-in user's name is passed as the reviewer to every approval made through the UI.
`CHANAKYA_AUTH=off` disables the check (tests / scripts only).
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import os
import secrets

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import JSONResponse

from chanakya.api.db import db

COOKIE = "chanakya_session"
DAYS = 14
ITER = 240_000
PUBLIC = {"/api/auth/status", "/api/auth/setup", "/api/auth/login", "/api/health"}
router = APIRouter(prefix="/api/auth")


def enabled() -> bool:
    return os.getenv("CHANAKYA_AUTH", "on").lower() != "off"


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def hash_password(pw: str) -> str:
    salt = secrets.token_bytes(16)
    return f"pbkdf2_sha256${ITER}${salt.hex()}${hashlib.pbkdf2_hmac('sha256', pw.encode(), salt, ITER).hex()}"


def verify_password(pw: str, stored: str) -> bool:
    try:
        _, it, salt, h = stored.split("$")
        got = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), int(it)).hex()
        return hmac.compare_digest(got, h)
    except ValueError:
        return False


def password_problem(pw) -> str | None:
    if not isinstance(pw, str) or len(pw) < 10:
        return "Use at least 10 characters."
    classes = {("l" if c.islower() else "u" if c.isupper() else "d" if c.isdigit() else "o") for c in pw}
    return None if len(classes) >= 2 else "Mix letters with numbers or symbols."


def public_user(u) -> dict | None:
    if u is None:
        return None
    return {"id": u["id"], "email": u["email"], "name": u["name"], "role": u["role"], "disabled": bool(u["disabled"]),
            "created": u["created"], "last_login": u["last_login"]}


def _th(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def session_user(token: str | None):
    if not token:
        return None
    with db() as con:
        r = con.execute("SELECT s.id sid, s.expires, u.* FROM ui_sessions s JOIN ui_users u ON u.id=s.user_id WHERE "
                        "s.token_hash=?", (_th(token),)).fetchone()
        if r is None or r["disabled"] or r["expires"] < _now().isoformat():
            return None
        con.execute("UPDATE ui_sessions SET last_seen=?, expires=? WHERE id=?",
                    (_now().isoformat(), (_now() + dt.timedelta(days=DAYS)).isoformat(), r["sid"]))
        con.commit()
        return r


def current_user(request: Request) -> dict:
    """The reviewer identity for approvals. With auth off: CHANAKYA_REVIEWER or 'local-user'."""
    if not enabled():
        return {"id": 0, "name": os.getenv("CHANAKYA_REVIEWER", "local-user"), "email": "", "role": "owner"}
    u = session_user(request.cookies.get(COOKIE))
    if u is None:
        raise HTTPException(401, "Sign in to continue")
    return dict(u)


async def middleware(request: Request, call_next):
    p = request.url.path
    if enabled() and p.startswith("/api/") and p not in PUBLIC and session_user(request.cookies.get(COOKIE)) is None:
        return JSONResponse({"detail": "Sign in to continue"}, status_code=401)
    return await call_next(request)


def _with_cookie(request: Request, content: dict, uid: int) -> JSONResponse:
    token = secrets.token_urlsafe(32)
    with db() as con:
        con.execute("INSERT INTO ui_sessions(user_id,token_hash,created,last_seen,expires,user_agent) "
                    "VALUES(?,?,?,?,?,?)", (uid, _th(token), _now().isoformat(), _now().isoformat(),
                                            (_now() + dt.timedelta(days=DAYS)).isoformat(),
                                            request.headers.get("user-agent", "")[:300]))
        con.commit()
    r = JSONResponse(content)
    r.set_cookie(COOKIE, token, max_age=DAYS * 86400, httponly=True, samesite="lax", path="/",
                 secure=request.url.scheme == "https")
    return r


@router.get("/status")
def status(request: Request):
    with db() as con:
        n = con.execute("SELECT COUNT(*) FROM ui_users").fetchone()[0]
    u = session_user(request.cookies.get(COOKIE)) if enabled() else None
    return {"setup_required": n == 0 and enabled(), "user": public_user(u)}


@router.post("/setup")
def setup(request: Request, body: dict = Body(...)):
    name, email, pw = (body.get("name") or "").strip(), (body.get("email") or "").strip().lower(), body.get("password")
    if not name or "@" not in email:
        raise HTTPException(400, "Name and a valid email are required.")
    if (p := password_problem(pw)):
        raise HTTPException(400, p)
    with db() as con:
        if con.execute("SELECT COUNT(*) FROM ui_users").fetchone()[0]:
            raise HTTPException(409, "An owner account already exists.")
        uid = con.execute("INSERT INTO ui_users(email,name,role,password_hash,created,last_login) VALUES(?,?,?,?,?,?)",
                          (email, name, "owner", hash_password(pw), _now().isoformat(), _now().isoformat())).lastrowid
        con.commit()
        u = con.execute("SELECT * FROM ui_users WHERE id=?", (uid,)).fetchone()
    return _with_cookie(request, {"user": public_user(u)}, uid)


@router.post("/login")
def login(request: Request, body: dict = Body(...)):
    email, pw = (body.get("email") or "").strip().lower(), body.get("password") or ""
    since = (_now() - dt.timedelta(minutes=15)).isoformat()
    with db() as con:
        if con.execute("SELECT COUNT(*) FROM ui_login_failures WHERE email=? AND at>?", (email, since)).fetchone()[0] >= 5:
            raise HTTPException(429, "Too many attempts, try again in 15 minutes.")
        u = con.execute("SELECT * FROM ui_users WHERE email=?", (email,)).fetchone()
        if u is None or not verify_password(pw, u["password_hash"]):
            con.execute("INSERT INTO ui_login_failures(email,at) VALUES(?,?)", (email, _now().isoformat()))
            con.commit()
            raise HTTPException(401, "Wrong email or password.")
        if u["disabled"]:
            raise HTTPException(403, "This account is disabled.")
        con.execute("UPDATE ui_users SET last_login=? WHERE id=?", (_now().isoformat(), u["id"]))
        con.commit()
    return _with_cookie(request, {"user": public_user(u)}, u["id"])


@router.post("/logout")
def logout(request: Request):
    t = request.cookies.get(COOKIE)
    if t:
        with db() as con:
            con.execute("DELETE FROM ui_sessions WHERE token_hash=?", (_th(t),))
            con.commit()
    r = JSONResponse({"ok": True})
    r.delete_cookie(COOKIE, path="/")
    return r


@router.get("/me")
def me(request: Request):
    u = current_user(request)
    return {"user": public_user(u) if "password_hash" in u else u}


@router.post("/password")
def change_password(request: Request, body: dict = Body(...)):
    u = current_user(request)
    if not verify_password(body.get("current") or body.get("current_password") or "", u.get("password_hash", "")):
        raise HTTPException(400, "The current password is wrong.")
    if (p := password_problem(body.get("new") or body.get("new_password"))):
        raise HTTPException(400, p)
    with db() as con:
        con.execute("UPDATE ui_users SET password_hash=? WHERE id=?",
                    (hash_password(body.get("new") or body.get("new_password")), u["id"]))
        con.commit()
    return {"ok": True}


@router.get("/sessions")
def sessions(request: Request):
    u = current_user(request)
    cur = _th(request.cookies.get(COOKIE) or "")
    with db() as con:
        rows = con.execute("SELECT * FROM ui_sessions WHERE user_id=? ORDER BY id DESC", (u["id"],)).fetchall()
    return {"sessions": [{"id": r["id"], "created": r["created"], "last_seen": r["last_seen"], "expires": r["expires"],
                          "user_agent": r["user_agent"], "current": r["token_hash"] == cur} for r in rows]}


@router.delete("/sessions/{sid}")
def revoke_session(sid: int, request: Request):
    """Sign out one of YOUR other sessions (Account page). Another user's session id is answered as not found."""
    u = current_user(request)
    cur = _th(request.cookies.get(COOKIE) or "")
    with db() as con:
        r = con.execute("SELECT token_hash FROM ui_sessions WHERE id=? AND user_id=?", (sid, u["id"])).fetchone()
        if r is None:
            raise HTTPException(404, "unknown session")
        if r["token_hash"] == cur:
            raise HTTPException(400, "this is your current session; use Sign out")
        con.execute("DELETE FROM ui_sessions WHERE id=?", (sid,))
        con.commit()
    return {"ok": True}
