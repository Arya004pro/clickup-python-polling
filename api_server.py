#!/usr/bin/env python
"""
ClickUp MCP - REST API Service

HTTP endpoints:
  POST /query
  GET  /status
  GET  /stats
  GET  /reports
  GET  /reports/latest
  GET  /reports/{report_name}
  GET  /
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import re
import smtplib
import sys
from datetime import datetime
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, Response
from pydantic import BaseModel
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

# Import the AI client directly (OpenRouter-only mode)
sys.path.insert(0, "/app")


def _resolve_client_class():
    from openrouter_client import OpenRouterMCPClient

    return "openrouter", OpenRouterMCPClient


AI_CLIENT_PROVIDER, AI_CLIENT_CLASS = _resolve_client_class()


class _SuppressStatusAccessLog(logging.Filter):
    """Filter out noisy heartbeat access logs for GET /status."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            args = record.args
            if isinstance(args, tuple) and len(args) >= 5:
                method = str(args[1]).upper()
                path = str(args[2]).split("?", 1)[0]
                if method == "GET" and path == "/status":
                    return False
        except Exception:
            pass
        return True


_uvicorn_access_logger = logging.getLogger("uvicorn.access")
if not any(
    isinstance(existing_filter, _SuppressStatusAccessLog)
    for existing_filter in _uvicorn_access_logger.filters
):
    _uvicorn_access_logger.addFilter(_SuppressStatusAccessLog())


class QueryRequest(BaseModel):
    question: str
    model: Optional[str] = None
    reset_conversation: bool = (
        False  # Clear conversation history before this query (use for batch reports)
    )


class QueryResponse(BaseModel):
    status: str
    question: str
    response: Optional[str] = None
    tokens_used: Optional[dict] = None
    report_saved: bool = False
    report_file: Optional[str] = None
    report_download_url: Optional[str] = None
    error: Optional[str] = None


class SpaceReportRequest(BaseModel):
    space_name: str
    period_type: str = "today"
    include_archived: bool = True
    schedule_label: Optional[str] = None
    custom_start: Optional[str] = None
    custom_end: Optional[str] = None


class SpaceReportResponse(BaseModel):
    status: str
    space_name: str
    period_type: str
    response: Optional[str] = None
    elapsed_s: Optional[float] = None
    error: Optional[str] = None


class SendReportEmailRequest(BaseModel):
    report_name: str
    to_email: Optional[str] = None
    subject: Optional[str] = None


class SendReportEmailResponse(BaseModel):
    status: str
    report_name: str
    to_email: Optional[str] = None
    subject: Optional[str] = None
    error: Optional[str] = None


class RenderPdfRequest(BaseModel):
    markdown: str
    title: Optional[str] = None
    filename: Optional[str] = None


app = FastAPI(
    title="ClickUp MCP REST API",
    description="Query ClickUp via MCP + AI provider",
    version="1.1.0",
)

client = None
client_ready = False
client_connect_lock = asyncio.Lock()
# The MCP SSE client (anyio cancel scopes) is NOT safe for concurrent use.
# Serialise all /query requests behind this lock.
query_lock = asyncio.Lock()
REPORTS_DIR = Path(os.getenv("REPORTS_DIR", r"D:\reports"))


def _ensure_reports_dir() -> None:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)


def _list_reports(limit: int = 50) -> list[dict]:
    if not REPORTS_DIR.exists():
        return []
    report_entries: list[tuple[Path, os.stat_result]] = []
    for p in REPORTS_DIR.glob("report_*.md"):
        try:
            report_entries.append((p, p.stat()))
        except OSError:
            continue

    report_entries.sort(key=lambda pair: pair[1].st_mtime, reverse=True)
    return [
        {
            "name": p.name,
            "size_bytes": st.st_size,
            "modified": datetime.fromtimestamp(st.st_mtime).isoformat(
                timespec="seconds"
            ),
        }
        for p, st in report_entries[:limit]
    ]


def _latest_report_path() -> Optional[Path]:
    reports = _list_reports(limit=1)
    if not reports:
        return None
    return REPORTS_DIR / reports[0]["name"]


def _looks_like_email(value: str) -> bool:
    if not value:
        return False
    return bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", value))


def _send_report_via_smtp(report_path: Path, to_email: str, subject: str) -> None:
    smtp_host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    smtp_email = os.getenv("SMTP_EMAIL", "")
    smtp_password = os.getenv("SMTP_PASSWORD", "")

    if not smtp_email or not smtp_password:
        raise RuntimeError(
            "SMTP_EMAIL/SMTP_PASSWORD not configured in environment."
        )

    report_content = report_path.read_text(encoding="utf-8")
    report_title = report_path.stem

    msg = MIMEMultipart("mixed")
    msg["From"] = smtp_email
    msg["To"] = to_email
    msg["Subject"] = subject

    body = (
        "Hello,\n\n"
        "Please find the requested ClickUp report attached.\n\n"
        f"Report: {report_title}\n"
        f"Generated from API dashboard at {datetime.now().isoformat(timespec='seconds')}\n\n"
        "Regards,\n"
        "ClickUp MCP API"
    )
    msg.attach(MIMEText(body, "plain", "utf-8"))

    pdf_bytes = _markdown_to_pdf_bytes(report_content, report_path.stem)
    if pdf_bytes:
        pdf_name = f"{report_title}.pdf"
        attachment = MIMEBase("application", "pdf")
        attachment.set_payload(pdf_bytes)
        encoders.encode_base64(attachment)
        attachment.add_header(
            "Content-Disposition", f'attachment; filename="{pdf_name}"'
        )
        msg.attach(attachment)
    else:
        # Fallback to markdown attachment if PDF conversion fails for any reason.
        attachment = MIMEBase("text", "markdown")
        attachment.set_payload(report_content.encode("utf-8"))
        encoders.encode_base64(attachment)
        fallback_name = f"{report_title}.md"
        attachment.add_header("Content-Disposition", f'attachment; filename="{fallback_name}"')
        msg.attach(attachment)

    with smtplib.SMTP(smtp_host, smtp_port) as server:
        server.starttls()
        server.login(smtp_email, smtp_password)
        server.send_message(msg)


