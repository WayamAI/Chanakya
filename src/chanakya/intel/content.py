"""Wayam AI content catalogue: the single source of approved company copy for every agent.

Stored in `config/wayam_content.yaml` (override with CHANAKYA_CONTENT_PATH) so it can be edited without a code change.
Accelerator facts come from the products table and proof points from the proof registry; this catalogue holds the
company introduction, voice rules, reusable paragraphs, per-accelerator approved angles and the signature.
`validate` refuses content that breaks the voice rules or claims proof Wayam does not have.
"""
from __future__ import annotations

import os
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_PATH = ROOT / "config" / "wayam_content.yaml"
REQUIRED = ("company", "voice", "paragraphs", "signature")


class ContentError(ValueError):
    pass


def path() -> Path:
    return Path(os.getenv("CHANAKYA_CONTENT_PATH") or DEFAULT_PATH)


def _strings(node, where=""):
    if isinstance(node, str):
        yield where, node
    elif isinstance(node, dict):
        for k, v in node.items():
            yield from _strings(v, f"{where}.{k}" if where else str(k))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _strings(v, f"{where}[{i}]")


def validate(data: dict) -> list[str]:
    from chanakya.intel.proof import claim_violations
    from chanakya.intel.voice import voice_issues
    if not isinstance(data, dict):
        return ["content must be a mapping"]
    issues = [f"missing section: {k}" for k in REQUIRED if k not in data]
    for where, s in _strings({k: v for k, v in data.items() if k != "voice"}):
        issues += [f"{where}: {i}" for i in voice_issues(s)]
        issues += [f"{where}: {i}" for i in claim_violations(s, "PRD-SETU")]
    if not (data.get("signature") or {}).get("lines"):
        issues.append("signature.lines is empty")
    return issues


def load() -> dict:
    p = path()
    if not p.exists():
        raise ContentError(f"content catalogue not found at {p}")
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    bad = validate(data)
    if bad:
        raise ContentError("content catalogue is invalid: " + "; ".join(bad))
    return data


def save(data: dict) -> dict:
    bad = validate(data)
    if bad:
        raise ContentError("; ".join(bad))
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")
    tmp.replace(p)
    return data


def paragraph(key: str, **fill) -> str:
    text = load()["paragraphs"][key]
    return text.format(**fill) if fill else text


def signature(sender: str | None = None) -> str:
    """Approved signature block; a non-placeholder CHANAKYA_SENDER replaces the name line."""
    lines = list(load()["signature"]["lines"])
    if sender and sender.strip() and not sender.startswith("["):
        lines[0] = sender.strip()
    return "\n".join(lines)


def accelerator_angle(product_id: str | None) -> str | None:
    return (load().get("accelerator_angles") or {}).get(product_id) if product_id else None


def for_accelerator(con, product_id: str) -> dict:
    """Everything an agent may say about one accelerator: products row + approved angle + allowed proof."""
    from chanakya.intel.proof import allowed_proofs
    from chanakya.intel.voice import accelerator_url
    r = con.execute("SELECT id, name, summary FROM products WHERE id=?", (product_id,)).fetchone()
    if r is None:
        raise ContentError(f"unknown accelerator {product_id}")
    return {"product_id": r["id"], "name": r["name"], "summary": r["summary"], "url": accelerator_url(r["id"]),
            "approved_angle": accelerator_angle(r["id"]), "allowed_proof": allowed_proofs(con, r["id"]),
            "company": load()["company"]}
