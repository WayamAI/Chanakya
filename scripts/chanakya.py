"""Chanakya - one CLI for the whole pipeline (top-down and bottom-up), its review gates and exports.

DRY RUN is the default everywhere: nothing external is called and no stage is marked done without --live.
Chanakya never sends email or LinkedIn messages; `outreach export` writes .eml drafts for you to send yourself.

Workflows
  workflow new top_down  --param product_id=PRD-SUTRADHAR [--param key=value ...]
  workflow new bottom_up --param query="Head of IT" [--param product_id=...]
  workflow run ID [--live] [--until STAGE]      (resume = run again; continues at the first stage not done)
  workflow stage ID STAGE [--live]              (one stage on its own, e.g. after new approvals)
  workflow status ID | events ID | set ID key=value ... | reset ID STAGE | list
Reviews (reviewer + reason always required; every action is kept in a history table)
  email   run [--live] | list [--status S] | show ID | approve ID | reject ID | override PERSON EMAIL [--evidence-url U]
          | history ID
  research run [--live] | list | show ID | approve ID | reject ID | history ID
  leads   list [--status S] | show ID | mark ID relevant|not_relevant | link ID --domain D   (bottom-up)
  qa      run | list | show ID | approve ID | reject ID | reopen ID | export --format csv|xlsx|jsonl --out PATH
          [--approved-only] | sync [--live]
  outreach generate | list | show ID | edit ID [--subject S] [--body-file F] | ready ID | discard ID
          | export --out DIR | history ID
Diagnostics
  harness status   (config, version, browser mode, page budget; `--live` also asks the harness if logged in)
  evaluate         (read-only output-quality checks: evidence, provenance, dedupe, traceability, claims, handoffs)
  llm-check        (configured model exists on Ollama Cloud; no silent fallback)
Companies / personas / roles / LinkedIn reviews keep their existing scripts (review_companies.py, review_personas.py,
review_roles.py, resolve_linkedin.py).
Env: CHANAKYA_DB_PATH, OLLAMA_*, LINKEDIN_HARNESS_DIR, LINKEDIN_HARNESS_CMD, CLAY_WORKSPACE_ID, CHANAKYA_SYNC_TARGETS,
     CHANAKYA_PAID_EMAIL_APPROVAL="reviewer|reason|max_records" (enables the PAID Clay email lookup).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from chanakya.storage.db import connect  # noqa: E402
from chanakya.workflow import orchestrator as W  # noqa: E402


def out(o) -> None:
    print(json.dumps(o, indent=2, ensure_ascii=False, default=str))


def _val(v: str):
    try:
        return json.loads(v)
    except json.JSONDecodeError:
        return v


def kv(items) -> dict:
    d = {}
    for it in items or []:
        k, sep, v = it.partition("=")
        if not sep:
            raise SystemExit(f"bad --param {it!r}; use key=value")
        d[k.strip()] = _val(v)
    return d


def load_env() -> None:
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env", override=False)
    except ImportError:
        pass


from chanakya.workflow.services import make_harness, make_llm, make_services  # noqa: E402,F401


def need(a, *names):
    for n in names:
        if not getattr(a, n, None):
            raise SystemExit(f"--{n.replace('_', '-')} is required")


def main(argv=None) -> int:
    load_env()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("group", choices=["workflow", "email", "research", "leads", "qa", "outreach", "harness",
                                      "llm-check", "evaluate"])
    ap.add_argument("cmd", nargs="?", default="list")
    ap.add_argument("args", nargs="*")
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--until")
    ap.add_argument("--param", action="append")
    ap.add_argument("--status")
    ap.add_argument("--by")
    ap.add_argument("--reason")
    ap.add_argument("--domain")
    ap.add_argument("--evidence-url")
    ap.add_argument("--subject")
    ap.add_argument("--body-file")
    ap.add_argument("--format", default="csv", choices=["csv", "xlsx", "jsonl"])
    ap.add_argument("--out")
    ap.add_argument("--approved-only", action="store_true")
    ap.add_argument("--limit", type=int, default=10)
    a = ap.parse_args(argv)
    con = connect(os.getenv("CHANAKYA_DB_PATH", str(ROOT / "data" / "chanakya.db")))
    arg = lambda i: a.args[i] if len(a.args) > i else None  # noqa: E731
    iid = lambda i=0: int(arg(i)) if arg(i) is not None else ap.error("ID required")  # noqa: E731
    try:
        return dispatch(a, ap, con, arg, iid)
    except (ValueError, RuntimeError, PermissionError) as e:
        print(f"error: {type(e).__name__}: {e}", file=sys.stderr)
        return 2


def dispatch(a, ap, con, arg, iid) -> int:
    g, c = a.group, a.cmd
    if g == "workflow":
        if c == "new":
            wid = W.create_workflow(con, arg(0) or ap.error("kind required"), kv(a.param))
            out(W.status(con, wid))
        elif c == "list":
            out([dict(r) for r in con.execute("SELECT id, kind, status, current_stage, updated_at FROM workflows")])
        elif c == "run":
            r = W.run(con, iid(), make_services(a.live), live=a.live, until=a.until)
            out(r)
            if not a.live:
                print("\nDRY RUN: nothing was called or marked done. Add --live to execute.", file=sys.stderr)
        elif c == "stage":
            out(W.run_stage(con, iid(), arg(1) or ap.error("STAGE required"), make_services(a.live), live=a.live))
        elif c == "status":
            out(W.status(con, iid()))
        elif c == "events":
            out(W.events(con, iid(), a.limit * 5))
        elif c == "set":
            out(W.update_params(con, iid(), **kv(a.args[1:])))
        elif c == "reset":
            W.reset_from(con, iid(), arg(1) or ap.error("STAGE required"))
            out(W.status(con, iid()))
        else:
            ap.error(f"unknown workflow command {c}")
    elif g == "email":
        from chanakya.emailhunter import agent as E
        if c == "run":
            out(E.run_email_hunter(con, allow_live=a.live, limit=a.limit, paid=make_services(a.live).paid_email)
                .__dict__)
        elif c == "list":
            out([{k: r[k] for k in ("id", "full_name", "email", "status", "method", "confidence", "review_status")}
                 for r in E.list_records(con, a.status)])
        elif c == "show":
            out(dict(E.get_record(con, iid())))
        elif c == "approve":
            out(E.approve(con, iid(), by=a.by or "", reason=a.reason or ""))
        elif c == "reject":
            E.reject(con, iid(), by=a.by or "", reason=a.reason or "")
            out({"rejected": iid()})
        elif c == "override":
            out({"email_record_id": E.override(con, iid(), arg(1) or "", by=a.by or "", reason=a.reason or "",
                                               evidence_url=a.evidence_url)})
        elif c == "history":
            out(E.history(con, iid()))
        else:
            ap.error(f"unknown email command {c}")
    elif g == "research":
        from chanakya.research import agent as R
        if c == "run":
            h = make_harness()
            if h is None:
                from chanakya.resolver.harness_client import HarnessClient
                if a.live:
                    raise RuntimeError("LINKEDIN_HARNESS_DIR is not set")
                h = HarnessClient(Path("<LINKEDIN_HARNESS_DIR>"))
            out(R.run_researcher(h, con, llm=make_llm(task="people_research") if a.live else None, allow_live=a.live,
                                 limit=min(a.limit, R.MAX_LIMIT)).__dict__)
        elif c == "list":
            out([dict(r) for r in con.execute("SELECT x.id, p.full_name, x.status, x.confidence, x.review_status, "
                                              "x.model, x.created_at FROM profile_research x JOIN people p ON "
                                              "p.id=x.person_id ORDER BY x.id")])
        elif c == "show":
            r = dict(R.get(con, iid()))
            for k in ("facts_json", "analysis_json", "dropped_json", "matches_json"):
                r[k] = json.loads(r[k] or "null")
            r.pop("harness_json")
            out(r)
        elif c in ("approve", "reject"):
            R.review(con, iid(), c + "d" if c == "approve" else "rejected", by=a.by or "", reason=a.reason or "")
            out({c: iid()})
        elif c == "history":
            out(R.history(con, iid()))
        else:
            ap.error(f"unknown research command {c}")
    elif g == "leads":
        from chanakya.bottomup import agent as B
        if c == "list":
            out(B.list_leads(con, a.status))
        elif c == "show":
            r = dict(con.execute("SELECT * FROM bottomup_leads WHERE id=?", (iid(),)).fetchone() or {})
            out({k: (json.loads(v) if k.endswith("_json") and v else v) for k, v in r.items()})
        elif c == "mark":
            B.review_lead(con, iid(), arg(1) or "", by=a.by or "", reason=a.reason or "")
            out({"lead": iid(), "status": arg(1)})
        elif c == "link":
            need(a, "domain", "by")
            out(dict(zip(("company_id", "person_id"), B.link_lead(con, iid(), domain=a.domain, by=a.by))))
        else:
            ap.error(f"unknown leads command {c}")
    elif g == "qa":
        from chanakya.qa import agent as Q
        if c == "run":
            out(Q.run_qa(con))
        elif c == "list":
            out(Q.rows(con, only_approved=a.approved_only))
        elif c == "show":
            r = dict(con.execute("SELECT * FROM prospects WHERE id=?", (iid(),)).fetchone() or {})
            out({k: (json.loads(v) if k.endswith("_json") and v else v) for k, v in r.items()})
        elif c in ("approve", "reject", "reopen"):
            Q.review_prospect(con, iid(), {"approve": "approved", "reject": "rejected", "reopen": "pending_review"}[c],
                              by=a.by or "", reason=a.reason or "")
            out({c: iid()})
        elif c == "export":
            need(a, "out")
            out(Q.sync(con, [(a.format, a.out)], only_approved=a.approved_only))
        elif c == "sync":
            out(Q.sync(con, allow_live=a.live))
        else:
            ap.error(f"unknown qa command {c}")
    elif g == "outreach":
        from chanakya.outreach import drafts as D
        if c == "generate":
            out({"draft_ids": D.generate(con, llm=make_llm(task="outreach_draft"), sender=os.getenv("CHANAKYA_SENDER", "[Your name]"))})
        elif c == "list":
            out([dict(r) for r in con.execute("SELECT d.id, p.full_name, d.to_address, d.subject, d.status, "
                                              "d.generator FROM outreach_drafts d JOIN people p ON p.id=d.person_id")])
        elif c == "show":
            out(dict(con.execute("SELECT * FROM outreach_drafts WHERE id=?", (iid(),)).fetchone() or {}))
        elif c == "edit":
            body = Path(a.body_file).read_text(encoding="utf-8") if a.body_file else None
            D.edit(con, iid(), by=a.by or "", reason=a.reason or "edited", subject=a.subject, body=body)
            out({"edited": iid()})
        elif c == "ready":
            D.mark_ready(con, iid(), by=a.by or "", reason=a.reason or "")
            out({"ready": iid(), "note": "Chanakya does not send. Send it yourself from your mail client."})
        elif c == "discard":
            D.discard(con, iid(), by=a.by or "", reason=a.reason or "")
            out({"discarded": iid()})
        elif c == "export":
            need(a, "out")
            out({"eml_files": D.export_eml(con, a.out, status=a.status)})
        elif c == "history":
            out(D.history(con, iid()))
        else:
            ap.error(f"unknown outreach command {c}")
    elif g == "harness":
        h = make_harness()
        if h is None:
            out({"configured": False, "fix": "set LINKEDIN_HARNESS_DIR to a checkout of "
                                             "github.com/mrigankad/Linkedinharness- (see docs/SETUP.md)"})
            return 1
        from chanakya.resolver.harness_client import harness_version
        info = {"configured": True, "dir": str(h.harness_dir), "command": h.command_prefix,
                "installed": (h.harness_dir / "linkedin_harness.py").exists(), "version": harness_version(h.harness_dir),
                "browser_mode": h.browser_mode(), "budget": h.budget()}
        if a.live and info["installed"]:
            st = h.status()
            info["status"] = {"ok": st.ok, "data": st.data, "error": st.error}
        out(info)
    elif g == "evaluate":
        from chanakya.qa.evaluate import evaluate
        r = evaluate(con)
        out(r)
        return 0 if r["summary"]["failed"] == 0 else 1
    elif g == "llm-check":
        llm = make_llm()
        if llm is None:
            # No key: still verify the intended model against Ollama Cloud's public catalogue (no auth needed).
            import httpx
            want = os.getenv("OLLAMA_MODEL") or "qwen-max"
            host = os.getenv("OLLAMA_HOST", "https://ollama.com").rstrip("/")
            try:
                r = httpx.get(f"{host}/api/tags", timeout=20)
                r.raise_for_status()
                models = [m.get("name") or m.get("model") for m in r.json().get("models", [])]
            except httpx.HTTPError as e:
                out({"configured_model": want, "key_configured": False, "catalogue_error": str(e)})
                return 1
            out({"configured_model": want, "key_configured": False, "in_public_catalogue": want in models,
                 "qwen_models_in_public_catalogue": [m for m in models if "qwen" in (m or "").lower()],
                 "public_catalogue": models})
            return 1
        models = llm.list_models()
        want = llm._s.ollama_model
        out({"configured_model": want, "available": want in models, "fallback": llm._s.ollama_fallback_model,
             "fallback_allowed": llm._s.ollama_allow_fallback,
             "qwen_models_on_account": [m for m in models if "qwen" in (m or "").lower()]})
        return 0 if want in models else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