def _markdown_to_pdf_bytes(markdown_content: str, title: str) -> Optional[bytes]:
    try:
        out = io.BytesIO()
        doc = SimpleDocTemplate(
            out,
            pagesize=A4,
            leftMargin=14 * mm,
            rightMargin=14 * mm,
            topMargin=16 * mm,
            bottomMargin=16 * mm,
        )
        styles = getSampleStyleSheet()
        normal = ParagraphStyle(
            "ReportBody",
            parent=styles["BodyText"],
            fontName="Helvetica",
            fontSize=10,
            leading=14,
            textColor=colors.HexColor("#1f2937"),
            spaceAfter=2,
        )
        h1 = ParagraphStyle(
            "H1",
            parent=styles["Heading1"],
            fontName="Helvetica-Bold",
            fontSize=17,
            leading=21,
            textColor=colors.HexColor("#0f3d71"),
            spaceBefore=8,
            spaceAfter=5,
        )
        h2 = ParagraphStyle(
            "H2",
            parent=styles["Heading2"],
            fontName="Helvetica-Bold",
            fontSize=13,
            leading=17,
            textColor=colors.HexColor("#144f93"),
            spaceBefore=6,
            spaceAfter=4,
        )
        h3 = ParagraphStyle(
            "H3",
            parent=styles["Heading3"],
            fontName="Helvetica-Bold",
            fontSize=11,
            leading=15,
            textColor=colors.HexColor("#1e3a5f"),
            spaceBefore=5,
            spaceAfter=3,
        )
        meta_style = ParagraphStyle(
            "Meta",
            parent=normal,
            fontSize=8.5,
            leading=11,
            textColor=colors.HexColor("#64748b"),
        )
        code_style = ParagraphStyle(
            "Code",
            parent=normal,
            fontName="Courier",
            fontSize=8.8,
            leading=11,
            backColor=colors.HexColor("#0f172a"),
            textColor=colors.HexColor("#e2e8f0"),
            leftIndent=6,
            rightIndent=6,
            spaceBefore=4,
            spaceAfter=6,
        )

        def _inline_markup(text: str) -> str:
            escaped = (
                text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            )
            escaped = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", escaped)
            escaped = re.sub(r"__(.+?)__", r"<b>\1</b>", escaped)
            escaped = re.sub(r"\*(.+?)\*", r"<i>\1</i>", escaped)
            escaped = re.sub(r"`(.+?)`", r"<font face='Courier'>\1</font>", escaped)
            return escaped

        page_width = doc.width
        generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        header_data = [
            [Paragraph("<b>ClickUp Report</b>", h2)],
            [Paragraph(_inline_markup(title), h1)],
            [Paragraph(f"Generated: {generated_at}", meta_style)],
        ]
        header = Table(header_data, colWidths=[page_width])
        header.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f8fbff")),
                    ("BOX", (0, 0), (-1, -1), 0.8, colors.HexColor("#d5e3f5")),
                    ("LEFTPADDING", (0, 0), (-1, -1), 10),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                    ("TOPPADDING", (0, 0), (-1, -1), 7),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                ]
            )
        )

        lines = (markdown_content or "").splitlines()
        story = [header, Spacer(1, 10)]
        i = 0
        in_code_block = False
        code_lines: list[str] = []

        def _flush_code_block() -> None:
            if not code_lines:
                return
            block = "\n".join(code_lines)
            escaped = (
                block.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            )
            story.append(Paragraph(escaped.replace("\n", "<br/>"), code_style))
            code_lines.clear()

        while i < len(lines):
            line = (lines[i] or "").strip()

            if line.startswith("```"):
                if in_code_block:
                    _flush_code_block()
                    in_code_block = False
                else:
                    in_code_block = True
                i += 1
                continue
            if in_code_block:
                code_lines.append(lines[i] or "")
                i += 1
                continue

            if not line:
                story.append(Spacer(1, 6))
                i += 1
                continue

            if re.match(r"^[-*_]{3,}$", line):
                story.append(Spacer(1, 4))
                rule = Table([[""]], colWidths=[page_width], rowHeights=[1.2])
                rule.setStyle(
                    TableStyle(
                        [("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#dce5f3"))]
                    )
                )
                story.append(rule)
                story.append(Spacer(1, 6))
                i += 1
                continue

            if line.startswith("# "):
                story.append(Paragraph(_inline_markup(line[2:].strip()), h1))
                i += 1
                continue
            if line.startswith("## "):
                story.append(Paragraph(_inline_markup(line[3:].strip()), h2))
                i += 1
                continue
            if line.startswith("### "):
                story.append(Paragraph(_inline_markup(line[4:].strip()), h3))
                i += 1
                continue

            if re.match(r"^[-*]\s+", line):
                bullet_text = re.sub(r"^[-*]\s+", "", line)
                story.append(Paragraph(f"&bull; {_inline_markup(bullet_text)}", normal))
                i += 1
                continue

            if "|" in line and i + 1 < len(lines):
                divider = (lines[i + 1] or "").strip()
                if re.match(r"^[\s|:\-]+$", divider) and "|" in divider:
                    headers = [c.strip() for c in line.strip("|").split("|")]
                    rows = [headers]
                    i += 2
                    while i < len(lines):
                        row_line = (lines[i] or "").strip()
                        if "|" not in row_line:
                            break
                        rows.append([c.strip() for c in row_line.strip("|").split("|")])
                        i += 1

                    header_cells = [
                        Paragraph(f"<b>{_inline_markup(c)}</b>", normal)
                        for c in headers
                    ]
                    body_rows = [
                        [Paragraph(_inline_markup(c), normal) for c in r]
                        for r in rows[1:]
                    ]
                    table_rows = [header_cells, *body_rows]
                    column_count = max(1, len(headers))
                    col_width = page_width / column_count
                    table = Table(
                        table_rows,
                        repeatRows=1,
                        colWidths=[col_width] * column_count,
                    )
                    table.setStyle(
                        TableStyle(
                            [
                                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#dfeaf9")),
                                ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#0f2f57")),
                                ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#c8d6ea")),
                                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                                ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
                                ("FONTSIZE", (0, 0), (-1, -1), 9),
                                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f7faff")]),
                                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                                ("TOPPADDING", (0, 0), (-1, -1), 5),
                                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                            ]
                        )
                    )
                    story.append(table)
                    story.append(Spacer(1, 8))
                    continue

            story.append(Paragraph(_inline_markup(line), normal))
            i += 1

        _flush_code_block()
        doc.build(story)
        return out.getvalue()
    except Exception:
        return None


