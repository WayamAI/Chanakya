"""Chanakya's own Entra app route (delegated Graph), against a mocked Microsoft (no network, no real account)."""
import json
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from chanakya.outreach import graph_oauth as G
from chanakya.outreach import outlook as O


TENANT = "11111111-2222-3333-4444-555555555555"


def _jwt(claims):
    import base64
    b = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
    return f"{b({'alg': 'none'})}.{b(claims)}.sig"


@pytest.fixture(autouse=True)
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("CHANAKYA_MS_CLIENT_ID", "00000000-0000-0000-0000-00000000abcd")
    monkeypatch.setenv("CHANAKYA_MS_TENANT_ID", TENANT)
    monkeypatch.delenv("CHANAKYA_MS_REDIRECT_URI", raising=False)
    monkeypatch.delenv("CHANAKYA_MS_CLIENT_SECRET", raising=False)
    monkeypatch.setenv("CHANAKYA_TOKEN_KEY_FILE", str(tmp_path / "k"))
    monkeypatch.delenv("CHANAKYA_TOKEN_KEY", raising=False)
    G._PENDING.clear()


class MS:
    def __init__(self, token_error=None, scope="openid profile offline_access User.Read Mail.ReadWrite", tid=TENANT):
        self.calls, self.token_error, self.scope, self.n, self.tid = [], token_error, scope, 0, tid
        self.drafts = {}

    def __call__(self, req: httpx.Request):
        self.calls.append((req.method, req.url.path))
        if req.url.path.endswith("/token"):
            if self.token_error:
                return httpx.Response(400, json={"error": "invalid_grant", "error_description": self.token_error})
            self.n += 1
            self.last_token_form = parse_qs(req.content.decode())
            return httpx.Response(200, json={"access_token": f"AT{self.n}", "refresh_token": f"RT{self.n}",
                                             "id_token": _jwt({"tid": self.tid}), "expires_in": 3600,
                                             "scope": self.scope})
        if req.url.path == "/v1.0/me":
            return httpx.Response(200, json={"displayName": "Test User", "mail": "test.user@contoso.example"})
        if req.url.path == "/v1.0/me/messages" and req.method == "POST":
            body = json.loads(req.content)
            m = {"id": f"MSG{len(self.drafts) + 1}", "isDraft": True, "webLink": "https://outlook/x", **body,
                 "body": {"contentType": "html", "content": "<p>" + body["body"]["content"] + "</p>"}}
            self.drafts[m["id"]] = m
            return httpx.Response(201, json=m)
        if req.url.path == "/v1.0/me/mailFolders/drafts/messages":  # find-draft by ChanakyaDraftId
            f = req.url.params["$filter"]
            hit = [m for m in self.drafts.values() for p in m.get("singleValueExtendedProperties", [])
                   if f"'{p['value']}'" in f]
            return httpx.Response(200, json={"value": hit[:1]})
        if req.url.path.startswith("/v1.0/me/mailFolders/drafts/messages/"):
            m = self.drafts.get(req.url.path.rsplit("/", 1)[1])
            return httpx.Response(200, json=m) if m else httpx.Response(404)
        return httpx.Response(404)


def _connect(con, ms):
    url = G.start(7)["auth_url"]
    q = parse_qs(urlparse(url).query)
    assert "Mail.Send" not in q["scope"][0] and q["code_challenge_method"] == ["S256"]
    return G.callback(con, 7, code="C", state=q["state"][0], transport=httpx.MockTransport(ms))


def test_scopes_are_delegated_minimum_without_send():
    assert G.SCOPES == ["openid", "profile", "offline_access", "User.Read", "Mail.ReadWrite"]


def test_connect_stores_encrypted_tokens_and_status_hides_them(con):
    ms = MS()
    assert _connect(con, ms)["account"] == "test.user@contoso.example"
    raw = con.execute("SELECT token_enc FROM outlook_connections").fetchone()[0]
    assert b"AT1" not in raw and b"RT1" not in raw
    s = G.status(con, 7)
    assert s["connected"] and "AT1" not in json.dumps(s) and s["send_permission"] is False


def test_state_is_bound_to_the_user_and_single_use(con):
    url = G.start(7)["auth_url"]
    st = parse_qs(urlparse(url).query)["state"][0]
    with pytest.raises(G.GraphAuthError, match="different Chanakya user"):
        G.callback(con, 8, code="C", state=st, transport=httpx.MockTransport(MS()))
    with pytest.raises(G.GraphAuthError, match="expired"):
        G.callback(con, 7, code="C", state=st, transport=httpx.MockTransport(MS()))


def test_admin_consent_policy_is_explained_not_bypassed(con):
    with pytest.raises(G.GraphAuthError, match="administrator.*Grant admin consent"):
        G.callback(con, 7, error="access_denied", error_description="AADSTS90094: admin approval required")


def test_refresh_on_expiry(con):
    ms = MS()
    _connect(con, ms)
    con.execute("UPDATE outlook_connections SET expires_at=0")
    assert G.access_token(con, 7, transport=httpx.MockTransport(ms)) == "AT2"


def test_refuses_token_with_send_scope(con):
    with pytest.raises(G.GraphAuthError, match="Mail.Send"):
        _connect(con, MS(scope="User.Read Mail.ReadWrite Mail.Send"))


