# Future Plan: AI-Powered Task Summaries via OpenRouter

## Goal

Instead of just listing tasks with time data, the model (OpenRouter) should read the **actual task content** — descriptions, comments, attachments, custom fields, tags — and produce **real human-readable summaries** for each project and space.

---

## Why This Is Different From Current Reports

| Current                                                | Future                                                                      |
| ------------------------------------------------------ | --------------------------------------------------------------------------- |
| AI lists tasks + time stats it fetches via MCP tools   | AI reads task descriptions/comments and _understands what work was done_    |
| Output is a structured table (task name, time, status) | Output is a summary paragraph: "Team worked on X, resolved Y, blocked on Z" |
| No semantic understanding of task content              | Actual project narrative                                                    |

---

## What Data the Model Needs Per Task

For each task that was active in the period, the model should receive:

```
Task: {name}
Status: {status}
Assignees: {list}
Due Date: {due_date}
Time Tracked: {time_spent}
Estimate: {time_estimate}
Priority: {priority}
Tags: {tags[]}

Description:
{task.description}  ← plain text, could be long

Comments (most recent first):
  - {comment.user} @ {comment.date}: {comment.text}
  - ...

Attachments:
  - {filename} (type: {mimetype}, url: {attachment_url}) ← model may not fetch, just aware it exists

Custom Fields:
  - {field_name}: {field_value}
```

---

## Data Fetching Requirements

The existing `report_generator.py` fetches tasks but does NOT fetch:

- `task.description` — available via `GET /task/{task_id}` (not in list endpoint)
- `task.comments` — `GET /task/{task_id}/comment`
- `task.attachments` — available in full task detail response
- `task.custom_fields` — available in full task detail response

### New function needed: `_fetch_task_detail(task_id, token)`

```python
def _fetch_task_detail(task_id: str, token: str) -> dict:
    """Fetch full task detail including description, custom_fields, attachments."""
    return _api_get(f"/task/{task_id}", token)

def _fetch_task_comments(task_id: str, token: str) -> list:
    """Fetch comments for a task."""
    data = _api_get(f"/task/{task_id}/comment", token)
    return data.get("comments", []) if data else []
```

### Rate limit concern

Fetching detail for every task is expensive. Strategy:

- Only fetch detail for **active tasks** (tasks with time_spent in the period)
- Limit to top-N tasks by time_spent per project (e.g., top 10)
- Use `ThreadPoolExecutor` for parallel fetching (already done for list fetching)

---

## Architecture: New Pipeline Step

### Option A — New Motia step `summarize_reports_step.py`

```
cron/manual trigger
    → report::generate          (existing: fetches AI markdown + metrics)
    → report::summarize         (NEW: fetches task details, calls OpenRouter "summarize" prompt)
    → report::send-email        (existing: renders HTML + sends)
```

The new enqueue key: `report::summarize`

### Option B — Extend `generate_reports_step.py`

Add a summarization sub-call after fetching the task data in `report_generator.py`, then combine the summary + structured table into one email.

**Recommendation**: Option A (separate step) — keeps concerns separated, can be disabled independently.

---

## System Prompt for Summarization Model

The model needs a different system prompt from the current one (which focuses on structured tables).

```
You are a project manager assistant. I will give you a list of tasks with their descriptions,
comments, and time data for a specific project during a specific period.

Your job is to produce a concise executive summary covering:
1. What was accomplished (based on task descriptions + completed tasks)
2. What is in progress (status != done)
3. Any blockers or issues mentioned in comments
4. Key highlights (high-priority tasks, long-running work)

Keep the summary to 2-4 paragraphs per project. Be specific — mention actual task names and outcomes.
Do NOT just list tasks. Write in plain English as if reporting to a manager.
```

---

## Output Format Per Space

```markdown
## AIX — Summary for [Period]

### AI Photo Manager

Work this period focused on resolving the model inference latency issue (4h tracked).
The team resolved the GPU memory leak bug and deployed hotfix v1.2.1.
One task (API batch processing) is still in progress with 2h logged.

### Taxation-CPA

Main deliverable: completed the bulk import feature (6.5h tracked across 3 tasks).
Comment threads indicate a design review was conducted on March 7. No blockers.

---

_Total: 4 projects | 12 active tasks | 18h 30m tracked_
```

---

## Attachment Handling

- Model is **told** attachments exist (filenames, types) but does not fetch/read them
- For image attachments: optionally pass signed URL — depends on whether model supports vision
- If OpenRouter model supports vision (e.g., `claude-3.5-sonnet`), can embed image URLs in the prompt
- For now: just list attachment filenames in the task context

---

## Rollout Plan

1. **Phase 1**: Add `_fetch_task_detail()` + `_fetch_task_comments()` to `report_generator.py`
2. **Phase 2**: Add `generate_space_summary()` function that returns per-project task context strings
3. **Phase 3**: Create `summarize_reports_step.py` Motia step
4. **Phase 4**: Add summarization model config to `.env` (can be different model than report model)
5. **Phase 5**: Update `email_sender.py` to include summary section per space above the detailed tables

---

## Open Questions

- Which OpenRouter model to use for summaries? (claude-3.5-sonnet for quality, or cheaper for cost)
- Max context length concern: if a project has 100 tasks with long descriptions, prompt could be huge
  → Mitigation: truncate descriptions to 500 chars, limit to top 15 tasks by time_spent
- Should summaries replace or supplement the current AI-generated tables?
  → Recommendation: summaries at top, current table detail below (toggle in email)
- Store summaries in Supabase for historical trend analysis? (out of scope for phase 1)

---

## Config to Add (`.env` / `docker-compose.yml`)

```env
SUMMARIZE_MODEL=anthropic/claude-3.5-sonnet          # model for summaries
SUMMARIZE_MAX_TASKS_PER_PROJECT=15                    # limit task detail fetching
SUMMARIZE_DESCRIPTION_MAX_CHARS=500                   # truncate long descriptions
SUMMARIZE_ENABLED=true                                # feature flag
```
