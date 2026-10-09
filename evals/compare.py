"""Run the same case set through each architecture and write a comparison.

    python evals/compare.py                       # public + extra cases, single/staged/rules
    python evals/compare.py --cases public --repeats 3
    python evals/compare.py --architectures single staged
    python evals/compare.py --resume              # only run cases without a saved result

Outputs (evals/results/):
    runs.csv            one row per (architecture, case, repeat)
    evaluation_results.csv  the same runs in the columns of templates/evaluation_results_template.csv
    summary.md          the comparison table used in the README / decision memo
    decisions/<arch>/   full decision + model draft + guardrail stats per case, for manual review
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EVALS = ROOT / "evals"
for p in (str(ROOT), str(EVALS)):
    if p not in sys.path:
        sys.path.insert(0, p)

import requests  # noqa: E402

from mock_service import mock_api_running  # noqa: E402
from run_public_evals import evaluate as minimum_checks  # noqa: E402
from src.data_access import get_request  # noqa: E402
from src.llm import DEFAULT_MODEL, llm_configured  # noqa: E402
from src.pipeline import analyze_request  # noqa: E402
from src.vendor_client import get_vendor_risk  # noqa: E402

OUT = EVALS / "results"


def load_cases(which: str) -> list[dict]:
    extra = json.loads((EVALS / "extra_cases.json").read_text(encoding="utf-8"))
    cases = []
    if which in ("public", "all"):
        for case in json.loads((EVALS / "public_cases.json").read_text(encoding="utf-8")):
            cases.append({**case, "request": get_request(case["request_id"]),
                          "expected_next_action": extra["public_case_actions"][case["case_id"]]})
    if which in ("extra", "all"):
        for case in extra["cases"]:
            request = case.get("request") or get_request(case["request_id"])
            cases.append({**case, "request_id": request["request_id"], "request": request})
    return cases


def risk_fn_for(case: dict):
    overrides = case.get("vendor_risk_override")
    if not overrides:
        return None

    def fetch(vendor_name: str) -> dict:
        record = overrides.get(vendor_name)
        if record is None:
            return get_vendor_risk(vendor_name)
        if record.get("force_error"):
            response = requests.Response()
            response.status_code = 503
            response._content = json.dumps({"detail": record.get("error_message")}).encode()
            raise requests.HTTPError(response=response)
        return {"vendor_name": vendor_name, **record}
    return fetch


def extra_checks(decision, expectations: dict) -> list[str]:
    failures = []
    approvals = [a.lower() for a in decision.required_approvals]
    for token in expectations.get("required_approvals_must_not_contain", []):
        if any(token in a for a in approvals):
            failures.append(f"unexpected approval: {token}")
    text = f"{decision.recommendation} {decision.next_step}".lower()
    for phrase in expectations.get("recommendation_must_not_contain", []):
        if phrase in text:
            failures.append(f"recommendation contains: {phrase!r}")
    return failures


def record_path(case_id: str, architecture: str, rep: int) -> Path:
    return OUT / "decisions" / architecture / f"{case_id}{'' if rep == 0 else f'_r{rep}'}.json"


def run_case(case: dict, architecture: str, rep: int) -> dict | None:
    """Run one case and save the full record. Returns None if the provider's daily quota ran out."""
    report = analyze_request(case["request"], architecture, vendor_risk_fn=risk_fn_for(case))
    if report.quota_retry_after is not None:
        return None
    d = report.decision
    record = {
        "case_id": case["case_id"], "title": case["title"], "expected_next_action": case["expected_next_action"],
        "minimum_check_failures": minimum_checks(d, case["expectations"]) + extra_checks(d, case["expectations"]),
        "decision": d.model_dump(), "model_draft": report.draft,
        "analyst_evidence_pack": report.evidence_pack, "guardrail_stats": report.stats, "error": report.error,
        "tool_trace": [{"tool": c["tool"], "args": c["args"]} for c in report.trace],
    }
    path = record_path(case["case_id"], architecture, rep)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2, default=str), encoding="utf-8")
    return row_from_record(record, architecture, rep)


