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
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

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


class QueryResponse(BaseModel):
    status: str
    question: str
    response: Optional[str] = None
    tokens_used: Optional[dict] = None
    report_saved: bool = False
    report_file: Optional[str] = None
    report_download_url: Optional[str] = None
    error: Optional[str] = None


app = FastAPI(
    title="ClickUp MCP REST API",
    description="Query ClickUp via MCP + AI provider",
    version="1.1.0",
)

client = None
client_ready = False
client_connect_lock = asyncio.Lock()
REPORTS_DIR = Path(os.getenv("REPORTS_DIR", r"D:\reports"))


def _ensure_reports_dir() -> None:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)


def _list_reports(limit: int = 50) -> list[dict]:
    if not REPORTS_DIR.exists():
        return []
    files = sorted(
        REPORTS_DIR.glob("report_*.md"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    items = []
    for p in files[:limit]:
        st = p.stat()
        items.append(
            {
                "name": p.name,
                "size_bytes": st.st_size,
                "modified": datetime.fromtimestamp(st.st_mtime).isoformat(
                    timespec="seconds"
                ),
            }
        )
    return items


def _latest_report_path() -> Optional[Path]:
    reports = _list_reports(limit=1)
    if not reports:
        return None
    return REPORTS_DIR / reports[0]["name"]


async def _connect_client(reuse_existing: bool = True) -> tuple[bool, str]:
    """Ensure the AI client has a live MCP connection."""
    global client, client_ready

    async with client_connect_lock:
        try:
            if client is None or not reuse_existing:
                client = AI_CLIENT_CLASS()
            else:
                try:
                    await client.disconnect_mcp()
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

    max_retries = 5
    retry_delay = 2
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
  <title>ClickUp MCP - AI Query Interface</title>
  <script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: "Segoe UI", Tahoma, Geneva, Verdana, sans-serif;
      background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
      min-height: 100vh;
      padding: 20px;
    }
    .container {
      max-width: 1100px;
      margin: 0 auto;
      background: #fff;
      border-radius: 14px;
      overflow: hidden;
      box-shadow: 0 18px 50px rgba(0, 0, 0, 0.22);
    }
    .header {
      background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
      color: #fff;
      padding: 28px;
    }
    .header h1 { font-size: 30px; margin-bottom: 8px; }
    .header p { opacity: 0.95; font-size: 14px; }
    .content { padding: 24px; }
    .form-group { margin-bottom: 16px; }
    label { display: block; font-weight: 600; margin-bottom: 8px; color: #333; }
    textarea {
      width: 100%;
      min-height: 110px;
      border: 2px solid #e2e4ea;
      border-radius: 8px;
      padding: 12px;
      font-size: 14px;
      resize: vertical;
    }
    textarea:focus { outline: none; border-color: #667eea; }
    .button-group { display: flex; gap: 10px; }
    button {
      flex: 1;
      border: 0;
      border-radius: 8px;
      padding: 12px 16px;
      font-size: 16px;
      font-weight: 600;
      cursor: pointer;
    }
    .btn-submit {
      background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
      color: #fff;
    }
    .btn-clear { background: #eceef4; color: #222; }
    .btn-submit[disabled] {
      opacity: 0.75;
      cursor: not-allowed;
    }
    .examples {
      margin-top: 16px;
      border-left: 4px solid #667eea;
      background: #f0f4ff;
      border-radius: 8px;
      padding: 12px 14px;
      color: #444;
      font-size: 13px;
    }
    .examples strong { color: #5866d8; display: block; margin-bottom: 8px; }
    .examples ul { margin-left: 18px; }
    .examples li { margin-bottom: 4px; }
    .loader {
      display: none;
      margin-top: 16px;
    }
    .loader.show { display: block; }
    .loader-card {
      background: radial-gradient(circle at 20% 15%, rgba(102,126,234,0.2), rgba(118,75,162,0.1));
      border: 1px solid #d8dcf5;
      border-radius: 12px;
      padding: 16px;
      text-align: center;
      position: relative;
      overflow: hidden;
    }
    .loader-card::before {
      content: "";
      position: absolute;
      inset: -120% -40%;
      background: linear-gradient(120deg, transparent 30%, rgba(255,255,255,0.7) 50%, transparent 70%);
      animation: sheen 2.4s linear infinite;
      pointer-events: none;
    }
    .loader-orbit {
      width: 74px;
      height: 74px;
      margin: 0 auto 10px;
      border-radius: 50%;
      border: 2px solid rgba(102,126,234,0.2);
      border-top-color: #667eea;
      position: relative;
      animation: spin 1.15s linear infinite;
    }
    .loader-orbit::after {
      content: "";
      position: absolute;
      inset: 14px;
      border-radius: 50%;
      border: 2px dashed rgba(118,75,162,0.5);
      animation: spin-reverse 1.8s linear infinite;
    }
    .loader-core {
      width: 14px;
      height: 14px;
      border-radius: 50%;
      background: #764ba2;
      box-shadow: 0 0 0 0 rgba(118,75,162,0.35);
      position: absolute;
      left: 50%;
      top: 50%;
      transform: translate(-50%, -50%);
      animation: pulse 1.2s ease-out infinite;
    }
    .loader-title {
      font-size: 16px;
      font-weight: 700;
      color: #334155;
      margin-bottom: 5px;
    }
    .loader-subtitle {
      font-size: 12px;
      color: #59667f;
      margin-bottom: 10px;
    }
    .loader-meta {
      width: min(360px, 92%);
      margin: 0 auto 8px;
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 12px;
      font-size: 12px;
      color: #46536b;
    }
    .loader-phase {
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      max-width: 260px;
      text-align: left;
    }
    .loader-percent {
      font-weight: 700;
      color: #3f4eb2;
      min-width: 36px;
      text-align: right;
    }
    .loader-bar {
      width: min(320px, 90%);
      height: 8px;
      border-radius: 999px;
      margin: 0 auto;
      background: #e8ebf9;
      overflow: hidden;
    }
    .loader-bar span {
      display: block;
      width: 8%;
      height: 100%;
      background: linear-gradient(90deg, #667eea, #764ba2);
      border-radius: inherit;
      transition: width 0.35s ease;
      box-shadow: 0 0 10px rgba(102, 126, 234, 0.5);
    }
    @keyframes spin {
      from { transform: rotate(0deg); }
      to { transform: rotate(360deg); }
    }
    @keyframes spin-reverse {
      from { transform: rotate(360deg); }
      to { transform: rotate(0deg); }
    }
    @keyframes pulse {
      0% { box-shadow: 0 0 0 0 rgba(118,75,162,0.4); }
      70% { box-shadow: 0 0 0 14px rgba(118,75,162,0); }
      100% { box-shadow: 0 0 0 0 rgba(118,75,162,0); }
    }
    @keyframes sheen {
      0% { transform: translateX(-100%); }
      100% { transform: translateX(100%); }
    }
    .error {
      display: none;
      margin-top: 14px;
      background: #ffebee;
      color: #c62828;
      border-radius: 8px;
      padding: 12px;
      font-size: 14px;
    }
    .error.show { display: block; }
    .response-box {
      display: none;
      margin-top: 20px;
      background: #f9f9fb;
      border-left: 4px solid #667eea;
      border-radius: 8px;
      padding: 16px;
      max-height: 65vh;
      overflow: auto;
    }
    .response-box.show { display: block; }
    .response-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 12px;
    }
    .response-title { color: #667eea; font-weight: 700; }
    .response-actions { display: flex; gap: 8px; }
    .download-btn {
      background: #667eea;
      color: #fff;
      padding: 6px 10px;
      border: 0;
      border-radius: 6px;
      cursor: pointer;
      font-size: 12px;
      flex: 0 0 auto;
    }
    .clear-report-btn {
      background: #e9edf7;
      color: #222;
      padding: 6px 10px;
      border: 0;
      border-radius: 6px;
      cursor: pointer;
      font-size: 12px;
      flex: 0 0 auto;
    }
    .response-content { color: #333; font-size: 13px; line-height: 1.7; }
    .response-content h1, .response-content h2, .response-content h3 { margin: 14px 0 6px; }
    .response-content table {
      width: 100%;
      border-collapse: collapse;
      margin: 12px 0;
      font-size: 12px;
      background: #fff;
    }
    .response-content th, .response-content td {
      border: 1px solid #d9dce6;
      padding: 8px 10px;
      text-align: left;
      vertical-align: top;
    }
    .response-content th { background: #667eea; color: #fff; }
    .response-content tr:nth-child(even) td { background: #f5f7ff; }
    .response-content ul { margin: 8px 0 8px 18px; }
    .response-content p { margin: 6px 0; }
    .status { margin-top: 10px; font-size: 12px; color: #666; }
    .status a { color: #4a57cf; }
    .reports-panel {
      margin-top: 20px;
      background: #f9f9fb;
      border-left: 4px solid #56a3ff;
      border-radius: 8px;
      padding: 14px;
    }
    .reports-title {
      color: #2c6fc2;
      font-weight: 700;
      margin-bottom: 10px;
    }
    .reports-table {
      width: 100%;
      border-collapse: collapse;
      background: #fff;
      font-size: 12px;
    }
    .reports-table th, .reports-table td {
      border: 1px solid #d9dce6;
      padding: 8px 10px;
      text-align: left;
      vertical-align: top;
    }
    .reports-table th {
      background: #eaf3ff;
      color: #2b4f75;
    }
    .reports-empty { font-size: 12px; color: #666; }
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
  </style>
</head>
<body>
  <div class="live-status" id="liveStatus">API Connected</div>
  <div class="container">
    <div class="header">
      <h1>ClickUp MCP AI Query</h1>
      <p>REST dashboard for querying ClickUp tools and viewing saved reports.</p>
    </div>
    <div class="content">
      <form id="queryForm">
        <div class="form-group">
          <label for="question">Your Query</label>
          <textarea id="question" name="question" required
            placeholder="Example: Can you generate last month's space task report for BlogManager"></textarea>
        </div>
        <div class="button-group">
          <button type="submit" class="btn-submit" id="submitBtn">Send Query</button>
          <button type="button" class="btn-clear" onclick="clearQueryInput()">Clear</button>
        </div>
      </form>

      <div class="examples">
        <strong>Example Queries:</strong>
        <ul>
          <li>Show me all workspaces and teams</li>
          <li>Generate time tracking report for last week</li>
          <li>Can you provide this month's space task report for XYZ</li>
        </ul>
      </div>

      <div class="loader" id="loader">
        <div class="loader-card">
          <div class="loader-orbit"><div class="loader-core"></div></div>
          <div class="loader-title">Analyzing Query and Building Report</div>
          <div class="loader-subtitle">Calling tools and preparing your report output.</div>
          <div class="loader-meta">
            <span class="loader-phase" id="loaderPhase">Preparing request...</span>
            <span class="loader-percent" id="loaderPercent">0%</span>
          </div>
          <div class="loader-bar"><span id="loaderProgress"></span></div>
        </div>
      </div>
      <div class="error" id="error"></div>

      <div class="response-box" id="responseBox">
        <div class="response-header">
          <div class="response-title">Response</div>
          <div class="response-actions">
            <button class="download-btn" onclick="downloadReport()" id="downloadBtn" style="display:none;">Download Markdown</button>
            <button class="clear-report-btn" onclick="clearReportView()" id="clearReportBtn" style="display:none;">Clear Report</button>
          </div>
        </div>
        <div class="response-content" id="responseContent"></div>
        <div class="status" id="responseStatus"></div>
      </div>

      <div class="reports-panel">
        <div class="reports-title">Saved Reports</div>
        <div id="reportsContainer" class="reports-empty">Loading saved reports...</div>
      </div>
    </div>
  </div>

  <script>
    let lastResponse = '';
    let progressTimer = null;
    let progressValue = 0;
    let backendWasOffline = false;
    let heartbeatTimer = null;
    const HEARTBEAT_ONLINE_MS = 10000;
    const HEARTBEAT_OFFLINE_MS = 2500;
    const HEARTBEAT_HIDDEN_MS = 30000;

    function escapeHtml(text) {
      return (text || '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
    }

    function basicMarkdownToHtml(markdown) {
      const lines = (markdown || '').replace(/\\r/g, '').split('\\n');
      const html = [];
      let i = 0;

      while (i < lines.length) {
        const line = lines[i] || '';
        const next = i + 1 < lines.length ? (lines[i + 1] || '') : '';
        const trimmed = line.trim();

        const isTableHeader = line.includes('|');
        const isTableDivider = /^\\s*\\|?[\\s:-]+(\\|[\\s:-]+)+\\|?\\s*$/.test(next.trim());
        if (isTableHeader && isTableDivider) {
          const headCells = line.trim().replace(/^\\||\\|$/g, '').split('|').map(c => escapeHtml(c.trim()));
          html.push('<table><thead><tr>' + headCells.map(c => `<th>${c}</th>`).join('') + '</tr></thead><tbody>');
          i += 2;
          while (i < lines.length && lines[i].includes('|')) {
            const rowCells = (lines[i] || '').trim().replace(/^\\||\\|$/g, '').split('|').map(c => `<td>${escapeHtml(c.trim())}</td>`).join('');
            html.push('<tr>' + rowCells + '</tr>');
            i += 1;
          }
          html.push('</tbody></table>');
          continue;
        }

        if (!trimmed) {
          i += 1;
          continue;
        }
        if (/^###\\s+/.test(trimmed)) {
          html.push('<h3>' + escapeHtml(trimmed.replace(/^###\\s+/, '')) + '</h3>');
          i += 1;
          continue;
        }
        if (/^##\\s+/.test(trimmed)) {
          html.push('<h2>' + escapeHtml(trimmed.replace(/^##\\s+/, '')) + '</h2>');
          i += 1;
          continue;
        }
        if (/^#\\s+/.test(trimmed)) {
          html.push('<h1>' + escapeHtml(trimmed.replace(/^#\\s+/, '')) + '</h1>');
          i += 1;
          continue;
        }
        if (/^[-*]\\s+/.test(trimmed)) {
          html.push('<ul>');
          while (i < lines.length && /^[-*]\\s+/.test((lines[i] || '').trim())) {
            html.push('<li>' + escapeHtml((lines[i] || '').trim().replace(/^[-*]\\s+/, '')) + '</li>');
            i += 1;
          }
          html.push('</ul>');
          continue;
        }
        html.push('<p>' + escapeHtml(trimmed) + '</p>');
        i += 1;
      }

      return html.join('\\n');
    }

    function markdownToHtml(markdown) {
      if (window.marked && typeof window.marked.parse === 'function') {
        if (typeof window.marked.setOptions === 'function') {
          window.marked.setOptions({ gfm: true, breaks: true });
        }
        return window.marked.parse(markdown || '');
      }
      return basicMarkdownToHtml(markdown);
    }

    function formatBytes(bytes) {
      const value = Number(bytes || 0);
      if (value < 1024) return `${value} B`;
      if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
      return `${(value / (1024 * 1024)).toFixed(1)} MB`;
    }

    async function refreshReports() {
      const container = document.getElementById('reportsContainer');
      try {
        const response = await fetch('/reports');
        const data = await response.json();
        const reports = Array.isArray(data.reports) ? data.reports : [];
        if (!reports.length) {
          container.innerHTML = '<div class="reports-empty">No saved reports found yet.</div>';
          return;
        }

        const rows = reports.map((r) => {
          const name = escapeHtml(r.name || '');
          const modified = escapeHtml(r.modified || '');
          const size = formatBytes(r.size_bytes || 0);
          return `<tr>
            <td><a href="/reports/${name}" target="_blank">${name}</a></td>
            <td>${modified}</td>
            <td>${size}</td>
          </tr>`;
        }).join('');

        container.innerHTML = `
          <table class="reports-table">
            <thead>
              <tr>
                <th>Report</th>
                <th>Modified</th>
                <th>Size</th>
              </tr>
            </thead>
            <tbody>${rows}</tbody>
          </table>
        `;
      } catch (err) {
        container.innerHTML = '<div class="reports-empty">Unable to load reports list.</div>';
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

    function setLoaderProgress(value, phase) {
      const progress = Math.max(0, Math.min(100, Math.round(value || 0)));
      document.getElementById('loaderProgress').style.width = `${progress}%`;
      document.getElementById('loaderPercent').textContent = `${progress}%`;
      if (phase) document.getElementById('loaderPhase').textContent = phase;
    }

    function stopProgressSimulation() {
      if (progressTimer) {
        clearInterval(progressTimer);
        progressTimer = null;
      }
    }

    function startProgressSimulation() {
      stopProgressSimulation();
      progressValue = 6;
      setLoaderProgress(progressValue, 'Preparing request...');
      progressTimer = setInterval(() => {
        progressValue = Math.min(92, progressValue + Math.max(1, Math.round((100 - progressValue) / 16)));
        let phase = 'Calling MCP tools...';
        if (progressValue >= 30) phase = 'Fetching and aggregating tasks...';
        if (progressValue >= 55) phase = 'Waiting for report job completion...';
        if (progressValue >= 78) phase = 'Formatting report output...';
        setLoaderProgress(progressValue, phase);
      }, 850);
    }

    async function completeProgressAndHide(loader) {
      stopProgressSimulation();
      setLoaderProgress(100, 'Completed');
      await new Promise((resolve) => setTimeout(resolve, 180));
      loader.classList.remove('show');
    }

    const questionInput = document.getElementById('question');
    questionInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        document.getElementById('queryForm').requestSubmit();
      }
    });

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

    function startHeartbeat() {
      if (heartbeatTimer) clearTimeout(heartbeatTimer);
      heartbeatCheck();
    }

    document.addEventListener('visibilitychange', () => {
      if (!backendWasOffline) {
        scheduleHeartbeat(document.hidden ? HEARTBEAT_HIDDEN_MS : HEARTBEAT_ONLINE_MS);
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
      startProgressSimulation();
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
        await completeProgressAndHide(loader);

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
        stopProgressSimulation();
        loader.classList.remove('show');
        error.textContent = 'Request failed: ' + err.message;
        error.classList.add('show');
      } finally {
        submitBtn.disabled = false;
        submitBtn.textContent = 'Send Query';
      }
    });

    refreshReports();
    startHeartbeat();
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
        ok, err = await _connect_client(reuse_existing=True)
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

    before_saved = client.stats.reports_saved
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
async def list_reports():
    return {
        "reports_dir": str(REPORTS_DIR),
        "count": len(_list_reports()),
        "reports": _list_reports(),
    }


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
