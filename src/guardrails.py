"""Turn a model draft into the final ProcurementDecision.

The model may add approvals, flags, missing items and evidence, but it cannot
remove anything the deterministic checks require, cannot pick a less cautious
next action than the evidence allows, and cannot cite records no tool returned.
Every change made here is written to `guardrail_notes` so it stays visible.
"""
from __future__ import annotations

import re

from src.contracts import EvidenceItem, ProcurementDecision
from src.rules import APPROVERS, BLOCKING_FLAGS, SPECIALIST_APPROVERS, RuleResult

ACTION_LABELS = {
    "request_clarification": "Request clarification",
    "escalate_for_review": "Escalate for review",
    "use_existing_tool": "Use existing tool first",
    "route_for_approval": "Route for approval",
}

_APPROVER_ALIASES = {
    "manager": "Manager", "line manager": "Manager", "direct manager": "Manager",
    "department head": "Department Head", "dept head": "Department Head", "head of department": "Department Head",
    "procurement": "Procurement", "finance": "Finance", "cfo": "CFO", "chief financial officer": "CFO",
    "security": "Security", "infosec": "Security", "privacy": "Privacy", "dpo": "Privacy", "legal": "Legal",
}

# The code already checks every §1 field for presence. The model may only add "missing
# information" for the two fields where presence isn't enough and judgement is needed
# (is the stated purpose / data class actually usable?). Anything else is a question.
_JUDGED_FIELDS = [
    ("purpose", ("business purpose", "justification", "business need", "use case")),
    ("data", ("data access", "data-access", "data class", "data type", "what data")),
]

# Text that claims this request is already approved - never acceptable from the copilot.
_APPROVAL_CLAIM = re.compile(
    r"\b(this|the) (request|purchase) (is|has been|was) (already )?(pre-?)?approved\b"
    r"|\b(cfo|ceo)[- ]approved\b|\bapproved immediately\b|\bauto-?approv",
    re.IGNORECASE)


def canonical_approver(name: str) -> str | None:
    key = re.sub(r"\b(review|reviewer|team|approval|approver|sign-?off)\b", "", name.lower())
    key = re.sub(r"[^a-z ]+", " ", key)
    return _APPROVER_ALIASES.get(" ".join(key.split()))


