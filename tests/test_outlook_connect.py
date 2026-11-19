"""Settings > Outlook connect flow, with a fake MCP-server process (no network, no Microsoft account)."""
import io

from chanakya.outreach import outlook as O


class FakeProc:
    def __init__(self, lines, rc=0):
        self.stdout = io.StringIO("".join(lines))
        self.returncode = None
        self._rc = rc

    def wait(self):
        self.returncode = self._rc

    def poll(self):
        return self.returncode

    def kill(self):
        self.returncode = -9


def _run(monkeypatch, lines, rc=0):
    monkeypatch.setattr(O, "server_js", lambda: type("P", (), {"exists": lambda self: True})())
    monkeypatch.setattr(O.shutil, "which", lambda n: "node")
    monkeypatch.setattr(O.subprocess, "Popen", lambda *a, **k: FakeProc(lines, rc))
    O._LOGIN.clear(); O._LOGIN["state"] = "idle"
    O.start_login()
    import time
    for _ in range(20):
        if O.login_state()["state"] in ("connected", "failed"):
            break
        time.sleep(0.05)
    return O.login_state()


def test_connect_reports_success_and_never_exposes_a_token(monkeypatch):
    s = _run(monkeypatch, ["To sign in, open https://login.microsoft.com/device and enter the code AB12CD34 to "
                           "authenticate.\n", '{"success": true, "userData": {"displayName": "X"}}\n'])
    assert s["state"] == "connected" and "proc" not in s and "token" not in str(s).lower()


def test_connect_failure_is_reported(monkeypatch):
    s = _run(monkeypatch, ['{"success": false, "message": "AADSTS70016 expired"}\n'], rc=1)
    assert s["state"] == "failed" and "AADSTS70016" in s["error"]


def test_code_parsing():
    m = O._CODE.search("open the page https://microsoft.com/devicelogin and enter the code ABCD1234 to authenticate.")
    assert m.groups() == ("https://microsoft.com/devicelogin", "ABCD1234")
