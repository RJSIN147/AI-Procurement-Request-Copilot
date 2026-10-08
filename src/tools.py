"""Tools the agents can call.

All tools are deterministic code over the starter data / mock API. The model
decides which ones to call and what to search for; it never computes budgets,
dates or thresholds itself.

Every record a tool returns carries a `ref` (E004, SW003, V005, budget:Finance,
vendor-risk:SignFlow, policy:§5, request:annual_cost_usd ...). Evidence the model
cites is only kept if its ref was actually returned by a tool in that run.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Any, Callable

import requests

from src import data_access as data
from src import injection, rules
from src.telemetry import RunTelemetryCounter
from src.vendor_client import get_vendor_risk

STOPWORDS = {"the", "and", "for", "with", "tool", "tools", "team", "teams", "software", "new", "need", "needs",
             "our", "their", "this", "that", "from", "into", "license", "licenses", "seats", "users"}


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    fn: Callable[..., dict]

    def spec(self) -> dict:
        return {"type": "function",
                "function": {"name": self.name, "description": self.description, "parameters": self.parameters}}


NO_ARGS = {"type": "object", "properties": {}}
VENDOR_ARG = {"type": "object", "properties": {"vendor_name": {
    "type": "string", "description": "Vendor to look up. Defaults to the vendor on the request."}}}


def normalize_ref(ref: str) -> str:
    text = str(ref).strip().strip("[]()'\"`").strip()
    lowered = text.lower()
    section = re.search(r"(?:§|section\s*|policy[:\s]*)(\d{1,2})\b", lowered)
    if section and ("policy" in lowered or "§" in lowered or "section" in lowered):
        return f"policy:§{int(section.group(1))}"
    if lowered.startswith("request.") or lowered.startswith("request "):
        lowered = "request:" + lowered[8:]
    return re.sub(r"\s+", "", lowered)


class ToolBox:
    """Tools bound to one request, with caching, a call trace and telemetry."""

    def __init__(self, request: dict, telemetry: RunTelemetryCounter | None = None,
                 vendor_risk_fn: Callable[[str], dict] | None = None):
        self.request = request
        self.telemetry = telemetry or RunTelemetryCounter()
        self.reference_date = data.policy_reference_date()
        self._vendor_risk_fn = vendor_risk_fn or get_vendor_risk
        self._cache: dict[str, Any] = {}
        self.trace: list[dict] = []
        self.refs: dict[str, str] = {}
        for key in request:
            self.refs[normalize_ref(f"request:{key}")] = "purchase_request"
        self.refs["request"] = "purchase_request"

        self.tools = {t.name: t for t in [
            Tool("get_requester_profile",
                 "Look up the requester in the employee directory: department, level, country and manager.",
                 NO_ARGS, self.get_requester_profile),
            Tool("check_department_budget",
                 "Deterministic budget check: compares the request's annual cost with the requester department's "
                 "available software budget.",
                 NO_ARGS, self.check_department_budget),
            Tool("search_software_catalog",
                 "Search the approved software catalog and purchase history. Use it to find the same product, the "
                 "same vendor, the same category, or any existing tool that could cover the stated use case. "
                 "Empty query = match on the request's vendor, product and category.",
                 {"type": "object", "properties": {"query": {
                     "type": "string", "description": "Keywords, e.g. a capability, category or product name."}}},
                 self.search_software_catalog),
            Tool("get_vendor_registry_record",
                 "Internal vendor registry: procurement status, security status and review date (with age in days "
                 "against the policy reference date), legal terms status.",
                 VENDOR_ARG, self.get_vendor_registry_record),
            Tool("get_vendor_risk_assessment",
                 "External vendor-risk service: current security review status, risk level, personal-data "
                 "processing and data residency. May be unavailable.",
                 VENDOR_ARG, self.get_vendor_risk_assessment),
            Tool("check_policy_rules",
                 "Deterministic policy engine. Returns the minimum required approvals, mandatory risk flags, "
                 "missing required information and the policy findings behind them. Source of truth for "
                 "thresholds and review triggers.",
                 NO_ARGS, self.check_policy_rules),
            Tool("read_policy",
                 "Return the exact wording of one procurement policy section (1-11). Only needed if you want to "
                 "quote a rule.",
                 {"type": "object", "properties": {"section": {"type": "integer", "minimum": 1, "maximum": 11}},
                  "required": ["section"]},
                 self.read_policy),
        ]}

    # ------------------------------------------------------------ plumbing

    def specs(self, names: list[str]) -> list[dict]:
        return [self.tools[n].spec() for n in names]

    def call(self, name: str, arguments: str | dict | None = None) -> dict:
        if name not in self.tools:
            return {"error": f"Unknown tool '{name}'."}
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments or "{}")
            except json.JSONDecodeError:
                return {"error": "Arguments were not valid JSON."}
        args = {k: v for k, v in (arguments or {}).items() if v not in (None, "")}

        self.telemetry.record_tool_call(name)
        started = time.perf_counter()
        try:
            result = self.tools[name].fn(**args)
        except TypeError as exc:
            result = {"error": f"Bad arguments for {name}: {exc}"}
        self._collect_refs(name, result)
        self.trace.append({"tool": name, "args": args, "result": result,
                           "ms": round((time.perf_counter() - started) * 1000, 1)})
        return result

    def _collect_refs(self, tool: str, result: Any) -> None:
        if isinstance(result, dict):
            for key, value in result.items():
                if key == "ref" and isinstance(value, str):
                    self.refs.setdefault(normalize_ref(value), tool)
                elif key == "records" and isinstance(value, list):
                    for ref in value:
                        if isinstance(ref, str):
                            self.refs.setdefault(normalize_ref(ref), tool)
                else:
                    self._collect_refs(tool, value)
        elif isinstance(result, list):
            for item in result:
                self._collect_refs(tool, item)

    def register_refs(self, refs: list[str], source: str) -> None:
        for ref in refs:
            self.refs.setdefault(normalize_ref(ref), source)

    def resolve_ref(self, ref: str) -> str | None:
        """Tool that produced this ref, or None if it never appeared in a tool result."""
        return self.refs.get(normalize_ref(ref))

    def _cached(self, key: str, loader: Callable[[], Any]) -> Any:
        if key not in self._cache:
            self._cache[key] = loader()
        return self._cache[key]

    # ------------------------------------------------------------ raw lookups (cached)

    def _employee(self) -> dict | None:
        rid = str(self.request.get("requester_id") or "")
        return self._cached(f"employee:{rid}", lambda: next(
            (e for e in data.records(data.load_employees()) if e["employee_id"] == rid), None))

    def _budget(self, department: str) -> dict | None:
        return self._cached(f"budget:{department}", lambda: next(
            (b for b in data.records(data.load_budgets()) if b["department"] == department), None))

    def _catalog(self) -> list[dict]:
        return self._cached("catalog", lambda: data.records(data.load_software_catalog()))

    def _purchases(self) -> list[dict]:
        return self._cached("purchases", lambda: data.records(data.load_purchase_history()))

    def _registry(self, vendor: str) -> dict | None:
        return self._cached(f"registry:{vendor.lower()}", lambda: next(
            (v for v in data.records(data.load_vendors()) if str(v["vendor_name"]).lower() == vendor.lower()), None))

    def _risk(self, vendor: str) -> dict:
        return self._cached(f"risk:{vendor.lower()}", lambda: self._fetch_risk(vendor))

    def _fetch_risk(self, vendor: str) -> dict:
        ref = f"vendor-risk:{vendor}"
        error = "unknown error"
        for attempt in range(2):
            try:
                record = self._vendor_risk_fn(vendor)
            except requests.HTTPError as exc:
                status = exc.response.status_code if exc.response is not None else None
                if status == 404:
                    return {"ref": ref, "vendor_name": vendor, "available": True, "found": False,
                            "note": "The vendor-risk service has no assessment on record for this vendor."}
                error = f"HTTP {status}"
                try:
                    error += f": {exc.response.json().get('detail')}"
                except Exception:
                    pass
            except requests.RequestException as exc:
                error = f"{type(exc).__name__}: could not reach the vendor-risk service"
            else:
                record = dict(record)
                record.pop("vendor_name", None)
                age = rules.review_age_days(record.get("last_review_date"), self.reference_date)
                return {"ref": ref, "vendor_name": vendor, "available": True, "found": True, **record,
                        "review_age_days": age, "reference_date": self.reference_date.isoformat()}
            if attempt == 0:
                time.sleep(0.3)
        return {"ref": ref, "vendor_name": vendor, "available": False, "found": False, "error": error}

    def injection_hits(self) -> list[dict]:
        vendor = str(self.request.get("vendor_name") or "")
        return injection.scan_request(self.request) + injection.scan_vendor_notes(
            self._registry(vendor) if vendor else None, self._risk(vendor) if vendor else {})

    # ------------------------------------------------------------ tools

    def get_requester_profile(self) -> dict:
        emp = self._employee()
        if emp is None:
            return {"found": False, "requester_id": self.request.get("requester_id"),
                    "note": "Requester ID not found in the employee directory."}
        manager = None
        if emp.get("manager_id"):
            m = next((e for e in data.records(data.load_employees()) if e["employee_id"] == emp["manager_id"]), None)
            if m:
                manager = {"ref": m["employee_id"], "name": m["name"], "level": m["level"], "department": m["department"]}
        return {"found": True, "ref": emp["employee_id"], "name": emp["name"], "department": emp["department"],
                "level": emp["level"], "country": emp["country"], "manager": manager}

    def check_department_budget(self) -> dict:
        emp = self._employee()
        cost = rules.annual_cost(self.request)
        if emp is None or not emp.get("department"):
            return {"checked": False, "reason": "Requester or department unknown, so no budget to check against."}
        dept = emp["department"]
        row = self._budget(dept)
        if row is None:
            return {"checked": False, "department": dept,
                    "reason": f"No software budget record for department '{dept}'."}
        available = float(row["available_usd"])
        out = {"ref": f"budget:{dept}", "checked": cost is not None, "department": dept,
               "annual_budget_usd": row["annual_software_budget_usd"], "committed_usd": row["committed_usd"],
               "available_usd": row["available_usd"], "requested_annual_cost_usd": cost}
        if cost is None:
            out["reason"] = "Request has no annual cost, so the budget cannot be compared."
        else:
            out["within_budget"] = cost <= available
            out["remaining_after_usd" if cost <= available else "shortfall_usd"] = abs(available - cost)
        return out

    def search_software_catalog(self, query: str = "") -> dict:
        catalog = self._catalog()
        terms = [t for t in re.findall(r"[a-z0-9]+", query.lower()) if (len(t) >= 3 or t == "ai") and t not in STOPWORDS]
        deterministic = {c["software_id"]: c["overlap"] for c in rules.find_overlap_candidates(self.request, catalog)}

        matches = []
        for row in catalog:
            haystack = " ".join(str(row.get(k) or "") for k in
                                ("product_name", "category", "vendor_name", "notes", "scope")).lower()
            hit_terms = [t for t in terms if re.search(rf"\b{re.escape(t)}", haystack)]
            if (terms and hit_terms) or (not terms and row["software_id"] in deterministic):
                matches.append((len(hit_terms), row, hit_terms))
        matches.sort(key=lambda m: -m[0])

        purchases = self._purchases()
        items = []
        for _, row, hit_terms in matches[:8]:
            history = [{"ref": p["purchase_id"], "date": p["purchase_date"], "department": p["department"],
                        "annual_amount_usd": p["annual_amount_usd"], "status": p["status"], "notes": p["notes"]}
                       for p in purchases if str(p["vendor_name"]).lower() == str(row["vendor_name"]).lower()]
            items.append({"ref": row["software_id"], "product_name": row["product_name"], "category": row["category"],
                          "vendor_name": row["vendor_name"], "status": row["status"], "scope": row["scope"],
                          "licensed_seats": row["licensed_seats"], "annual_cost_usd": row["annual_cost_usd"],
                          "notes": row["notes"], "matched_terms": hit_terms,
                          "overlap_with_request": deterministic.get(row["software_id"], []),
                          "purchase_history": history})
        return {"query": query, "match_count": len(items), "matches": items}

    def get_vendor_registry_record(self, vendor_name: str | None = None) -> dict:
        vendor = vendor_name or str(self.request.get("vendor_name") or "")
        row = self._registry(vendor)
        if row is None:
            return {"found": False, "vendor_name": vendor, "note": "Vendor is not in the internal vendor registry."}
        age = rules.review_age_days(row.get("security_review_date"), self.reference_date)
        return {"found": True, "ref": row["vendor_id"], "vendor_name": row["vendor_name"],
                "procurement_status": row["procurement_status"], "security_status": row["security_status"],
                "security_review_date": row["security_review_date"], "review_age_days": age,
                "review_current": rules.is_approved(row["security_status"]) and age is not None
                and age <= rules.REVIEW_VALID_DAYS,
                "legal_terms_status": row["legal_terms_status"], "notes": row["notes"],
                "reference_date": self.reference_date.isoformat()}

    def get_vendor_risk_assessment(self, vendor_name: str | None = None) -> dict:
        vendor = vendor_name or str(self.request.get("vendor_name") or "")
        return self._risk(vendor)

    def check_policy_rules(self) -> dict:
        result = self.policy_result()
        out = result.to_dict()
        out["findings"] = [{"check": f.check, "finding": f.finding, "ref": f.ref, "records": f.records}
                           for f in result.findings]
        out.pop("overlap_candidates")
        return out

    def read_policy(self, section: int) -> dict:
        sections = data.policy_sections()
        try:
            number = int(section)
        except (TypeError, ValueError):
            return {"error": "section must be a number between 1 and 11"}
        if number not in sections:
            return {"error": f"No policy section {number}."}
        return {"ref": f"policy:§{number}", "text": sections[number]}

    # ------------------------------------------------------------ rules (used by tools + guardrail)

    def policy_result(self) -> rules.RuleResult:
        def run() -> rules.RuleResult:
            vendor = str(self.request.get("vendor_name") or "")
            emp = self._employee()
            # Always the vendor on the request - a tool argument the model chose (or text in the
            # request) can't point the rules at a different vendor.
            registry = self._registry(vendor) if vendor else None
            risk = self._risk(vendor) if vendor else {"ref": "vendor-risk:unknown", "available": False,
                                                       "found": False, "error": "no vendor named"}
            return rules.evaluate(
                self.request, employee=emp,
                budget=self._budget(emp["department"]) if emp and emp.get("department") else None,
                catalog=self._catalog(), registry=registry, risk=risk,
                injection_hits=self.injection_hits(), reference_date=self.reference_date)
        return self._cached("policy_result", run)

    def results_digest(self) -> list[dict]:
        """Compact list of what each tool returned (input for the staged reviewer)."""
        return [{"tool": t["tool"], "args": t["args"], "result": t["result"]} for t in self.trace]
