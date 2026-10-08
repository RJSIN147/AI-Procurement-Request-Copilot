from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

import openai

from src.agents import run_single
from src.contracts import ProcurementDecision, RunTelemetry
from src.guardrails import finalize
from src.llm import LLMClient, LLMUnavailable
from src.telemetry import RunTelemetryCounter
from src.tools import ToolBox

ARCHITECTURES = ("single", "rules")


@dataclass
class RunReport:
    decision: ProcurementDecision
    rules: dict
    draft: dict | None
    evidence_pack: dict | None
    trace: list[dict]
    stats: dict
    error: str | None


def analyze_request(request: dict, architecture: str = "single", *,
                    vendor_risk_fn: Callable[[str], dict] | None = None) -> RunReport:
    """Run one request through an architecture.

    "rules" skips the LLM entirely; it's only used as a reference point in the evals.
    """
    if architecture not in ARCHITECTURES:
        raise ValueError(f"architecture must be one of {ARCHITECTURES}, got {architecture!r}")

    started = time.perf_counter()
    telemetry = RunTelemetryCounter()
    toolbox = ToolBox(request, telemetry, vendor_risk_fn)
    draft = pack = None
    error = model = None

    if architecture != "rules":
        try:
            llm = LLMClient(telemetry)
            model = llm.model
            draft = run_single(request, toolbox, llm)
            if draft is None:
                error = "the model did not return a valid decision"
        except LLMUnavailable as exc:
            error = str(exc)
        except openai.OpenAIError as exc:
            error = f"LLM call failed: {type(exc).__name__}"

    rule_result = toolbox.policy_result()
    decision, stats = finalize(request, draft, rule_result, toolbox, llm_error=error,
                               rules_only=architecture == "rules")
    decision.telemetry = RunTelemetry(
        llm_calls=telemetry.llm_calls, tool_calls=telemetry.tool_calls, tool_names=telemetry.tool_names,
        architecture=architecture, model=model, latency_ms=round((time.perf_counter() - started) * 1000, 1),
        prompt_tokens=telemetry.prompt_tokens, completion_tokens=telemetry.completion_tokens)
    return RunReport(decision=decision, rules=rule_result.to_dict(),
                     draft=draft.model_dump() if draft else None,
                     evidence_pack=pack.model_dump() if pack else None,
                     trace=toolbox.trace, stats=stats, error=error)
