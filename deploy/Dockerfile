# AIRA's own layer, as one image: the FastAPI backend, the console, and the agent code.
#
# What this image is NOT. It does not contain the 29-container OpenTelemetry demo, Prometheus,
# Alertmanager, OpenSearch, flagd or Ollama. Those are the system AIRA watches and the model it
# reasons with, and together they want more RAM and a GPU than any free tier provides. Pretending
# otherwise would produce a deployment that looks live and investigates nothing.
#
# So the image runs with AIRA_MODE=archive by default: the console, the API, every recorded
# investigation, the execution traces and the incident-memory store are all served from real data
# this system produced, and /health says plainly that the monitored stack is not part of this
# deployment. Point it at a reachable stack with AIRA_MODE=full and the same image investigates
# live incidents - the mode changes what it claims, not what it can do.

FROM python:3.12-slim AS base

# libgomp is onnxruntime's runtime dependency for the CPU embedding model; curl is for HEALTHCHECK.
RUN apt-get update \
 && apt-get install -y --no-install-recommends libgomp1 curl \
 && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first, in their own layer, so editing the application does not reinstall 235
# packages.
#
# --no-deps is deliberate. requirements.txt comes from `uv pip freeze`, which records the complete
# closure of a working environment - every transitive dependency is already pinned in the file, so
# there is nothing for a resolver to work out. Asking pip to resolve it anyway fails: uv installed
# opentelemetry-sdk 1.42.1 alongside exporters from the 1.45 line, which runs fine and which pip
# correctly refuses to re-derive, because google-adk caps the sdk at 1.42.1 while the exporters ask
# for ~=1.45. Treating the freeze as the lockfile it is reproduces the environment that was
# actually tested rather than a different one pip finds agreeable. The container is then started
# below as the real check that nothing is missing.
COPY backend/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --no-deps -r /app/requirements.txt

COPY backend/app /app/app

# The data this deployment serves, copied from deploy/seed rather than from the working copies.
#
# Three reasons, each of which broke something first. backend/data/aira.db is in .gitignore, so it
# never reaches the repository and this COPY would fail outright on a cloud build. The live qdrant
# directory holds a .lock file belonging to whichever process has it open, and a stale lock baked
# into an image makes the container refuse to start. And a database copied while the backend is
# mid-write can be torn - deploy/seed/aira.db is produced with sqlite's own backup API, not cp.
#
# Refresh the seed before deploying:  python scripts/refresh_deploy_seed.py
COPY deploy/seed/aira.db /app/data/aira.db
COPY deploy/seed/qdrant /app/qdrant

# Bake the embedding model into the image rather than downloading it on the first request. A cold
# container that stalls for 10 seconds fetching 80 MB looks like a hang to whoever opened the page.
ENV AIRA_FASTEMBED_CACHE=/app/.cache/fastembed
RUN python -c "from fastembed import TextEmbedding; \
    TextEmbedding('BAAI/bge-small-en-v1.5', cache_dir='/app/.cache/fastembed', \
                  providers=['CPUExecutionProvider'])"

ENV AIRA_MODE=archive \
    AIRA_DATA_DIR=/app/data \
    AIRA_DB_PATH=/app/data/aira.db \
    AIRA_QDRANT_PATH=/app/qdrant \
    AIRA_LOG_DIR=/app/logs \
    PORT=8000

# Qdrant runs embedded and takes an exclusive lock on its directory, so this image must run as a
# single process. One worker is not a performance compromise here, it is a correctness constraint.
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD curl -fsS http://localhost:${PORT}/health || exit 1

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --workers 1"]