def row_from_record(record: dict, architecture: str, rep: int) -> dict:
    d, s, t = record["decision"], record["guardrail_stats"], record["decision"]["telemetry"]
    expected, failures = record["expected_next_action"], record["minimum_check_failures"]
    draft_action_ok = s["draft_action"] in expected if s["draft_present"] else None
    return {
        "case_id": record["case_id"], "request_id": d["request_id"], "architecture": architecture, "rep": rep,
        "passed_minimum_checks": not failures, "expected_action": "|".join(expected),
        "next_action": d["next_action"], "action_correct": d["next_action"] in expected,
        "draft_action": s["draft_action"] or "", "draft_action_correct": "" if draft_action_ok is None else draft_action_ok,
        "action_overridden": s["action_overridden"],
        "approvals_added_by_guardrail": ";".join(s["approvals_added"]),
        "flags_added_by_guardrail": ";".join(s["flags_added"]),
        "draft_policy_complete": s["draft_present"] and not s["approvals_added"] and not s["flags_added"],
        "evidence_cited": s["evidence_total"], "evidence_ungrounded": s["evidence_ungrounded"],
        "latency_ms": t["latency_ms"], "rate_limit_wait_ms": t["rate_limit_wait_ms"],
        "llm_calls": t["llm_calls"], "tool_calls": t["tool_calls"],
        "prompt_tokens": t["prompt_tokens"], "completion_tokens": t["completion_tokens"],
        "error": record["error"] or "", "failures": " | ".join(failures),
    }


TEMPLATE_COLUMNS = ["case_id", "architecture", "correct_next_action", "grounded_evidence", "policy_followed",
                    "human_escalation_correct", "latency_ms", "llm_calls", "tool_calls", "notes"]


def template_row(row: dict) -> dict:
    """One row in the format of templates/evaluation_results_template.csv."""
    escalate_expected = "escalate_for_review" in row["expected_action"].split("|")
    notes = [row["failures"]] if row["failures"] else []
    if not row["action_correct"]:
        notes.append(f"next action {row['next_action']}, expected {row['expected_action'].replace('|', ' or ')}")
    if row["action_overridden"]:
        notes.append(f"guardrail changed draft action {row['draft_action']}")
    if row["approvals_added_by_guardrail"] or row["flags_added_by_guardrail"]:
        notes.append("guardrail added " + ", ".join(filter(None, [row["approvals_added_by_guardrail"],
                                                                   row["flags_added_by_guardrail"]])).replace(";", ", "))
    if row["evidence_ungrounded"]:
        notes.append(f"{row['evidence_ungrounded']}/{row['evidence_cited']} AI evidence items dropped as ungrounded")
    return {
        "case_id": row["case_id"], "architecture": row["architecture"],
        "correct_next_action": row["action_correct"],
        # final output only keeps evidence whose refs came back from a tool; this says whether the model's
        # own draft needed anything dropped
        "grounded_evidence": not row["evidence_ungrounded"],
        "policy_followed": row["passed_minimum_checks"],
        "human_escalation_correct": (row["next_action"] == "escalate_for_review") == escalate_expected,
        "latency_ms": row["latency_ms"], "llm_calls": row["llm_calls"], "tool_calls": row["tool_calls"],
        "notes": "; ".join(notes),
    }


def pct(n: int, d: int) -> str:
    return f"{n}/{d} ({100 * n / d:.0f}%)" if d else "n/a"


