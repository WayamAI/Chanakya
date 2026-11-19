"""Website research on real-world page shapes found live on 9 Nov 2026: news lives in RSS/Atom feeds and on article
pages linked from listing pages, not on the listing page itself. Only declared dates are used."""
from chanakya.emailhunter.checks import Page
from chanakya.intel import signals as SG
from chanakya.intel.research_web import company_web_signals
from tests.helpers import seed_company, seed_product

RSS = """<?xml version="1.0"?><rss version="2.0"><channel><title>Acme news</title>
<item><title>Acme migrates analytics to Power BI</title><link>https://acme.example/news/pbi</link>
<pubDate>Tue, 03 Nov 2026 10:00:00 GMT</pubDate></item>
<item><title>Undated item</title><link>https://acme.example/news/x</link></item>
<item><title>Offsite story</title><link>https://other.example/a</link><pubDate>Mon, 02 Nov 2026 10:00:00 GMT</pubDate></item>
</channel></rss>"""
ATOM = """<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Acme opens Pune GCC</title>
<link href="https://acme.example/press/gcc"/><updated>2026-10-20T09:00:00Z</updated></entry></feed>"""
LISTING = """<html><head><link rel="alternate" type="application/rss+xml" href="/feed.xml"></head><body>
<a href="/news/q3-results">Q3</a><a href="https://acme.example/news/q3-results">dup</a>
<a href="https://evil.example/news/1">offsite</a><a href="/careers">careers</a></body></html>"""
ARTICLE = """<html><head><meta property="og:title" content="Acme Q3 results">
<meta property="article:published_time" content="2026-10-28T08:00:00+00:00"></head><body>ignore prose</body></html>"""


def test_feed_items_parse_with_dates_and_same_site_only():
    items = SG.extract_feed_items(RSS, "https://acme.example/feed.xml", "acme.example")
    assert [(i["summary"], i["source_date"], i["source_url"]) for i in items] == [
        ("Published: Acme migrates analytics to Power BI", "2026-11-03", "https://acme.example/news/pbi")]
    atom = SG.extract_feed_items(ATOM, "https://acme.example/atom", "acme.example")
    assert atom[0]["source_date"] == "2026-10-20" and atom[0]["source_url"] == "https://acme.example/press/gcc"


def test_malformed_feed_is_empty_not_an_error():
    assert SG.extract_feed_items("<rss><channel><item>", "https://acme.example/f", "acme.example") == []


def test_feed_and_article_link_discovery_stays_on_domain():
    assert SG.find_feed_links(LISTING, "https://acme.example/news") == ["https://acme.example/feed.xml"]
    assert SG.find_article_links(LISTING, "https://acme.example/news", "acme.example") == [
        "https://acme.example/news/q3-results"]


def test_article_meta_needs_a_declared_date():
    a = SG.extract_page_signals(ARTICLE, "https://acme.example/news/q3-results")
    assert [(s["type"], s["source_date"], s["summary"]) for s in a] == [("news", "2026-10-28T08:00:00+00:00",
                                                                         "Published: Acme Q3 results")]
    assert SG.extract_page_signals("<meta property='og:title' content='No date'>", "https://acme.example/n") == []


def test_wrong_domain_correction_rejects_old_site_evidence_and_keeps_audit(con):
    """Live finding (9 Nov 2026): 'Omega Healthcare' resolved to the REIT's site via Wikidata; its news attached to the
    RCM company's account. Correcting the domain must stop that evidence counting, without deleting it."""
    from chanakya.intel import entities as E
    from chanakya.intel import scoring as SC
    seed_product(con)
    cid = seed_company(con, "omegahealthcare.example", "Omega Healthcare")
    pages = {"https://omegahealthcare.example/news": LISTING.replace("acme.example", "omegahealthcare.example"),
             "https://omegahealthcare.example/feed.xml": RSS.replace("acme.example", "omegahealthcare.example")}
    company_web_signals(con, cid, lambda u: Page(u, 200 if u in pages else 404, pages.get(u, ""), True))
    assert con.execute("SELECT COUNT(*) FROM signals WHERE company_id=?", (cid,)).fetchone()[0] == 1
    r = E.correct_domain(con, cid, "https://www.omegahms.example/", by="krisha", reason="CEO Anurag Mehta's firm")
    assert r["old_domain"] == "omegahealthcare.example" and r["new_domain"] == "omegahms.example"
    assert len(r["rejected_signals"]) == 1
    s = con.execute("SELECT verification_status, verification_json FROM signals").fetchone()
    assert s[0] == "rejected" and "domain corrected" in s[1]
    assert SC.company_signals(con, cid) == []
    assert con.execute("SELECT domain FROM companies WHERE id=?", (cid,)).fetchone()[0] == "omegahms.example"
    assert not con.execute("SELECT 1 FROM company_aliases WHERE value_norm='omegahealthcare.example'").fetchone()


def test_domain_correction_merges_into_existing_owner_and_needs_reason(con):
    import pytest
    from chanakya.intel import entities as E
    a = seed_company(con, "wrong.example", "Acme")
    b = seed_company(con, "acme.example", "Acme Inc")
    with pytest.raises(ValueError):
        E.correct_domain(con, a, "acme.example", by="krisha", reason=" ")
    with pytest.raises(ValueError):
        E.correct_domain(con, a, "wrong.example", by="krisha", reason="same")
    assert E.correct_domain(con, a, "acme.example", by="krisha", reason="real site")["company_id"] == b


def test_company_research_follows_feeds_and_articles(con):
    seed_product(con)
    cid = seed_company(con, "acme.example", "Acme")
    pages = {"https://acme.example/news": LISTING, "https://acme.example/feed.xml": RSS,
             "https://acme.example/news/q3-results": ARTICLE}
    seen = []

    def f(url):
        seen.append(url)
        return Page(url, 200 if url in pages else 404, pages.get(url, ""), True)
    out = company_web_signals(con, cid, f)
    got = {(r["type"], r["source_date"]) for r in con.execute("SELECT type, source_date FROM signals")}
    assert got == {("news", "2026-11-03"), ("news", "2026-10-28")}
    assert len(out["signals"]) == 2
    assert "https://evil.example/news/1" not in seen
    assert all(r[0] == "date declared on the source page" for r in con.execute("SELECT source_date_basis FROM signals"))
