"""
Manual Trigger Step - HTTP endpoint to test the report flow manually.

POST /trigger-report with optional body:
  {
    "period": "today" | "yesterday",
    "schedule_label": "Test Report",
    "spaces": ["AIX"]  # optional, defaults to all monitored spaces
  }
"""

from typing import Any
from motia import ApiRequest, ApiResponse, FlowContext, http


config = {
    "name": "ManualReportTrigger",
    "description": "HTTP endpoint to manually trigger report generation for testing",
    "flows": ["clickup-daily-reports"],
    "triggers": [
        http("POST", "/trigger-report"),
    ],
    "enqueues": ["report::generate"],
}


async def handler(request: ApiRequest[dict[str, Any]], ctx: FlowContext[Any]) -> ApiResponse[Any]:
    """Accept a manual trigger and enqueue report generation."""
    body = request.body or {}
    period = body.get("period", "today")
    schedule_label = body.get("schedule_label", "Manual Test")
    spaces = body.get("spaces")

    ctx.logger.info(
        f"Manual trigger - period={period}, label={schedule_label}, spaces={spaces or 'ALL'}"
    )

    await ctx.enqueue(
        {
            "topic": "report::generate",
            "data": {
                "period": period,
                "schedule_label": schedule_label,
                "spaces": spaces,
            },
        }
    )

    return ApiResponse(
        status=200,
        body={
            "status": "triggered",
            "period": period,
            "schedule_label": schedule_label,
            "spaces": spaces or "ALL",
            "message": "Report generation enqueued. Check logs for progress.",
        },
    )
