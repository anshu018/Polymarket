# Start from a slim python image to minimize vulnerabilities and size
FROM python:3.12-slim

# Set the working directory inside the container
WORKDIR /app

# Upgrade pip and install curl for healthchecks
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --upgrade pip

# Create a non-root user (appuser) for security
RUN useradd -m appuser

# Copy requirements first to leverage Docker layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN python -m spacy download en_core_web_sm

# Copy all source code
COPY . .

# Adjust permissions so our non-root user can read/write where necessary
RUN chown -R appuser:appuser /app

# Switch to the non-root user
USER appuser

# glibc malloc arena fragmentation is a notorious RSS creeper in threaded
# Python containers (spaCy NER runs in executor threads); capping arenas is
# standard practice and costs nothing.
ENV MALLOC_ARENA_MAX=2

# REAL liveness probe (crash hardening 2026-11-01): curl the bot's own
# in-process /health endpoint instead of spawning a second Python importing
# the whole app (`import main` cost ~200MB + CPU contention and probed nothing
# about the running process; 3 slow checks during a signal burst or the
# startup window made Docker kill the container). start-period covers
# initialization; a hung event loop cannot answer → restart.
HEALTHCHECK --interval=60s --timeout=10s --start-period=120s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8077/health || exit 1

# Environment variables are expected to be injected via --env-file from systemd
# Command to run the agent
CMD ["python", "main.py"]
