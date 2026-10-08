"""Architecture A: single tool-using agent."""
from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, Field

from src.llm import LLMClient, run_tool_loop
from src.prompts import SINGLE_SYSTEM, SUBMIT_DECISION, request_message
from src.tools import ToolBox

SINGLE_TOOLS = ["get_requester_profile", "check_department_budget", "search_software_catalog",
                "get_vendor_registry_record", "get_vendor_risk_assessment", "check_policy_rules", "read_policy"]


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


def _request_json(request: dict) -> str:
    return json.dumps(request, indent=2, default=str)


def run_single(request: dict, toolbox: ToolBox, llm: LLMClient) -> DecisionDraft | None:
    messages = [
        {"role": "system", "content": SINGLE_SYSTEM},
        {"role": "user", "content": request_message(_request_json(request), toolbox.reference_date.isoformat())},
    ]
    return run_tool_loop(llm, toolbox, messages, SINGLE_TOOLS, SUBMIT_DECISION, DecisionDraft.model_validate,
                         max_turns=7)