def canonical_flag(flag: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", flag.lower()).strip("_")


def _missing_topic(text: str) -> str | None:
    lowered = text.lower()
    for topic, terms in _JUDGED_FIELDS:
        if any(t in lowered for t in terms):
            return topic
    return None


def _ordered(items: list[str], order: list[str] | None = None) -> list[str]:
    out = list(dict.fromkeys(items))
    if order:
        out.sort(key=lambda x: order.index(x) if x in order else len(order))
    return out


def _join(items: list[str]) -> str:
    return ", ".join(items) if items else "none"


def templated(action: str, approvals: list[str], flags: list[str], missing: list[str],
              rr: RuleResult) -> tuple[str, str]:
    """Plain recommendation + next step, used when the model's wording can't be kept."""
    specialists = [a for a in approvals if a in SPECIALIST_APPROVERS]
    business = [a for a in approvals if a not in SPECIALIST_APPROVERS]
    if action == "request_clarification":
        return ("The request is incomplete, so it cannot be assessed for approval yet.",
                f"Return the request to the requester for: {'; '.join(missing)}. Re-run the review once it is complete.")
    if action == "escalate_for_review":
        reviewers = list(specialists)
        if {"budget_insufficient", "budget_unverified"} & set(flags) and "Finance" not in reviewers:
            reviewers.append("Finance")
        reasons = [f.replace("_", " ") for f in flags if f in BLOCKING_FLAGS or f.endswith("_review_required")]
        return (f"Reviews are required before any business approval ({_join(reasons)}).",
                f"Send the evidence package to {_join(reviewers or ['Procurement'])}; business approvals "
                f"({_join(business)}) follow once those reviews clear.")
    if action == "use_existing_tool":
        names = _join([c["product_name"] for c in rr.overlap_candidates])
        return (f"An approved tool may already cover this need ({names}).",
                f"Procurement to confirm with the requester whether {names} covers the need before any new purchase.")
    return ("Evidence is complete and no specialist review is triggered.",
            f"Route to {_join(business)} for approval.")


def _with_label(action: str, text: str) -> str:
    label = ACTION_LABELS[action]
    return text if text.lower().startswith(label.lower()) else f"{label}: {text}"


def finalize(request: dict, draft, rr: RuleResult, toolbox, *, llm_error: str | None = None,
             rules_only: bool = False) -> tuple[ProcurementDecision, dict]:
    notes: list[str] = []
    rule_evidence = [EvidenceItem(source="check_policy_rules", finding=f.finding,
                                  reference=", ".join([f.ref, *f.records]))
                     for f in rr.findings]
    request_id = str(request.get("request_id") or "NEW-REQUEST")

    if draft is None:
        flags = list(rr.risk_flags)
        if rules_only:
            notes.append("Rules-only run: no LLM was used.")
        else:
            flags.append("ai_analysis_unavailable")
            notes.append(f"AI analysis unavailable ({llm_error or 'no valid model output'}); "
                         "decision is based on the deterministic checks only.")
        action = rr.minimum_action
        rec, step = templated(action, rr.required_approvals, flags, rr.missing_information, rr)
        if rr.overlap_candidates and action != "request_clarification":
            step += " A reviewer should also judge whether the overlapping catalog tools already cover the need."
        decision = ProcurementDecision(
            request_id=request_id, recommendation=_with_label(action, rec), evidence=rule_evidence,
            required_approvals=rr.required_approvals, missing_information=rr.missing_information,
            risk_flags=flags, next_step=step, human_review_required=True, next_action=action,
            guardrail_notes=notes)
        return decision, {"draft_present": False, "draft_action": None, "action_overridden": False,
                          "approvals_added": [], "flags_added": [], "evidence_total": 0,
                          "evidence_ungrounded": 0, "unknown_approvers": []}

    # approvals: rules are the floor
    model_approvals, unknown = [], []
    for name in draft.required_approvals:
        canon = canonical_approver(name)
        (model_approvals if canon else unknown).append(canon or name)
    approvals_added = [a for a in rr.required_approvals if a not in model_approvals]
    if approvals_added:
        notes.append(f"Added approvals required by the policy checks that the draft left out: {_join(approvals_added)}.")
    if unknown:
        notes.append(f"Ignored approver names that aren't roles in the policy: {_join(unknown)}.")
    approvals = _ordered(rr.required_approvals + model_approvals, APPROVERS)

    # flags: rules are the floor
    model_flags = [canonical_flag(f) for f in draft.risk_flags if canonical_flag(f)]
    if draft.suspicious_content and "prompt_injection_detected" not in model_flags:
        model_flags.append("prompt_injection_detected")
    flags_added = [f for f in rr.risk_flags if f not in model_flags]
    if flags_added:
        notes.append(f"Added risk flags from the policy checks that the draft left out: {_join(flags_added)}.")
    flags = _ordered(rr.risk_flags + model_flags)

    # missing information: §1 items only; anything else becomes an open question
    missing = list(rr.missing_information)
    topics = {_missing_topic(m) for m in missing}
    open_questions = list(draft.open_questions)
    for item in draft.missing_information:
        topic = _missing_topic(item)
        if topic is None:
            open_questions.append(item)
        elif topic not in topics:
            missing.append(item)
            topics.add(topic)
    if missing and "missing_information" not in flags:
        flags.append("missing_information")

    # evidence: keep only items whose refs came back from a tool in this run
    evidence, ungrounded = [], []
    for item in draft.evidence:
        parts = [p.strip() for p in re.split(r"[,;]", item.ref) if p.strip()]
        sources = [toolbox.resolve_ref(p) for p in parts]
        if parts and all(sources):
            evidence.append(EvidenceItem(source=sources[0], finding=item.finding, reference=", ".join(parts)))
        else:
            ungrounded.append(item.ref)
    if ungrounded:
        notes.append(f"Dropped {len(ungrounded)} evidence item(s) citing refs no tool returned: {_join(ungrounded)}.")

    # next action: can't be less cautious than the evidence allows
    blockers = [a for a in approvals if a in SPECIALIST_APPROVERS] + [f for f in flags if f in BLOCKING_FLAGS]
    action = draft.next_action
    if missing and action != "request_clarification":
        action = "request_clarification"
    elif blockers and action in ("route_for_approval", "use_existing_tool"):
        action = "escalate_for_review"
    overridden = action != draft.next_action
    if overridden:
        notes.append(f"Next action changed from {draft.next_action} to {action} "
                     f"({'missing information' if missing else 'blocking reviews: ' + _join(blockers)}).")

    rec, step = draft.recommendation.strip(), draft.next_step.strip()
    fallback_rec, fallback_step = templated(action, approvals, flags, missing, rr)
    if overridden or not rec or _APPROVAL_CLAIM.search(rec):
        if rec and not overridden:
            notes.append("Replaced recommendation text that claimed the request was already approved.")
        rec = fallback_rec
    if overridden or not step or _APPROVAL_CLAIM.search(step):
        step = fallback_step

    decision = ProcurementDecision(
        request_id=request_id, recommendation=_with_label(action, rec), evidence=evidence + rule_evidence,
        required_approvals=approvals, missing_information=missing, risk_flags=flags, next_step=step,
        human_review_required=True, next_action=action, open_questions=open_questions, guardrail_notes=notes)
    stats = {"draft_present": True, "draft_action": draft.next_action, "action_overridden": overridden,
             "approvals_added": approvals_added, "flags_added": flags_added,
             "evidence_total": len(draft.evidence), "evidence_ungrounded": len(ungrounded),
             "unknown_approvers": unknown}
    return decision, stats
