# Future Plan: High-Intelligence Manager Summaries (Second-Model Pipeline)

## Objective
Keep the current report generation exactly as-is, and add a separate high-quality summary stage that gives managers a fast scan of "who did what" for any selected period (`today`, `yesterday`, `this_week`, `custom`, etc.).

## What We Will Not Do
- Do not bloat the core report markdown with extra long summary blocks.
- Do not hardcode period logic to `today`.
- Do not replace existing detailed tables; keep them for audit/detail.

## Target Output (Manager View)
Per space, show a short digest:
- 1 line per person, max 10-14 words per line
- status signal: `completed`, `in progress`, `blocked`
- total tracked time for that person in the selected period

Example:
- **Arya Patel** - Report pipeline optimization and docker fixes (completed, 5h 46m)
- **Rishabh Khadela** - W2 validation and extraction debugging (in progress, 5h 33m)

## Architecture (Recommended)
1. `report::generate` (existing): build standard space report markdown/data.
2. `report::summarize` (new): call a stronger model with a strict prompt + schema.
3. `report::send-email` (existing): include digest at top, then full detailed report.

Flow:
`cron/manual trigger -> report::generate -> report::summarize -> report::send-email`

## Why Separate Step
- Cleaner failure handling (summary can fail without blocking report email).
- Independent model tuning and cost control.
- Easy A/B testing with feature flag.

## Data Contract for Summarizer
Input (per space):
- period metadata (`period_type`, `start_date`, `end_date`)
- per-member worked tasks (only tasks with tracked time in period)
- for each task: name, status, tracked time, description excerpt, time-note excerpt

Limits before model call:
- max members: 12
- max tasks per member: 8
- description max chars: 280
- note max chars: 180

## Model Strategy
Use a dedicated summary model (higher intelligence than report model):
- primary: `SUMMARY_MODEL_PRIMARY`
- fallback: `SUMMARY_MODEL_FALLBACK`
- temperature: `0.0-0.2`
- strict prompt requesting concise bullets only

Suggested env:
```env
SUMMARY_ENABLED=true
SUMMARY_MODEL_PRIMARY=anthropic/claude-sonnet-4
SUMMARY_MODEL_FALLBACK=openai/gpt-4.1-mini
SUMMARY_MAX_MEMBERS=12
SUMMARY_MAX_TASKS_PER_MEMBER=8
SUMMARY_TIMEOUT_S=45
```

## Prompt Strategy (Strict)
System prompt rules:
- output only markdown bullets
- one bullet per person
- avoid raw task lists and repeated project names
- no invented work
- if unclear: explicitly say "insufficient detail"

## Reliability and Cost Controls
- Feature flag (`SUMMARY_ENABLED`) to disable quickly.
- Timeout and fallback model.
- If summary fails: send email without summary (never block delivery).
- Optional cache key: `(space, period, report_hash)` to avoid repeated summary calls.

## Observability
Log these per space:
- summary_started_at, summary_finished_at
- summary_model_used
- summary_tokens_in/out
- summary_elapsed_s
- summary_status (`ok`, `timeout`, `fallback_ok`, `failed`)

## Rollout Plan
1. Phase 1: Add new `report::summarize` step and queue wiring.
2. Phase 2: Implement strict prompt + schema + fallback model.
3. Phase 3: Add email placement (`Manager Digest` block above full report).
4. Phase 4: Add logs/metrics and failure-safe fallback behavior.
5. Phase 5: Run for 3-5 days, review quality, then tune prompt/limits.

## Acceptance Criteria
- Manager digest always reflects requested period (not hardcoded to today).
- Digest remains concise (<=12 lines, <=1 line per person).
- Email still sends even if summary model fails.
- End-to-end added latency target: <=20s per space for summary stage.
