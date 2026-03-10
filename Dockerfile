# Multi-stage Dockerfile for ClickUp MCP Server + AI Client
# Stage 1: Build dependencies
FROM python:3.11-slim AS builder

WORKDIR /build

# Copy requirements and build wheels
COPY requirements.txt .
RUN pip install --user --no-cache-dir -r requirements.txt

# Stage 2: Runtime
FROM python:3.11-slim

WORKDIR /app

# Install runtime dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy Python dependencies from builder
COPY --from=builder /root/.local /root/.local

# Copy application code
COPY . .

# Set environment variables
ENV PATH=/root/.local/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Expose MCP server port
EXPOSE 8001

# Health check disabled at image level; defined per-service in docker-compose.yml
HEALTHCHECK NONE

# Default to MCP server (can be overridden for AI client)
CMD ["python", "-m", "clickup_mcp.server"]
