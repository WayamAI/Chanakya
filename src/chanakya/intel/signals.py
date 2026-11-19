"""Demand signals (Lakshya layer 1): store, attach to canonical companies, fit to accelerators, verify sources.

Rules (handover):
  * Each signal keeps its source URL, SOURCE date, CAPTURE date, summary, company, type, verification and evidence.
  * Recency = exp(-days/60) from the source date (settings.recency_basis); the capture date is used only when no source
    date is known, and the basis actually used is returned so the UI can say so.
  * Verification is never automatic: fetching a URL can show it is reachable and mentions the company
    ('source_reachable' / 'source_mentions_company'); only a human sets 'verified'. Single-source and vendor claims stay
    unverified.
  * Signals are idempotent by external_id (e.g. 'lakshya:sig-01') or by (company, url, type).
"""
from __future__ import annotations

import datetime as dt
import json
import math
import re
from pathlib import Path

from chanakya.intel import entities as E
from chanakya.intel import settings as S
from chanakya.storage.db import jdump, now

VERIFICATION = ("unverified", "source_reachable", "source_mentions_company", "source_unreachable", "robots_disallowed",
                "verified", "rejected")
# Handover accelerator ids -> catalogue product ids
def product_id_for(acc_id: str) -> str:
    return "PRD-" + acc_id.upper()


def parse_date(s) -> dt.date | None:
    if not s:
        return None
    if isinstance(s, (int, float)):  # epoch ms (Lakshya createdAt)
        return dt.datetime.fromtimestamp(s / 1000, dt.timezone.utc).date()
    try:
        return dt.date.fromisoformat(str(s)[:10])
    except ValueError:
        return None


def recency(con, source_date, captured_at, today: dt.date | None = None) -> dict:
    today = today or dt.date.today()
    basis_pref = S.get(con, "recency_basis")
    sd, cd = parse_date(source_date), parse_date(captured_at)
    if basis_pref == "source_date" and sd:
        used, d = "source_date", sd
    elif cd:
        used, d = ("capture_date_fallback" if basis_pref == "source_date" else "capture_date"), cd
    else:
        used, d = "unknown", today
    days = max(0, (today - d).days)
    return {"r": round(math.exp(-days / float(S.get(con, "recency_half_scale_days"))), 4), "days": days, "basis": used}


def signal_strength(con, s) -> dict:
    """weight(type) x recency. The building block of every Lakshya score."""
    rec = recency(con, s["source_date"], s["captured_at"])
    w = S.weight(con, s["type"])
    return {"weight": w, **rec, "strength": round(w * rec["r"], 4)}


def store_signal(con, *, type: str, summary: str, direction: str, provenance: str, company: dict | None = None,
                 external_id: str | None = None, source_url: str | None = None, source_date=None,
                 source_date_basis: str | None = None, captured_at: str | None = None, industry=None, region=None,
                 roles=None, urgency=None, budget=None, noise=False, is_gap=False, gap_theme=None, gap_need=None,
                 reuse=None, angle=None, fits: list[dict] | None = None, fit_method: str = "analyst",
                 market_subject: str | None = None,
                 workflow_id=None, verification_status: str = "unverified") -> tuple[int, bool]:
    """company = kwargs for entities.resolve_company (name/domain/linkedin_url/key). Returns (signal_id, created)."""
    if type not in S.SIGNAL_TYPES:
        raise ValueError(f"unknown signal type {type!r}; one of {sorted(S.SIGNAL_TYPES)}")
    cid = None
    if company and (company.get("name") or company.get("domain") or company.get("key")):
        cid = E.resolve_company(con, direction=direction, source=f"signal:{provenance}", **company)["company_id"]
    row = None
    if external_id:
        row = con.execute("SELECT id FROM signals WHERE external_id=?", (external_id,)).fetchone()
    elif source_url:
        row = con.execute("SELECT id FROM signals WHERE source_url=? AND type=? AND company_id IS ?",
                          (source_url, type, cid)).fetchone()
    t = now()
    if not parse_date(source_date) and not source_date_basis:
        source_date_basis = "no source date supplied (capture time only)"
    vals = dict(company_id=cid, company_name=(company or {}).get("name") or market_subject,
                company_key=(company or {}).get("key") or ("unknown" if market_subject else None),
                type=type, source_url=source_url, source_date=str(parse_date(source_date)) if parse_date(source_date)
                else None, source_date_basis=source_date_basis, summary=summary, industry=industry, region=region,
                roles_json=jdump(roles or []), urgency=urgency, budget=budget, noise=int(bool(noise)),
                is_gap=int(bool(is_gap)), gap_theme=gap_theme or None, gap_need=gap_need or None, reuse=reuse or None,
                angle=angle or None, provenance=provenance, direction=direction, workflow_id=workflow_id)
    if row:
        sid = row[0]
        sets = ", ".join(f"{k}=COALESCE(?, {k})" if k in ("source_date", "company_id") else f"{k}=?" for k in vals)
        con.execute(f"UPDATE signals SET {sets}, updated_at=? WHERE id=?", (*vals.values(), t, sid))
        created = False
    else:
        sid = con.execute(f"INSERT INTO signals({','.join(vals)},external_id,captured_at,verification_status,created_at,"
                          f"updated_at) VALUES({','.join('?' * (len(vals) + 5))})",
                          (*vals.values(), external_id, captured_at or t, verification_status, t, t)).lastrowid
        created = True
    for f in fits or []:
        pid = f.get("product_id") or product_id_for(f["id"])
        if con.execute("SELECT 1 FROM products WHERE id=?", (pid,)).fetchone():
            con.execute("INSERT INTO signal_fits(signal_id,product_id,score,why,method,created_at) VALUES(?,?,?,?,?,?) "
                        "ON CONFLICT(signal_id,product_id,method) DO UPDATE SET score=excluded.score, why=excluded.why",
                        (sid, pid, int(f["score"]), f.get("why"), fit_method, t))
    E.record_discovery(con, "signal", sid, direction, provenance, workflow_id=workflow_id,
                       detail={"external_id": external_id, "created": created})
    return sid, created


