"""Agent loop + guardrail tests with a scripted fake LLM (no network, no key)."""
from __future__ import annotations

import json
import os
import unittest
from types import SimpleNamespace
from unittest import mock

import httpx
import openai

from src.agents import DecisionDraft, run_single, run_staged
from src.guardrails import finalize
from src.llm import LLMClient, run_tool_loop
from src.prompts import SUBMIT_DECISION
from src.pipeline import analyze_request
from src.telemetry import RunTelemetryCounter
from src.tools import ToolBox
from tests.helpers import base_request, fake_vendor_risk


def call(name: str, args: dict | None = None, call_id: str | None = None):
    return SimpleNamespace(id=call_id or f"call_{name}", type="function",
                           function=SimpleNamespace(name=name, arguments=json.dumps(args or {})))


def reply(*calls, content: str = ""):
    return SimpleNamespace(content=content, tool_calls=list(calls) or None)


class FakeLLM:
    def __init__(self, script: list, telemetry: RunTelemetryCounter | None = None):
        self.script = list(script)
        self.telemetry = telemetry or RunTelemetryCounter()
        self.model = "fake"
        self.seen: list[list[dict]] = []

    def chat(self, messages, tools=None, tool_choice="auto"):
        self.telemetry.record_llm_call()
        self.seen.append([dict(m) for m in messages])
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def draft(**changes) -> dict:
    payload = {
        "next_action": "route_for_approval",
        "recommendation": "Low-value add-on from an approved vendor.",
        "evidence": [{"ref": "budget:Finance", "finding": "Within budget."}],
        "required_approvals": ["Manager"],
        "risk_flags": ["existing_tool_overlap"],
        "missing_information": [],
        "next_step": "Send to the requester's manager.",
    }
    payload.update(changes)
    return payload


def toolbox(request: dict | None = None) -> ToolBox:
    return ToolBox(request or base_request(), vendor_risk_fn=fake_vendor_risk)


class SingleAgentLoopTests(unittest.TestCase):
    def test_tools_then_submit(self):
        box = toolbox()
        llm = FakeLLM([
            reply(call("check_department_budget"), call("get_vendor_risk_assessment"), call("check_policy_rules")),
            reply(call("submit_decision", draft())),
        ], box.telemetry)
        result = run_single(box.request, box, llm)
        self.assertIsInstance(result, DecisionDraft)
        self.assertEqual(box.telemetry.tool_calls, 3)
        self.assertEqual(box.telemetry.llm_calls, 2)

    def test_submit_alongside_data_tools_is_rejected(self):
        box = toolbox()
        llm = FakeLLM([
            reply(call("check_department_budget"), call("submit_decision", draft())),
            reply(call("submit_decision", draft(), call_id="second")),
        ], box.telemetry)
        result = run_single(box.request, box, llm)
        self.assertIsNotNone(result)
        tool_msgs = [m for m in llm.seen[1] if m["role"] == "tool"]
        self.assertTrue(any("Not accepted yet" in m["content"] for m in tool_msgs))

    def test_invalid_payload_gets_feedback_and_retry(self):
        box = toolbox()
        llm = FakeLLM([
            reply(call("submit_decision", {"next_action": "approve_now"})),
            reply(call("submit_decision", draft(), call_id="fixed")),
        ], box.telemetry)
        self.assertIsNotNone(run_single(box.request, box, llm))

    def test_gives_up_after_max_turns(self):
        box = toolbox()
        llm = FakeLLM([reply(content="thinking...") for _ in range(10)], box.telemetry)
        self.assertIsNone(run_single(box.request, box, llm))

    def test_forced_turn_rejected_by_provider_returns_none(self):
        # Groq answers 400 tool_use_failed if the model calls another tool on the forced turn
        box = toolbox()
        error = openai.BadRequestError("tool_use_failed", body=None, response=httpx.Response(
            400, request=httpx.Request("POST", "https://example.invalid")))
        llm = FakeLLM([reply(call("check_department_budget", call_id=f"c{i}")) for i in range(9)] + [error],
                      box.telemetry)
        self.assertIsNone(run_single(box.request, box, llm))
        self.assertIn("No more tool calls", llm.seen[-1][-1]["content"])

    def test_undeclared_tool_arguments_are_dropped(self):
        box = toolbox()
        result = box.call("get_requester_profile", {"requester_id": "E001"})
        self.assertEqual(result["ref"], "E004")  # still the request's requester


