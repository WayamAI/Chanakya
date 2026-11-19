"""Agent 5 building blocks: syntax validation, MX lookup, name parsing, address patterns, website scraping.

Truthfulness rules:
  * An MX record proves only that the DOMAIN accepts mail. It never proves that a mailbox exists.
  * No SMTP RCPT probing is done (it is unreliable, often blocked, and can look like abuse).
"""
from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
import unicodedata
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import urljoin, urlparse

EMAIL_RE = re.compile(r"(?<![A-Za-z0-9._%+-])([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})(?![A-Za-z0-9-])")
SYNTAX_RE = re.compile(r"^[a-z0-9](?:[a-z0-9._%+-]{0,62}[a-z0-9])?@(?:[a-z0-9-]+\.)+[a-z]{2,}$")
ROLE_LOCALS = frozenset({"info", "contact", "sales", "support", "hello", "admin", "hr", "careers", "jobs", "office",
                         "enquiry", "enquiries", "inquiry", "marketing", "media", "press", "help", "team", "mail",
                         "noreply", "no-reply", "webmaster", "privacy", "legal", "accounts", "billing", "feedback",
                         "secretary", "president", "director", "ceo", "pr", "investor", "investors", "grievance"})
HONORIFICS = frozenset({"mr", "mrs", "ms", "dr", "prof", "ca", "cs", "er", "sir", "shri", "smt", "adv", "phd",
                        "mba", "cfa", "cpa", "jr", "sr", "ii", "iii"})
CONTACT_PATHS = ("", "/contact", "/contact-us", "/about", "/about-us", "/team", "/leadership", "/our-team",
                 "/management")


def valid_syntax(email: str | None) -> bool:
    return bool(email) and bool(SYNTAX_RE.match(email.strip().lower()))


def _ascii(s: str) -> str:
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()


def name_parts(full_name: str) -> tuple[str | None, str | None]:
    toks = [t for t in re.split(r"[^a-z]+", _ascii(full_name or "")) if t and t not in HONORIFICS]
    if not toks:
        return None, None
    return toks[0], (toks[-1] if len(toks) > 1 else None)


PATTERNS: dict[str, Callable[[str, str], str]] = {
    "first.last": lambda f, l: f"{f}.{l}",
    "firstlast": lambda f, l: f"{f}{l}",
    "first_last": lambda f, l: f"{f}_{l}",
    "flast": lambda f, l: f"{f[0]}{l}",
    "f.last": lambda f, l: f"{f[0]}.{l}",
    "firstl": lambda f, l: f"{f}{l[0]}",
    "last.first": lambda f, l: f"{l}.{f}",
    "lastf": lambda f, l: f"{l}{f[0]}",
    "first": lambda f, l: f,
}


def apply_pattern(pattern: str, full_name: str, domain: str) -> str | None:
    f, l = name_parts(full_name)
    if not f or (pattern != "first" and not l):
        return None
    return f"{PATTERNS[pattern](f, l or '')}@{domain}"


def patterns_matching(local: str, full_name: str) -> list[str]:
    f, l = name_parts(full_name)
    if not f or not l:
        return []
    return [p for p, fn in PATTERNS.items() if fn(f, l) == local.lower()]


def on_domain(email: str, domain: str) -> bool:
    host = email.rsplit("@", 1)[-1].lower()
    return host == domain or host.endswith("." + domain)


# ---------------- MX ----------------
@dataclass
class MXResult:
    status: str  # "ok" (has MX), "none" (domain has no MX), "unknown" (lookup failed / no resolver)
    hosts: list[str] = field(default_factory=list)
    method: str = ""
    error: str | None = None

    def as_dict(self) -> dict:
        return {"status": self.status, "hosts": self.hosts, "method": self.method, "error": self.error,
                "note": "MX shows the domain accepts mail; it does not prove the mailbox exists."}


def _mx_dnspython(domain: str) -> MXResult:
    import dns.resolver  # optional dependency
    try:
        ans = dns.resolver.resolve(domain, "MX", lifetime=8)
        return MXResult("ok", sorted(str(r.exchange).rstrip(".") for r in ans), "dnspython")
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
        return MXResult("none", [], "dnspython")