def best_fit(con, signal_id: int) -> tuple[str | None, int]:
    r = con.execute("SELECT product_id, MAX(score) FROM signal_fits WHERE signal_id=?", (signal_id,)).fetchone()
    return (r[0], r[1] or 0) if r and r[0] else (None, 0)


# ---------------- import: Lakshya handover research (real analyst research, 4 Nov 2026) ----------------
LAKSHYA_PROVENANCE = "lakshya-handover-research-2026-11-04"


def import_lakshya_seed(con, path: str | Path, *, workflow_id=None) -> dict:
    """Load lakshya_seed_all.json: 38 signals with source links, 27 accounts, 3 gap themes. These are REAL research
    findings from the handover (not synthetic), stored as top-down signals, unverified until re-checked. The seed has no
    source date per signal, only the capture time: source_date stays empty and the basis says so."""
    d = json.loads(Path(path).read_text(encoding="utf-8"))
    created = updated = 0
    for sid_ext, s in d["signals"].items():
        _, new = store_signal(
            con, type=s["type"], summary=s["summary"], direction=E.TOP, provenance=LAKSHYA_PROVENANCE,
            # companyKey 'unknown' = a market-level signal (e.g. a regulation for all payers): no account is created
            company=None if (s.get("companyKey") or "unknown") == "unknown" else {"name": s["company"],
                                                                                   "key": s.get("companyKey")},
            market_subject=s["company"], external_id=f"lakshya:{sid_ext}",
            source_url=s.get("url") or None, source_date=None,
            source_date_basis="not recorded in handover seed (capture time only)",
            captured_at=dt.datetime.fromtimestamp(s["createdAt"] / 1000, dt.timezone.utc).isoformat(timespec="seconds"),
            industry=s.get("industry"), region=s.get("region"), roles=s.get("roles"), urgency=s.get("urgency"),
            budget=s.get("budget"), noise=s.get("noise"), is_gap=s.get("isGap"), gap_theme=s.get("gapTheme"),
            gap_need=s.get("gapNeed"), reuse=s.get("reuse"), angle=s.get("angle"), fits=s.get("fits"),
            fit_method="analyst (handover)", workflow_id=workflow_id)
        created += new
        updated += not new
        if s.get("text"):
            con.execute("UPDATE signals SET verification_json=COALESCE(verification_json, ?) WHERE external_id=?",
                        (jdump({"handover_note": s["text"]}), f"lakshya:{sid_ext}"))
    for key, a in d.get("accounts", {}).items():
        E.resolve_company(con, name=a["name"], key=key, direction=E.TOP, source=LAKSHYA_PROVENANCE,
                          workflow_id=workflow_id, detail={"lakshya_stage": a.get("stage"), "notes": a.get("notes")})
    con.commit()
    return {"signals_created": created, "signals_updated": updated, "accounts": len(d.get("accounts", {})),
            "themes": len(d.get("themes", {}))}


# ---------------- live source verification ----------------
_DATE_PATTERNS = [
    r'"datePosted"\s*:\s*"([0-9]{4}-[0-9]{2}-[0-9]{2})', r'"datePublished"\s*:\s*"([0-9]{4}-[0-9]{2}-[0-9]{2})',
    r'property="article:published_time"\s+content="([0-9]{4}-[0-9]{2}-[0-9]{2})',
    r'content="([0-9]{4}-[0-9]{2}-[0-9]{2})[^"]*"\s+property="article:published_time"',
    r'<time[^>]+datetime="([0-9]{4}-[0-9]{2}-[0-9]{2})']


