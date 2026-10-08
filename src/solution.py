from __future__ import annotations

from src.contracts import Architecture, ProcurementDecision
from src.data_access import get_request
from src.pipeline import analyze_request


def handle_request(request_id: str, architecture: Architecture = "single") -> ProcurementDecision:
    """Assessment adapter used by the eval harness.

    single -> one tool-using agent (src/agents.py: run_single)
    staged -> analyst agent -> deterministic policy checks -> reviewer agent (run_staged)
    Both go through the same deterministic rules and guardrail (src/rules.py, src/guardrails.py).
    """
    if architecture not in ("single", "staged"):
        raise ValueError(f"architecture must be 'single' or 'staged', got {architecture!r}")
    return analyze_request(get_request(request_id), architecture).decision
