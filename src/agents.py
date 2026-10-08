"""Architecture A (single agent) and B (analyst -> reviewer)."""
from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, Field

from src.data_access import load_policy_text, policy_sections
from src.llm import LLMClient, run_tool_loop
from src.prompts import (ANALYST_SYSTEM, REVIEWER_SYSTEM, SINGLE_SYSTEM, SUBMIT_DECISION, SUBMIT_EVIDENCE_PACK,
                         request_message)
from src.tools import ToolBox

DATA_TOOLS = ["get_requester_profile", "check_department_budget", "search_software_catalog",
              "get_vendor_registry_record", "get_vendor_risk_assessment"]
SINGLE_TOOLS = DATA_TOOLS + ["check_policy_rules", "read_policy"]


class CitedFinding(BaseModel):
    ref: str
    finding: str


class DecisionDraft(BaseModel):
    next_action: Literal["request_clarification", "escalate_for_review", "use_existing_tool", "route_for_approval"]
    recommendation: str
    evidence: list[CitedFinding] = Field(default_factory=list)
    required_approvals: list[str] = Field(default_factory=list)
    risk_flags: list[str] = Field(default_factory=list)
    missing_information: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    suspicious_content: list[str] = Field(default_factory=list)
    next_step: str


class ExistingToolNote(BaseModel):
    ref: str
    covers_need: Literal["yes", "partly", "no", "unclear"]
    note: str


class EvidencePack(BaseModel):
    need_summary: str
    existing_tools: list[ExistingToolNote] = Field(default_factory=list)
    budget: str = ""
    vendor_status: str = ""
    data_and_integrations: str = ""
    gaps_or_conflicts: list[str] = Field(default_factory=list)
    suspicious_content: list[str] = Field(default_factory=list)
    evidence: list[CitedFinding] = Field(default_factory=list)


def _request_json(request: dict) -> str:
    return json.dumps(request, indent=2, default=str)


def run_single(request: dict, toolbox: ToolBox, llm: LLMClient) -> DecisionDraft | None:
    messages = [
        {"role": "system", "content": SINGLE_SYSTEM},
        {"role": "user", "content": request_message(_request_json(request), toolbox.reference_date.isoformat())},
    ]
    return run_tool_loop(llm, toolbox, messages, SINGLE_TOOLS, SUBMIT_DECISION, DecisionDraft.model_validate,
                         max_turns=7)


def run_staged(request: dict, toolbox: ToolBox, llm: LLMClient) -> tuple[DecisionDraft | None, EvidencePack | None]:
    # Stage 1: analyst gathers evidence with the data tools only.
    analyst_messages = [
        {"role": "system", "content": ANALYST_SYSTEM},
        {"role": "user", "content": request_message(_request_json(request), toolbox.reference_date.isoformat())},
    ]
    pack = run_tool_loop(llm, toolbox, analyst_messages, DATA_TOOLS, SUBMIT_EVIDENCE_PACK,
                         EvidencePack.model_validate, max_turns=6)

    # Between the stages: deterministic policy checks, run by code rather than chosen by a model.
    policy_check = toolbox.call("check_policy_rules")
    toolbox.register_refs([f"policy:§{n}" for n in policy_sections()], "policy_document")

    digest = [t for t in toolbox.results_digest() if t["tool"] != "check_policy_rules"]
    pack_text = pack.model_dump_json(indent=2) if pack else "The analyst did not return an evidence pack."
    reviewer_input = (
        request_message(_request_json(request), toolbox.reference_date.isoformat())
        + f"\n\n<analyst_evidence_pack>\n{pack_text}\n</analyst_evidence_pack>"
        + f"\n\n<tool_results>\n{json.dumps(digest, default=str)}\n</tool_results>"
        + f"\n\n<policy_check>\n{json.dumps(policy_check, default=str)}\n</policy_check>"
        + f"\n\n<policy>\n{load_policy_text()}\n</policy>"
    )
    # Stage 2: reviewer has no data tools; it must decide from the pack + tool results.
    reviewer_messages = [{"role": "system", "content": REVIEWER_SYSTEM},
                         {"role": "user", "content": reviewer_input}]
    draft = run_tool_loop(llm, toolbox, reviewer_messages, [], SUBMIT_DECISION, DecisionDraft.model_validate,
                          max_turns=2, force_final_from=0)
    return draft, pack