def published_date(html: str) -> str | None:
    for p in _DATE_PATTERNS:
        m = re.search(p, html or "", re.I)
        if m:
            return m.group(1)
    return None


def page_title(html: str) -> str | None:
    m = re.search(r"<title[^>]*>(.*?)</title>", html or "", re.I | re.S)
    return re.sub(r"\s+", " ", m.group(1)).strip()[:200] if m else None


def verify_signal(con, signal_id: int, fetcher) -> dict:
    """Fetch the source URL (robots.txt respected by the fetcher). Records reachability, title, whether the page names
    the company, and a published date found in the page metadata (which becomes the source date if none was known)."""
    s = con.execute("SELECT * FROM signals WHERE id=?", (signal_id,)).fetchone()
    if not s["source_url"]:
        return {"status": s["verification_status"], "reason": "no source URL"}
    p = fetcher(s["source_url"])
    company = s["company_name"] or ""
    toks = [t for t in E.norm_name(company).split() if len(t) > 2]
    text = (p.text or "").lower()
    mentions = bool(toks) and sum(t in text for t in toks) >= max(1, len(toks) - 1)
    pub = published_date(p.text) if p.text else None
    if not p.robots_allowed:
        status = "robots_disallowed"
    elif p.status and p.status < 400 and p.text:
        status = "source_mentions_company" if mentions else "source_reachable"
    else:
        status = "source_unreachable"
    prev = json.loads(s["verification_json"] or "{}")
    ver = {**prev, "checked_at": now(), "http_status": p.status, "final_url": p.url, "title": page_title(p.text),
           "mentions_company": mentions, "published_date_found": pub, "error": p.error,
           "note": "Automatic check only: reachable/mentions. A human must open the page to mark it verified."}
    if s["verification_status"] in ("verified", "rejected"):  # never override a human decision
        status = s["verification_status"]
    con.execute("UPDATE signals SET verification_status=?, verification_json=?, updated_at=? WHERE id=?",
                (status, jdump(ver), now(), signal_id))
    if pub and not s["source_date"]:
        con.execute("UPDATE signals SET source_date=?, source_date_basis=? WHERE id=?",
                    (pub, "published date from source page metadata", signal_id))
    con.commit()
    return {"status": status, **ver}


def review_signal(con, signal_id: int, status: str, *, by: str, reason: str) -> None:
    if status not in ("verified", "rejected", "unverified"):
        raise ValueError("status must be verified, rejected or unverified")
    if not (by or "").strip() or not (reason or "").strip():
        raise ValueError("reviewer and reason are required")
    s = con.execute("SELECT verification_json FROM signals WHERE id=?", (signal_id,)).fetchone()
    if s is None:
        raise ValueError(f"unknown signal {signal_id}")
    v = json.loads(s[0] or "{}")
    v.setdefault("human_reviews", []).append({"status": status, "by": by, "reason": reason, "at": now()})
    con.execute("UPDATE signals SET verification_status=?, verification_json=?, updated_at=? WHERE id=?",
                (status, jdump(v), now(), signal_id))
    con.commit()