def summarise(rows: list[dict], architectures: list[str], model: str) -> str:
    lines = [f"# Evaluation results\n",
             f"Model: `{model}` via Groq · cases: {len({r['case_id'] for r in rows})} · "
             f"runs per case: {max(r['rep'] for r in rows) + 1} · generated by `python evals/compare.py`\n",
             "| Metric | " + " | ".join(architectures) + " |",
             "|---|" + "---:|" * len(architectures)]

    def col(fn) -> list[str]:
        return [fn([r for r in rows if r["architecture"] == a]) for a in architectures]

    def avg(key):
        return lambda rs: f"{statistics.mean(r[key] for r in rs):,.1f}" if rs else "n/a"

    def median(key):
        return lambda rs: f"{statistics.median(r[key] for r in rs):,.0f}" if rs else "n/a"

    def with_draft(rs):
        return [r for r in rs if r["draft_action"]]

    metrics = [
        ("Minimum checks passed (final output)", lambda rs: pct(sum(r["passed_minimum_checks"] for r in rs), len(rs))),
        ("Correct next action (final output)", lambda rs: pct(sum(r["action_correct"] for r in rs), len(rs))),
        ("Correct next action (model draft, before guardrail)",
         lambda rs: pct(sum(r["draft_action_correct"] is True for r in with_draft(rs)), len(with_draft(rs)))),
        ("Draft already had every required approval + flag",
         lambda rs: pct(sum(r["draft_policy_complete"] for r in with_draft(rs)), len(with_draft(rs)))),
        ("Runs where guardrail changed the next action",
         lambda rs: pct(sum(r["action_overridden"] for r in with_draft(rs)), len(with_draft(rs)))),
        ("AI evidence items grounded in tool output",
         lambda rs: pct(sum(r["evidence_cited"] - r["evidence_ungrounded"] for r in rs),
                        sum(r["evidence_cited"] for r in rs))),
        ("Runs with LLM errors / fallback", lambda rs: pct(sum(bool(r["error"]) for r in rs), len(rs))),
        ("Avg latency (ms)", avg("latency_ms")),
        ("Median latency (ms)", median("latency_ms")),
        ("Avg Groq rate-limit wait excluded from latency (ms)", avg("rate_limit_wait_ms")),
        ("Avg LLM calls", avg("llm_calls")),
        ("Avg tool calls", avg("tool_calls")),
        ("Avg tokens (prompt + completion)",
         lambda rs: f"{statistics.mean(r['prompt_tokens'] + r['completion_tokens'] for r in rs):,.0f}" if rs else "n/a"),
    ]
    for name, fn in metrics:
        lines.append(f"| {name} | " + " | ".join(col(fn)) + " |")

    lines += ["", "## Per case", "", "| Case | Expected action | " + " | ".join(architectures) + " |",
              "|---|---|" + "---|" * len(architectures)]
    for case_id in dict.fromkeys(r["case_id"] for r in rows):
        cells = []
        for a in architectures:
            rs = [r for r in rows if r["case_id"] == case_id and r["architecture"] == a]
            ok = sum(r["passed_minimum_checks"] and r["action_correct"] for r in rs)
            actions = ", ".join(sorted({r["next_action"] for r in rs}))
            cells.append(f"{'PASS' if ok == len(rs) else f'{ok}/{len(rs)}'} · {actions}")
        expected = next(r["expected_action"] for r in rows if r["case_id"] == case_id)
        lines.append(f"| {case_id} | {expected.replace('|', ' / ')} | " + " | ".join(cells) + " |")

    failing = [r for r in rows if not (r["passed_minimum_checks"] and r["action_correct"])]
    if failing:
        lines += ["", "## Failures", ""]
        for r in failing:
            why = r["failures"] or f"next action {r['next_action']} (expected {r['expected_action'].replace('|', ' / ')})"
            lines.append(f"- {r['architecture']} {r['case_id']} (run {r['rep']}): {why}")
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--architectures", nargs="+", default=["single", "staged", "rules"],
                        choices=["single", "staged", "rules"])
    parser.add_argument("--cases", choices=["public", "extra", "all"], default="all")
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--pause", type=float, default=0.0, help="seconds between runs (rate limits)")
    parser.add_argument("--resume", action="store_true",
                        help="keep finished cases in evals/results/decisions and only run the missing ones")
    parser.add_argument("--wait-for-quota", action="store_true",
                        help="if Groq's daily limit runs out, wait for it instead of stopping")
    args = parser.parse_args()

    if not llm_configured() and any(a != "rules" for a in args.architectures):
        print("No GROQ_API_KEY set - LLM architectures will fall back to rules-only output.")

    model = os.getenv("MODEL_NAME") or DEFAULT_MODEL
    cases = load_cases(args.cases)
    rows = []
    with mock_api_running():
        for architecture in args.architectures:
            print(f"\n== {architecture}")
            for case in cases:
                for rep in range(args.repeats):
                    path = record_path(case["case_id"], architecture, rep)
                    if args.resume and path.exists():
                        rows.append(row_from_record(json.loads(path.read_text(encoding="utf-8")), architecture, rep))
                        print(f"done  {case['case_id']:<7} (kept from earlier run)")
                        continue
                    row = run_case(case, architecture, rep)
                    while row is None:
                        # Free tier is 200k tokens/day per model, less than one full comparison.
                        if not args.wait_for_quota:
                            print(f"\nGroq daily limit reached at {architecture} {case['case_id']}. Finished cases "
                                  "are saved; rerun later with --resume (or use --wait-for-quota).")
                            return
                        print(f"wait  {case['case_id']:<7} daily limit reached, waiting 10 min", flush=True)
                        time.sleep(600)
                        row = run_case(case, architecture, rep)
                    rows.append(row)
                    status = "PASS" if row["passed_minimum_checks"] and row["action_correct"] else "FAIL"
                    print(f"{status}  {case['case_id']:<7} {row['next_action']:<22} {row['latency_ms']:>8.0f} ms  "
                          f"llm={row['llm_calls']} tools={row['tool_calls']}"
                          + (f"  [{row['failures'] or 'wrong action'}]" if status == "FAIL" else "")
                          + (f"  error: {row['error']}" if row["error"] else ""))
                    if args.pause:
                        time.sleep(args.pause)

    OUT.mkdir(exist_ok=True)
    with (OUT / "runs.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
    with (OUT / "evaluation_results.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=TEMPLATE_COLUMNS)
        writer.writeheader()
        writer.writerows(template_row(r) for r in rows)
    summary = summarise(rows, args.architectures, model)
    (OUT / "summary.md").write_text(summary, encoding="utf-8")
    print("\n" + summary)
    print(f"Wrote {OUT.relative_to(ROOT)}/runs.csv, evaluation_results.csv, summary.md and decisions/")


if __name__ == "__main__":
    main()
