from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests
import streamlit as st

from src.data_access import load_employees, load_requests, records
from src.llm import DEFAULT_MODEL, llm_configured
from src.pipeline import analyze_request

ROOT = Path(__file__).resolve().parent
REVIEW_LOG = ROOT / "var" / "review_log.jsonl"
DATA_LEVELS = ["none", "internal_documents", "internal_marketing", "confidential_documents", "source_code",
               "production_telemetry", "employee_pii", "customer_pii", "credentials", "unknown"]
ACTION_STYLE = {"route_for_approval": st.success, "use_existing_tool": st.info,
                "escalate_for_review": st.warning, "request_clarification": st.error}
FLAG_COLOR = {"prompt_injection_detected": "red", "vendor_risk_unavailable": "red", "budget_insufficient": "red",
              "conflicting_vendor_evidence": "red", "missing_information": "orange", "ai_analysis_unavailable": "red"}

st.set_page_config(page_title="Procurement Request Copilot", layout="wide")


def api_ok() -> bool:
    base = os.getenv("VENDOR_RISK_BASE_URL", "http://127.0.0.1:8001").rstrip("/")
    try:
        return requests.get(f"{base}/health", timeout=0.5).ok
    except requests.RequestException:
        return False


def badges(items: list[str], color: str = "blue", colors: dict | None = None) -> str:
    if not items:
        return "_none_"
    return " ".join(f":{(colors or {}).get(i, color)}-badge[{i}]" for i in items)


