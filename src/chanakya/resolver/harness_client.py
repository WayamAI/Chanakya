"""Subprocess adapter for Mriganka's LinkedIn browser harness (github.com/mrigankad/Linkedinharness-).

Chanakya never imports the harness or contains browser code. It runs the harness CLI
(`uv run linkedin_harness.py <cmd> ...`) in the harness folder and parses the last stdout line,
which the harness guarantees to be `JSON: {"ok": ..., "data": ..., "saved": ...}`.

Only read-only, LLM-free commands are allowed, and each agent gets its own subset (least privilege, like
clay/policy.py): Resolver = resolve/status, Profile Researcher = research/status, bottom-up discovery =
people/companies/profile/company/status. `run`/`chat`/`nav`/`login`/`serve`/`batch` are never allowed, so this adapter
can never trigger messages, connection requests or free-form tasks.

Browser sessions: if the harness `serve` process is running (it writes `.browser.json` with a CDP URL), every
command attaches to that ONE browser instead of launching a new one. `browser_mode()` reports which applies.
Page budget: the harness counts page views per day in `outputs/usage.json` and refuses past DAILY_PAGE_LIMIT;
`budget()` reads the same file so callers can stop BEFORE starting a command that would exceed it.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

HARNESS_SCRIPT = "linkedin_harness.py"
ALLOWED_COMMANDS = frozenset({"resolve", "status"})  # Agent 4 (default; kept for backward compatibility)
RESEARCH_COMMANDS = frozenset({"research", "status"})  # Agent 6
BOTTOMUP_COMMANDS = frozenset({"people", "companies", "profile", "company", "status"})  # bottom-up discovery
NEVER_ALLOWED = frozenset({"run", "chat", "nav", "login", "serve", "batch", "watch", "feed", "screenshot"})
# Estimated page views per command (from the harness source: resolve = up to 2 searches, research = profile +
# experience + activity + comments + optional posts tab, search = up to MAX_SEARCH_PAGES pages).
PAGE_COST = {"status": 1, "resolve": 2, "research": 5, "people": 2, "companies": 1, "profile": 1, "company": 1}
DEFAULT_TIMEOUT_S = 300.0


class HarnessError(RuntimeError):
    """The harness could not be run or returned unusable output. `kind` classifies the failure."""

    def __init__(self, kind: str, message: str, raw: dict | None = None):
        super().__init__(f"{kind}: {message}")
        self.kind = kind
        self.raw = raw


@dataclass
class HarnessResult:
    ok: bool
    data: dict | None
    saved: str | None
    error: str | None
    error_kind: str | None
    raw: dict
    argv: list[str]
    harness_version: str
    returncode: int | None = None
    stderr_tail: str = ""


@dataclass
class CompletedRun:
    returncode: int
    stdout: str
    stderr: str


Runner = Callable[[list[str], Path, float], CompletedRun]


def _default_runner(argv: list[str], cwd: Path, timeout: float) -> CompletedRun:
    try:
        p = subprocess.run(argv, cwd=str(cwd), capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout, env=_child_env())
    except subprocess.TimeoutExpired as e:
        raise HarnessError("timeout", f"harness did not finish within {timeout:.0f}s") from e
    except FileNotFoundError as e:
        raise HarnessError("not_installed", f"cannot start {argv[0]!r}: {e}") from e
    return CompletedRun(p.returncode, p.stdout or "", p.stderr or "")


def _child_env() -> dict:
    """Pass the environment through, minus Azure keys: Azure is out of scope and the harness prefers Azure
    if those variables are set (they only matter for its LLM commands, which this adapter never runs)."""
    return {k: v for k, v in os.environ.items() if not k.startswith("AZURE_OPENAI_")}


def classify_error(msg: str) -> str:
    m = (msg or "").lower()
    if "not logged in" in m or ("login" in m and "checkpoint" in m):
        return "not_logged_in"
    if "daily page limit" in m:
        return "page_limit"
    if "captcha" in m or "security check" in m or "security checkpoint" in m:
        return "security_check"
    return "harness_error"


def parse_output(stdout: str) -> dict:
    """Return the JSON object from the LAST line starting with `JSON: `."""
    for line in reversed(stdout.splitlines()):
        if line.startswith("JSON: "):
            try:
                obj = json.loads(line[len("JSON: "):])
            except json.JSONDecodeError as e:
                raise HarnessError("bad_output", f"unparseable JSON line: {e}") from e
            if not isinstance(obj, dict) or "ok" not in obj:
                raise HarnessError("bad_output", "JSON line has no 'ok' field")
            return obj
    raise HarnessError("bad_output", "no 'JSON: ' line in harness output")


def harness_version(harness_dir: Path) -> str:
    """`git:<sha>` if the harness folder is a git checkout, else `sha256:<hash of linkedin_harness.py>`."""
    head = harness_dir / ".git" / "HEAD"
    try:
        ref = head.read_text(encoding="utf-8").strip()
        if ref.startswith("ref: "):
            ref_file = harness_dir / ".git" / ref[5:]
            if ref_file.exists():
                return "git:" + ref_file.read_text(encoding="utf-8").strip()
            packed = harness_dir / ".git" / "packed-refs"
            for ln in packed.read_text(encoding="utf-8").splitlines() if packed.exists() else []:
                if ln.endswith(" " + ref[5:]):
                    return "git:" + ln.split()[0]
        elif ref:
            return "git:" + ref
    except OSError:
        pass
    script = harness_dir / HARNESS_SCRIPT
    if script.exists():
        return "sha256:" + hashlib.sha256(script.read_bytes()).hexdigest()
    return "unknown"


@dataclass
class HarnessClient:
    harness_dir: Path
    command_prefix: list[str] = field(default_factory=lambda: ["uv", "run", HARNESS_SCRIPT])
    timeout_s: float = DEFAULT_TIMEOUT_S
    runner: Runner = _default_runner
    allowed_commands: frozenset = ALLOWED_COMMANDS
    daily_page_limit: int | None = None  # None = read DAILY_PAGE_LIMIT (harness default 200)
    # LinkedIn policy (intel/compliance.py): the real subprocess may touch LinkedIn only when a human enabled
    # settings.linkedin_automation. Injected runners (tests, simulated mode) never reach LinkedIn and are not gated.
    automation_allowed: bool = False

    def __post_init__(self):
        self.harness_dir = Path(self.harness_dir)
        assert not (self.allowed_commands & NEVER_ALLOWED), "write/LLM harness commands may never be allowed"

    def with_commands(self, commands: frozenset) -> "HarnessClient":
        return HarnessClient(self.harness_dir, list(self.command_prefix), self.timeout_s, self.runner,
                             frozenset(commands), self.daily_page_limit, self.automation_allowed)

    # ---- session / budget ----
    def browser_mode(self) -> str:
        """'attached' when the harness `serve` browser is up (.browser.json present), else 'launch-per-command'."""
        return "attached" if (self.harness_dir / ".browser.json").exists() else "launch-per-command"

    def budget(self, today: str | None = None) -> dict:
        import datetime as dt
        limit = self.daily_page_limit or int(os.getenv("DAILY_PAGE_LIMIT", "200"))
        today = today or dt.date.today().isoformat()
        try:
            used = int(json.loads((self.harness_dir / "outputs" / "usage.json").read_text()).get(today, 0))
        except (OSError, ValueError, AttributeError):
            used = 0
        return {"date": today, "used": used, "limit": limit, "remaining": max(limit - used, 0)}

    def ensure_budget(self, cmd: str, count: int = 1) -> dict:
        b = self.budget()
        need = PAGE_COST.get(cmd, 1) * count
        if b["remaining"] < need:
            raise HarnessError("page_limit", f"daily LinkedIn page budget: {b['remaining']} left, `{cmd}` x{count} "
                                             f"needs ~{need}. Try tomorrow.")
        return b

    @classmethod
    def from_env(cls, **kw) -> "HarnessClient":
        d = os.getenv("LINKEDIN_HARNESS_DIR", "").strip()
        if not d:
            raise HarnessError("not_configured", "LINKEDIN_HARNESS_DIR is not set")
        cmd = os.getenv("LINKEDIN_HARNESS_CMD", "").split()  # e.g. ".venv/Scripts/python linkedin_harness.py"
        if cmd:
            kw.setdefault("command_prefix", cmd)
        return cls(Path(d), timeout_s=float(os.getenv("LINKEDIN_HARNESS_TIMEOUT_S", DEFAULT_TIMEOUT_S)), **kw)

    def check_installed(self) -> None:
        if not (self.harness_dir / HARNESS_SCRIPT).exists():
            raise HarnessError("not_installed", f"{HARNESS_SCRIPT} not found in {self.harness_dir}")

    def build_argv(self, cmd: str, *args: str, **opts: str) -> list[str]:
        if cmd not in self.allowed_commands or cmd in NEVER_ALLOWED:
            raise HarnessError("command_not_allowed", f"harness command {cmd!r} is not allowed")
        argv = [*self.command_prefix, cmd, *args]
        for k, v in opts.items():
            if v:
                argv += [f"-{k}" if len(k) == 1 else f"--{k}", str(v)]  # harness uses `-n` but `--days`
        return argv

    def resolve_argv(self, name: str, company: str = "", title: str = "") -> list[str]:
        if not name or not name.strip():
            raise HarnessError("bad_input", "name is required")
        return self.build_argv("resolve", name.strip(), company=(company or "").strip(), title=(title or "").strip())

    def _call(self, argv: list[str]) -> HarnessResult:
        if self.runner is _default_runner and not self.automation_allowed:
            raise HarnessError("policy_disabled", "LinkedIn automation is disabled (LinkedIn terms; handover rule "
                                                  "'Never scrape LinkedIn'). Use the LinkedIn search links and the "
                                                  "manual import, or record a decision to enable it in Settings.")
        self.check_installed()
        version = harness_version(self.harness_dir)
        done = self.runner(argv, self.harness_dir, self.timeout_s)
        obj = parse_output(done.stdout)
        err = obj.get("error")
        return HarnessResult(ok=bool(obj.get("ok")), data=obj.get("data"), saved=obj.get("saved"), error=err,
                             error_kind=None if obj.get("ok") else classify_error(err or ""), raw=obj, argv=argv,
                             harness_version=version, returncode=done.returncode, stderr_tail=done.stderr[-2000:])

    def status(self) -> HarnessResult:
        return self._call(self.build_argv("status"))

    def resolve(self, name: str, company: str = "", title: str = "") -> HarnessResult:
        return self._call(self.resolve_argv(name, company, title))

    # ---- Agent 6 / bottom-up (each refused unless this client's allowlist includes it) ----
    def research_argv(self, profile_url: str, n: int = 10, days: int = 90) -> list[str]:
        if "linkedin.com/in/" not in (profile_url or ""):
            raise HarnessError("bad_input", "research needs a linkedin.com/in/ profile URL")
        return self.build_argv("research", profile_url.strip(), n=str(n), days=str(days))

    def research(self, profile_url: str, n: int = 10, days: int = 90) -> HarnessResult:
        return self._call(self.research_argv(profile_url, n, days))

    def search_argv(self, kind: str, query: str, n: int = 10) -> list[str]:
        if kind not in ("people", "companies") or not (query or "").strip():
            raise HarnessError("bad_input", "search needs kind people|companies and a query")
        return self.build_argv(kind, query.strip(), n=str(n))

    def search(self, kind: str, query: str, n: int = 10) -> HarnessResult:
        return self._call(self.search_argv(kind, query, n))

    def profile(self, url: str) -> HarnessResult:
        return self._call(self.build_argv("profile", url.strip()))

    def company(self, url: str) -> HarnessResult:
        return self._call(self.build_argv("company", url.strip()))
