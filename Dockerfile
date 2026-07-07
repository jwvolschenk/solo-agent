# Solo Agent — monitoring dashboard + directive queue + Ralph orchestrator.
# Single process serves the UI (:8090), API, and WebSocket.
FROM python:3.12-slim

# git is required by the orchestrator's git_ops (snapshot/revert on the target repo).
# curl for healthchecks. ca-certificates for https to CDNs.
RUN apt-get update && apt-get install -y --no-install-recommends \
        git curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install dependencies first (better layer caching).
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the application.
COPY src/ ./src/
COPY pytest.ini ./

# Runtime directories (mounted as volumes in compose, but ensure they exist).
RUN mkdir -p /data /state

ENV HOST=0.0.0.0 \
    PORT=8090 \
    PYTHONUNBUFFERED=1

EXPOSE 8090

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -sf http://localhost:8090/api/health || exit 1

CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8090"]
