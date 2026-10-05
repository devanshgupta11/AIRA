"""Runtime settings for the AIRA backend (overridable with environment variables)."""

import os
from pathlib import Path

# Keep CrewAI fully local: no anonymous telemetry, no trace upload. Must be set before crewai is imported.
os.environ.setdefault("CREWAI_DISABLE_TELEMETRY", "true")
os.environ.setdefault("CREWAI_TRACING_ENABLED", "false")
os.environ.setdefault("OTEL_SDK_DISABLED", "true")

BACKEND_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("AIRA_DATA_DIR", BACKEND_DIR / "data"))
DB_PATH = Path(os.getenv("AIRA_DB_PATH", DATA_DIR / "aira.db"))
LOG_DIR = Path(os.getenv("AIRA_LOG_DIR", BACKEND_DIR.parent / "logs"))

PROMETHEUS_URL = os.getenv("AIRA_PROMETHEUS_URL", "http://localhost:9090")
ALERTMANAGER_URL = os.getenv("AIRA_ALERTMANAGER_URL", "http://localhost:9093")
OLLAMA_URL = os.getenv("AIRA_OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("AIRA_OLLAMA_MODEL", "qwen2.5:7b-instruct")

# Seconds the agent may take before the incident is marked as an error.
AGENT_TIMEOUT_S = float(os.getenv("AIRA_AGENT_TIMEOUT_S", "180"))

DATA_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)