def _mx_nslookup(domain: str) -> MXResult:
    exe = shutil.which("nslookup")
    if not exe:
        return MXResult("unknown", [], "none", "no DNS resolver available (install dnspython)")
    p = subprocess.run([exe, "-type=mx", domain], capture_output=True, text=True, timeout=15)
    out = (p.stdout or "") + (p.stderr or "")
    hosts = sorted({m.group(1).rstrip(".").lower() for m in re.finditer(r"mail exchanger\s*=\s*(?:\d+\s+)?(\S+)", out)})
    if hosts:
        return MXResult("ok", hosts, "nslookup")
    if re.search(r"non-existent domain|NXDOMAIN|No answer|can't find", out, re.I):
        return MXResult("none", [], "nslookup")
    return MXResult("unknown", [], "nslookup", out.strip()[-200:] or "no MX answer parsed")


def check_mx(domain: str) -> MXResult:
    try:
        return _mx_dnspython(domain)
    except ImportError:
        pass
    except Exception as e:  # timeouts, SERVFAIL
        return MXResult("unknown", [], "dnspython", f"{type(e).__name__}: {e}")
    try:
        return _mx_nslookup(domain)
    except Exception as e:
        return MXResult("unknown", [], "nslookup", f"{type(e).__name__}: {e}")


# ---------------- website ----------------
@dataclass
class Page:
    url: str
    status: int | None
    text: str
    robots_allowed: bool
    error: str | None = None

    @property
    def sha256(self) -> str | None:
        return hashlib.sha256(self.text.encode()).hexdigest() if self.text else None


Fetcher = Callable[[str], Page]
USER_AGENT = "ChanakyaResearchBot/0.1 (public contact page reader; respects robots.txt)"


def default_fetcher(timeout: float = 15.0) -> Fetcher:
    """httpx GET with robots.txt respected per host. Read-only, small pages, no form submission."""
    import httpx
    from urllib.robotparser import RobotFileParser

    robots: dict[str, RobotFileParser | None] = {}
    client = httpx.Client(timeout=timeout, follow_redirects=True, headers={"User-Agent": USER_AGENT})

    def allowed(url: str) -> bool:
        host = urlparse(url).scheme + "://" + urlparse(url).netloc
        if host not in robots:
            rp = RobotFileParser()
            try:
                r = client.get(host + "/robots.txt")
                rp.parse(r.text.splitlines() if r.status_code == 200 else [])
                robots[host] = rp
            except Exception:
                robots[host] = None  # robots unreachable: treat as allowed (standard crawler behaviour)
        rp = robots[host]
        return True if rp is None else rp.can_fetch(USER_AGENT, url)

    def fetch(url: str) -> Page:
        if not allowed(url):
            return Page(url, None, "", False, "disallowed by robots.txt")
        import time
        r = None
        for attempt in range(3):  # transient failures (timeouts, connection resets, 429/502/503/504): back off
            try:
                r = client.get(url)
                if r.status_code not in (429, 502, 503, 504) or attempt == 2:
                    break
            except (httpx.TimeoutException, httpx.TransportError) as e:
                if attempt == 2:
                    return Page(url, None, "", True, f"{type(e).__name__}: {e} (after 3 attempts)")
            time.sleep(1.5 * (attempt + 1))
        try:
            ctype = r.headers.get("content-type", "")
            # html/text pages plus RSS/Atom feeds (application/rss+xml, application/atom+xml, application/xml)
            text = r.text[:500_000] if any(t in ctype for t in ("html", "text", "xml")) else ""
            return Page(str(r.url), r.status_code, text, True)
        except Exception as e:
            return Page(url, None, "", True, f"{type(e).__name__}: {e}")
    return fetch


def extract_emails(html: str) -> list[str]:
    text = re.sub(r"\s*[\[\(]at[\]\)]\s*", "@", html, flags=re.I)  # "name [at] example.com" obfuscation
    text = re.sub(r"\s*[\[\(]dot[\]\)]\s*", ".", text, flags=re.I)
    found = []
    for m in EMAIL_RE.finditer(text):
        e = m.group(1).strip(".").lower()
        if valid_syntax(e) and not re.search(r"\.(png|jpe?g|gif|svg|webp|css|js)$", e) and e not in found:
            found.append(e)
    return found


def contact_urls(domain: str) -> list[str]:
    base = f"https://{domain}"
    return [urljoin(base + "/", p.lstrip("/")) if p else base + "/" for p in CONTACT_PATHS]