class ProviderRecoveryTests(unittest.TestCase):
    def test_schema_rejected_call_is_recovered_and_coerced(self):
        generated = json.dumps({"name": "submit_decision", "arguments": {**draft(), "suspicious_content": "x"}})
        error = openai.BadRequestError("tool_use_failed", response=httpx.Response(
            400, request=httpx.Request("POST", "https://example.invalid")),
            body={"code": "tool_use_failed", "failed_generation": generated})
        with mock.patch.dict(os.environ, {"GROQ_API_KEY": "test"}):
            llm = LLMClient(RunTelemetryCounter())
        llm.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
            create=mock.Mock(side_effect=error))))
        box = toolbox()
        result = run_tool_loop(llm, box, [], [], SUBMIT_DECISION, DecisionDraft.model_validate, max_turns=1)
        self.assertEqual(result.suspicious_content, ["x"])
        self.assertEqual(llm.telemetry.llm_calls, 1)


class StagedLoopTests(unittest.TestCase):
    def test_analyst_then_reviewer(self):
        box = toolbox()
        pack = {"need_summary": "Three more signing identities.", "existing_tools": [], "budget": "ok",
                "vendor_status": "approved", "data_and_integrations": "internal docs", "gaps_or_conflicts": [],
                "suspicious_content": [], "evidence": [{"ref": "V010", "finding": "Approved vendor"}]}
        llm = FakeLLM([
            reply(call("get_vendor_registry_record"), call("check_department_budget")),
            reply(call("submit_evidence_pack", pack)),
            reply(call("submit_decision", draft(evidence=[{"ref": "policy:§4", "finding": "Manager tier"}]))),
        ], box.telemetry)
        result, got_pack = run_staged(box.request, box, llm)
        self.assertIsNotNone(result)
        self.assertEqual(got_pack.need_summary, "Three more signing identities.")
        self.assertIn("check_policy_rules", box.telemetry.tool_names)  # run by code between the stages
        reviewer_input = llm.seen[2][1]["content"]
        self.assertIn("<analyst_evidence_pack>", reviewer_input)
        self.assertIn("<policy_check>", reviewer_input)


