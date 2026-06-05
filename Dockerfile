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
RUN python -m spacy download en_core_web_md

# Copy all source code
COPY . .

# Adjust permissions so our non-root user can read/write where necessary
RUN chown -R appuser:appuser /app

# Switch to the non-root user
USER appuser

# Use a healthcheck to ensure the container verifies main is importable
HEALTHCHECK --interval=60s --timeout=10s --retries=3 \
  CMD python -c "import main" || exit 1

# Environment variables are expected to be injected via --env-file from systemd
# Command to run the agent
CMD ["python", "main.py"]
