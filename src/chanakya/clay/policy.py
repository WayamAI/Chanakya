"""Clay tool policy: one place that says which agent may call which Clay tool.

WORKSPACE_APPROVED is what the account owner approved (2026-11-03). Each agent gets a strict subset, so a
bug in one agent cannot call another agent's tools. Enrichment tools are held for everyone.
"""
WORKSPACE_APPROVED = frozenset({"get-credits-available", "get-current-workspace", "search-companies", "get-task",
                                "get-task-context", "load-more-search-results", "search-contacts"})
HELD = frozenset({"add-company-data-points", "add-contact-data-points", "run_subroutine",
                  "run_subroutine_no_mapping", "run_subroutine_direct", "search-contacts-by-name"})

# Agent 2 - Company Scout: company discovery only.
COMPANY_SCOUT = frozenset({"get-credits-available", "get-current-workspace", "search-companies", "get-task",
                           "get-task-context", "load-more-search-results"})
# Agent 3 - People Finder (not implemented yet): people discovery only.
PEOPLE_FINDER = frozenset({"get-credits-available", "get-current-workspace", "search-contacts", "get-task",
                           "get-task-context", "load-more-search-results"})

assert COMPANY_SCOUT <= WORKSPACE_APPROVED and PEOPLE_FINDER <= WORKSPACE_APPROVED
assert not (COMPANY_SCOUT | PEOPLE_FINDER) & HELD

# Agent 5 - Email Hunter. Free tools only by default. `add-contact-data-points` (Email data point) is PAID and stays
# in HELD: it can be called only through emailhunter.clay.PaidEmailGateway with an explicit, named SpendApproval
# (reviewer, reason, record cap). Real schema (docs/clay_capabilities.json): {taskId, dataPoints:[{type:"Email"}],
# entityIds}; it needs the taskId of an earlier search-contacts call.
EMAIL_HUNTER = frozenset({"get-credits-available", "get-current-workspace", "get-task-context"})
EMAIL_HUNTER_PAID = frozenset({"add-contact-data-points"})
assert EMAIL_HUNTER <= WORKSPACE_APPROVED and not EMAIL_HUNTER & HELD and EMAIL_HUNTER_PAID <= HELD