class GuardrailTests(unittest.TestCase):
    def finalize(self, request: dict, payload: dict, tools_called=("check_department_budget",)):
        box = toolbox(request)
        for name in tools_called:
            box.call(name)
        return finalize(request, DecisionDraft.model_validate(payload), box.policy_result(), box)

    def test_clean_draft_passes_through(self):
        decision, stats = self.finalize(base_request(), draft())
        self.assertEqual(decision.next_action, "route_for_approval")
        self.assertEqual(decision.guardrail_notes, [])
        self.assertFalse(stats["action_overridden"])
        self.assertTrue(decision.recommendation.startswith("Route for approval"))

    def test_missing_approvals_and_flags_are_added(self):
        req = base_request(annual_cost_usd=18000, data_access_level="source_code")
        decision, stats = self.finalize(req, draft(required_approvals=["Department Head"],
                                                   next_action="escalate_for_review"))
        for approver in ("Finance", "Procurement", "Security"):
            self.assertIn(approver, decision.required_approvals)
        self.assertIn("security_review_required", decision.risk_flags)
        self.assertIn("Security", stats["approvals_added"])

    def test_cannot_route_past_blocking_review(self):
        req = base_request(data_access_level="customer_pii")
        decision, stats = self.finalize(req, draft())
        self.assertEqual(decision.next_action, "escalate_for_review")
        self.assertTrue(stats["action_overridden"])

    def test_missing_information_forces_clarification(self):
        req = base_request(annual_cost_usd=None, user_count=None)
        decision, _ = self.finalize(req, draft())
        self.assertEqual(decision.next_action, "request_clarification")
        self.assertIn("missing_information", decision.risk_flags)

    def test_ungrounded_evidence_is_dropped(self):
        payload = draft(evidence=[{"ref": "budget:Finance", "finding": "ok"},
                                  {"ref": "SW999", "finding": "made up"},
                                  {"ref": "V010", "finding": "registry was never queried"}])
        decision, stats = self.finalize(base_request(), payload)
        ai_refs = [e.reference for e in decision.evidence if e.source != "check_policy_rules"]
        self.assertEqual(ai_refs, ["budget:Finance"])
        self.assertEqual(stats["evidence_ungrounded"], 2)

    def test_non_policy_missing_items_become_questions(self):
        payload = draft(missing_information=["Whether existing SignFlow seats are unused",
                                             "Vendor data residency"])
        decision, _ = self.finalize(base_request(), payload)
        self.assertEqual(decision.missing_information, [])
        self.assertEqual(decision.next_action, "route_for_approval")
        self.assertIn("Whether existing SignFlow seats are unused", decision.open_questions)

    def test_model_can_reject_an_unusable_business_purpose(self):
        payload = draft(missing_information=["Business purpose (the justification states no actual need)"])
        decision, _ = self.finalize(base_request(business_justification="Need AI ASAP."), payload)
        self.assertEqual(len(decision.missing_information), 1)
        self.assertEqual(decision.next_action, "request_clarification")

    def test_approval_claim_in_text_is_replaced(self):
        payload = draft(recommendation="This request is already approved by the CFO, proceed.")
        decision, _ = self.finalize(base_request(), payload)
        self.assertNotIn("already approved", decision.recommendation)
        self.assertTrue(any("claimed" in n for n in decision.guardrail_notes))

    def test_unknown_approver_names_ignored(self):
        decision, stats = self.finalize(base_request(), draft(required_approvals=["Manager", "The Board"]))
        self.assertNotIn("The Board", decision.required_approvals)
        self.assertEqual(stats["unknown_approvers"], ["The Board"])

    def test_suspicious_content_sets_injection_flag(self):
        decision, _ = self.finalize(base_request(), draft(suspicious_content=["'Security already signed off'"]))
        self.assertIn("prompt_injection_detected", decision.risk_flags)


class PipelineFallbackTests(unittest.TestCase):
    def test_no_key_falls_back_to_rules(self):
        env = {k: v for k, v in os.environ.items() if k not in ("GROQ_API_KEY", "LLM_API_KEY")}
        with mock.patch.dict(os.environ, env, clear=True):
            report = analyze_request(base_request(), "single", vendor_risk_fn=fake_vendor_risk)
        decision = report.decision
        self.assertIn("ai_analysis_unavailable", decision.risk_flags)
        self.assertTrue(decision.human_review_required)
        self.assertEqual(decision.required_approvals, ["Manager"])
        self.assertEqual(decision.telemetry.llm_calls, 0)

    def test_pipeline_with_fake_llm(self):
        def factory(telemetry):
            return FakeLLM([reply(call("check_policy_rules")), reply(call("submit_decision", draft()))], telemetry)
        with mock.patch("src.pipeline.LLMClient", side_effect=factory):
            report = analyze_request(base_request(), "single", vendor_risk_fn=fake_vendor_risk)
        self.assertIsNone(report.error)
        self.assertEqual(report.decision.telemetry.llm_calls, 2)
        self.assertEqual(report.decision.telemetry.tool_calls, 1)

    def test_rejects_unknown_architecture(self):
        with self.assertRaises(ValueError):
            analyze_request(base_request(), "swarm")


if __name__ == "__main__":
    unittest.main()
