"""Deterministic policy checks (data/procurement_policy.md).

Everything here is plain code: thresholds, budget arithmetic, review dates and
the Security / Privacy / Legal triggers. The LLM never supplies numbers to these
functions - they read the request and the source records directly.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import date

APPROVERS = ["Manager", "Department Head", "Procurement", "Finance", "CFO", "Security", "Privacy", "Legal"]
SPECIALIST_APPROVERS = {"Security", "Privacy", "Legal"}

REVIEW_VALID_DAYS = 365          # §5
NEW_VENDOR_LEGAL_THRESHOLD = 10_000  # §7 ("$10,000 or more")

# §4: (upper bound of the tier, minimum approvals)
APPROVAL_TIERS = [
    (1_000, ["Manager"]),
    (10_000, ["Department Head", "Procurement"]),
    (25_000, ["Department Head", "Finance", "Procurement"]),
    (float("inf"), ["Department Head", "Finance", "CFO", "Procurement"]),
]

UNKNOWN_VALUES = {"", "unknown", "tbd", "tbc", "n/a", "na", "not sure", "?"}

# §5 triggers. Matched as substrings so values like "employee_pii" or
# "production_telemetry" are caught without listing every variant.
SECURITY_DATA_TERMS = ("source_code", "source code", "confidential", "pii", "personal",
                       "credential", "secret", "password", "production")
SECURITY_INTEGRATION_TERMS = ("git", "source code", "production", "cloud account", "aws", "azure",
                              "gcp", "kubernetes", "database", "secret", "credential", "vault")
PII_TERMS = ("pii", "personal")

# Flags that stop a request going straight to business approvers.
BLOCKING_FLAGS = {"budget_insufficient", "budget_unverified", "vendor_risk_unavailable",
                  "conflicting_vendor_evidence", "vendor_assessment_missing"}


@dataclass
class Finding:
    check: str
    finding: str
    ref: str                                  # policy section, e.g. "policy:§4"
    records: list[str] = field(default_factory=list)
    approvals: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)


@dataclass
class RuleResult:
    reference_date: str
    annual_cost_usd: float | None
    required_approvals: list[str]
    risk_flags: list[str]
    missing_information: list[str]
    blockers: list[str]
    overlap_candidates: list[dict]
    findings: list[Finding]

    @property
    def minimum_action(self) -> str:
        if self.missing_information:
            return "request_clarification"
        if self.blockers:
            return "escalate_for_review"
        return "route_for_approval"

    def to_dict(self) -> dict:
        data = asdict(self)
        data["minimum_action"] = self.minimum_action
        return data


# ---------------------------------------------------------------- helpers

def _blank(value: object) -> bool:
    return value is None or str(value).strip().lower() in UNKNOWN_VALUES


def _norm(value: object) -> str:
    return str(value or "").strip().lower()


def usd(amount: float | int | None) -> str:
    if amount is None:
        return "n/a"
    return f"${amount:,.0f}" if float(amount).is_integer() else f"${amount:,.2f}"


def annual_cost(request: dict) -> float | None:
    value = request.get("annual_cost_usd")
    if isinstance(value, bool) or value is None:
        return None
    try:
        amount = float(str(value).replace(",", "").replace("$", "").strip())
    except ValueError:
        return None
    return amount if amount >= 0 else None


def parse_date(value: object) -> date | None:
    if _blank(value):
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def review_age_days(review_date: object, reference_date: date) -> int | None:
    parsed = parse_date(review_date)
    return (reference_date - parsed).days if parsed else None


def is_approved(status: object) -> bool:
    return _norm(status) in {"approved", "standard"}


def data_level(request: dict) -> str:
    return _norm(request.get("data_access_level"))


def integrations(request: dict) -> list[str]:
    """requested_integrations as a list of strings, whatever shape it arrived in.

    A bare string would otherwise be iterated character by character and slip past
    the Security triggers.
    """
    value = request.get("requested_integrations")
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(v) for v in value if not _blank(v)]
    return [part.strip() for part in re.split(r"[,;\n]", str(value)) if not _blank(part)]


def is_sensitive_data(level: str) -> bool:
    return any(term in level for term in SECURITY_DATA_TERMS)


def is_pii(level: str) -> bool:
    return any(term in level for term in PII_TERMS)


def is_ai_tool(request: dict, candidates: list[dict]) -> bool:
    text = " ".join(str(request.get(k) or "") for k in ("category", "product_name"))
    text += " " + " ".join(str(c.get("category") or "") for c in candidates)
    return bool(re.search(r"\bai\b", text, re.IGNORECASE))


def approval_tier(amount: float) -> list[str]:
    for upper, approvals in APPROVAL_TIERS:
        if amount <= upper:
            return list(approvals)
    return list(APPROVAL_TIERS[-1][1])


def find_overlap_candidates(request: dict, catalog: list[dict]) -> list[dict]:
    """Same vendor, same product or same category in the approved catalog (§3)."""
    vendor = _norm(request.get("vendor_name"))
    product = _norm(request.get("product_name"))
    category = _norm(request.get("category"))
    out = []
    for row in catalog:
        reasons = []
        if vendor and _norm(row.get("vendor_name")) == vendor:
            reasons.append("same vendor")
        row_product = _norm(row.get("product_name"))
        if product and row_product and (row_product in product or product in row_product):
            reasons.append("same product")
        if category and _norm(row.get("category")) == category:
            reasons.append("same category")
        if reasons:
            out.append({**row, "overlap": reasons})
    return out


# ---------------------------------------------------------------- checks

def check_required_information(request: dict, employee: dict | None) -> list[str]:
    missing = []
    if employee is None:
        missing.append("Requester (employee ID not found in the directory)")
    elif _blank(employee.get("department")):
        missing.append("Requester department")
    if _blank(request.get("product_name")):
        missing.append("Product name")
    if _blank(request.get("vendor_name")):
        missing.append("Vendor name")
    if annual_cost(request) is None:
        missing.append("Annual cost (or a reasonable annual estimate)")
    users = request.get("user_count")
    if isinstance(users, bool) or not isinstance(users, (int, float)) or users <= 0:
        missing.append("Number of users / licenses")
    if _blank(request.get("business_justification")):
        missing.append("Business purpose")
    if _blank(request.get("data_access_level")):
        missing.append("Intended data-access level")
    if request.get("requested_integrations") is None:
        missing.append("Required integrations (or confirm there are none)")
    return missing


def check_threshold(cost: float | None) -> Finding:
    if cost is None:
        return Finding("approval_threshold",
                       "Approval tier cannot be set until an annual cost is provided; Procurement to triage.",
                       "policy:§4", approvals=["Procurement"])
    approvals = approval_tier(cost)
    return Finding("approval_threshold",
                   f"Annual amount {usd(cost)} falls in the tier requiring {', '.join(approvals)}.",
                   "policy:§4", approvals=approvals)


def check_budget(cost: float | None, employee: dict | None, budget: dict | None) -> Finding:
    if employee is None or _blank(employee.get("department")):
        return Finding("budget", "Budget could not be checked because the requester's department is unknown.",
                       "policy:§2", approvals=["Finance"], flags=["budget_unverified"])
    dept = employee["department"]
    if budget is None:
        return Finding("budget", f"No software budget record exists for {dept}; available budget cannot be verified.",
                       "policy:§2", approvals=["Finance"], flags=["budget_unverified"])
    available = float(budget["available_usd"])
    record = [f"budget:{dept}"]
    if cost is None:
        return Finding("budget", f"Budget check pending: no annual cost given ({dept} has {usd(available)} available).",
                       "policy:§2", records=record)
    if cost > available:
        return Finding("budget",
                       f"Annual cost {usd(cost)} exceeds {dept}'s available software budget of {usd(available)} "
                       f"(shortfall {usd(cost - available)}); Finance budget-exception review needed.",
                       "policy:§2", records=record, approvals=["Finance"], flags=["budget_insufficient"])
    return Finding("budget",
                   f"Annual cost {usd(cost)} is within {dept}'s available software budget of {usd(available)} "
                   f"({usd(available - cost)} left). This does not by itself mean the purchase should be approved.",
                   "policy:§2", records=record)


def check_overlap(candidates: list[dict], request: dict) -> list[Finding]:
    if not candidates:
        return []
    parts, refs = [], []
    for c in candidates:
        seats = f", {c['licensed_seats']} seats" if c.get("licensed_seats") else ""
        parts.append(f"{c['product_name']} ({c['software_id']}, {c['status']}, {c['scope']}{seats}; {' + '.join(c['overlap'])})")
        refs.append(c["software_id"])
    findings = [Finding("existing_tools",
                        "Existing catalog software overlaps with this request: " + "; ".join(parts)
                        + ". Overlap is not a rejection - confirm the gap or use existing capacity first.",
                        "policy:§3", records=refs, flags=["existing_tool_overlap"])]

    level = data_level(request)
    for c in candidates:
        if "limited" in _norm(c.get("status")) and is_sensitive_data(level):
            findings.append(Finding("ai_tool_scope",
                                    f"{c['product_name']} is approved for limited use only; that approval does not "
                                    f"cover '{level}' data.",
                                    "policy:§8", records=[c["software_id"]],
                                    approvals=["Security"], flags=["security_review_required"]))
    return findings


def check_vendor(request: dict, registry: dict | None, risk: dict, reference_date: date) -> list[Finding]:
    vendor = request.get("vendor_name") or "The vendor"
    findings: list[Finding] = []

    reg_status_ok = False
    reg_date = None
    if registry is None:
        findings.append(Finding("vendor_registry",
                                f"{vendor} is not in the internal vendor registry (treated as a new vendor with no "
                                "security assessment or legal terms on file).",
                                "policy:§5", approvals=["Security", "Legal"],
                                flags=["new_vendor", "security_review_required", "legal_review_required"]))
    else:
        reg_ref = [registry["vendor_id"]]
        reg_status_ok = is_approved(registry.get("security_status"))
        reg_date = parse_date(registry.get("security_review_date"))
        age = review_age_days(reg_date, reference_date)
        if not is_approved(registry.get("procurement_status")):
            findings.append(Finding("vendor_registry",
                                    f"{vendor} has procurement status '{registry.get('procurement_status')}' in the "
                                    "registry (new / not fully onboarded).",
                                    "policy:§7", records=reg_ref, flags=["new_vendor"]))
        if not reg_status_ok:
            findings.append(Finding("vendor_security",
                                    f"Registry security status for {vendor} is '{registry.get('security_status')}'.",
                                    "policy:§5", records=reg_ref, approvals=["Security"],
                                    flags=["security_review_required"]))
        elif age is None or age < 0:
            problem = "has no review date on file" if age is None else f"has a review date ({reg_date}) after the reference date"
            findings.append(Finding("vendor_security",
                                    f"Registry shows {vendor} as approved but {problem}.",
                                    "policy:§5", records=reg_ref, approvals=["Security"],
                                    flags=["security_review_required"]))
        elif age > REVIEW_VALID_DAYS:
            findings.append(Finding("vendor_security",
                                    f"Registry security review for {vendor} is dated {reg_date} ({age} days before "
                                    f"{reference_date}); assessments are only current for {REVIEW_VALID_DAYS} days.",
                                    "policy:§5", records=reg_ref, approvals=["Security"],
                                    flags=["vendor_review_expired", "security_review_required"]))

    risk_ref = [risk["ref"]]
    if not risk.get("available"):
        findings.append(Finding("vendor_risk",
                                f"Vendor-risk service could not be reached for {vendor} ({risk.get('error', 'unavailable')}). "
                                "Risk status is unverified - not assuming a favourable result.",
                                "policy:§10", records=risk_ref, approvals=["Security"],
                                flags=["vendor_risk_unavailable"]))
        return findings
    if not risk.get("found"):
        findings.append(Finding("vendor_risk",
                                f"The vendor-risk service has no assessment on record for {vendor}.",
                                "policy:§5", records=risk_ref, approvals=["Security"],
                                flags=["vendor_assessment_missing", "security_review_required"]))
        return findings

    api_status = _norm(risk.get("security_review_status"))
    api_date = parse_date(risk.get("last_review_date"))
    api_age = review_age_days(api_date, reference_date)
    api_status_ok = api_status == "approved"
    # a missing or future-dated review counts as not current (fail closed)
    if api_status == "expired" or (api_status_ok and (api_age is None or not 0 <= api_age <= REVIEW_VALID_DAYS)):
        findings.append(Finding("vendor_risk",
                                f"Vendor-risk service reports {vendor}'s assessment as '{api_status}' "
                                f"(last review {api_date or 'not recorded'}).",
                                "policy:§5", records=risk_ref, approvals=["Security"],
                                flags=["vendor_review_expired", "security_review_required"]))
    elif not api_status_ok:
        findings.append(Finding("vendor_risk",
                                f"Vendor-risk service reports {vendor}'s security review as '{api_status}'.",
                                "policy:§5", records=risk_ref, approvals=["Security"],
                                flags=["security_review_required"]))
    if _norm(risk.get("risk_level")) == "high":
        findings.append(Finding("vendor_risk", f"Vendor-risk service rates {vendor} as high risk.",
                                "policy:§5", records=risk_ref, approvals=["Security"],
                                flags=["high_vendor_risk", "security_review_required"]))

    if registry is not None:
        status_conflict = reg_status_ok != api_status_ok
        date_conflict = reg_date is not None and api_date is not None and reg_date != api_date
        if status_conflict or date_conflict:
            findings.append(Finding("vendor_conflict",
                                    f"Registry says security '{registry.get('security_status')}' "
                                    f"(review {reg_date or 'n/a'}) but the vendor-risk service says '{api_status}' "
                                    f"(review {api_date or 'n/a'}). Not picking one - Security to confirm.",
                                    "policy:§5", records=[registry["vendor_id"], *risk_ref], approvals=["Security"],
                                    flags=["conflicting_vendor_evidence", "security_review_required"]))
    return findings


def check_data_handling(request: dict, risk: dict) -> list[Finding]:
    findings: list[Finding] = []
    level = data_level(request)
    vendor = request.get("vendor_name") or "The vendor"
    reasons = []
    if is_sensitive_data(level):
        reasons.append(f"data access level '{level}'")
    for integration in integrations(request):
        if any(term in _norm(integration) for term in SECURITY_INTEGRATION_TERMS):
            reasons.append(f"integration '{integration}'")
    if reasons:
        findings.append(Finding("security_scope", "Security review required for " + " and ".join(reasons) + ".",
                                "policy:§5", records=["request:data_access_level"], approvals=["Security"],
                                flags=["security_review_required"]))
    if is_pii(level):
        findings.append(Finding("privacy", f"The tool would process {level.replace('_', ' ')}.",
                                "policy:§6", records=["request:data_access_level"], approvals=["Privacy"],
                                flags=["privacy_review_required"]))
    if is_sensitive_data(level):
        if risk.get("available") and risk.get("found") and risk.get("stores_data_outside_region"):
            findings.append(Finding("data_residency",
                                    f"{vendor} stores data outside the operating region and the request involves "
                                    f"'{level}' data - a material cross-region issue.",
                                    "policy:§6", records=[risk["ref"]],
                                    approvals=["Privacy", "Legal"],
                                    flags=["privacy_review_required", "legal_review_required", "cross_region_data"]))
        elif not (risk.get("available") and risk.get("found")):
            findings.append(Finding("data_residency",
                                    f"Data residency for {vendor} could not be verified and the request involves "
                                    f"'{level}' data; Privacy to confirm where data is stored.",
                                    "policy:§6", records=[risk["ref"]], approvals=["Privacy"],
                                    flags=["privacy_review_required"]))
    return findings


def check_legal(cost: float | None, registry: dict | None) -> list[Finding]:
    if registry is None:
        return []  # already covered by the "not in registry" finding
    findings = []
    new_vendor = not is_approved(registry.get("procurement_status"))
    if new_vendor and cost is not None and cost >= NEW_VENDOR_LEGAL_THRESHOLD:
        findings.append(Finding("legal", f"New vendor with annual spend {usd(cost)} (>= {usd(NEW_VENDOR_LEGAL_THRESHOLD)}).",
                                "policy:§7", records=[registry["vendor_id"]], approvals=["Legal"],
                                flags=["legal_review_required"]))
    if not is_approved(registry.get("legal_terms_status")):
        findings.append(Finding("legal", f"Legal terms status is '{registry.get('legal_terms_status') or 'not on file'}', "
                                "not approved/standard.",
                                "policy:§7", records=[registry["vendor_id"]], approvals=["Legal"],
                                flags=["legal_review_required"]))
    return findings


def check_ai_tool(request: dict, candidates: list[dict]) -> list[Finding]:
    if not is_ai_tool(request, candidates):
        return []
    level = data_level(request)
    if _blank(level):
        return [Finding("ai_tool_scope",
                        "AI tool with no stated data class - Security scope cannot be determined until the requester "
                        "says what data it will touch.",
                        "policy:§8", records=["request:data_access_level"])]
    return [Finding("ai_tool_scope",
                    "AI tool: normal budget, security, privacy and legal rules apply; earlier approvals do not extend "
                    "to new use cases or data classes.",
                    "policy:§8")]


def check_untrusted_content(hits: list[dict]) -> list[Finding]:
    return [Finding("untrusted_content",
                    f"Instruction-like text in {hit['field']} was ignored: \"{hit['excerpt']}\"",
                    "policy:§9", records=[hit["ref"]], flags=["prompt_injection_detected"])
            for hit in hits]


# ---------------------------------------------------------------- entry point

def _ordered(items: list[str], order: list[str] | None = None) -> list[str]:
    seen = list(dict.fromkeys(items))
    if order:
        seen.sort(key=lambda x: order.index(x) if x in order else len(order))
    return seen


def evaluate(request: dict, *, employee: dict | None, budget: dict | None, catalog: list[dict],
             registry: dict | None, risk: dict, injection_hits: list[dict], reference_date: date) -> RuleResult:
    cost = annual_cost(request)
    candidates = find_overlap_candidates(request, catalog)
    missing = check_required_information(request, employee)

    findings: list[Finding] = []
    if missing:
        findings.append(Finding("required_information",
                                "Request is not ready for approval; missing: " + "; ".join(missing) + ".",
                                "policy:§1", flags=["missing_information"]))
    findings.append(check_threshold(cost))
    findings.append(check_budget(cost, employee, budget))
    findings += check_overlap(candidates, request)
    findings += check_vendor(request, registry, risk, reference_date)
    findings += check_data_handling(request, risk)
    findings += check_legal(cost, registry)
    findings += check_ai_tool(request, candidates)
    findings += check_untrusted_content(injection_hits)

    approvals = _ordered([a for f in findings for a in f.approvals], APPROVERS)
    flags = _ordered([fl for f in findings for fl in f.flags])
    blockers = [a for a in approvals if a in SPECIALIST_APPROVERS] + [fl for fl in flags if fl in BLOCKING_FLAGS]

    return RuleResult(
        reference_date=reference_date.isoformat(),
        annual_cost_usd=cost,
        required_approvals=approvals,
        risk_flags=flags,
        missing_information=missing,
        blockers=blockers,
        overlap_candidates=[{k: c.get(k) for k in ("software_id", "product_name", "category", "status", "scope",
                                                    "licensed_seats", "overlap")} for c in candidates],
        findings=findings,
    )
