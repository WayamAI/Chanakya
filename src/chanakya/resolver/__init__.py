"""Agent 4 - LinkedIn Resolver.

Wraps Mriganka's existing LinkedIn browser harness (`resolve` command) through a subprocess adapter. No browser
automation lives in Chanakya. Dry run by default, strictly sequential, every result unverified until a human
approves it.
"""
from chanakya.resolver.agent import (Candidate, ResolverBlocked, ResolverReport, run_resolver,  # noqa: F401
                                     select_candidates)
from chanakya.resolver.harness_client import HarnessClient, HarnessError, HarnessResult  # noqa: F401
