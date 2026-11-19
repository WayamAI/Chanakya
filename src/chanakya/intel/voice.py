"""Vineet's writing voice (handover section 2 and Lakshya Studio VOICE / cleanText) and the email structure.

Short, direct, first person, spoken. No em or en dashes, no emojis, no corporate jargon, no exclamation marks, not
salesy. Email structure (handover 8.8): greeting, one sentence on the specific signal, one on what Wayam does plus one
proof point it has, one small ask (20 minutes), accelerator URL, signature. 58 to 82 words worked.
"""
from __future__ import annotations

import re

JARGON = ["leverage", "leverages", "leveraged", "leveraging", "synergy", "synergies", "ecosystem", "ecosystems",
          "seamless", "seamlessly", "cutting-edge", "cutting edge", "unlock", "unlocks", "unlocked", "unlocking",
          "best-in-class", "best in class", "holistic", "robust", "game-changer", "game changer", "game-changing"]
SWAPS = [(r"\bleverag(e|es|ed|ing)\b", "use"), (r"\bsynerg(y|ies)\b", "fit"), (r"\becosystems?\b", "network"),
         (r"\bseamless(ly)?\b", "smooth"), (r"\bcutting[- ]edge\b", "modern"), (r"\bunlock(s|ed|ing)?\b", "open up"),
         (r"\bbest[- ]in[- ]class\b", "strong"), (r"\bholistic\b", "complete"), (r"\bgame[- ]chang(er|ing)\b", "major"),
         (r"\brobust\b", "solid")]
EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿️]")
WEB_URL = "https://wayam.ai/accelerators/"
SPECIAL_URLS = {"arjuna": "https://wayam.ai/arjuna"}  # handover: Arjuna lives at wayam.ai/arjuna


def clean_text(t: str) -> str:
    t = EMOJI.sub("", t or "")
    t = re.sub(r"\s*[—―]\s*", ", ", t).replace("–", "-")
    for pat, rep in SWAPS:
        t = re.sub(pat, rep, t, flags=re.I)
    t = t.replace("!", ".")
    return re.sub(r"[ \t]{2,}", " ", t).replace(" ,", ",").strip()


def voice_issues(t: str) -> list[str]:
    issues = []
    if re.search(r"[—–―]", t):
        issues.append("contains an em or en dash")
    if EMOJI.search(t):
        issues.append("contains an emoji")
    if "!" in t:
        issues.append("contains an exclamation mark")
    low = t.lower()
    issues += [f"jargon: {w}" for w in JARGON if re.search(r"\b" + re.escape(w) + r"\b", low)]
    return issues


def accelerator_url(product_id: str) -> str:
    slug = product_id.lower().removeprefix("prd-")
    return SPECIAL_URLS.get(slug, WEB_URL + slug)


def word_count(t: str) -> int:
    return len(re.findall(r"[A-Za-z0-9][A-Za-z0-9'.%$,-]*", t))