async def _connect_client(reuse_existing: bool = True) -> tuple[bool, str]:
    """Ensure the AI client has a live MCP connection."""
    global client, client_ready

    async with client_connect_lock:
        try:
            # Always create a fresh instance so anyio TaskGroup cancel-scope state
            # from a previous failed/cancelled connection is never reused.
            old_client = client
            client = AI_CLIENT_CLASS()
            if old_client is not None:
                try:
                    await old_client.disconnect_mcp()
                except Exception:
                    pass
            await client.connect_mcp()
            client_ready = True
            return True, ""
        except Exception as exc:
            client_ready = False
            return False, str(exc)[:160]


@app.on_event("startup")
async def startup_event():
    _ensure_reports_dir()

    max_retries = 8
    retry_delay = 3
    for attempt in range(max_retries):
        print(f"[Attempt {attempt + 1}/{max_retries}] Initializing AI client...")
        ok, err = await _connect_client(reuse_existing=False)
        if ok:
            print("API client initialized and ready.")
            return
        print(f"Attempt {attempt + 1} failed: {err}")
        if attempt < max_retries - 1:
            await asyncio.sleep(retry_delay)
        else:
            print("Initial retries exhausted. Client will retry on first query.")


@app.on_event("shutdown")
async def shutdown_event():
    global client, client_ready
    if client:
        try:
            await client.disconnect_mcp()
        except Exception:
            pass
    client_ready = False
    client = None


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>ClickUp MCP Dashboard</title>
  <script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: "Segoe UI", Tahoma, Geneva, Verdana, sans-serif;
      background: linear-gradient(135deg, #355c7d 0%, #6c5b7b 50%, #c06c84 100%);
      min-height: 100vh;
      padding: 20px;
      color: #1f2937;
    }
    .container {
      max-width: 1320px;
      margin: 0 auto;
      background: #fff;
      border-radius: 14px;
      overflow: hidden;
      box-shadow: 0 24px 60px rgba(0, 0, 0, 0.28);
    }
    .header {
      background: linear-gradient(95deg, #1f3b68, #2d5f8b);
      color: #fff;
      padding: 22px 24px;
    }
    .header h1 { font-size: 26px; margin-bottom: 6px; }
    .header p { opacity: 0.95; font-size: 13px; }
    .tabs {
      display: flex;
      gap: 8px;
      padding: 16px 20px 0;
      background: #f6f8ff;
      border-bottom: 1px solid #e1e8f5;
    }
    .tab-btn {
      border: 1px solid #cdd8ee;
      background: #edf2ff;
      color: #1d3b66;
      padding: 8px 14px;
      border-radius: 8px 8px 0 0;
      font-weight: 700;
      font-size: 13px;
      cursor: pointer;
    }
    .tab-btn.active {
      background: #fff;
      border-bottom-color: #fff;
    }
    .content { padding: 20px; }
    .page { display: none; }
    .page.active { display: block; }
    .card {
      background: #f9fbff;
      border: 1px solid #dce5f5;
      border-radius: 10px;
      padding: 14px;
    }
    .form-group { margin-bottom: 14px; }
    label { display: block; font-weight: 700; margin-bottom: 6px; font-size: 13px; }
    textarea, input {
      width: 100%;
      border: 1px solid #c6d2ea;
      border-radius: 8px;
      padding: 10px;
      font-size: 13px;
    }
    textarea { min-height: 110px; resize: vertical; }
    textarea:focus, input:focus {
      outline: none;
      border-color: #4d73b9;
      box-shadow: 0 0 0 2px rgba(77, 115, 185, 0.12);
    }
    .button-row {
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
    }
    button {
      border: 0;
      border-radius: 8px;
      padding: 10px 14px;
      font-size: 13px;
      font-weight: 700;
      cursor: pointer;
    }
    .btn-primary { background: #2d5f8b; color: #fff; }
    .btn-secondary { background: #e4eaf7; color: #1f2f49; }
    .btn-small { padding: 6px 10px; font-size: 12px; }
    .btn-primary[disabled], .btn-secondary[disabled] {
      opacity: 0.7; cursor: not-allowed;
    }
    .loader {
      display: none;
      margin-top: 14px;
      background: #eef4ff;
      border: 1px solid #cfdbf4;
      border-radius: 8px;
      padding: 10px 12px;
      font-size: 12px;
      color: #27456f;
    }
    .loader.show { display: block; }
    .error {
      display: none;
      margin-top: 12px;
      background: #ffecec;
      color: #992323;
      border-radius: 8px;
      border: 1px solid #f6bbbb;
      padding: 10px;
      font-size: 13px;
    }
    .error.show { display: block; }
    .response-box {
      display: none;
      margin-top: 16px;
      background: #fff;
      border: 1px solid #d7e1f5;
      border-radius: 10px;
      padding: 14px;
      max-height: 70vh;
      overflow: auto;
    }
    .response-box.show { display: block; }
    .response-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 10px;
      gap: 10px;
    }
    .response-title { color: #204974; font-weight: 800; font-size: 14px; }
    .response-actions { display: flex; gap: 8px; }
    .response-content { color: #1f2937; font-size: 13px; line-height: 1.6; }
    .response-content table {
      width: 100%;
      border-collapse: collapse;
      margin: 10px 0;
      font-size: 12px;
      background: #fff;
    }
    .response-content th, .response-content td {
      border: 1px solid #dce3f3;
      padding: 7px 9px;
      text-align: left;
      vertical-align: top;
    }
    .response-content th { background: #2d5f8b; color: #fff; }
    .response-content tr:nth-child(even) td { background: #f7faff; }
    .status-line { margin-top: 8px; font-size: 12px; color: #475569; }
    .status-line a { color: #2d5f8b; }
    .grid-2 {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 10px;
      margin-bottom: 12px;
    }
    .reports-wrap {
      border: 1px solid #d7e1f5;
      border-radius: 10px;
      overflow: hidden;
      background: #fff;
    }
    .reports-table {
      width: 100%;
      border-collapse: collapse;
      font-size: 12px;
    }
    .reports-table th, .reports-table td {
      border-bottom: 1px solid #e4eaf7;
      padding: 8px 10px;
      text-align: left;
      vertical-align: middle;
    }
    .reports-table thead th {
      background: #f2f6ff;
      color: #27456f;
      font-weight: 800;
    }
    .reports-table tbody tr:nth-child(even) td { background: #fbfcff; }
    .actions { display: flex; gap: 6px; }
    .pager {
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding: 10px;
      border-top: 1px solid #e4eaf7;
      background: #fafcff;
      font-size: 12px;
    }
    .toast {
      margin-top: 10px;
      font-size: 12px;
      padding: 8px 10px;
      border-radius: 8px;
      display: none;
    }
    .toast.show { display: block; }
    .toast.ok { background: #e9f9ee; color: #115d2f; border: 1px solid #b6e3c6; }
    .toast.err { background: #ffecec; color: #8f1f1f; border: 1px solid #f0b7b7; }
    .reports-empty {
      padding: 16px;
      color: #6b7280;
      font-size: 13px;
    }
    .live-status {
      position: fixed;
      top: 12px;
      right: 14px;
      z-index: 9999;
      font-size: 11px;
      font-weight: 700;
      padding: 6px 10px;
      border-radius: 999px;
      background: #d6f5e4;
      color: #0f6b3f;
      border: 1px solid #9ed8bc;
      transition: all 0.2s ease;
    }
    .live-status.offline {
      background: #ffefef;
      color: #b62424;
      border-color: #f0b3b3;
      animation: status-blink 1.1s ease-in-out infinite;
    }
    @keyframes status-blink {
      0%, 100% { opacity: 1; }
      50% { opacity: 0.45; }
    }
    @media (max-width: 980px) {
      .grid-2 { grid-template-columns: 1fr; }
      .response-header { flex-direction: column; align-items: flex-start; }
      .pager { flex-direction: column; align-items: flex-start; gap: 8px; }
    }
  </style>
</head>
<body>
  <div class="live-status" id="liveStatus">API Connected</div>
  <div class="container">
    <div class="header">
      <h1>ClickUp MCP Dashboard</h1>
      <p>Query ClickUp tools, browse saved reports, and send a selected report to email.</p>
    </div>
    <div class="tabs">
      <button class="tab-btn active" id="tabQueryBtn" onclick="showTab('query')">Query</button>
      <button class="tab-btn" id="tabReportsBtn" onclick="showTab('reports')">Reports</button>
    </div>
    <div class="content">
      <section class="page active" id="pageQuery">
        <div class="card">
          <form id="queryForm">
            <div class="form-group">
              <label for="question">Your Query</label>
              <textarea id="question" name="question" required placeholder="Example: Generate yesterday space task report for Monitored AIX"></textarea>
            </div>
            <div class="button-row">
              <button type="submit" class="btn-primary" id="submitBtn">Send Query</button>
              <button type="button" class="btn-secondary" onclick="clearQueryInput()">Clear</button>
              <button type="button" class="btn-secondary" onclick="showTab('reports')">Go to Reports</button>
            </div>
          </form>
          <div class="loader" id="loader">Working on your request...</div>
          <div class="error" id="error"></div>
          <div class="response-box" id="responseBox">
            <div class="response-header">
              <div class="response-title">Response</div>
              <div class="response-actions">
                <button class="btn-primary btn-small" onclick="downloadReport()" id="downloadBtn" style="display:none;">Download</button>
                <button class="btn-secondary btn-small" onclick="clearReportView()" id="clearReportBtn" style="display:none;">Clear</button>
              </div>
            </div>
            <div class="response-content" id="responseContent"></div>
            <div class="status-line" id="responseStatus"></div>
          </div>
        </div>
      </section>

      <section class="page" id="pageReports">
        <div class="card">
          <div class="grid-2">
            <div class="form-group" style="margin-bottom:0;">
              <label for="recipientEmail">Send report to email (optional override)</label>
              <input id="recipientEmail" type="email" placeholder="Leave blank to use SMTP_TO from .env" />
            </div>
            <div class="form-group" style="margin-bottom:0;">
              <label for="emailSubject">Email subject (optional)</label>
              <input id="emailSubject" type="text" placeholder="ClickUp Report - <report-name>" />
            </div>
          </div>
          <div class="button-row" style="margin-bottom:10px;">
            <button class="btn-secondary" onclick="refreshReports()">Refresh Reports</button>
            <button class="btn-secondary" onclick="showTab('query')">Back to Query</button>
          </div>
          <div class="reports-wrap">
            <div id="reportsContainer" class="reports-empty">Loading reports...</div>
            <div class="pager" id="reportsPager" style="display:none;">
              <div id="reportsPageInfo">Page 1</div>
              <div class="button-row">
                <button class="btn-secondary btn-small" id="prevPageBtn">Previous</button>
                <button class="btn-secondary btn-small" id="nextPageBtn">Next</button>
              </div>
            </div>
          </div>
          <div class="toast" id="mailToast"></div>
        </div>
      </section>
    </div>
  </div>

  <script>
    let lastResponse = '';
    let backendWasOffline = false;
    let heartbeatTimer = null;
    let reportsData = [];
    let reportsPage = 1;
    const REPORTS_PAGE_SIZE = 15;
    const HEARTBEAT_ONLINE_MS = 10000;
    const HEARTBEAT_OFFLINE_MS = 2500;
    const HEARTBEAT_HIDDEN_MS = 30000;

    function showTab(tab) {
      const isQuery = tab === 'query';
      document.getElementById('pageQuery').classList.toggle('active', isQuery);
      document.getElementById('pageReports').classList.toggle('active', !isQuery);
      document.getElementById('tabQueryBtn').classList.toggle('active', isQuery);
      document.getElementById('tabReportsBtn').classList.toggle('active', !isQuery);
      if (!isQuery) refreshReports();
    }

    function escapeHtml(text) {
      return (text || '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
    }

    function markdownToHtml(markdown) {
      if (window.marked && typeof window.marked.parse === 'function') {
        if (typeof window.marked.setOptions === 'function') {
          window.marked.setOptions({ gfm: true, breaks: true });
        }
        return window.marked.parse(markdown || '');
      }
      return `<pre>${escapeHtml(markdown || '')}</pre>`;
    }

    function formatBytes(bytes) {
      const value = Number(bytes || 0);
      if (value < 1024) return `${value} B`;
      if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
      return `${(value / (1024 * 1024)).toFixed(1)} MB`;
    }

    function showToast(message, ok=true) {
      const el = document.getElementById('mailToast');
      el.textContent = message;
      el.className = `toast show ${ok ? 'ok' : 'err'}`;
      setTimeout(() => {
        el.classList.remove('show');
      }, 4000);
    }

    function renderReports() {
      const container = document.getElementById('reportsContainer');
      const pager = document.getElementById('reportsPager');
      const pageInfo = document.getElementById('reportsPageInfo');
      const prevBtn = document.getElementById('prevPageBtn');
      const nextBtn = document.getElementById('nextPageBtn');

      if (!reportsData.length) {
        container.innerHTML = '<div class="reports-empty">No reports found.</div>';
        pager.style.display = 'none';
        return;
      }

      const totalPages = Math.max(1, Math.ceil(reportsData.length / REPORTS_PAGE_SIZE));
      reportsPage = Math.max(1, Math.min(reportsPage, totalPages));
      const start = (reportsPage - 1) * REPORTS_PAGE_SIZE;
      const pageRows = reportsData.slice(start, start + REPORTS_PAGE_SIZE);

      const rows = pageRows.map((r) => {
        const name = escapeHtml(r.name || '');
        const modified = escapeHtml(r.modified || '');
        const size = formatBytes(r.size_bytes || 0);
        return `
          <tr>
            <td><a href="/reports/${name}" target="_blank">${name}</a></td>
            <td>${modified}</td>
            <td>${size}</td>
            <td>
              <div class="actions">
                <a class="btn-secondary btn-small" href="/reports/${name}" target="_blank">Open</a>
                <button class="btn-primary btn-small send-btn" data-report="${name}">Send</button>
              </div>
            </td>
          </tr>
        `;
      }).join('');

      container.innerHTML = `
        <table class="reports-table">
          <thead>
            <tr><th>Report</th><th>Modified</th><th>Size</th><th>Actions</th></tr>
          </thead>
          <tbody>${rows}</tbody>
        </table>
      `;

      pager.style.display = 'flex';
      pageInfo.textContent = `Page ${reportsPage} / ${totalPages} (${reportsData.length} reports)`;
      prevBtn.disabled = reportsPage <= 1;
      nextBtn.disabled = reportsPage >= totalPages;

      document.querySelectorAll('.send-btn').forEach((btn) => {
        btn.addEventListener('click', () => sendReportByEmail(btn.dataset.report, btn));
      });
    }

    async function refreshReports() {
      const container = document.getElementById('reportsContainer');
      container.innerHTML = '<div class="reports-empty">Loading reports...</div>';
      try {
        const response = await fetch('/reports?limit=500', { cache: 'no-store' });
        const data = await response.json();
        reportsData = Array.isArray(data.reports) ? data.reports : [];
        renderReports();
      } catch (err) {
        container.innerHTML = '<div class="reports-empty">Unable to load reports list.</div>';
      }
    }

    async function sendReportByEmail(reportName, btn) {
      if (!reportName) return;
      const toEmail = (document.getElementById('recipientEmail').value || '').trim();
      const subject = (document.getElementById('emailSubject').value || '').trim();
      btn.disabled = true;
      btn.textContent = 'Sending...';
      try {
        const response = await fetch('/reports/send', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            report_name: reportName,
            to_email: toEmail || null,
            subject: subject || null,
          }),
        });
        const data = await response.json();
        if (data.status === 'success') {
          showToast(`Sent ${reportName} to ${data.to_email}`, true);
        } else {
          showToast(`Send failed: ${data.error || 'unknown error'}`, false);
        }
      } catch (err) {
        showToast(`Send failed: ${err.message}`, false);
      } finally {
        btn.disabled = false;
        btn.textContent = 'Send';
      }
    }

    function downloadReport() {
      if (!lastResponse) return;
      const element = document.createElement('a');
      element.setAttribute('href', 'data:text/markdown;charset=utf-8,' + encodeURIComponent(lastResponse));
      element.setAttribute('download', `report_${new Date().toISOString().slice(0,10)}.md`);
      element.style.display = 'none';
      document.body.appendChild(element);
      element.click();
      document.body.removeChild(element);
    }

    function clearQueryInput() {
      document.getElementById('question').value = '';
      document.getElementById('question').focus();
    }

    function clearReportView() {
      lastResponse = '';
      document.getElementById('responseContent').innerHTML = '';
      document.getElementById('responseStatus').textContent = '';
      document.getElementById('responseBox').classList.remove('show');
      document.getElementById('downloadBtn').style.display = 'none';
      document.getElementById('clearReportBtn').style.display = 'none';
      document.getElementById('error').classList.remove('show');
    }

    function setLiveStatus(online) {
      const badge = document.getElementById('liveStatus');
      if (online) {
        badge.textContent = 'API Connected';
        badge.classList.remove('offline');
      } else {
        badge.textContent = 'Reconnecting...';
        badge.classList.add('offline');
      }
    }

    function scheduleHeartbeat(delayMs) {
      if (heartbeatTimer) clearTimeout(heartbeatTimer);
      heartbeatTimer = setTimeout(heartbeatCheck, delayMs);
    }

    async function heartbeatCheck() {
      const nextOnlineDelay = document.hidden ? HEARTBEAT_HIDDEN_MS : HEARTBEAT_ONLINE_MS;
      try {
        const response = await fetch('/status', { cache: 'no-store' });
        if (!response.ok) throw new Error('status-check-failed');
        setLiveStatus(true);
        if (backendWasOffline) {
          backendWasOffline = false;
          window.location.reload();
          return;
        }
        scheduleHeartbeat(nextOnlineDelay);
      } catch (err) {
        setLiveStatus(false);
        backendWasOffline = true;
        scheduleHeartbeat(HEARTBEAT_OFFLINE_MS);
      }
    }

    document.getElementById('prevPageBtn').addEventListener('click', () => {
      reportsPage = Math.max(1, reportsPage - 1);
      renderReports();
    });
    document.getElementById('nextPageBtn').addEventListener('click', () => {
      reportsPage += 1;
      renderReports();
    });

    const questionInput = document.getElementById('question');
    questionInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        document.getElementById('queryForm').requestSubmit();
      }
    });

    document.getElementById('queryForm').addEventListener('submit', async (e) => {
      e.preventDefault();
      const question = document.getElementById('question').value.trim();
      if (!question) return;

      const loader = document.getElementById('loader');
      const error = document.getElementById('error');
      const responseBox = document.getElementById('responseBox');
      const downloadBtn = document.getElementById('downloadBtn');
      const clearReportBtn = document.getElementById('clearReportBtn');
      const submitBtn = document.getElementById('submitBtn');

      loader.classList.add('show');
      error.classList.remove('show');
      responseBox.classList.remove('show');
      downloadBtn.style.display = 'none';
      clearReportBtn.style.display = 'none';
      submitBtn.disabled = true;
      submitBtn.textContent = 'Working...';

      try {
        const response = await fetch('/query', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ question }),
        });
        const data = await response.json();
        loader.classList.remove('show');

        if (data.status === 'error') {
          error.textContent = 'Error: ' + (data.error || 'Unknown error');
          error.classList.add('show');
          return;
        }
        if (!data.response) {
          error.textContent = 'No response received';
          error.classList.add('show');
          return;
        }

        lastResponse = data.response;
        document.getElementById('responseContent').innerHTML = markdownToHtml(data.response);

        const statusEl = document.getElementById('responseStatus');
        let statusHtml = `Tokens: input=${data.tokens_used?.input || 0} | output=${data.tokens_used?.output || 0}`;
        if (data.report_saved && data.report_file) {
          statusHtml += ` | Saved: ${data.report_file}`;
          if (data.report_download_url) {
            statusHtml += ` | <a href="${data.report_download_url}" target="_blank">Open saved report</a>`;
          }
        }
        statusEl.innerHTML = statusHtml;
        downloadBtn.style.display = 'inline-block';
        clearReportBtn.style.display = 'inline-block';
        responseBox.classList.add('show');
        refreshReports();
      } catch (err) {
        loader.classList.remove('show');
        error.textContent = 'Request failed: ' + err.message;
        error.classList.add('show');
      } finally {
        submitBtn.disabled = false;
        submitBtn.textContent = 'Send Query';
      }
    });

    refreshReports();
    heartbeatCheck();
  </script>
</body>
</html>
    """

@app.post("/query", response_model=QueryResponse)
async def query_ai(req: QueryRequest):
    global client, client_ready

    if client is None or getattr(client, "mcp_session", None) is None:
        client_ready = False

    if not client_ready:
        # The MCP server may have just restarted — retry a few times before failing.
        ok, err = False, "not attempted"
        for _attempt in range(4):
            ok, err = await _connect_client(reuse_existing=True)
            if ok:
                break
            if _attempt < 3:
                await asyncio.sleep(3)
        if not ok:
            return QueryResponse(
                status="error",
                question=req.question,
                error=f"Client still initializing. Retry shortly. Details: {err}",
            )

    if client is None:
        return QueryResponse(
            status="error",
            question=req.question,
            error="MCP client not ready. Please retry in a few seconds.",
        )

    if not req.question or not req.question.strip():
        return QueryResponse(
            status="error",
            question=req.question,
            error="Question cannot be empty.",
        )

    if req.reset_conversation:
        client.conversation = []

    before_saved = client.stats.reports_saved
    async with query_lock:
        try:
            answer = await client.chat(req.question)
            after_saved = client.stats.reports_saved
            latest = _latest_report_path()
            report_saved = after_saved > before_saved and latest is not None
            report_file = latest.name if latest else None
            report_url = f"/reports/{latest.name}" if latest else None

            return QueryResponse(
                status="success",
                question=req.question,
                response=answer,
                tokens_used={
                    "input": client.stats.total_input_tokens,
                    "output": client.stats.total_output_tokens,
                },
                report_saved=report_saved,
                report_file=report_file,
                report_download_url=report_url,
            )
        except Exception as exc:
            client_ready = False
            return QueryResponse(
                status="error",
                question=req.question,
                error=f"Error: {str(exc)[:200]}",
            )


@app.post("/report/space", response_model=SpaceReportResponse)
async def generate_space_report(req: SpaceReportRequest):
    global client, client_ready

    if client is None or getattr(client, "mcp_session", None) is None:
        client_ready = False

    if not client_ready:
        ok, err = False, "not attempted"
        for _attempt in range(4):
            ok, err = await _connect_client(reuse_existing=True)
            if ok:
                break
            if _attempt < 3:
                await asyncio.sleep(3)
        if not ok:
            return SpaceReportResponse(
                status="error",
                space_name=req.space_name,
                period_type=req.period_type,
                error=f"Client still initializing. Retry shortly. Details: {err}",
            )

    if client is None:
        return SpaceReportResponse(
            status="error",
            space_name=req.space_name,
            period_type=req.period_type,
            error="MCP client not ready. Please retry in a few seconds.",
        )

    started = asyncio.get_running_loop().time()
    use_isolated_client = (
        os.getenv("REPORT_DIRECT_ISOLATED_CLIENT", "true").strip().lower()
        not in {"0", "false", "no"}
    )

    async def _run_with_client(active_client) -> Optional[str]:
        return await active_client.generate_space_report_direct(
            space_name=req.space_name,
            period_type=req.period_type,
            include_archived=req.include_archived,
            schedule_label=req.schedule_label,
            custom_start=req.custom_start,
            custom_end=req.custom_end,
        )

    try:
        if use_isolated_client:
            temp_client = AI_CLIENT_CLASS()
            await temp_client.connect_mcp()
            try:
                report_text = await _run_with_client(temp_client)
            finally:
                try:
                    await temp_client.disconnect_mcp()
                except Exception:
                    pass
        else:
            async with query_lock:
                report_text = await _run_with_client(client)

        elapsed = round(asyncio.get_running_loop().time() - started, 2)
        if not report_text:
            return SpaceReportResponse(
                status="error",
                space_name=req.space_name,
                period_type=req.period_type,
                elapsed_s=elapsed,
                error="No report content returned.",
            )
        if report_text.startswith("Error:"):
            return SpaceReportResponse(
                status="error",
                space_name=req.space_name,
                period_type=req.period_type,
                elapsed_s=elapsed,
                error=report_text[6:].strip() or report_text,
            )
        return SpaceReportResponse(
            status="success",
            space_name=req.space_name,
            period_type=req.period_type,
            response=report_text,
            elapsed_s=elapsed,
        )
    except Exception as exc:
        client_ready = False
        return SpaceReportResponse(
            status="error",
            space_name=req.space_name,
            period_type=req.period_type,
            error=f"Error: {str(exc)[:200]}",
        )


@app.get("/status")
async def status():
    return {
        "api_status": "ok",
        "client_ready": client_ready,
        "mcp_connected": client is not None and client.mcp_session is not None,
        "active_model": client.active_model if client else None,
        "tools_loaded": len(client.openai_tools) if client else 0,
        "ai_provider": (
            getattr(client, "active_provider", AI_CLIENT_PROVIDER)
            if client
            else AI_CLIENT_PROVIDER
        ),
        "reports_dir": str(REPORTS_DIR),
    }


@app.get("/stats")
async def get_stats():
    if not client:
        return {"error": "Client not initialized"}
    latest = _latest_report_path()
    return {
        "api_calls": client.stats.total_api_calls,
        "tool_calls": client.stats.tool_calls_made,
        "input_tokens": client.stats.total_input_tokens,
        "output_tokens": client.stats.total_output_tokens,
        "reports_saved": client.stats.reports_saved,
        "latest_report": latest.name if latest else None,
        "reports_dir": str(REPORTS_DIR),
        "session_duration": client.stats.elapsed(),
        "models_used": client.stats.models_used,
    }


@app.get("/reports")
async def list_reports(limit: int = 200):
    safe_limit = max(1, min(limit, 1000))
    reports = _list_reports(limit=safe_limit)
    return {
        "reports_dir": str(REPORTS_DIR),
        "count": len(reports),
        "reports": reports,
    }


@app.post("/reports/send", response_model=SendReportEmailResponse)
async def send_report_to_email(req: SendReportEmailRequest):
    report_name = (req.report_name or "").strip()
    if not report_name:
        return SendReportEmailResponse(
            status="error", report_name="", error="report_name is required."
        )
    if "/" in report_name or "\\" in report_name or ".." in report_name:
        return SendReportEmailResponse(
            status="error", report_name=report_name, error="Invalid report name."
        )

    report_path = REPORTS_DIR / report_name
    if not report_path.exists() or not report_path.is_file():
        return SendReportEmailResponse(
            status="error", report_name=report_name, error="Report not found."
        )

    to_email = (req.to_email or os.getenv("SMTP_TO", "")).strip()
    if not _looks_like_email(to_email):
        return SendReportEmailResponse(
            status="error",
            report_name=report_name,
            to_email=to_email or None,
            error="Valid recipient email is required.",
        )

    default_title = Path(report_name).stem
    subject = (req.subject or f"ClickUp Report - {default_title}").strip()
    try:
        await asyncio.to_thread(_send_report_via_smtp, report_path, to_email, subject)
        return SendReportEmailResponse(
            status="success",
            report_name=report_name,
            to_email=to_email,
            subject=subject,
        )
    except Exception as exc:
        return SendReportEmailResponse(
            status="error",
            report_name=report_name,
            to_email=to_email,
            subject=subject,
            error=str(exc)[:220],
        )


@app.post("/render/pdf")
async def render_pdf(req: RenderPdfRequest):
    title = (req.title or "ClickUp Report").strip()
    filename = (req.filename or f"{title}.pdf").strip()
    safe_filename = re.sub(r"[^\w\-. ]+", "_", filename).replace(" ", "_")
    if not safe_filename.lower().endswith(".pdf"):
        safe_filename = f"{safe_filename}.pdf"
    pdf_bytes = _markdown_to_pdf_bytes(req.markdown or "", title=title)
    if not pdf_bytes:
        raise HTTPException(status_code=500, detail="PDF rendering failed.")
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{safe_filename}"'},
    )


@app.get("/reports/latest")
async def download_latest_report():
    latest = _latest_report_path()
    if latest is None or not latest.exists():
        raise HTTPException(status_code=404, detail="No reports found.")
    return FileResponse(path=latest, media_type="text/markdown", filename=latest.name)


@app.get("/reports/{report_name}")
async def download_report(report_name: str):
    if "/" in report_name or "\\" in report_name or ".." in report_name:
        raise HTTPException(status_code=400, detail="Invalid report name.")
    if not report_name.endswith(".md"):
        raise HTTPException(
            status_code=400, detail="Only .md report files are supported."
        )

    path = REPORTS_DIR / report_name
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="Report not found.")
    return FileResponse(path=path, media_type="text/markdown", filename=path.name)


if __name__ == "__main__":
    import uvicorn

    print("=" * 70)
    print("  ClickUp MCP - REST API Service")
    print("=" * 70)
    print("  Web Dashboard: http://localhost:8003")
    print("  API Base:      http://localhost:8003")
    print("=" * 70)

    uvicorn.run(app, host="0.0.0.0", port=8003, log_level="info")


