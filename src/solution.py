from __future__ import annotations

from src.contracts import Architecture, ProcurementDecision
from src.data_access import get_request
from src.pipeline import analyze_request


def handle_request(request_id: str, architecture: Architecture = "single") -> ProcurementDecision:
    """Assessment adapter used by the eval harness.

    single -> one tool-using agent (src/agents.py: run_single), then the deterministic
    rules + guardrail (src/rules.py, src/guardrails.py).
    """
    if architecture == "staged":
        raise NotImplementedError("staged architecture not built yet")
    if architecture != "single":
        raise ValueError(f"architecture must be 'single' or 'staged', got {architecture!r}")
    return analyze_request(get_request(request_id), architecture).decision
