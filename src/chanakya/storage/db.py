"""SQLite persistence shared by agents. Each agent owns its tables; no cross-agent writes."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS offerings (            -- Agent 1: Offering DB
  id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, description TEXT,
  problems_json TEXT, source_path TEXT NOT NULL, source_sha256 TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS products (             -- Agent 1: Offering DB (structured catalogue import)
  id TEXT PRIMARY KEY, name TEXT NOT NULL, status TEXT, summary TEXT, problems_solved_json TEXT,
  target_industries_json TEXT, target_industries_note TEXT, buyer_roles_json TEXT, integrations_json TEXT,
  deployment TEXT, differentiators_json TEXT, caveats_json TEXT, evidence_json TEXT, capability_ids_json TEXT,
  needs_json TEXT, verification TEXT NOT NULL, upstream_review_json TEXT, source_path TEXT NOT NULL,
  source_sha256 TEXT NOT NULL, catalogue_version TEXT, raw_json TEXT NOT NULL, imported_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS icps (                 -- Agent 1: ICP Builder output
  id INTEGER PRIMARY KEY, version INTEGER NOT NULL, icp_json TEXT NOT NULL,
  source_shas TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS companies (            -- Agent 2: SQL Companies DB
  id INTEGER PRIMARY KEY, domain TEXT NOT NULL UNIQUE, name TEXT,
  data_json TEXT NOT NULL, fit_score INTEGER, fit_json TEXT, icp_id INTEGER REFERENCES icps(id),
  first_seen TEXT NOT NULL, last_seen TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS company_sources (      -- provenance for every observation
  id INTEGER PRIMARY KEY, company_id INTEGER NOT NULL REFERENCES companies(id),
  source TEXT NOT NULL, tool TEXT, clay_task_id TEXT, source_url TEXT, query TEXT,
  raw_json TEXT NOT NULL, fetched_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS industry_mappings (    -- Agent 2: ICP term -> Clay taxonomy, with review trail
  id INTEGER PRIMARY KEY, icp_id INTEGER REFERENCES icps(id), field TEXT NOT NULL, original_term TEXT NOT NULL,
  canonical_json TEXT NOT NULL, status TEXT NOT NULL, method TEXT NOT NULL, confidence REAL NOT NULL,
  rationale TEXT, alternatives_json TEXT, rejected_json TEXT, created_at TEXT NOT NULL,
  reviewed_by TEXT, reviewed_at TEXT);
CREATE TABLE IF NOT EXISTS raw_responses (        -- verbatim provider responses, one per discovery call
  id INTEGER PRIMARY KEY, provider TEXT NOT NULL, tool TEXT NOT NULL, task_id TEXT, query TEXT,
  response_json TEXT NOT NULL, fetched_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS human_reviews (        -- explicit human qualification decisions (source of truth)
  id INTEGER PRIMARY KEY, company_id INTEGER NOT NULL REFERENCES companies(id), status TEXT NOT NULL,
  reviewer TEXT NOT NULL, reason TEXT NOT NULL, reviewed_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS legacy_fit_archive (   -- pre-v2 'fit' values, archived before clearing
  id INTEGER PRIMARY KEY, company_id INTEGER NOT NULL REFERENCES companies(id), fit_score INTEGER, fit_json TEXT,
  archived_at TEXT NOT NULL, reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS persona_role_mappings ( -- Agent 3: buyer persona -> searchable roles, with review trail
  id INTEGER PRIMARY KEY, icp_id INTEGER REFERENCES icps(id), product_id TEXT, persona TEXT NOT NULL,
  persona_source TEXT NOT NULL, titles_json TEXT NOT NULL, seniorities_json TEXT NOT NULL, status TEXT NOT NULL,
  evidence_json TEXT NOT NULL, created_at TEXT NOT NULL, reviewed_by TEXT, reviewed_at TEXT, reason TEXT);
CREATE TABLE IF NOT EXISTS persona_mapping_history ( -- Agent 3: append-only audit trail of persona reviews
  id INTEGER PRIMARY KEY, mapping_id INTEGER NOT NULL REFERENCES persona_role_mappings(id), action TEXT NOT NULL,
  from_status TEXT, to_status TEXT NOT NULL, snapshot_json TEXT NOT NULL, reviewer TEXT NOT NULL,
  reason TEXT NOT NULL, evidence_json TEXT NOT NULL, at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS people (               -- Agent 3: deduplicated people
  id INTEGER PRIMARY KEY, dedupe_key TEXT NOT NULL UNIQUE, dedupe_basis TEXT NOT NULL, full_name TEXT NOT NULL,
  linkedin_url TEXT, verification TEXT NOT NULL DEFAULT 'unverified', first_seen TEXT NOT NULL,
  last_seen TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS person_roles (         -- Agent 3: person <-> company association with the observed role
  id INTEGER PRIMARY KEY, person_id INTEGER NOT NULL REFERENCES people(id),
  company_id INTEGER NOT NULL REFERENCES companies(id), title TEXT, seniority TEXT, is_current INTEGER,
  role_match_json TEXT NOT NULL, review_status TEXT NOT NULL DEFAULT 'needs_manual_review',
  first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, UNIQUE(person_id, company_id, title));
CREATE TABLE IF NOT EXISTS person_sources (       -- Agent 3: provenance for every person observation
  id INTEGER PRIMARY KEY, person_id INTEGER NOT NULL REFERENCES people(id),
  company_id INTEGER NOT NULL REFERENCES companies(id), run_id INTEGER REFERENCES runs(id),
  raw_response_id INTEGER REFERENCES raw_responses(id), source TEXT NOT NULL, tool TEXT, task_id TEXT,
  query TEXT, raw_json TEXT NOT NULL, fetched_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS lakshya_records (  -- Lakshya handover intelligence beyond signals (historical, read-only)
  id INTEGER PRIMARY KEY, kind TEXT NOT NULL, external_id TEXT NOT NULL UNIQUE, company_id INTEGER, product_id TEXT,
  title TEXT NOT NULL, body TEXT, data_json TEXT NOT NULL, source_url TEXT, source_file TEXT NOT NULL,
  source_ref TEXT, as_of TEXT NOT NULL, content_sha256 TEXT NOT NULL, imported_at TEXT NOT NULL,
  updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS lakshya_records_company ON lakshya_records(company_id);
CREATE TABLE IF NOT EXISTS lakshya_imports (  -- one row per ingestion run: what was read and what changed
  id INTEGER PRIMARY KEY, root TEXT NOT NULL, files_json TEXT NOT NULL, result_json TEXT NOT NULL, by TEXT,
  at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS suppression ( -- do-not-contact list (GDPR objection / opt-out): no draft is generated
  id INTEGER PRIMARY KEY, person_id INTEGER, email TEXT, domain TEXT, reason TEXT NOT NULL, added_by TEXT NOT NULL,
  added_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY, agent TEXT NOT NULL, status TEXT NOT NULL, detail_json TEXT,
  started_at TEXT NOT NULL, finished_at TEXT);
CREATE TABLE IF NOT EXISTS person_role_review_history ( -- Agent 3: append-only audit trail of role reviews
  id INTEGER PRIMARY KEY, role_id INTEGER NOT NULL REFERENCES person_roles(id), action TEXT NOT NULL,
  from_status TEXT, to_status TEXT NOT NULL, snapshot_json TEXT NOT NULL, reviewer TEXT NOT NULL,
  reason TEXT NOT NULL, evidence_json TEXT NOT NULL, at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS linkedin_resolutions ( -- Agent 4: one harness `resolve` attempt per person/role
  id INTEGER PRIMARY KEY, person_id INTEGER NOT NULL REFERENCES people(id),
  person_role_id INTEGER REFERENCES person_roles(id), company_id INTEGER REFERENCES companies(id),
  run_id INTEGER REFERENCES runs(id), input_json TEXT NOT NULL, status TEXT NOT NULL, error_kind TEXT,
  error TEXT, confidence TEXT, score REAL, profile_url TEXT, best_json TEXT, alternatives_json TEXT,
  searches_json TEXT, harness_json TEXT NOT NULL, harness_saved_path TEXT, harness_version TEXT NOT NULL,
  harness_argv_json TEXT NOT NULL, verification TEXT NOT NULL DEFAULT 'unverified',
  review_status TEXT NOT NULL DEFAULT 'pending_review', approved_url TEXT, reviewed_by TEXT, reviewed_at TEXT,
  review_reason TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS linkedin_resolution_history ( -- Agent 4: append-only audit trail of reviews
  id INTEGER PRIMARY KEY, resolution_id INTEGER NOT NULL REFERENCES linkedin_resolutions(id),
  action TEXT NOT NULL, from_status TEXT, to_status TEXT NOT NULL, url TEXT, reviewer TEXT NOT NULL,
  reason TEXT NOT NULL, at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS llm_calls (            -- shared: every structured LLM call (model actually used, outcome)
  id INTEGER PRIMARY KEY, agent TEXT NOT NULL, purpose TEXT NOT NULL, model TEXT, ok INTEGER NOT NULL,
  attempts INTEGER NOT NULL, error TEXT, prompt_tokens INTEGER, output_tokens INTEGER, latency_s REAL,
  run_id INTEGER REFERENCES runs(id), at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS website_pages (        -- Agent 5: every company web page fetched, for provenance
  id INTEGER PRIMARY KEY, domain TEXT NOT NULL, url TEXT NOT NULL, http_status INTEGER, robots_allowed INTEGER,
  sha256 TEXT, emails_json TEXT NOT NULL, error TEXT, run_id INTEGER REFERENCES runs(id), fetched_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS domain_email_patterns ( -- Agent 5: inferred address pattern per domain, with evidence
  id INTEGER PRIMARY KEY, domain TEXT NOT NULL, pattern TEXT, confidence REAL NOT NULL, basis TEXT NOT NULL,
  evidence_json TEXT NOT NULL, mx_json TEXT NOT NULL, run_id INTEGER REFERENCES runs(id), created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS email_records (        -- Agent 5: one email finding per person attempt
  id INTEGER PRIMARY KEY, person_id INTEGER NOT NULL REFERENCES people(id),
  company_id INTEGER REFERENCES companies(id), run_id INTEGER REFERENCES runs(id), email TEXT,
  method TEXT NOT NULL, status TEXT NOT NULL, confidence REAL NOT NULL, pattern TEXT, source_url TEXT,
  evidence_json TEXT NOT NULL, mx_json TEXT, raw_response_id INTEGER REFERENCES raw_responses(id),
  review_status TEXT NOT NULL DEFAULT 'pending_review', reviewed_by TEXT, reviewed_at TEXT, review_reason TEXT,
  created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS email_record_history ( -- Agent 5: append-only review trail
  id INTEGER PRIMARY KEY, email_record_id INTEGER NOT NULL REFERENCES email_records(id), action TEXT NOT NULL,
  from_status TEXT, to_status TEXT NOT NULL, email TEXT, reviewer TEXT NOT NULL, reason TEXT NOT NULL,
  at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS profile_research (     -- Agent 6: one harness `research` attempt + validated analysis
  id INTEGER PRIMARY KEY, person_id INTEGER NOT NULL REFERENCES people(id), profile_url TEXT NOT NULL,
  run_id INTEGER REFERENCES runs(id), status TEXT NOT NULL, error_kind TEXT, error TEXT,
  facts_json TEXT, analysis_json TEXT, dropped_json TEXT, matches_json TEXT, confidence TEXT,
  model TEXT, harness_json TEXT NOT NULL, harness_saved_path TEXT, harness_version TEXT NOT NULL,
  harness_argv_json TEXT NOT NULL, review_status TEXT NOT NULL DEFAULT 'pending_review', reviewed_by TEXT,
  reviewed_at TEXT, review_reason TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS profile_research_history (
  id INTEGER PRIMARY KEY, research_id INTEGER NOT NULL REFERENCES profile_research(id), action TEXT NOT NULL,
  from_status TEXT, to_status TEXT NOT NULL, reviewer TEXT NOT NULL, reason TEXT NOT NULL, at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS bottomup_leads (       -- bottom-up discovery: people found on LinkedIn BEFORE any company
  id INTEGER PRIMARY KEY, workflow_id INTEGER, query TEXT NOT NULL, profile_url TEXT NOT NULL UNIQUE,
  name TEXT, headline TEXT, search_json TEXT NOT NULL, profile_json TEXT, relevance_json TEXT,
  company_observed TEXT, company_linkedin_url TEXT, company_json TEXT, company_domain TEXT,
  company_id INTEGER REFERENCES companies(id), person_id INTEGER REFERENCES people(id),
  matches_json TEXT, status TEXT NOT NULL, error TEXT, harness_version TEXT NOT NULL,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS bottomup_lead_history (
  id INTEGER PRIMARY KEY, lead_id INTEGER NOT NULL REFERENCES bottomup_leads(id), from_status TEXT,
  to_status TEXT NOT NULL, actor TEXT NOT NULL, note TEXT, at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS prospects (            -- Agent 7: one structured, QA-checked record per person
  id INTEGER PRIMARY KEY, person_id INTEGER NOT NULL UNIQUE REFERENCES people(id),
  company_id INTEGER REFERENCES companies(id), record_json TEXT NOT NULL, score INTEGER NOT NULL,
  tier TEXT NOT NULL, issues_json TEXT NOT NULL, conflicts_json TEXT NOT NULL, qa_status TEXT NOT NULL,
  qa_run_id INTEGER REFERENCES runs(id), review_status TEXT NOT NULL DEFAULT 'pending_review',
  reviewed_by TEXT, reviewed_at TEXT, review_reason TEXT, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS qa_results (           -- Agent 7: append-only result of every check, every run
  id INTEGER PRIMARY KEY, run_id INTEGER REFERENCES runs(id), entity TEXT NOT NULL, entity_id INTEGER,
  check_name TEXT NOT NULL, status TEXT NOT NULL, detail_json TEXT NOT NULL, at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS prospect_history (
  id INTEGER PRIMARY KEY, prospect_id INTEGER NOT NULL REFERENCES prospects(id), action TEXT NOT NULL,
  from_status TEXT, to_status TEXT NOT NULL, snapshot_json TEXT NOT NULL, reviewer TEXT NOT NULL,
  reason TEXT NOT NULL, at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sync_runs (            -- Agent 7: every export / sync, with target and row count
  id INTEGER PRIMARY KEY, target TEXT NOT NULL, kind TEXT NOT NULL, dry_run INTEGER NOT NULL, rows INTEGER NOT NULL,
  status TEXT NOT NULL, detail_json TEXT NOT NULL, at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS outreach_drafts (      -- outreach preparation: drafts only, never sent by Chanakya
  id INTEGER PRIMARY KEY, prospect_id INTEGER NOT NULL REFERENCES prospects(id),
  person_id INTEGER NOT NULL REFERENCES people(id), research_id INTEGER REFERENCES profile_research(id),
  channel TEXT NOT NULL, to_address TEXT, subject TEXT NOT NULL, body TEXT NOT NULL, hooks_json TEXT NOT NULL,
  generator TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'draft', created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS outreach_draft_history (
  id INTEGER PRIMARY KEY, draft_id INTEGER NOT NULL REFERENCES outreach_drafts(id), action TEXT NOT NULL,
  from_status TEXT, to_status TEXT NOT NULL, subject TEXT, body TEXT, reviewer TEXT NOT NULL, reason TEXT NOT NULL,
  at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS workflows (            -- orchestrator: one top-down or bottom-up pipeline instance
  id INTEGER PRIMARY KEY, kind TEXT NOT NULL, params_json TEXT NOT NULL, status TEXT NOT NULL,
  current_stage TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS workflow_stages (
  id INTEGER PRIMARY KEY, workflow_id INTEGER NOT NULL REFERENCES workflows(id), seq INTEGER NOT NULL,
  stage TEXT NOT NULL, status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, run_ids_json TEXT NOT NULL,
  output_json TEXT, error TEXT, started_at TEXT, finished_at TEXT, UNIQUE(workflow_id, stage));
CREATE TABLE IF NOT EXISTS settings (             -- configurable methodology values (weights, recency basis, NDA date)
  key TEXT PRIMARY KEY, value_json TEXT NOT NULL, updated_by TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS company_aliases (      -- entity resolution: every identity a company was seen under
  id INTEGER PRIMARY KEY, company_id INTEGER NOT NULL REFERENCES companies(id), kind TEXT NOT NULL,
  value_norm TEXT NOT NULL, value_raw TEXT, source TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(kind, value_norm));
CREATE TABLE IF NOT EXISTS discovery_events (     -- provenance + direction of every discovery (top_down | bottom_up)
  id INTEGER PRIMARY KEY, entity TEXT NOT NULL, entity_id INTEGER NOT NULL, direction TEXT NOT NULL, source TEXT NOT NULL,
  workflow_id INTEGER, run_id INTEGER, detail_json TEXT, at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS signals (              -- Lakshya demand signals, one row per piece of evidence
  id INTEGER PRIMARY KEY, external_id TEXT UNIQUE, company_id INTEGER REFERENCES companies(id), company_name TEXT,
  company_key TEXT, type TEXT NOT NULL, source_url TEXT, source_date TEXT, source_date_basis TEXT, captured_at TEXT NOT NULL,
  summary TEXT NOT NULL, industry TEXT, region TEXT, roles_json TEXT, urgency INTEGER, budget TEXT, noise INTEGER NOT NULL
  DEFAULT 0, is_gap INTEGER NOT NULL DEFAULT 0, gap_theme TEXT, gap_need TEXT, reuse TEXT, angle TEXT,
  verification_status TEXT NOT NULL DEFAULT 'unverified', verification_json TEXT, provenance TEXT NOT NULL,
  direction TEXT NOT NULL, workflow_id INTEGER, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS signal_fits (          -- accelerator fit per signal (0..100), with method and reason
  id INTEGER PRIMARY KEY, signal_id INTEGER NOT NULL REFERENCES signals(id), product_id TEXT NOT NULL, score INTEGER NOT NULL,
  why TEXT, method TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(signal_id, product_id, method));
CREATE TABLE IF NOT EXISTS opportunities (        -- one canonical opportunity per company x accelerator (100-point model)
  id INTEGER PRIMARY KEY, company_id INTEGER NOT NULL REFERENCES companies(id), product_id TEXT NOT NULL,
  need_fit INTEGER NOT NULL, timing INTEGER NOT NULL, access INTEGER NOT NULL, proof_fit INTEGER NOT NULL,
  score INTEGER NOT NULL, explanation_json TEXT NOT NULL, origin TEXT, status TEXT NOT NULL DEFAULT 'open',
  computed_at TEXT NOT NULL, UNIQUE(company_id, product_id));
CREATE TABLE IF NOT EXISTS opportunity_history (
  id INTEGER PRIMARY KEY, opportunity_id INTEGER NOT NULL REFERENCES opportunities(id), score INTEGER NOT NULL,
  components_json TEXT NOT NULL, reason TEXT NOT NULL, at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS gap_themes (           -- needs with no accelerator at 60+; verdict computed, decision human
  id INTEGER PRIMARY KEY, key TEXT NOT NULL UNIQUE, name TEXT NOT NULL, need TEXT, reuse_json TEXT, verdict TEXT,
  accounts INTEGER, signal_types INTEGER, best_fit INTEGER, decision_status TEXT NOT NULL DEFAULT 'watching',
  decision_by TEXT, decision_reason TEXT, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS gap_evidence (
  id INTEGER PRIMARY KEY, theme_id INTEGER NOT NULL REFERENCES gap_themes(id), signal_id INTEGER NOT NULL
  REFERENCES signals(id), UNIQUE(theme_id, signal_id));
CREATE TABLE IF NOT EXISTS accelerator_proof (    -- the ONLY proof points outreach may use (and claims it must not make)
  id INTEGER PRIMARY KEY, product_id TEXT NOT NULL, claim TEXT NOT NULL, allowed INTEGER NOT NULL, source_url TEXT,
  note TEXT, created_at TEXT NOT NULL, UNIQUE(product_id, claim));
CREATE TABLE IF NOT EXISTS company_merges (       -- entity resolution merges, with a snapshot of the merged record
  id INTEGER PRIMARY KEY, kept_id INTEGER NOT NULL, merged_id INTEGER NOT NULL, reason TEXT NOT NULL, by TEXT NOT NULL,
  snapshot_json TEXT NOT NULL, at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS integration_checks (   -- every connectivity test: live / failed / disabled / simulated
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, mode TEXT NOT NULL, ok INTEGER NOT NULL, detail_json TEXT NOT NULL,
  by TEXT, at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_signals_company ON signals(company_id);
CREATE INDEX IF NOT EXISTS ix_disc_entity ON discovery_events(entity, entity_id);
CREATE INDEX IF NOT EXISTS ix_opp_score ON opportunities(score);
CREATE INDEX IF NOT EXISTS ix_alias_company ON company_aliases(company_id);
CREATE TABLE IF NOT EXISTS industry_mapping_history ( -- Agent 2: append-only trail of industry mapping reviews
  id INTEGER PRIMARY KEY, mapping_id INTEGER NOT NULL REFERENCES industry_mappings(id), from_status TEXT,
  to_status TEXT NOT NULL, canonical_json TEXT, reviewer TEXT NOT NULL, reason TEXT NOT NULL, at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS spend_approvals (      -- Agent 5: named approval for PAID Clay email lookups (cap <= 5)
  id INTEGER PRIMARY KEY, kind TEXT NOT NULL, reviewer TEXT NOT NULL, reason TEXT NOT NULL, max_records INTEGER NOT NULL,
  status TEXT NOT NULL, created_at TEXT NOT NULL, revoked_by TEXT, revoked_at TEXT);
CREATE TABLE IF NOT EXISTS ui_users (             -- HTTP API: local accounts (the signed-in name is the reviewer)
  id INTEGER PRIMARY KEY, email TEXT NOT NULL UNIQUE, name TEXT NOT NULL, role TEXT NOT NULL,
  password_hash TEXT NOT NULL, disabled INTEGER NOT NULL DEFAULT 0, created TEXT NOT NULL, last_login TEXT);
CREATE TABLE IF NOT EXISTS ui_sessions (
  id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES ui_users(id), token_hash TEXT NOT NULL UNIQUE,
  created TEXT NOT NULL, last_seen TEXT NOT NULL, expires TEXT NOT NULL, user_agent TEXT);
CREATE TABLE IF NOT EXISTS ui_login_failures (id INTEGER PRIMARY KEY, email TEXT NOT NULL, at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS workflow_events (      -- orchestrator: append-only execution log
  id INTEGER PRIMARY KEY, workflow_id INTEGER NOT NULL REFERENCES workflows(id), stage TEXT, level TEXT NOT NULL,
  message TEXT NOT NULL, detail_json TEXT, at TEXT NOT NULL);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: str | Path) -> sqlite3.Connection:
    p = Path(path)
    if str(p) != ":memory:":
        p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(p), timeout=30)  # API thread + background job share the file
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    _migrate(con)
    return con


def _cols(con, table: str) -> set[str]:
    return {r[1] for r in con.execute(f"PRAGMA table_info({table})")}


def _migrate(con) -> None:
    """Additive, idempotent migrations for databases created by earlier versions."""
    if "content_sha256" not in _cols(con, "icps"):
        con.execute("ALTER TABLE icps ADD COLUMN content_sha256 TEXT")
    if "product_id" not in _cols(con, "icps"):
        con.execute("ALTER TABLE icps ADD COLUMN product_id TEXT")
    if "summary_sha256" not in _cols(con, "products"):
        con.execute("ALTER TABLE products ADD COLUMN summary_sha256 TEXT")
    for col, typ in (("search_filter_score", "INTEGER"), ("qualification_status", "TEXT"), ("assessment_json", "TEXT"),
                     ("search_filter_json", "TEXT"), ("qualification_source", "TEXT")):
        if col not in _cols(con, "companies"):
            con.execute(f"ALTER TABLE companies ADD COLUMN {col} {typ}")
    if "identity_status" not in _cols(con, "companies"):  # 'resolved' (real domain) | 'unresolved' (name only so far)
        con.execute("ALTER TABLE companies ADD COLUMN identity_status TEXT")
        con.execute("UPDATE companies SET identity_status='resolved' WHERE identity_status IS NULL")
    if "raw_response_id" not in _cols(con, "company_sources"):
        con.execute("ALTER TABLE company_sources ADD COLUMN raw_response_id INTEGER REFERENCES raw_responses(id)")
    if "run_id" not in _cols(con, "company_sources"):
        con.execute("ALTER TABLE company_sources ADD COLUMN run_id INTEGER REFERENCES runs(id)")
    for col, typ in (("run_id", "INTEGER REFERENCES runs(id)"), ("sequence", "INTEGER"), ("phase", "TEXT")):
        if col not in _cols(con, "raw_responses"):
            con.execute(f"ALTER TABLE raw_responses ADD COLUMN {col} {typ}")
    for col in ("outlook_message_id", "outlook_web_link", "outlook_account", "outlook_created_at"):
        if col not in _cols(con, "outreach_drafts"):  # Outlook draft created from this Chanakya draft (never sent)
            con.execute(f"ALTER TABLE outreach_drafts ADD COLUMN {col} TEXT")
    # Persona mappings: 'stated' (auto-usable) was replaced by an explicit review; only 'approved' is usable now.
    con.execute("UPDATE persona_role_mappings SET status='pending' WHERE status='stated'")
    _migrate_legacy_fit(con)
    _migrate_human_reviews(con)
    con.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_icps_content ON icps(content_sha256) "
                "WHERE content_sha256 IS NOT NULL")
    con.commit()


def _migrate_legacy_fit(con) -> None:
    """v1 stored a filter-only score under the misleading name `fit_score`. Archive it, move the filter-match JSON to
    `search_filter_json`, clear the legacy columns, and give unreviewed rows the system default status.
    Repeatable: rows already migrated have NULL legacy columns and a non-NULL status."""
    t = now()
    rows = con.execute("SELECT id, fit_score, fit_json, search_filter_json FROM companies "
                       "WHERE fit_score IS NOT NULL OR fit_json IS NOT NULL").fetchall()
    for r in rows:
        con.execute("INSERT INTO legacy_fit_archive(company_id,fit_score,fit_json,archived_at,reason) VALUES(?,?,?,?,?)",
                    (r["id"], r["fit_score"], r["fit_json"], t, "v2 migration: 'fit' was a search-filter match only"))
        con.execute("UPDATE companies SET search_filter_json=COALESCE(search_filter_json, ?), fit_score=NULL, "
                    "fit_json=NULL WHERE id=?", (r["fit_json"], r["id"]))
    con.execute("UPDATE companies SET qualification_status='needs_manual_review', qualification_source='system' "
                "WHERE qualification_status IS NULL")
    con.execute("UPDATE companies SET qualification_source='system' WHERE qualification_source IS NULL")


def _migrate_human_reviews(con) -> None:
    """Copy reviews embedded in assessment_json (v2 interim format) into human_reviews, once per company."""
    for r in con.execute("SELECT id, assessment_json FROM companies WHERE assessment_json LIKE '%human_reviews%'"):
        if con.execute("SELECT 1 FROM human_reviews WHERE company_id=?", (r["id"],)).fetchone():
            continue
        for h in json.loads(r["assessment_json"]).get("human_reviews", []):
            if h.get("reviewer") and h.get("reason") and h.get("status"):
                con.execute("INSERT INTO human_reviews(company_id,status,reviewer,reason,reviewed_at) VALUES(?,?,?,?,?)",
                            (r["id"], h["status"], h["reviewer"], h["reason"], h.get("reviewed_at") or "unknown"))


def upsert_icp(con, icp_json: str, source_shas: dict, product_id: str | None = None) -> tuple[int, bool]:
    """Store an ICP once per unique content. Returns (icp id, created?). Identical re-imports reuse the row."""
    import hashlib
    sha = hashlib.sha256((icp_json + json.dumps(source_shas, sort_keys=True)).encode()).hexdigest()
    row = con.execute("SELECT id FROM icps WHERE content_sha256=?", (sha,)).fetchone()
    if row:
        return row[0], False
    ver = con.execute("SELECT COALESCE(MAX(version),0) FROM icps").fetchone()[0] + 1
    cur = con.execute("INSERT INTO icps(version,icp_json,source_shas,created_at,content_sha256,product_id) "
                      "VALUES(?,?,?,?,?,?)", (ver, icp_json, json.dumps(source_shas, sort_keys=True), now(), sha,
                                              product_id))
    return cur.lastrowid, True


def jdump(v) -> str | None:
    return None if v is None else json.dumps(v, ensure_ascii=False, default=str)


def start_run(con, agent: str, detail: dict | None = None) -> int:
    cur = con.execute("INSERT INTO runs(agent,status,detail_json,started_at) VALUES(?,?,?,?)",
                      (agent, "running", json.dumps(detail or {}), now()))
    con.commit()
    return cur.lastrowid


def finish_run(con, run_id: int, status: str, detail: dict) -> None:
    con.execute("UPDATE runs SET status=?, detail_json=?, finished_at=? WHERE id=?",
                (status, json.dumps(detail, default=str), now(), run_id))
    con.commit()
