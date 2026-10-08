"""Cheap pattern screen for instruction-like text inside business data (policy §9).

This is a first line only. The prompts also tell the model to treat request text
and vendor notes as data, and the guardrail never lets the model drop a
required approval, so a missed pattern here cannot unlock anything.
"""
from __future__ import annotations

import re

PATTERNS = [
    r"\bignore\b[^.]{0,40}\b(rules|instructions|policy|policies|controls|guidelines)\b",
    r"\bdisregard\b[^.]{0,40}\b(rules|instructions|policy|policies|controls|above)\b",
    r"\b(treat|mark|consider|record)\b[^.]{0,40}\bas\b[^.]{0,20}\b(approved|pre-approved|signed off)\b",
    r"\b(cfo|ceo|finance|security|legal|privacy)[- ](approved|signed[- ]off)\b",
    r"\bapprove\b[^.]{0,30}\b(immediately|right away|automatically|without (a )?review)\b",
    r"\b(bypass|skip|override|disable|waive)\b[^.]{0,30}\b(security|privacy|legal|procurement|polic(y|ies)|reviews?|controls|approvals?|checks?)\b",
    r"\b(system prompt|developer mode|you are now|new instructions)\b",
    r"\b(reveal|print|expose|send)\b[^.]{0,30}\b(system prompt|api key|secrets?|passwords?|credentials?)\b",
]
_COMPILED = [re.compile(p, re.IGNORECASE) for p in PATTERNS]


def _excerpt(text: str, start: int, end: int, limit: int = 160) -> str:
    left = max(text.rfind(".", 0, start) + 1, 0)
    right = text.find(".", end)
    right = len(text) if right == -1 else right + 1
    snippet = text[left:right].strip()
    return snippet if len(snippet) <= limit else snippet[: limit - 3] + "..."


def scan_text(text: str) -> list[str]:
    if not text:
        return []
    found = []
    for pattern in _COMPILED:
        for match in pattern.finditer(text):
            snippet = _excerpt(text, match.start(), match.end())
            if snippet not in found:
                found.append(snippet)
    return found


def scan_request(request: dict) -> list[dict]:
    hits = []
    for key, value in request.items():
        values = value if isinstance(value, list) else [value]
        for item in values:
            if isinstance(item, str):
                for snippet in scan_text(item):
                    hits.append({"field": f"request.{key}", "ref": f"request:{key}", "excerpt": snippet})
    return hits


def scan_vendor_notes(registry: dict | None, risk: dict) -> list[dict]:
    hits = []
    if registry and registry.get("notes"):
        for snippet in scan_text(str(registry["notes"])):
            hits.append({"field": "vendor registry notes", "ref": registry["vendor_id"], "excerpt": snippet})
    if risk.get("found") and risk.get("notes"):
        for snippet in scan_text(str(risk["notes"])):
            hits.append({"field": "vendor-risk notes", "ref": risk["ref"], "excerpt": snippet})
    return hits
