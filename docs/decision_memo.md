# Architecture Decision Memo

## Decision

**A, the single agent**: one model and seven tools, inside deterministic rules and a code guardrail.

## Evidence

Same 17 cases (6 public + 11 edge), `gpt-oss-120b` on Groq, temperature 0, 1 run each (`evals/results/`).

| Metric | Single agent | Staged / 2-agent |
|---|---:|---:|
| Cases passing (minimum checks + correct next action) | 15/17 | 15/17 |
| Avg latency (excl. rate-limit waits) | 9.2 s | 9.7 s |
| Avg LLM calls | 7.1 | 7.2 |
| Avg tool calls | 5.9 | 6.2 |
| Avg tokens | 11.9k | 10.2k |
| Notable policy/grounding failures | none; 1 ungrounded evidence item | 1 draft missed an approval; 1 next action corrected by the guardrail; 9 ungrounded items (4 from a ref-escaping bug, since fixed) |

Both missed the same two judgement cases. In EXT-03 the tool is already licensed company-wide, so the right answer is "use the existing tool"; in EXT-11 the justification is "need this asap, thx". In EXT-03 the staged analyst got it right, but the reviewer ignored that, added an unneeded Privacy review and escalated.

The rules-only baseline also scores 15/17, so policy compliance comes from code, not from how the model work is split.

## Trade-offs

Staged gave about 15% fewer tokens (the reviewer reads one compact input instead of running a tool loop) and an evidence pack that's easy to inspect.

In return: two prompts, two schemas and a handoff, so more places to drift and harder debugging. It lost a correct finding between the stages, needed more guardrail corrections, and was about 0.5 s slower. None of that bought accuracy, and a simpler system that performs as well is the stronger answer.

## Risks / limitations

Before production I'd validate:

- **Variance**: 1 run per case is thin. EXT-03 passed for A in an earlier run on the same code. I'd run 5+ repeats and report the spread.
- **Judgement cases**: I'd test a bigger set of overlap and vague-justification requests, since that's where both fail. A cheap fix is a deterministic hint: same product, approved company-wide, with seats free → suggest the existing tool.
- **Keyword-based data/integration triggers**: these need checking against real historical requests.
- **Provider limits**: the free tier (200k tokens/day) can't run a full eval in one day, so production needs a paid tier.
- **Missing data**: approver identities, seat utilisation.

## Why this is the right MVP

The job is to route requests correctly, show the evidence, and never approve anything on its own. Deterministic rules set the floor for approvals, flags and next action. The guardrail only lets the model add caution. Citations are checked against tool output, and a person makes the final call. One agent is enough to gather evidence and write a clear recommendation inside that frame. A second agent adds cost and failure points without better outcomes, so I'd revisit it only if repeated runs on a larger judgement-heavy set showed a real gain.
