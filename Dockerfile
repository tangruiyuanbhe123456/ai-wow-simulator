# AI WoW Simulator — single-image Dockerfile.
# Runs the FastAPI server (port 8787 by default; override with WOW_PORT env).
# SQLite DB is persisted at /data/world.db (mount a volume there).
FROM python:3.14-slim

WORKDIR /app

# Install dependencies first (better layer caching).
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy the application source.
COPY server/ ./server/
COPY scripts/ ./scripts/
COPY mock_agents/ ./mock_agents/
COPY web/ ./web/
COPY start.bat Makefile README.md ./

# Default SQLite location; mount a volume here to persist matches / players.
ENV WOW_DB_PATH=/data/world.db
RUN mkdir -p /data

EXPOSE 8787

# Bootstrap a clean world.db on first run, then start the server.
CMD ["sh", "-c", "python scripts/bootstrap.py && python -m uvicorn server.main:app --host 0.0.0.0 --port ${WOW_PORT:-8787} --log-level info"]