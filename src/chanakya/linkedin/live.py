"""Visible LinkedIn research session (spec Part C): a real, headed Chrome window the user can watch.

Flow: Start -> Chrome opens on LinkedIn with a dedicated Chanakya profile (data/linkedin_profile, so the login is
remembered) -> if LinkedIn shows a login page, CAPTCHA or checkpoint, the agent WAITS and the user completes it in the
window themselves -> once a signed-in page is seen, queued searches run in that same window: type-free URL search,
slow scroll, read the visible result cards, next page. A challenge appearing mid-search pauses the run until the user
clears it. Every step is an event (shown live in Chanakya), a JPEG of the current page is kept for the live view, and
results go into `bottomup_leads` (deduplicated by profile URL) for human review.

Never: bypass CAPTCHAs/checkpoints, stealth or anti-detection flags, private data, messages or connection requests.
Pacing is deliberately human-scale and a daily page budget (CHANAKYA_LINKEDIN_PAGES_PER_DAY, default 40) applies.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import os
import queue
import random
import threading
import time
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
PROFILE_DIR = Path(os.getenv("CHANAKYA_LINKEDIN_PROFILE_DIR", str(ROOT / "data" / "linkedin_profile")))
USAGE_FILE = ROOT / "data" / "linkedin_live_usage.json"
CHALLENGE = ("/checkpoint", "/challenge", "captcha", "/uas/", "/authwall", "/login", "/signup")
SIGNED_IN = ("/feed", "/search/", "/in/", "/mynetwork", "/company/", "/jobs")
VERSION = "live-chrome-1"

EXTRACT_JS = r"""
() => {
  const out = [], seen = new Set();
  for (const a of document.querySelectorAll('main a[href*="/in/"]')) {
    const url = a.href.split('?')[0].replace(/\/$/, '');
    if (seen.has(url) || !/linkedin\.com\/in\/[^/]+$/.test(url)) continue;
    const card = a.closest('li') || a.closest('[data-chameleon-result-urn]') || a.closest('div[data-view-name]');
    if (!card) continue;
    const lines = card.innerText.split('\n').map(s => s.trim())
      .filter(s => s && !/^(Connect|Follow|Message|View .*profile|•.*|\d+(st|nd|rd|th)\+?|Status is .*|Current:|Past:)$/i.test(s));
    if (!lines.length) continue;
    seen.add(url);
    out.push({url, title: lines[0], lines: lines.slice(1, 6)});
  }
  return out;
}
"""


COMPANY_JS = r"""
() => {
  const out = [], seen = new Set();
  for (const a of document.querySelectorAll('main a[href*="/company/"]')) {
    const m = a.href.match(/linkedin\.com\/company\/([^/?#]+)/);
    if (!m) continue;
    const url = 'https://www.linkedin.com/company/' + m[1];
    if (seen.has(url)) continue;
    const card = a.closest('li') || a.closest('div[data-view-name]');
    if (!card) continue;
    const lines = card.innerText.split('\n').map(s => s.trim()).filter(s => s && !/^(Follow|Following|Message)$/i.test(s));
    if (!lines.length) continue;
    seen.add(url);
    out.push({url, title: lines[0], lines: lines.slice(1, 5)});
  }
  return out;
}
"""

COMPANY_TABLE = ("CREATE TABLE IF NOT EXISTS linkedin_company_candidates (id INTEGER PRIMARY KEY, linkedin_url TEXT NOT "
                 "NULL UNIQUE, name TEXT NOT NULL, lines_json TEXT, product_id TEXT, query TEXT, status TEXT NOT NULL "
                 "DEFAULT 'pending_review', reviewed_by TEXT, review_reason TEXT, created_at TEXT NOT NULL, updated_at "
                 "TEXT NOT NULL)")


CARD_JS = r"""
(kind) => {
  // Layout-agnostic: from every result link, climb to the smallest ancestor that looks like one result card
  // (has a Follow/Connect/Message button or several text lines) and is not the nav bar or side rail.
  const pat = kind === 'company' ? /linkedin\.com\/company\/([^/?#]+)/ : /linkedin\.com\/in\/([^/?#]+)/;
  const base = kind === 'company' ? 'https://www.linkedin.com/company/' : 'https://www.linkedin.com/in/';
  const out = [], seen = new Set();
  const skip = (el) => el.closest('header, nav, aside, footer, [role="banner"], [role="navigation"], [role="complementary"]');
  for (const a of document.querySelectorAll('a[href]')) {
    const m = a.href.match(pat);
    if (!m || skip(a)) continue;
    const url = base + decodeURIComponent(m[1]);
    if (seen.has(url)) continue;
    let card = a, best = null;
    for (let i = 0; i < 10 && card && card !== document.body; i++, card = card.parentElement) {
      const lines = (card.innerText || '').split('\n').map(s => s.trim()).filter(Boolean);
      const hasBtn = card.querySelector('button');
      if (lines.length >= 2 && (hasBtn || lines.length >= 3)) { best = card; break; }
    }
    if (!best) continue;
    // a card that contains several result links is a list, not a card
    const links = new Set([...best.querySelectorAll('a[href]')].map(x => (x.href.match(pat) || [])[1]).filter(Boolean));
    if (links.size > 1) continue;
    const lines = best.innerText.split('\n').map(s => s.trim()).filter(s => s &&
      !/^(Connect|Follow|Following|Message|Pending|View .*profile|Learn more|•.*|· ?\d+(st|nd|rd|th)\+?|\d+(st|nd|rd|th)\+?|Status is .*|Promoted|.*degree connection|· ?\d.*)$/i.test(s));
    if (!lines.length) continue;
    seen.add(url);
    const linkText = (a.innerText || '').split('\n').map(x => x.trim()).filter(Boolean)[0] || '';
    const title = (linkText && linkText.length < 90 && !/^(view|see) /i.test(linkText)) ? linkText : lines[0];
    out.push({url, title: title.replace(/\s*(·|•).*$/, ''),
              lines: lines.filter(l => l !== title && l !== linkText).slice(0, 6)});
  }
  return out;
}
"""
EXTRACT_JS = "person"
COMPANY_JS = "company"


ABOUT_JS = r"""
() => {
  let website = null, about = null;
  for (const dt of document.querySelectorAll('dt, h3')) {
    if (/^\s*website\s*$/i.test(dt.innerText)) {
      const a = (dt.nextElementSibling && dt.nextElementSibling.querySelector('a[href]')) ||
                (dt.parentElement && dt.parentElement.querySelector('a[href^="http"]:not([href*="linkedin.com"])'));
      if (a) { website = a.innerText.trim() || a.href; break; }
    }
  }
  if (!website) {
    const a = [...document.querySelectorAll('main a[href^="http"]')].find(x => !/linkedin\.com|lnkd\.in|bing\.com|microsoft\.com/.test(x.href) && /\./.test(x.innerText));
    if (a) website = a.innerText.trim();
  }
  const p = [...document.querySelectorAll('main p')].map(x => x.innerText.trim()).sort((a, b) => b.length - a.length)[0];
  about = p || null;
  return {website, about};
}
"""


RUN_TABLE = ("CREATE TABLE IF NOT EXISTS linkedin_runs (id INTEGER PRIMARY KEY, product_id TEXT, accelerator TEXT, "
             "status TEXT NOT NULL, company_ids_json TEXT NOT NULL DEFAULT '[]', person_ids_json TEXT NOT NULL DEFAULT "
             "'[]', companies_found_json TEXT NOT NULL DEFAULT '[]', started_by TEXT, started_at TEXT NOT NULL, "
             "finished_at TEXT, events_json TEXT NOT NULL DEFAULT '[]', settings_json TEXT NOT NULL DEFAULT '{}')")
# LinkedIn geo ids for the company-search HQ filter (country level; cities fall back to keywords + research filter)
def mastersheet_content(con, product_ids: list[str]) -> dict[str, list[str]]:
    """Authority content per accelerator: the client use-case titles imported from the sales team's master sheet
    (catalogue_use_cases). Keyed by accelerator name; accelerators without use cases are left out."""
    if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='catalogue_use_cases'").fetchone():
        return {}
    out: dict[str, list[str]] = {}
    for pid in product_ids:
        name = con.execute("SELECT name FROM products WHERE id=?", (pid,)).fetchone()
        titles = [r[0] for r in con.execute("SELECT title FROM catalogue_use_cases WHERE product_id=? AND "
                                            "COALESCE(title,'')<>'' ORDER BY id", (pid,))]
        if name and titles:
            out[name[0]] = titles
    return out


def _round_robin(comps: list[dict], n: int) -> list[dict]:
    """Pick the companies to research one search at a time, so a single broad search cannot fill every slot."""
    groups: dict[str, list[dict]] = {}
    for c in comps:
        groups.setdefault(c.get("query") or "", []).append(c)
    out, i = [], 0
    while len(out) < min(n, len(comps)):
        for g in groups.values():
            if i < len(g) and len(out) < n:
                out.append(g[i])
        i += 1
    return out


def _sizes(sizes: str | None) -> str:
    """LinkedIn company-size codes: D 51-200, E 201-500, F 501-1k, G 1k-5k, H 5k-10k, I 10k+. Default 51+."""
    got = "".join(ch for ch in (sizes or "").upper() if ch in "BCDEFGHI")
    return got or "DEFGHI"


GEO_URNS = {"india": "102713980", "united kingdom": "101165590", "uk": "101165590", "united states": "103644278",
            "usa": "103644278", "us": "103644278"}


def ensure_run_table(con) -> None:
    con.execute(RUN_TABLE)
    have = {r[1] for r in con.execute("PRAGMA table_info(linkedin_runs)")}
    for col, ddl in (("events_json", "TEXT NOT NULL DEFAULT '[]'"), ("settings_json", "TEXT NOT NULL DEFAULT '{}'")):
        if col not in have:
            con.execute(f"ALTER TABLE linkedin_runs ADD COLUMN {col} {ddl}")


def _today():
    return dt.date.today().isoformat()


def pages_used() -> int:
    try:
        d = json.loads(USAGE_FILE.read_text())
        return d.get(_today(), 0)
    except Exception:  # noqa: BLE001
        return 0


def _use_page():
    try:
        d = json.loads(USAGE_FILE.read_text())
    except Exception:  # noqa: BLE001
        d = {}
    d = {_today(): d.get(_today(), 0) + 1}
    USAGE_FILE.parent.mkdir(parents=True, exist_ok=True)
    USAGE_FILE.write_text(json.dumps(d))


def page_limit() -> int:
    """Daily cap. CHANAKYA_LINKEDIN_PAGES_OVERRIDE="YYYY-MM-DD:N" raises it for that one date only."""
    ov = os.getenv("CHANAKYA_LINKEDIN_PAGES_OVERRIDE", "")
    if ":" in ov:
        day, n = ov.split(":", 1)
        if day.strip() == _today() and n.strip().isdigit():
            return int(n)
    return int(os.getenv("CHANAKYA_LINKEDIN_PAGES_PER_DAY", "100"))


def company_from_headline(h: str | None) -> str | None:
    if not h:
        return None
    low = h.lower()
    for sep in (" at ", " @ "):
        i = low.rfind(sep)
        if i >= 0:
            return h[i + len(sep):].split("|")[0].split("·")[0].strip() or None
    return None


class LiveSession:
    """One headed browser for the whole app. All Playwright calls happen on the session's own thread."""

    def __init__(self, db_factory=None, launcher=None):
        self.db_factory = db_factory
        self.launcher = launcher  # tests inject a fake; default launches real Chrome via Playwright
        self.jobs: queue.Queue = queue.Queue()
        self.thread: threading.Thread | None = None
        self.lock = threading.Lock()
        self.stop_flag = threading.Event()
        self.cancel_flag = threading.Event()
        self.shot: bytes | None = None
        self.events: list[dict] = []
        self.state = {"status": "stopped", "browser": "disconnected", "linkedin_session": "unknown", "action": None,
                      "page_url": None, "query": None, "target": None, "inspected": 0, "matches": 0, "new": 0,
                      "duplicates": 0, "skipped": 0, "errors": 0, "pending_searches": 0, "started_by": None,
                      "last_search": None, "followup": None, "run": None, "prospect": None}

    # ---------- state ----------
    def _ev(self, msg: str, level: str = "info", **detail):
        e = {"at": dt.datetime.now().isoformat(timespec="seconds"), "level": level, "message": msg, **detail}
        with self.lock:
            self.events.append(e)
            self.events = self.events[-300:]
            self.state["action"] = msg
            if level == "error":
                self.state["errors"] += 1

    def _set(self, **kw):
        with self.lock:
            self.state.update(kw)

    def status(self) -> dict:
        with self.lock:
            return {**self.state, "pages_used_today": pages_used(), "page_limit": page_limit(),
                    "pending_searches": self.jobs.qsize(), "events": list(self.events[-80:]),
                    "has_screenshot": self.shot is not None}

    # ---------- control ----------
    def start(self, by: str) -> dict:
        if self.thread and self.thread.is_alive():
            return self.status()
        self.stop_flag.clear()
        self.events = []
        self._set(status="launching", browser="launching", started_by=by, errors=0)
        self.thread = threading.Thread(target=self._run, name="linkedin-live", daemon=True)
        self.thread.start()
        return self.status()

    def stop(self):
        self.stop_flag.set()
        self.cancel_flag.set()
        self._ev("stop requested")

    def cancel_search(self):
        self.cancel_flag.set()
        while not self.jobs.empty():
            self.jobs.get_nowait()
        self._ev("search cancelled by user")

    def enqueue_accelerator(self, plan: dict, *, company_pages: int = 1, max_companies: int = 5,
                            titles_per_company: int = 2, by: str = "", user_id: int | None = None,
                            extras: dict | None = None) -> dict:
        self.jobs.put({"kind": "accelerator", "plan": plan, "query": plan["accelerator"], "by": by,
                       "extras": extras or {},
                       "company_pages": max(1, min(int(company_pages), 3)),
                       "max_companies": max(1, min(int(max_companies), 50)),
                       "titles_per_company": max(1, min(int(titles_per_company), 3)), "workflow_id": None,
                       "user_id": user_id})
        self._ev(f"accelerator search queued: {plan['accelerator']}")
        return self.status()

    def enqueue_multi(self, plans: list[dict], *, company_pages: int = 1, max_companies: int = 8, by: str = "",
                      user_id: int | None = None, extras: dict | None = None) -> dict:
        """extras: run settings shared by all plans (roles, geography, revenue {min_cr, max_cr})."""
        self.jobs.put({"kind": "accelerator", "plans": plans, "plan": plans[0], "query": f"{len(plans)} accelerators",
                       "by": by, "company_pages": max(1, min(int(company_pages), 2)),
                       "max_companies": max(1, min(int(max_companies), 15)), "titles_per_company": 1,
                       "workflow_id": None, "user_id": user_id, "extras": extras or {}})
        self._ev(f"multi-accelerator search queued: {', '.join(p['accelerator'] for p in plans)[:200]}")
        return self.status()

    def enqueue_person(self, name: str, company: str, by: str = "", role: str = "") -> int:
        """Prospect Intelligence: research one named person at a company. Returns the report id."""
        from chanakya.linkedin import prospect as PI
        if not (name or "").strip() or not (company or "").strip():
            raise ValueError("person name and company are both required")
        with self.db_factory() as con:
            rid = PI.create(con, name, company, by, role)
        self._set(prospect={"report_id": rid, "status": "searching"})
        self.jobs.put({"kind": "person", "report_id": rid, "name": name.strip(), "company": company.strip(),
                       "role": (role or "").strip(), "by": by})
        self._ev(f"Prospect Intelligence queued: {name.strip()} ({company.strip()})")
        return rid

    def enqueue_person_profile(self, report_id: int, url: str) -> None:
        self.jobs.put({"kind": "person_profile", "report_id": report_id, "url": url})
        self._ev("Prospect Intelligence: profile chosen", url=url)

    def enqueue(self, query: str, *, pages: int = 2, target: str | None = None, workflow_id: int | None = None,
                by: str = "") -> dict:
        if not (query or "").strip():
            raise ValueError("query is required")
        self.jobs.put({"query": query.strip(), "pages": max(1, min(int(pages), 5)), "target": target,
                       "workflow_id": workflow_id, "by": by})
        self._ev(f"search queued: {query}")
        return self.status()

    # ---------- browser thread ----------
    def _launch(self):
        if self.launcher:
            return self.launcher()
        from playwright.sync_api import sync_playwright
        pw = sync_playwright().start()
        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        kw = dict(user_data_dir=str(PROFILE_DIR), headless=False, no_viewport=True, args=["--start-maximized"])
        try:
            ctx = pw.chromium.launch_persistent_context(channel="chrome", **kw)  # the user's real Chrome
        except Exception:  # noqa: BLE001 - Chrome not installed: Playwright's bundled Chromium, still visible
            ctx = pw.chromium.launch_persistent_context(**kw)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        return pw, ctx, page

    def _snap(self, page):
        try:
            self.shot = page.screenshot(type="jpeg", quality=82)
            self._set(page_url=page.url)
        except Exception:  # noqa: BLE001 - window closed or navigating
            pass

    def _wait(self, page, secs: float):
        end = time.time() + secs
        while time.time() < end and not self.stop_flag.is_set():
            self._snap(page)
            time.sleep(min(1.5, max(0.1, end - time.time())))

    def _challenged(self, page) -> bool:
        u = (page.url or "").lower()
        return any(c in u for c in CHALLENGE) or not any(s in u for s in SIGNED_IN)

    def _wait_for_user(self, page, why: str) -> bool:
        """Block until the user has signed in / cleared the challenge in the window. Never automated."""
        self._set(status="waiting_for_user", linkedin_session="needs_user")
        self._ev(f"{why}: please complete it in the Chrome window. The agent is waiting and will not touch the page.",
                 level="warn")
        while not self.stop_flag.is_set():
            self._wait(page, 2)
            try:
                if not self._challenged(page):
                    self._set(status="ready", linkedin_session="signed_in")
                    self._ev("signed-in LinkedIn page detected; continuing")
                    return True
            except Exception:  # noqa: BLE001
                return False
        return False

    def _run(self):
        try:
            pw, ctx, page = self._launch()
        except Exception as e:  # noqa: BLE001
            self._set(status="error", browser="disconnected")
            self._ev(f"could not launch Chrome: {type(e).__name__}: {e}", level="error")
            return
        self._set(browser="connected", status="opening")
        self._ev("Chrome opened (visible window, Chanakya profile)")
        try:
            page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded")
            _use_page()
            self._wait(page, 3)
            if self._challenged(page):
                if not self._wait_for_user(page, "LinkedIn needs you to sign in"):
                    return
            else:
                self._set(status="ready", linkedin_session="signed_in")
                self._ev("already signed in to LinkedIn")
            while not self.stop_flag.is_set():
                try:
                    job = self.jobs.get(timeout=2)
                except queue.Empty:
                    self._snap(page)
                    continue
                self.cancel_flag.clear()
                self._search(page, job)
                if self.state["status"] == "searching":
                    self._set(status="ready")
        except Exception as e:  # noqa: BLE001 - window closed by the user or a navigation failure
            self._ev(f"browser session ended: {type(e).__name__}: {str(e)[:200]}", level="error")
        finally:
            self._set(status="stopped", browser="disconnected", linkedin_session="unknown")
            for f in (getattr(ctx, "close", None), getattr(pw, "stop", None)):
                try:
                    f and f()
                except Exception:  # noqa: BLE001
                    pass

    def _search(self, page, job):
        if job.get("kind") == "accelerator":
            return self._accelerator(page, job)
        if job.get("kind") in ("person", "person_profile"):
            from chanakya.linkedin import prospect as PI
            return PI.run_job(self, page, job)
        self._set(status="searching", query=job["query"], target=job.get("target"), inspected=0, matches=0, new=0,
                  duplicates=0, skipped=0)
        found = self._people(page, job["query"], job["pages"])
        res = self._store(found, job)
        self._set(matches=len(found), new=res["new"], duplicates=res["duplicates"],
                  last_search={"query": job["query"], "at": dt.datetime.now().isoformat(timespec="seconds"), **res})
        self._ev(f"search finished: {len(found)} candidates, {res['new']} new, {res['duplicates']} already known; "
                 "sent to Review > Bottom-up leads", kind="done")

    def _open_results(self, page, kind: str, q: str, n: int, geo_urn: str | None = None,
                      sizes: str | None = None) -> bool:
        if self.cancel_flag.is_set() or self.stop_flag.is_set():
            return False
        if pages_used() >= page_limit():
            self._ev(f"daily LinkedIn page budget reached ({page_limit()}); stopping", level="warn")
            return False
        url = (f"https://www.linkedin.com/search/results/{kind}/?keywords=" + urllib.parse.quote(q)
               + ("&companySize=" + urllib.parse.quote(json.dumps(list(_sizes(sizes)), separators=(",", ":")))
                  if kind == "companies" else "")  # default 51+ employees: operating companies, not one-person pages
               + (f"&companyHqGeo=%5B%22{geo_urn}%22%5D" if kind == "companies" and geo_urn else "")
               + (f"&page={n}" if n > 1 else ""))
        self._set(query=q)
        self._ev(f"opening {kind} results for '{q}' (page {n})")
        page.goto(url, wait_until="domcontentloaded")
        _use_page()
        self._wait(page, random.uniform(3, 5))
        if self._challenged(page) and not self._wait_for_user(page, "LinkedIn showed a check"):
            return False
        self._set(status="searching")
        for _ in range(4):  # slow scroll so lazy-loaded cards render, like a person reading
            page.mouse.wheel(0, 700)
            self._wait(page, random.uniform(0.8, 1.6))
        return True

    def _read(self, page, js, n):
        kind = js if js in ("company", "person") else "person"
        import re as _re
        self._ev(f"reading result cards on page {n}")
        try:
            items = page.evaluate(CARD_JS, kind) or []
        except Exception as e:  # noqa: BLE001
            self._ev(f"could not read results: {e}", level="error")
            items = []
        deg = _re.compile(r"^\W*(\d+(st|nd|rd|th)\+?|.*degree connection.*)\W*$", _re.I)
        for it in items:
            it["lines"] = [l for l in it.get("lines") or [] if not deg.match(l)]
        if not items:  # keep the page so the reader can be fixed against LinkedIn's real markup
            try:
                dbg = ROOT / "data" / f"linkedin_debug_{kind}.html"
                dbg.write_text(page.content(), encoding="utf-8")
                self._ev(f"no cards read; page saved to {dbg.name} for diagnosis", level="warn")
            except Exception:  # noqa: BLE001
                pass
        return items

    def _people(self, page, q: str, pages: int) -> list[dict]:
        found: list[dict] = []
        for n in range(1, pages + 1):
            if not self._open_results(page, "people", q, n):
                break
            items = self._read(page, EXTRACT_JS, n)
            if not items:
                self._ev(f"no readable results on page {n}", level="warn")
                break
            for it in items:
                self._set(inspected=self.state["inspected"] + 1)
                if it["title"].lower().startswith("linkedin member"):  # out-of-network: name hidden, not collected
                    self._set(skipped=self.state["skipped"] + 1)
                    continue
                found.append(it)
                self._ev(f"candidate: {it['title']} - {(it['lines'] or [''])[0][:90]}", kind="candidate", url=it["url"])
            self._wait(page, random.uniform(3, 6))
        return found

    def _company_people(self, page, company_url: str, keyword: str) -> list[dict]:
        """The company's own People tab filtered by keyword: only people LinkedIn links to this company."""
        if self.cancel_flag.is_set() or self.stop_flag.is_set() or pages_used() >= page_limit():
            return []
        self._ev(f"opening company People tab: '{keyword}'")
        page.goto(company_url.rstrip("/") + "/people/?keywords=" + urllib.parse.quote(keyword),
                  wait_until="domcontentloaded")
        _use_page()
        self._wait(page, random.uniform(3, 4.5))
        if self._challenged(page) and not self._wait_for_user(page, "LinkedIn showed a check"):
            return []
        for _ in range(3):
            page.mouse.wheel(0, 700)
            self._wait(page, random.uniform(0.8, 1.4))
        items = self._read(page, "person", 1)
        for it in items:
            self._set(inspected=self.state["inspected"] + 1)
        return [it for it in items if not it["title"].lower().startswith("linkedin member")]

    def _companies(self, page, q: str, pages: int, geo_urn: str | None = None, sizes: str | None = None) -> list[dict]:
        out = []
        for n in range(1, pages + 1):
            if not self._open_results(page, "companies", q, n, geo_urn, sizes):
                break
            items = self._read(page, COMPANY_JS, n)
            for it in items:
                self._set(inspected=self.state["inspected"] + 1)
                out.append(it)
                self._ev(f"company: {it['title']} - {(it['lines'] or [''])[0][:90]}", kind="company", url=it["url"])
            if not items:
                break
            self._wait(page, random.uniform(3, 6))
        return out

    def _about(self, page, c) -> dict:
        """Official website + description from the company's LinkedIn About page (one page view)."""
        if pages_used() >= page_limit() or self.stop_flag.is_set():
            return {}
        self._ev(f"opening {c['title']} About page for the official website")
        try:
            page.goto(c["url"].rstrip("/") + "/about/", wait_until="domcontentloaded")
            _use_page()
            self._wait(page, random.uniform(2.5, 4))
            if self._challenged(page) and not self._wait_for_user(page, "LinkedIn showed a check"):
                return {}
            return page.evaluate(ABOUT_JS) or {}
        except Exception as e:  # noqa: BLE001
            self._ev(f"could not read About page: {str(e)[:120]}", level="warn")
            return {}

    def _run_update(self, run_id, **cols):
        from chanakya.storage.db import jdump
        with self.db_factory() as con:
            ensure_run_table(con)
            sets = ", ".join(f"{k}=?" for k in cols)
            con.execute(f"UPDATE linkedin_runs SET {sets} WHERE id=?",
                        [jdump(v) if isinstance(v, (list, dict)) else v for v in cols.values()] + [run_id])
            con.commit()

    def _accelerator(self, page, job):
        from chanakya.linkedin import claude_research as CR
        from chanakya.linkedin import pipeline as PL
        from chanakya.storage.db import now
        plans = job.get("plans") or [job["plan"]]
        multi = len(plans) > 1
        plan = plans[0] if not multi else {
            "accelerator": f"{len(plans)} accelerators", "product_id": ",".join(p["product_id"] for p in plans),
            "source": "; ".join(sorted({p.get("source", "") for p in plans})), "buyer_titles": [],
            "company_queries": [], **{k: v for k, v in (job.get("extras") or {}).items()
                                      if k in ("roles", "geography", "geo_urn", "revenue", "company_sizes",
                                               "authority_content")}}
        if multi:  # merge the plans' company searches, deduplicated, capped to protect the LinkedIn page budget
            per = max(1, -(-10 // len(plans)))
            seen_q = set()
            for p in plans:
                for q in p["company_queries"][:per]:
                    k = q["query"].strip().lower()
                    if k not in seen_q:
                        seen_q.add(k)
                        plan["company_queries"].append({**q, "for": p["accelerator"]})
            plan["company_queries"] = plan["company_queries"][:10]
        custom = [q.strip() for q in (job.get("extras") or {}).get("company_queries") or [] if q and q.strip()]
        if custom:  # the person's own company searches replace the planned ones (still capped at 10)
            plan["company_queries"] = [{"query": q, "why": "set for this run"} for q in custom[:10]]
        with self.db_factory() as con:
            ensure_run_table(con)
            geo = (plan.get("geography") or "").strip() or None
            roles = [r for r in (plan.get("roles") or []) if isinstance(r, str) and r.strip()]
            # authority content: a list given for this run wins; otherwise each accelerator's master-sheet use cases
            given = [c for c in (plan.get("authority_content") or (job.get("extras") or {}).get("authority_content")
                                 or []) if isinstance(c, str) and c.strip()]
            content = given or mastersheet_content(con, [p["product_id"] for p in plans])
            settings = {"geography": geo, "roles": roles, "authority_content": content,
                        "authority_content_source": ("set for this run" if given else
                                                     "master sheet use cases" if content else None),
                        "events": bool(plan.get("events")), "event_sources": plan.get("event_sources") or [],
                        "max_companies": job.get("max_companies"), "revenue": plan.get("revenue") or None,
                        "company_queries": custom or None, "company_sizes": plan.get("company_sizes") or None}
            run_id = con.execute("INSERT INTO linkedin_runs(product_id,accelerator,status,started_by,started_at,"
                                 "settings_json) VALUES(?,?,?,?,?,?)",
                                 (plan["product_id"], plan["accelerator"], "searching", job.get("by"), now(),
                                  json.dumps(settings))).lastrowid
            con.commit()
        self._set(status="searching", target=plan["accelerator"], inspected=0, matches=0, new=0, duplicates=0,
                  skipped=0, run={"id": run_id, "status": "searching", "accelerator": plan["accelerator"]},
                  followup=None)
        self._ev(f"run #{run_id}: accelerator search for {plan['accelerator']} (plan by {plan.get('source')}): "
                 f"{len(plan['company_queries'])} company searches")
        geo_urn = plan.get("geo_urn") or (GEO_URNS.get(geo.lower()) if geo else None)
        if geo:
            self._ev(f"location: {geo}" + (" (LinkedIn HQ filter on)" if geo_urn else
                                           " (added to the search words; research keeps only companies there)"))
        comps, seen = [], set()
        for cq in plan["company_queries"]:
            q = cq["query"] if (not geo or geo_urn) else f"{cq['query']} {geo}"
            for c in self._companies(page, q, job.get("company_pages", 1), geo_urn, plan.get("company_sizes")):
                c["title"] = PL.clean_company_name(c["title"])
                if c["url"] not in seen and PL.real_company(c, cq["query"]):
                    seen.add(c["url"])
                    comps.append({**c, "query": cq["query"]})
        top = _round_robin(comps, job.get("max_companies", 5))
        self._ev(f"{len(comps)} companies found; Claude is researching the top {len(top)} on the web in parallel "
                 "(website, CEO, CFO, with sources)")
        from chanakya.linkedin.planner import catalogue
        accs, name_to_pid = [], {}
        for p in plans:
            try:
                with self.db_factory() as con:
                    cat = catalogue(con, p["product_id"])
                accs.append({k: cat[k] for k in ("name", "summary", "problems_solved", "target_industries",
                                                 "buyer_roles", "needs")})
                name_to_pid[cat["name"]] = p["product_id"]
            except KeyError:  # accelerator missing from the catalogue: research without scoring it, reported
                self._ev(f"{p['product_id']} is not in the catalogue; not scored", level="warn")
        acc = (accs if multi else accs[0]) if accs else None
        extras = {"roles": roles, "geography": geo, "authority_content": settings["authority_content"],
                  "revenue": settings["revenue"]} \
            if (roles or geo or settings["authority_content"] or settings["revenue"]) else None
        research = CR.research_all(top, accelerator=acc, extras=extras)
        people_ids, company_ids, buyer_ids = [], [], []

        def keep(it, cid, how, verified=True, buyer=False):
            with self.db_factory() as con:
                pid = PL.add_person(con, it, cid, how, verified=verified)
            if buyer and pid not in buyer_ids:
                buyer_ids.append(pid)
            if pid not in people_ids:
                people_ids.append(pid)
                self._set(new=len(people_ids), matches=self.state["matches"] + 1)
                self._ev(f"leader: {it['title']} - {it.get('current') or (it.get('lines') or [''])[0]}"
                         + ("" if verified else " (employer not named in headline)"), kind="candidate",
                         url=it.get("url"))

        for c, rs in zip(top, research):
            if self.cancel_flag.is_set() or self.stop_flag.is_set():
                break
            self._set(target=c["title"])
            if rs.get("error"):
                self._ev(f"Claude research for {c['title']} failed: {rs['error']}", level="warn")
            from chanakya.intel.entities import norm_domain
            web = rs.get("website")
            if not web:
                web = self._about(page, c).get("website")
            if web:
                c["website"], c["domain"] = web, norm_domain(web)
            c["about"] = rs.get("summary")
            for k in ("match_percent", "match_why", "buy_percent", "buy_why", "buy_sources", "matches", "best_accelerator",
                      "hq", "in_geography", "location", "content_score", "content_best", "content_why",
                      "revenue_cr", "revenue_fy", "revenue_source", "in_revenue_band"):
                if k in rs:
                    c[k] = rs[k]
            if geo and rs.get("in_geography") is False:  # hard location filter: researched, shown, not pursued
                c["filtered"] = f"not in {geo} (HQ {rs.get('hq') or 'unknown'})"
                self._ev(f"{c['title']}: skipped, {c['filtered']}", level="warn")
                continue
            if geo and rs.get("in_geography") is None:
                c["location_note"] = f"location in {geo} not confirmed"
            band = settings["revenue"]
            if band and rs.get("in_revenue_band") is False:  # same rule as location: researched, shown, not pursued
                c["filtered"] = (f"revenue \u20b9{rs.get('revenue_cr'):,.0f} Cr ({rs.get('revenue_fy') or 'latest'}) "
                                 f"outside \u20b9{band.get('min_cr') or 0:,.0f}-{band.get('max_cr') or 0:,.0f} Cr")
                self._ev(f"{c['title']}: skipped, {c['filtered']}", level="warn")
                continue
            if band and rs.get("in_revenue_band") is None:
                c["location_note"] = "; ".join(x for x in (c.get("location_note"), "revenue not confirmed by a source")
                                               if x)
            c["product_id"] = name_to_pid.get(rs.get("best_accelerator"), plans[0]["product_id"] if not multi else None)
            c["accelerator"] = rs.get("best_accelerator") or (plans[0]["accelerator"] if not multi else None)
            if "match_percent" in rs:
                self._ev(f"{c['title']}: {rs['match_percent']}% match"
                         + (f" with {c['accelerator']}" if multi and c.get("accelerator") else "")
                         + f", {rs['buy_percent']}% likely to buy", kind="score")
            self._ev(f"{c['title']}: website {web or 'unknown'}" + (f"; {rs['summary'][:120]}" if rs.get("summary")
                                                                     else ""))
            with self.db_factory() as con:
                cid = PL.add_company(con, c, c.get("product_id") or plans[0]["product_id"])
            company_ids.append(cid)
            missing = []
            for role in ("ceo", "cfo"):
                p = rs.get(role)
                if p:
                    keep({"title": p["name"], "url": p.get("linkedin_url"), "current": f"{p.get('title') or role.upper()}"
                          f" at {c['title']}", "lines": [], "source_url": p.get("source_url")}, cid,
                         f"Claude web research: {p.get('source_url')}")
                else:
                    missing.append(role.upper())
            got = set()
            for b in rs.get("buyers") or []:
                keep({"title": b["name"], "url": b.get("linkedin_url"), "current": f"{b.get('title') or b['role']} at "
                      f"{c['title']}", "lines": [], "source_url": b.get("source_url")}, cid,
                     f"Claude web research ({b['role']}): {b.get('source_url')}", buyer=True)
                got.add(b.get("role"))
            for role in [r for r in roles if r not in got]:  # LinkedIn People tab for roles Claude could not source
                words = [w for w in re.findall(r"[a-z&]+", role.lower()) if len(w) > 2 and w not in ("head", "manager")]
                for it in self._company_people(page, c["url"], role)[:5]:
                    head = " ".join((it.get("lines") or [])[:1]).lower()
                    if words and sum(w in head for w in words) >= max(1, len(words) // 2):
                        keep({**it, "current": (it.get("lines") or [role])[0]}, cid, f"{role} - {c['title']} People tab",
                             False, buyer=True)
                        break
                else:
                    self._ev(f"no {role} found for {c['title']}", level="warn")
            for kw in missing:  # LinkedIn People tab only for what Claude could not source
                hits = 0
                for it in self._company_people(page, c["url"], kw):
                    ok, verified = PL.leader_at(it, c["title"], kw)
                    if ok:
                        keep(it, cid, f"{kw} - {c['title']} People tab", verified)
                        hits += 1
                if not hits:
                    self._ev(f"no {kw} found for {c['title']}", level="warn")
            for t in [x["query"] for x in plan["buyer_titles"][:max(0, job.get("titles_per_company", 1) - 1)]]:
                if self.cancel_flag.is_set() or self.stop_flag.is_set():
                    break
                q = f"{t} {c['title']}"
                found = self._people(page, q, 1)
                for it in found:
                    it["current"] = next((l.split(":", 1)[1].strip() for l in it.get("lines") or []
                                          if l.lower().startswith("current:")), None)
                for it in [x for x in found if PL.works_at(x, c["title"])][:3]:
                    keep(it, cid, q)
            self._run_update(run_id, company_ids_json=company_ids, person_ids_json=people_ids)
        self._run_update(run_id, status="writing_drafts", companies_found_json=[
            {"name": c["title"], "linkedin_url": c["url"], "lines": c.get("lines"), "query": c.get("query"),
             "researched": c in top, "website": c.get("website"), "about": c.get("about"),
             **{k: c.get(k) for k in ("match_percent", "match_why", "buy_percent", "buy_why", "buy_sources",
                                      "matches", "accelerator", "product_id", "hq", "in_geography", "content_score",
                                      "content_best", "content_why", "filtered", "location_note", "revenue_cr",
                                      "revenue_fy", "revenue_source", "in_revenue_band")}}
            for c in comps])
        if settings["events"]:
            self._ev("finding relevant upcoming events and specialist press (second output of this run)")
            evs = CR.find_events(accs[0] if accs else {"name": plan["accelerator"]}, roles=roles or None,
                                 geography=geo, sources=settings["event_sources"])
            self._run_update(run_id, events_json=evs)
            self._ev(f"{len(evs)} relevant events found (Excel: Events sheet)")
        self._set(run={"id": run_id, "status": "writing_drafts", "accelerator": plan["accelerator"]},
                  last_search={"query": f"accelerator: {plan['accelerator']}", "companies": len(top),
                               "people": len(people_ids), "new": len(people_ids), "duplicates": 0,
                               "at": dt.datetime.now().isoformat(timespec="seconds")})
        self._ev(f"LinkedIn + web part done: {len(top)} companies, {len(people_ids)} leaders. Finding emails and "
                 "writing CEO/CFO drafts in the background", kind="done")
        threading.Thread(target=self._followup, args=(people_ids, plan, job.get("by") or "chanakya",
                                                      job.get("user_id"), run_id, buyer_ids),
                         name="linkedin-followup", daemon=True).start()

    def _followup(self, people_ids, plan, by, user_id=None, run_id=None, buyer_ids=None):
        """Emails (parallel) -> personalised CEO/CFO drafts. Saving to Outlook is one click afterwards."""
        import os
        from concurrent.futures import ThreadPoolExecutor
        from chanakya.linkedin import pipeline as PL
        from chanakya.workflow.services import make_llm
        fu = {"status": "running", "people": len(people_ids), "emails": 0, "drafts": 0, "outlook": 0}
        self._set(followup=fu)
        try:
            llm = make_llm(task="outreach_draft")
        except Exception:  # noqa: BLE001
            llm = None
        sender = os.getenv("CHANAKYA_SENDER", "Krisha\nWayam AI")

        def one(pid):
            with self.db_factory() as con:
                name = con.execute("SELECT full_name FROM people WHERE id=?", (pid,)).fetchone()[0]
                try:
                    u = PL.xray_missing_linkedin(con, pid)
                    if u:
                        self._ev(f"LinkedIn for {name} found by web X-ray search: {u}")
                except Exception as ex:  # noqa: BLE001
                    self._ev(f"X-ray search failed for {name}: {str(ex)[:120]}", level="warn")
                try:
                    e = PL.find_email(con, pid)
                    if e.get("email"):
                        fu["emails"] += 1
                    self._ev(f"email for {name}: {e.get('email') or 'not found'} ({e.get('status')})")
                except Exception as ex:  # noqa: BLE001
                    self._ev(f"email lookup failed for {name}: {str(ex)[:150]}", level="warn")
                if not PL.is_leader(con, pid) and pid not in (buyer_ids or []):
                    return
                if llm is None:
                    self._ev("no Claude model configured for drafts (ANTHROPIC_API_KEY)", level="error")
                    return
                try:
                    prod = PL.best_product(con, pid) or plan["product_id"].split(",")[0]
                    d = PL.write_draft(con, pid, prod, llm, sender)
                    fu["drafts"] += 1
                    shown = ("already written" if d.get("reused") else d["status"])
                    self._ev(f"draft #{d['draft_id']} for {name}: {shown}"
                             + (f" ({'; '.join(d.get('issues', []))[:150]})" if d.get("issues") else ""))
                except Exception as ex:  # noqa: BLE001
                    self._ev(f"draft failed for {name}: {str(ex)[:200]}", level="error")

        # one at a time: parallel writers made SQLite answer "database is locked" (live run 13 Nov)
        for pid in people_ids:
            if self.stop_flag.is_set():
                break
            one(pid)
        fu["status"] = "done"
        if run_id:
            from chanakya.storage.db import now
            self._run_update(run_id, status="ready", finished_at=now())
            self._set(run={**(self.state.get("run") or {}), "id": run_id, "status": "ready"})
        self._ev(f"run complete: {fu['emails']} emails, {fu['drafts']} CEO/CFO drafts. Download the Excel and press "
                 "'Draft all in Outlook'", kind="done")

    def _store_companies(self, items, job) -> dict:
        if not self.db_factory:
            return {"new": 0}
        from chanakya.storage.db import jdump, now
        new = 0
        with self.db_factory() as con:
            con.execute(COMPANY_TABLE)
            for it in items:
                cur = con.execute("INSERT OR IGNORE INTO linkedin_company_candidates(linkedin_url,name,lines_json,"
                                  "product_id,query,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                                  (it["url"], it["title"], jdump(it.get("lines") or []), job["plan"]["product_id"],
                                   it.get("query"), now(), now()))
                new += cur.rowcount
            con.commit()
        return {"new": new}

    def _store(self, items, job) -> dict:
        if not self.db_factory:
            return {"new": 0, "duplicates": 0, "lead_ids": []}
        from chanakya.people.finder import normalize_linkedin
        from chanakya.storage.db import finish_run, jdump, now, start_run
        new = dup = 0
        ids = []
        with self.db_factory() as con:
            run_id = start_run(con, "linkedin_live", {"query": job["query"], "by": job.get("by")})
            for it in items:
                url = normalize_linkedin(it["url"]) or it["url"]
                row = con.execute("SELECT id FROM bottomup_leads WHERE profile_url=?", (url,)).fetchone()
                if row:
                    dup += 1
                    ids.append(row[0])
                    continue
                head = (it.get("lines") or [None])[0]
                cur = next((l.split(":", 1)[1].strip() for l in it.get("lines") or [] if l.lower().startswith("current:")),
                           None)
                lid = con.execute(
                    "INSERT INTO bottomup_leads(workflow_id,query,profile_url,name,headline,search_json,status,"
                    "company_observed,harness_version,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (job.get("workflow_id"), job["query"], url, it["title"], head,
                     jdump({"item": it, "source": "linkedin_live_chrome", "by": job.get("by")}), "found",
                     company_from_headline(cur) or company_from_headline(head) or job.get("target"), VERSION, now(), now())).lastrowid
                con.execute("INSERT INTO bottomup_lead_history(lead_id,from_status,to_status,actor,note,at) "
                            "VALUES(?,?,?,?,?,?)", (lid, None, "found", "linkedin_live",
                                                    f"visible Chrome search: {job['query']}", now()))
                new += 1
                ids.append(lid)
            con.commit()
            finish_run(con, run_id, "ok", {"new": new, "duplicates": dup, "lead_ids": ids})
        return {"new": new, "duplicates": dup, "lead_ids": ids}


SESSION: LiveSession | None = None


def session(db_factory=None) -> LiveSession:
    global SESSION
    if SESSION is None:
        SESSION = LiveSession(db_factory)
    return SESSION
