from __future__ import annotations

from typing import Literal
from pydantic import BaseModel, Field


class EvidenceItem(BaseModel):
    source: str = Field(description="Tool/data source name")
    finding: str = Field(description="Concise factual finding")
    reference: str | None = Field(default=None, description="Optional record ID / policy section / endpoint")


class RunTelemetry(BaseModel):
    llm_calls: int | None = None
    tool_calls: int | None = None
    tool_names: list[str] = Field(default_factory=list)
    # additions (optional, so the original contract still validates)
    architecture: str | None = None
    model: str | None = None
    latency_ms: float | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


class ProcurementDecision(BaseModel):
    request_id: str
    recommendation: str = Field(description="Short recommendation label or sentence")
    evidence: list[EvidenceItem] = Field(default_factory=list)
    required_approvals: list[str] = Field(default_factory=list)
    missing_information: list[str] = Field(default_factory=list)
    risk_flags: list[str] = Field(default_factory=list)
    next_step: str
    human_review_required: bool = True
    telemetry: RunTelemetry | None = None
    # additions
    next_action: str | None = Field(default=None, description="request_clarification | escalate_for_review | "
                                                              "use_existing_tool | route_for_approval")
    open_questions: list[str] = Field(default_factory=list)
    guardrail_notes: list[str] = Field(default_factory=list, description="What code changed in the model's draft")


Architecture = Literal["single", "staged"]

# Suggested approval names for consistency in evaluation:
# Manager, Department Head, Procurement, Finance, CFO, Security, Privacy, Legal
#
# Suggested risk-flag taxonomy (you may add others):
# existing_tool_overlap
# budget_insufficient
# security_review_required
# privacy_review_required
# legal_review_required
# vendor_review_expired
# conflicting_vendor_evidence
# vendor_risk_unavailable
# prompt_injection_detected
# missing_information
#
# Extra flags used by this implementation:
# new_vendor, budget_unverified, vendor_assessment_missing, high_vendor_risk,
# cross_region_data, ai_analysis_unavailable