# ---------------- deterministic signals from a company's own public pages ----------------
def jsonld_blocks(html: str) -> list:
    out = []
    for m in re.finditer(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', html or "", re.I | re.S):
        try:
            v = json.loads(m.group(1).strip())
        except ValueError:
            continue
        stack = v if isinstance(v, list) else [v]
        while stack:
            x = stack.pop()
            if isinstance(x, dict):
                out.append(x)
                stack += [y for y in x.get("@graph", []) if isinstance(y, dict)]
            elif isinstance(x, list):
                stack += x
    return out


def extract_page_signals(html: str, url: str) -> list[dict]:
    """Job postings (hiring) and dated articles (news) declared in structured data. Nothing is inferred from prose."""
    out = []
    for b in jsonld_blocks(html):
        t = b.get("@type")
        types = t if isinstance(t, list) else [t]
        if "JobPosting" in types and b.get("title"):
            out.append({"type": "hiring", "summary": f"Job posting: {b['title']}"[:400], "source_date": b.get("datePosted"),
                        "source_url": b.get("url") or url, "evidence": {"title": b.get("title"),
                                                                       "description": re.sub(r"<[^>]+>", " ",
                                                                                             str(b.get("description", "")))[:1500]}})
        elif any(x in types for x in ("NewsArticle", "BlogPosting", "Article", "PressRelease")) and b.get("headline"):
            out.append({"type": "news", "summary": f"Published: {b['headline']}"[:400],
                        "source_date": b.get("datePublished"), "source_url": b.get("url") or url,
                        "evidence": {"headline": b.get("headline")}})
    if not out:
        # Article pages often declare their date only in Open Graph / article meta tags, not JSON-LD.
        date = _meta(html, "article:published_time") or _meta(html, "datePublished")
        title = _meta(html, "og:title")
        if date and title:
            out.append({"type": "news", "summary": f"Published: {title}"[:400], "source_date": date,
                        "source_url": url, "evidence": {"headline": title, "date_from": "meta tag"}})
    return out


def _meta(html: str, prop: str) -> str | None:
    for tag in re.findall(r"<meta\b[^>]*>", html or "", re.I):
        if re.search(rf"""(?:property|name|itemprop)\s*=\s*["']{re.escape(prop)}["']""", tag, re.I):
            m = re.search(r"""content\s*=\s*["']([^"']+)["']""", tag, re.I)
            if m:
                return m.group(1).strip()
    return None


def _same_site(url: str, domain: str) -> bool:
    from urllib.parse import urlparse
    host = (urlparse(url).hostname or "").lower()
    d = domain.lower()
    return host == d or host.endswith("." + d)


def find_feed_links(html: str, base_url: str) -> list[str]:
    """RSS/Atom feeds the page declares with <link rel="alternate">."""
    from urllib.parse import urljoin
    out = []
    for tag in re.findall(r"<link\b[^>]*>", html or "", re.I):
        if re.search(r"""type\s*=\s*["']application/(rss|atom)\+xml["']""", tag, re.I):
            m = re.search(r"""href\s*=\s*["']([^"']+)["']""", tag, re.I)
            if m:
                u = urljoin(base_url, m.group(1).strip())
                if u not in out:
                    out.append(u)
    return out


ARTICLE_PATH = re.compile(r"/(news|press|press-releases?|newsroom|media|blog|insights|articles?)/[^/?#]+", re.I)


def find_article_links(html: str, base_url: str, domain: str, limit: int = 5) -> list[str]:
    """Same-site links that look like individual news/press/blog articles, in page order."""
    from urllib.parse import urljoin, urldefrag
    out = []
    for href in re.findall(r"""<a\b[^>]*href\s*=\s*["']([^"']+)["']""", html or "", re.I):
        u = urldefrag(urljoin(base_url, href.strip()))[0].rstrip("/")
        if u.startswith("http") and _same_site(u, domain) and ARTICLE_PATH.search(u) and u not in out \
                and u != base_url.rstrip("/"):
            out.append(u)
            if len(out) >= limit:
                break
    return out


def extract_feed_items(xml: str, feed_url: str, domain: str, limit: int = 10) -> list[dict]:
    """Dated items from an RSS 2.0 or Atom feed. Undated items and links to other sites are skipped."""
    import email.utils
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring((xml or "").strip().encode("utf-8"))
    except ET.ParseError:
        return []
    strip = lambda t: t.split("}", 1)[-1]  # noqa: E731
    out = []
    for el in root.iter():
        if strip(el.tag) not in ("item", "entry"):
            continue
        f = {strip(c.tag): c for c in el}
        title = (f["title"].text or "").strip() if "title" in f else ""
        link_el = f.get("link")
        link = (link_el.get("href") or link_el.text or "").strip() if link_el is not None else ""
        raw = next(((f[k].text or "").strip() for k in ("pubDate", "published", "updated", "date") if k in f), "")
        date = None
        if raw:
            try:
                date = email.utils.parsedate_to_datetime(raw).date()
            except (TypeError, ValueError):
                date = parse_date(raw[:10])
        if not (title and link and date and _same_site(link, domain)):
            continue
        out.append({"type": "news", "summary": f"Published: {title}"[:400], "source_date": str(date),
                    "source_url": link, "evidence": {"headline": title, "feed": feed_url}})
        if len(out) >= limit:
            break
    return out


def rule_fit(con, text: str) -> list[dict]:
    """Conservative keyword fit between signal text and each accelerator's stated needs (no LLM). Capped at 55, so a
    rule fit alone can never clear the 60 'strong fit' line; labelled method='rule (keyword overlap, needs review)'."""
    words = set(re.findall(r"[a-z]{5,}", (text or "").lower()))
    out = []
    for r in con.execute("SELECT id, needs_json, summary FROM products"):
        needs = " ".join([(n.get("quote") or "") for n in json.loads(r[1] or "[]") if isinstance(n, dict)] + [r[2] or ""])
        nw = set(re.findall(r"[a-z]{5,}", needs.lower())) - {"which", "their", "about", "these", "where", "there"}
        hits = sorted(words & nw)
        if len(hits) >= 2:
            out.append({"product_id": r[0], "score": min(55, 20 + 7 * len(hits)), "why": "shared terms: " +
                        ", ".join(hits[:8])})
    return sorted(out, key=lambda f: -f["score"])[:3]
