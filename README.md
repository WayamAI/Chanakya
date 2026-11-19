# Chanakya: GTM prospect intelligence engine for Wayam

Seven agents plus a bottom-up entry point, one SQLite database, human review at every gate, and one orchestrator.
Chanakya prepares outreach drafts; **it never sends email or LinkedIn messages**.

| # | Agent | Module | External |
|---|---|---|---|
| 1 | Catalog (Offering DB, Needs Analyzer, ICP Builder) | `catalog/` | Ollama |
| 2 | Company Scout | `scout/` | Clay `search-companies` |
| 3 | People Finder | `people/` | Clay `search-contacts` |
| 4 | LinkedIn Resolver | `resolver/` | harness `resolve` |
| 5 | Email Hunter | `emailhunter/` | company websites, DNS MX, Clay Email (paid, approval only) |
| 6 | Profile Researcher | `research/` | harness `research`, Ollama |
| 7 | QA / Sync | `qa/` | CSV / XLSX / JSONL / webhook |
| BU | Bottom-up discovery | `bottomup/` | harness `people`, `profile`, `companies`, `company` |
| — | Outreach drafts | `outreach/` | Ollama (optional) |
| — | Orchestrator | `workflow/orchestrator.py`, `scripts/chanakya.py` | — |

**UI:** `.\scripts\start_chanakya.ps1` starts the API (:8010) and the LinkedIn-harness-based UI (:3000).
Add `-Simulated` for an isolated test database with simulated external services.


## Run it

Every command is a DRY RUN unless you add `--live`.

```powershell
python -m pytest -q                                                        # offline tests

# Top-down: catalogue -> companies -> people -> LinkedIn -> email -> research -> QA -> drafts
python scripts/chanakya.py workflow new top_down --param product_id=PRD-SUTRADHAR
python scripts/chanakya.py workflow run 1                                  # whole plan, nothing executed
python scripts/chanakya.py workflow run 1 --live                           # runs until the first gate
#   ... do the review the message asks for, e.g.
python scripts/review_companies.py qualify icai.org --by "Krisha" --reason "fits Sutradhar ICP"
python scripts/chanakya.py workflow run 1 --live                           # resume
python scripts/chanakya.py workflow set 1 'confirm_people_query={"icai.org": "<exact DSL from the output>"}'
python scripts/chanakya.py workflow run 1 --live

# Bottom-up: LinkedIn people search first, companies derived from profiles
python scripts/chanakya.py workflow new bottom_up --param "query=Head of Finance" --param search_n=5
python scripts/chanakya.py workflow run 2 --live

# Status, logs, single stages
python scripts/chanakya.py workflow status 1
python scripts/chanakya.py workflow events 1
python scripts/chanakya.py workflow stage 1 email --live

# Reviews
python scripts/review_roles.py list --status needs_manual_review
python scripts/resolve_linkedin.py list --status pending_review
python scripts/chanakya.py email list --status pending_review
python scripts/chanakya.py email approve 3 --by "Krisha" --reason "published on team page"
python scripts/chanakya.py research approve 1 --by "Krisha" --reason "quotes checked"
python scripts/chanakya.py qa list
python scripts/chanakya.py qa approve 1 --by "Krisha" --reason "ready for outreach"

# Drafts and exports (drafts are never sent)
python scripts/chanakya.py outreach list
python scripts/chanakya.py outreach edit 1 --body-file my_version.txt --by "Krisha" --reason "personalised"
python scripts/chanakya.py outreach export --out exports/drafts
python scripts/chanakya.py qa export --format xlsx --out exports/prospects.xlsx --approved-only
```