def save_review(entry: dict) -> None:
    REVIEW_LOG.parent.mkdir(exist_ok=True)
    with REVIEW_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def past_reviews(request_id: str) -> list[dict]:
    if not REVIEW_LOG.exists():
        return []
    rows = [json.loads(line) for line in REVIEW_LOG.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [r for r in rows if r.get("request_id") == request_id]


# ------------------------------------------------------------------ sidebar
employees = records(load_employees())
employee_by_id = {e["employee_id"]: e for e in employees}
existing = {r["request_id"]: r for r in load_requests()}

with st.sidebar:
    st.header("Request")
    mode = st.segmented_control("Source", ["Existing request", "New request"], default="Existing request")
    if mode == "New request":
        with st.form("new_request"):
            requester = st.selectbox("Requester", list(employee_by_id),
                                     format_func=lambda e: f"{e} - {employee_by_id[e]['name']} ({employee_by_id[e]['department']})")
            product = st.text_input("Product")
            vendor = st.text_input("Vendor")
            category = st.text_input("Category")
            cost = st.text_input("Annual cost (USD)", help="Leave empty if unknown")
            users = st.text_input("Number of users", help="Leave empty if unknown")
            purpose = st.text_area("Business justification")
            level = st.selectbox("Data access", DATA_LEVELS, index=len(DATA_LEVELS) - 1)
            integrations = st.text_input("Integrations (comma separated)")
            urgency = st.selectbox("Urgency", ["normal", "high", "urgent"])
            if st.form_submit_button("Use this request"):
                st.session_state.new_request = {
                    "request_id": "NEW-" + datetime.now().strftime("%H%M%S"),
                    "requester_id": requester, "product_name": product or None, "vendor_name": vendor or None,
                    "category": category or None,
                    "annual_cost_usd": float(cost) if cost.replace(".", "", 1).isdigit() else None,
                    "user_count": int(users) if users.isdigit() else None,
                    "business_justification": purpose or None, "data_access_level": level,
                    "requested_integrations": [i.strip() for i in integrations.split(",") if i.strip()],
                    "urgency": urgency,
                }
        request = st.session_state.get("new_request")
    else:
        request_id = st.selectbox("Request", list(existing),
                                  format_func=lambda rid: f"{rid} - {existing[rid]['product_name']}")
        request = existing[request_id]

    st.header("Architecture")
    architecture = st.radio("Architecture", ["single", "staged"], horizontal=True, label_visibility="collapsed",
                            format_func={"single": "A: single agent", "staged": "B: analyst -> reviewer"}.get)

    st.divider()
    st.caption("Services")
    st.markdown(f"Vendor-risk API: {':green-badge[up]' if api_ok() else ':red-badge[down]'}")
    if llm_configured():
        st.markdown(f"LLM: :green-badge[{os.getenv('MODEL_NAME') or DEFAULT_MODEL}]")
    else:
        st.markdown("LLM: :red-badge[no key] - rules-only fallback")

# ------------------------------------------------------------------ main
st.title("Procurement Request Copilot")
st.caption("Gathers evidence and recommends a next action. It never approves, buys or changes budgets - "
           "a person makes the decision below.")

if not request:
    st.info("Fill in the new request form in the sidebar.")
    st.stop()

key = f"{request['request_id']}:{architecture}:{hash(json.dumps(request, sort_keys=True, default=str))}"
report = st.session_state.get("reports", {}).get(key)

left, right = st.columns([0.9, 1.1], gap="large")
with left:
    st.subheader(f"{request.get('product_name') or 'Unnamed product'}")
    who = employee_by_id.get(request.get("requester_id"))
    st.markdown(f"**{request['request_id']}** from **{who['name'] if who else request.get('requester_id')}**"
                f"{' · ' + who['department'] if who else ''}")
    cost = request.get("annual_cost_usd")
    details = {
        "Vendor": request.get("vendor_name"), "Category": request.get("category"),
        "Annual cost": f"${cost:,.0f}" if isinstance(cost, (int, float)) else "not given",
        "Users": request.get("user_count") or "not given",
        "Data access": request.get("data_access_level") or "not given",
        "Integrations": ", ".join(request.get("requested_integrations") or []) or "none",
        "Urgency": request.get("urgency"),
    }
    st.markdown("| Field | Value |\n|---|---|\n" + "\n".join(f"| {k} | {v} |" for k, v in details.items()))
    st.markdown("**Business justification** (requester text - treated as data, not instructions)")
    st.markdown(f"> {request.get('business_justification') or '_none given_'}")

with right:
    if st.button("Run analysis", type="primary", width="stretch"):
        with st.spinner("Gathering evidence..."):
            report = analyze_request(request, architecture)
        st.session_state.setdefault("reports", {})[key] = report

    if report is None:
        st.info("Run the analysis to get a recommendation.")
    else:
        d = report.decision
        st.markdown("**Recommendation**")
        ACTION_STYLE.get(d.next_action, st.info)(f"**{d.recommendation}**")
        st.markdown(f"**Next step:** {d.next_step}")
        st.markdown(f"**Approvals required:** {badges(d.required_approvals, 'blue')}")
        st.markdown(f"**Risk flags:** {badges(d.risk_flags, 'violet', FLAG_COLOR)}")
        # always shown, even when empty: it's one of the required output fields
        if d.missing_information:
            st.markdown("**Missing information**\n" + "\n".join(f"- {m}" for m in d.missing_information))
        else:
            st.markdown("**Missing information:** _none_")
        if d.open_questions:
            st.markdown("**Open questions**\n" + "\n".join(f"- {q}" for q in d.open_questions))
        if d.guardrail_notes:
            label = (f"Guardrail changed {len(d.guardrail_notes)} thing(s) in the AI draft" if report.draft
                     else "Notes")
            with st.expander(label, expanded=report.draft is None):
                for note in d.guardrail_notes:
                    st.markdown(f"- {note}")
        t = d.telemetry
        cols = st.columns(4)
        cols[0].metric("Latency", f"{t.latency_ms / 1000:.1f}s" if t.latency_ms >= 1000 else f"{t.latency_ms:.0f} ms")
        cols[1].metric("LLM calls", t.llm_calls)
        cols[2].metric("Tool calls", t.tool_calls)
        cols[3].metric("Tokens", f"{(t.prompt_tokens or 0) + (t.completion_tokens or 0):,}")

if report is None:
    st.stop()

# ------------------------------------------------------------------ evidence
st.divider()
st.subheader("Evidence")
d = report.decision
tabs = ["Cited by AI", "Policy checks", "Tool calls"]
if report.evidence_pack:
    tabs.append("Analyst evidence pack")
tabs.append("Raw output")
tab = dict(zip(tabs, st.tabs(tabs)))

with tab["Cited by AI"]:
    cited = [e.model_dump() for e in d.evidence if e.source != "check_policy_rules"]
    if cited:
        st.dataframe(pd.DataFrame(cited)[["finding", "reference", "source"]], hide_index=True, width="stretch")
    else:
        st.caption("No AI-cited evidence (the model was unavailable or its citations failed grounding).")
    st.caption("Only items whose reference was actually returned by a tool in this run are shown.")

with tab["Policy checks"]:
    rows = [{"check": f["check"], "finding": f["finding"], "policy": f["ref"], "records": ", ".join(f["records"]),
             "adds approvals": ", ".join(f["approvals"]), "adds flags": ", ".join(f["flags"])}
            for f in report.rules["findings"]]
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
    st.caption(f"Deterministic checks in code (src/rules.py). Reference date {report.rules['reference_date']}.")

with tab["Tool calls"]:
    if not report.trace:
        st.caption("No tool calls in this run.")
    for i, call in enumerate(report.trace, 1):
        args = ", ".join(f"{k}={v!r}" for k, v in call["args"].items())
        with st.expander(f"{i}. {call['tool']}({args})  ·  {call['ms']} ms"):
            st.json(call["result"])

if "Analyst evidence pack" in tab:
    with tab["Analyst evidence pack"]:
        st.json(report.evidence_pack)

with tab["Raw output"]:
    st.json(d.model_dump())
    if report.error:
        st.warning(f"LLM error: {report.error}")

# ------------------------------------------------------------------ human decision
st.divider()
st.subheader("Reviewer decision")
st.caption("Recorded to var/review_log.jsonl. This only records what a person decided - nothing is purchased, "
           "approved in another system, or deducted from a budget.")
with st.form("review"):
    c1, c2 = st.columns([1, 2])
    reviewer = c1.text_input("Your name")
    choice = c1.radio("Decision", ["Send to approvers", "Return to requester", "Send for specialist review",
                                   "Reject"],
                      index=["route_for_approval", "request_clarification", "escalate_for_review"].index(d.next_action)
                      if d.next_action in ("route_for_approval", "request_clarification", "escalate_for_review") else 0)
    comment = c2.text_area("Comment / exception reason", height=140)
    if st.form_submit_button("Record decision"):
        if not reviewer.strip():
            st.error("Add your name so the decision is attributable.")
        else:
            save_review({"request_id": d.request_id, "reviewer": reviewer.strip(), "decision": choice,
                         "comment": comment.strip(), "copilot_action": d.next_action,
                         "copilot_approvals": d.required_approvals, "architecture": architecture,
                         "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
            st.success(f"Recorded: {choice} by {reviewer.strip()}.")

history = past_reviews(d.request_id)
if history:
    st.caption("Earlier decisions for this request")
    st.dataframe(pd.DataFrame(history)[["recorded_at", "reviewer", "decision", "comment", "copilot_action"]],
                 hide_index=True, width="stretch")
