# Runs on x86-64 and ARM64 (Oracle Ampere A1). Pure-Python dependencies; no compiler needed.
FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
COPY pyproject.toml README.md ./
COPY mev ./mev
COPY scripts ./scripts
RUN pip install --no-cache-dir . && mkdir -p /data
VOLUME ["/data"]
# Default command is the hot loop; docker-compose.app.yml overrides it for the agent and API.
CMD ["python", "-m", "mev.main"]