def test_draft_via_graph_reuses_review_checks_and_never_sends(con):
    from tests.test_account_drafts_ai import _account, _approve_role
    from chanakya.outreach import account_drafts as AD
    cid, pid, _ = _account(con)
    _approve_role(con, pid)
    did = AD.generate_for_person(con, pid, by="krisha", sender="Vineet Shukla")["draft_id"]
    ms = MS()
    _connect(con, ms)
    t = httpx.MockTransport(ms)
    with pytest.raises(O.OutlookError, match="edit or mark it ready"):
        O.create_outlook_draft(con, did, by="krisha", session_factory=lambda: G.GraphSession(con, 7, t))
    con.execute("UPDATE outreach_drafts SET status='edited', subject='OBIEE to Power BI', body=? WHERE id=?", ("Hi Darren,\n\nI saw the notice. Worth 20 minutes?", did))
    r = O.create_outlook_draft(con, did, by="krisha", session_factory=lambda: G.GraphSession(con, 7, t))
    assert r["outlook_message_id"] == "MSG1" and r["sent"] is False
    assert r["verified_in_drafts"] is True and all(r["readback"].values())
    assert not any("send" in p.lower() for _, p in ms.calls)
    # retry after a lost response (message created, id never stored): finds MSG1 instead of creating MSG2
    con.execute("UPDATE outreach_drafts SET outlook_message_id=NULL WHERE id=?", (did,))
    r2 = O.create_outlook_draft(con, did, by="krisha", session_factory=lambda: G.GraphSession(con, 7, t))
    assert r2["outlook_message_id"] == "MSG1" and len(ms.drafts) == 1
    # stored id: a second save is refused outright
    with pytest.raises(O.OutlookError, match="already in Outlook"):
        O.create_outlook_draft(con, did, by="krisha", session_factory=lambda: G.GraphSession(con, 7, t))
    # read-back that differs from the approved draft is NOT reported as success
    con.execute("UPDATE outreach_drafts SET outlook_message_id=NULL WHERE id=?", (did,))
    ms.drafts["MSG1"]["subject"] = "changed in Outlook"
    with pytest.raises(O.OutlookError, match="read-back check failed"):
        O.create_outlook_draft(con, did, by="krisha", session_factory=lambda: G.GraphSession(con, 7, t))


def test_not_connected_is_a_clear_error(con):
    with pytest.raises(G.GraphAuthError, match="not connected"):
        G.access_token(con, 99)


def test_authorize_url_tenant_redirect_and_token_uses_same_redirect(con):
    url = G.start(7)["auth_url"]
    u = urlparse(url)
    q = parse_qs(u.query)
    assert u.netloc == "login.microsoftonline.com" and u.path == f"/{TENANT}/oauth2/v2.0/authorize"
    assert q["redirect_uri"] == ["http://localhost:3000/auth/callback"]
    assert set(q["scope"][0].split()) == {"openid", "profile", "offline_access", "User.Read", "Mail.ReadWrite"}
    ms = MS()
    G.callback(con, 7, code="C", state=q["state"][0], transport=httpx.MockTransport(ms))
    assert ms.last_token_form["redirect_uri"] == ["http://localhost:3000/auth/callback"]
    assert ms.last_token_form["code_verifier"][0]  # PKCE verifier sent only from the backend


def test_wrong_tenant_account_rejected(con):
    with pytest.raises(G.GraphAuthError, match="different organisation"):
        _connect(con, MS(tid="99999999-0000-0000-0000-000000000000"))
    assert G.status(con, 7)["connected"] is False


def test_malformed_tenant_id_refuses_to_start(monkeypatch):
    monkeypatch.setenv("CHANAKYA_MS_TENANT_ID", "0a4f2c26-aa35-45f1-9431-3ebea3cf0a9")
    assert G.configured() is False
    with pytest.raises(G.GraphAuthError, match="not a valid Directory"):
        G.start(7)


def test_tokens_isolated_per_user(con):
    _connect(con, MS())
    with pytest.raises(G.GraphAuthError, match="not connected"):
        G.access_token(con, 8)


def test_refresh_failure_is_recorded_and_raised(con, monkeypatch):
    ms = MS()
    _connect(con, ms)
    con.execute("UPDATE outlook_connections SET expires_at=0 WHERE user_id=7")
    ms.token_error = "AADSTS700082: refresh token expired"
    with pytest.raises(G.GraphAuthError):
        G.access_token(con, 7, transport=httpx.MockTransport(ms))
    assert "AADSTS700082" in (G.status(con, 7)["error"] or "")


def test_disconnect_clears_tokens(con):
    _connect(con, MS())
    G.disconnect(con, 7)
    assert G.status(con, 7)["connected"] is False


def test_granted_send_kept_unused_only_when_explicitly_tolerated(con, monkeypatch):
    ms = MS(scope="openid profile offline_access User.Read Mail.ReadWrite Mail.Send")
    with pytest.raises(G.GraphAuthError, match="never requests"):
        _connect(con, ms)
    monkeypatch.setenv("CHANAKYA_TOLERATE_GRANTED_MAIL_SEND", "1")
    G._PENDING.clear()
    _connect(con, ms)
    st = G.status(con, 7)
    assert st["connected"] and st["send_granted_unused"] and "Mail.Send" not in " ".join(G.SCOPES)
