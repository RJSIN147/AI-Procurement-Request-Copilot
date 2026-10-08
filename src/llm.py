"""Thin wrapper over an OpenAI-compatible chat API (Groq by default)."""
from __future__ import annotations

import json
import os
from typing import Callable

import openai
from openai import OpenAI
from pydantic import ValidationError

from src.telemetry import RunTelemetryCounter

DEFAULT_BASE_URL = "https://api.groq.com/openai/v1"
DEFAULT_MODEL = "openai/gpt-oss-120b"


class LLMUnavailable(RuntimeError):
    pass


def llm_configured() -> bool:
    return bool(os.getenv("GROQ_API_KEY") or os.getenv("LLM_API_KEY"))


class LLMClient:
    def __init__(self, telemetry: RunTelemetryCounter, model: str | None = None):
        api_key = os.getenv("GROQ_API_KEY") or os.getenv("LLM_API_KEY")
        if not api_key:
            raise LLMUnavailable("No LLM key configured (set GROQ_API_KEY in .env)")
        self.telemetry = telemetry
        self.model = model or os.getenv("MODEL_NAME") or DEFAULT_MODEL
        self.client = OpenAI(api_key=api_key, base_url=os.getenv("LLM_BASE_URL", DEFAULT_BASE_URL),
                             max_retries=6, timeout=60)

    def chat(self, messages: list[dict], tools: list[dict] | None = None, tool_choice: str | dict = "auto"):
        kwargs: dict = {"model": self.model, "messages": messages, "temperature": 0, "max_completion_tokens": 2500}
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = tool_choice
        # Groq returns 400 tool_use_failed when the model emits a malformed tool call;
        # that is usually a one-off, so retry it a couple of times.
        for attempt in range(3):
            try:
                response = self.client.chat.completions.create(**kwargs)
            except openai.BadRequestError as exc:
                self.telemetry.record_llm_call()
                if "tool_use_failed" in str(exc) and attempt < 2:
                    continue
                raise
            usage = response.usage
            self.telemetry.record_llm_call(getattr(usage, "prompt_tokens", 0) or 0,
                                           getattr(usage, "completion_tokens", 0) or 0)
            return response.choices[0].message
        raise RuntimeError("unreachable")


def _assistant_message(msg) -> dict:
    out: dict = {"role": "assistant", "content": msg.content or ""}
    if msg.tool_calls:
        out["tool_calls"] = [{"id": c.id, "type": "function",
                              "function": {"name": c.function.name, "arguments": c.function.arguments or "{}"}}
                             for c in msg.tool_calls]
    return out


def run_tool_loop(llm: LLMClient, toolbox, messages: list[dict], tool_names: list[str], final_tool: dict,
                  validate: Callable[[dict], object], max_turns: int = 6, force_final_from: int | None = None):
    """Let the model call tools until it calls `final_tool` with a valid payload.

    Returns the validated payload, or None if the model never produced one.
    """
    final_name = final_tool["function"]["name"]
    tools = toolbox.specs(tool_names) + [final_tool]
    force_from = max_turns - 1 if force_final_from is None else force_final_from

    for turn in range(max_turns):
        choice = {"type": "function", "function": {"name": final_name}} if turn >= force_from else "auto"
        msg = llm.chat(messages, tools=tools, tool_choice=choice)
        messages.append(_assistant_message(msg))
        calls = msg.tool_calls or []
        if not calls:
            messages.append({"role": "user", "content": f"Call {final_name} with your answer now."})
            continue

        data_calls = [c for c in calls if c.function.name != final_name]
        for call in data_calls:
            result = toolbox.call(call.function.name, call.function.arguments)
            messages.append({"role": "tool", "tool_call_id": call.id, "content": json.dumps(result, default=str)})

        for call in calls:
            if call.function.name != final_name:
                continue
            if data_calls:
                # it hasn't seen the results of the tools it just asked for
                feedback = "Not accepted yet: read the tool results above first, then call it again."
            else:
                try:
                    return validate(json.loads(call.function.arguments or "{}"))
                except (json.JSONDecodeError, ValidationError, ValueError) as exc:
                    feedback = f"Invalid {final_name} payload: {str(exc)[:600]}. Fix it and call {final_name} again."
            messages.append({"role": "tool", "tool_call_id": call.id, "content": feedback})
    return None
