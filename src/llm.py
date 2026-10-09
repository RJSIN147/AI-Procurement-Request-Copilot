"""Thin wrapper over an OpenAI-compatible chat API (Groq by default)."""
from __future__ import annotations

import json
import os
import time
from types import SimpleNamespace
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


def _recover_tool_call(exc: openai.BadRequestError, n: int):
    body = exc.body if isinstance(exc.body, dict) else {}
    generated = body.get("failed_generation") or (body.get("error") or {}).get("failed_generation")
    if not generated:
        return None
    try:
        parsed = json.loads(generated)
        name, arguments = parsed["name"], parsed.get("arguments", {})
    except (json.JSONDecodeError, KeyError, TypeError):
        return None
    if not isinstance(arguments, str):
        arguments = json.dumps(arguments)
    call = SimpleNamespace(id=f"recovered_{n}", type="function",
                           function=SimpleNamespace(name=name, arguments=arguments))
    return SimpleNamespace(content="", tool_calls=[call])


def _retry_after(exc: openai.APIStatusError, attempt: int) -> float:
    try:
        return min(float(exc.response.headers.get("retry-after", "")), 60.0) + 0.5
    except (TypeError, ValueError):
        return min(2.0 * 2 ** attempt, 30.0)


class LLMClient:
    def __init__(self, telemetry: RunTelemetryCounter, model: str | None = None):
        api_key = os.getenv("GROQ_API_KEY") or os.getenv("LLM_API_KEY")
        if not api_key:
            raise LLMUnavailable("No LLM key configured (set GROQ_API_KEY in .env)")
        self.telemetry = telemetry
        self.model = model or os.getenv("MODEL_NAME") or DEFAULT_MODEL
        # Retries are handled below so rate-limit waits can be measured separately from latency.
        self.client = OpenAI(api_key=api_key, base_url=os.getenv("LLM_BASE_URL", DEFAULT_BASE_URL),
                             max_retries=0, timeout=60)

    def chat(self, messages: list[dict], tools: list[dict] | None = None, tool_choice: str | dict = "auto"):
        kwargs: dict = {"model": self.model, "messages": messages, "temperature": 0, "max_completion_tokens": 2500}
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = tool_choice
        malformed = server_errors = 0
        for attempt in range(12):
            try:
                response = self.client.chat.completions.create(**kwargs)
            except openai.RateLimitError as exc:
                # Groq free tier is 8k tokens/min, so this happens mid-run. Not counted as latency.
                wait = _retry_after(exc, attempt)
                self.telemetry.rate_limit_wait_ms += wait * 1000
                time.sleep(wait)
                continue
            except (openai.APIConnectionError, openai.InternalServerError):
                server_errors += 1
                if server_errors > 2:
                    raise
                time.sleep(2 * server_errors)
                continue
            except openai.BadRequestError as exc:
                self.telemetry.record_llm_call()
                # Groq validates tool arguments against the schema and rejects near-misses (e.g. a
                # string where the schema says array). It returns what the model generated, so recover
                # that call and let our own validation coerce it or send the error back to the model.
                recovered = _recover_tool_call(exc, malformed)
                if recovered is not None:
                    return recovered
                malformed += 1
                if "tool_use_failed" in str(exc) and malformed < 3:
                    continue
                raise
            usage = response.usage
            self.telemetry.record_llm_call(getattr(usage, "prompt_tokens", 0) or 0,
                                           getattr(usage, "completion_tokens", 0) or 0)
            return response.choices[0].message
        raise LLMUnavailable("Gave up after repeated rate limiting")


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
        forced = turn >= force_from
        if forced and turn > 0:
            messages.append({"role": "user", "content": f"No more tool calls. Call {final_name} now with what you "
                                                        "have, and say what you could not check."})
        choice = {"type": "function", "function": {"name": final_name}} if forced else "auto"
        try:
            msg = llm.chat(messages, tools=tools, tool_choice=choice)
        except openai.BadRequestError:
            if forced:
                # the model kept trying another tool on the forced turn; give up cleanly
                return None
            raise
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
